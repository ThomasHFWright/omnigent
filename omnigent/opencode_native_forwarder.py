"""SSE consumer that mirrors OpenCode events into an Omnigent session.

The runner owns this forwarder (parallel to the codex-native forwarder).
It connects to the per-session ``opencode serve`` SSE stream (``GET
/event``), filters to the session's OpenCode session id, and translates
OpenCode events into Omnigent session-stream events posted to
``/v1/sessions/{id}/events`` — the same envelope the codex forwarder uses
(``external_conversation_item`` / ``external_session_status`` /
``external_output_text_delta``).

Design references: the SSE-event → Omnigent-event translation table in
``designs/opencode-harness-and-unified-interface.md`` §A.9. The forwarder
is tolerant of unknown events (logged, never fatal) and dedupes by stable
OpenCode message / part / tool-call ids so web and TUI driving the same
session never double-post.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from omnigent.opencode_native_bridge import update_active_message_id, update_last_event_id
from omnigent.opencode_native_client import OpenCodeClient, OpenCodeEvent
from omnigent.opencode_native_permissions import (
    PolicyDecision,
    decision_to_reply,
    map_verdict_to_decision,
    normalize_for_policy,
    parse_permission_request,
    reply_body,
)

_logger = logging.getLogger(__name__)

_AGENT_NAME = "opencode"
# Omnigent session-event types (must match the server's ingestion route;
# shared with the codex-native forwarder).
_EXTERNAL_ITEM = "external_conversation_item"
_EXTERNAL_STATUS = "external_session_status"
_EXTERNAL_TEXT_DELTA = "external_output_text_delta"

_STATUS_RUNNING = "running"
_STATUS_IDLE = "idle"

# Bound the dedupe set so a long-lived session can't grow it without limit.
_MAX_DEDUPE_KEYS = 8192

# OpenCode step "finish" reasons that mean the assistant turn is complete
# (no further tool loop). Anything else (e.g. "tool-calls") keeps it busy.
_TERMINAL_FINISH = frozenset({"stop", "end_turn", "completed", "length", "error", "aborted"})

# Policy verdict resolver: receives a normalized policy input and returns a
# verdict mapping (or None when no policy is configured / reachable).
PolicyEvaluator = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]]


@dataclass
class OpenCodeForwarderState:
    """
    Mutable per-run forwarder state.

    :param seen: Bounded set of dedupe keys already posted.
    :param turn_active: Whether a turn is currently streaming.
    :param text_index: Per-text-stream chunk index for delta ordering.
    """

    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)
    turn_active: bool = False
    text_index: dict[str, int] = field(default_factory=dict)

    def mark(self, key: str) -> bool:
        """
        Record *key*; return ``True`` the first time it is seen.

        :param key: Stable dedupe key, e.g. ``"opencode:ses:msg:prt"``.
        :returns: ``True`` when newly seen, ``False`` for a duplicate.
        """
        if key in self.seen:
            return False
        self.seen[key] = None
        while len(self.seen) > _MAX_DEDUPE_KEYS:
            self.seen.popitem(last=False)
        return True

    def next_text_index(self, stream_id: str) -> int:
        """
        Return the next zero-based chunk index for a text stream.

        :param stream_id: Stable text-stream id.
        :returns: Monotonic chunk index.
        """
        index = self.text_index.get(stream_id, 0)
        self.text_index[stream_id] = index + 1
        return index


class OpenCodeNativeForwarder:
    """
    Translate one OpenCode session's SSE stream into Omnigent events.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param opencode_session_id: OpenCode session id to filter on.
    :param opencode_client: Client connected to the ``opencode serve``
        server (for SSE + permission replies).
    :param server_client: HTTP client for the Omnigent server (event posts).
    :param bridge_dir: Native OpenCode bridge directory (status/active-id
        persistence). ``None`` disables bridge writes (tests).
    :param workspace: Session workspace, used for permission normalization.
    :param policy_evaluator: Optional async policy resolver. ``None`` uses
        *default_decision* for every permission request.
    :param default_decision: Decision used when no evaluator is provided or
        it returns ``None``. Defaults to ``allow_once`` (codex-native
        parity: an unconfigured policy lets the native agent run its own
        tools); set ``reject`` to fail closed.
    """

    def __init__(
        self,
        *,
        session_id: str,
        opencode_session_id: str,
        opencode_client: OpenCodeClient,
        server_client: httpx.AsyncClient,
        bridge_dir: Path | None = None,
        workspace: str | None = None,
        policy_evaluator: PolicyEvaluator | None = None,
        default_decision: PolicyDecision = "allow_once",
    ) -> None:
        self._session_id = session_id
        self._opencode_session_id = opencode_session_id
        self._opencode = opencode_client
        self._server = server_client
        self._bridge_dir = bridge_dir
        self._workspace = workspace
        self._policy_evaluator = policy_evaluator
        self._default_decision = default_decision
        self.state = OpenCodeForwarderState()

    async def seed_dedupe_from_history(self) -> None:
        """
        Pre-seed dedupe state from existing OpenCode messages.

        Prevents re-posting prior history on a resume/reconnect. Best
        effort: a failure leaves the dedupe set empty (at worst a few
        re-posts on resume).
        """
        try:
            messages = await self._opencode.list_messages(self._opencode_session_id)
        except Exception:  # noqa: BLE001 - seeding is best effort.
            _logger.debug("OpenCode forwarder could not seed dedupe from history", exc_info=True)
            return
        for message in messages:
            info = message.get("info") if isinstance(message, Mapping) else None
            message_id = info.get("id") if isinstance(info, Mapping) else None
            parts = message.get("parts") if isinstance(message, Mapping) else None
            if isinstance(parts, list):
                for part in parts:
                    part_id = part.get("id") if isinstance(part, Mapping) else None
                    if isinstance(part_id, str):
                        self.state.mark(self._key("part", part_id))
            if isinstance(message_id, str):
                self.state.mark(self._key("message", message_id))

    async def run(self, *, max_reconnects: int | None = None) -> None:
        """
        Run the SSE consume loop with reconnect/backoff.

        :param max_reconnects: Reconnect cap (``None`` = unbounded); used
            by tests to bound the loop.
        """
        await self.seed_dedupe_from_history()
        attempt = 0
        backoff = 0.5
        while True:
            try:
                await self._consume_once()
                # Clean stream end (server closed): reconnect.
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect on any transient SSE failure.
                _logger.warning(
                    "OpenCode forwarder SSE error for session=%s; reconnecting",
                    self._session_id,
                    exc_info=True,
                )
            attempt += 1
            if max_reconnects is not None and attempt > max_reconnects:
                return
            await asyncio.sleep(min(backoff, 5.0))
            backoff = min(backoff * 2, 5.0)

    async def _consume_once(self) -> None:
        """Consume the SSE stream once, dispatching each event."""
        async for event in self._opencode.events():
            await self.handle_event(event)

    async def handle_event(self, event: OpenCodeEvent) -> None:
        """
        Translate one OpenCode event into Omnigent session events.

        :param event: A decoded OpenCode SSE event.
        """
        if not self._event_targets_session(event):
            return
        if event.id and self._bridge_dir is not None:
            update_last_event_id(self._bridge_dir, event.id)
        handler = _HANDLERS.get(event.type)
        if handler is None:
            _logger.debug(
                "OpenCode forwarder ignoring event type=%s for session=%s",
                event.type,
                self._session_id,
            )
            return
        await handler(self, event)

    # --- filtering -------------------------------------------------------

    def _event_targets_session(self, event: OpenCodeEvent) -> bool:
        """
        Return whether *event* belongs to this forwarder's session.

        Events without a session id (e.g. ``server.connected``) pass
        through so readiness/global signals are not dropped.

        :param event: A decoded OpenCode event.
        :returns: ``True`` when the event should be handled.
        """
        props = event.properties
        session_id = props.get("sessionID") or props.get("session_id")
        info = props.get("info")
        if session_id is None and isinstance(info, Mapping):
            session_id = info.get("id")
        if session_id is None:
            return True
        return bool(session_id == self._opencode_session_id)

    # --- dedupe / keys ---------------------------------------------------

    def _key(self, *parts: str) -> str:
        """
        Build a session-scoped dedupe key.

        :param parts: Key segments, e.g. ``("text", "prt_1")``.
        :returns: ``"opencode:<sessionID>:<part>:..."``.
        """
        return "opencode:" + ":".join((self._opencode_session_id, *parts))

    # --- posting helpers -------------------------------------------------

    async def _post_event(self, event_type: str, data: dict[str, Any]) -> httpx.Response | None:
        """
        POST one Omnigent session event with a single retry.

        :param event_type: Omnigent event type, e.g.
            ``"external_session_status"``.
        :param data: Event data payload.
        :returns: The HTTP response, or ``None`` on transport failure.
        """
        url = f"/v1/sessions/{quote(self._session_id, safe='')}/events"
        payload = {"type": event_type, "data": data}
        try:
            return await self._server.post(url, json=payload)
        except httpx.HTTPError:
            _logger.warning(
                "OpenCode forwarder failed to post %s for session=%s",
                event_type,
                self._session_id,
                exc_info=True,
            )
            return None

    async def _post_status(self, status: str) -> None:
        """Publish a coarse session status edge."""
        await self._post_event(_EXTERNAL_STATUS, {"status": status})

    async def _post_assistant_text(self, text: str) -> None:
        """Persist a finalized assistant message."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": _AGENT_NAME,
                    "content": [{"type": "output_text", "text": text}],
                },
                "response_id": self._opencode_session_id,
            },
        )

    async def _post_text_delta(self, stream_id: str, delta: str) -> None:
        """Publish a streaming assistant text delta."""
        await self._post_event(
            _EXTERNAL_TEXT_DELTA,
            {
                "delta": delta,
                "message_id": stream_id,
                "index": self.state.next_text_index(stream_id),
            },
        )

    async def _post_tool_call(self, call_id: str, tool: str, arguments: dict[str, Any]) -> None:
        """Mirror a tool invocation as a function_call item."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "function_call",
                "item_data": {
                    "agent": _AGENT_NAME,
                    "name": tool,
                    "arguments": json.dumps(arguments, ensure_ascii=True),
                    "call_id": call_id,
                },
                "response_id": self._opencode_session_id,
            },
        )

    async def _post_tool_output(self, call_id: str, output: str) -> None:
        """Mirror a tool result as a function_call_output item."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "function_call_output",
                "item_data": {"call_id": call_id, "output": output},
                "response_id": self._opencode_session_id,
            },
        )

    async def _begin_turn_if_needed(self) -> None:
        """Post a single ``running`` status at the start of a turn."""
        if not self.state.turn_active:
            self.state.turn_active = True
            await self._post_status(_STATUS_RUNNING)

    async def _end_turn(self) -> None:
        """Post ``idle`` and clear active state at turn end."""
        self.state.turn_active = False
        if self._bridge_dir is not None:
            update_active_message_id(self._bridge_dir, None, status="idle")
        await self._post_status(_STATUS_IDLE)

    # --- per-event handlers ----------------------------------------------

    async def _on_step_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.step.started`` — turn begins."""
        message_id = event.properties.get("assistantMessageID")
        if isinstance(message_id, str) and self._bridge_dir is not None:
            update_active_message_id(self._bridge_dir, message_id, status="busy")
        await self._begin_turn_if_needed()

    async def _on_step_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.step.ended`` — turn may be complete."""
        finish = event.properties.get("finish")
        finish_token = ""
        if isinstance(finish, str):
            finish_token = finish.lower()
        elif isinstance(finish, Mapping):
            reason = finish.get("reason") or finish.get("type")
            finish_token = str(reason).lower() if reason is not None else ""
        if finish_token in _TERMINAL_FINISH or finish_token == "":
            await self._end_turn()

    async def _on_step_failed(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.step.failed`` — surface + end turn."""
        del event
        await self._end_turn()

    async def _on_text_delta(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.text.delta`` — stream assistant text."""
        delta = event.properties.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        text_id = event.properties.get("textID") or event.properties.get("assistantMessageID")
        stream_id = self._key("text", str(text_id))
        await self._begin_turn_if_needed()
        await self._post_text_delta(stream_id, delta)

    async def _on_text_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.text.ended`` — finalize assistant text."""
        text = event.properties.get("text")
        text_id = event.properties.get("textID") or event.properties.get("assistantMessageID")
        if not isinstance(text, str) or not text:
            return
        if not self.state.mark(self._key("text-final", str(text_id))):
            return
        await self._post_assistant_text(text)

    async def _on_tool_called(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.tool.called`` — mirror tool invocation."""
        call_id = event.properties.get("callID")
        tool = event.properties.get("tool")
        if not isinstance(call_id, str) or not isinstance(tool, str):
            return
        if not self.state.mark(self._key("tool-call", call_id)):
            return
        raw_input = event.properties.get("input")
        arguments = raw_input if isinstance(raw_input, dict) else {"input": raw_input}
        await self._begin_turn_if_needed()
        await self._post_tool_call(call_id, tool, arguments)

    async def _on_tool_success(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.tool.success`` — mirror tool result."""
        call_id = event.properties.get("callID")
        if not isinstance(call_id, str):
            return
        if not self.state.mark(self._key("tool-out", call_id)):
            return
        output = _tool_output_text(event.properties)
        await self._post_tool_output(call_id, output)

    async def _on_tool_failed(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.tool.failed`` — mirror tool error."""
        call_id = event.properties.get("callID")
        if not isinstance(call_id, str):
            return
        if not self.state.mark(self._key("tool-out", call_id)):
            return
        error = event.properties.get("error")
        output = f"[error] {error}" if error is not None else "[error]"
        await self._post_tool_output(call_id, output)

    async def _on_interrupt_requested(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.interrupt.requested`` — cancelling."""
        del event
        await self._post_status("cancelling")

    async def _on_prompt_promoted(self, event: OpenCodeEvent) -> None:
        """Handle ``session.next.prompt.promoted`` — queued prompt active."""
        del event
        await self._begin_turn_if_needed()

    async def _on_session_error(self, event: OpenCodeEvent) -> None:
        """Handle ``session.error`` — log + end turn."""
        _logger.warning(
            "OpenCode session error for session=%s: %s",
            self._session_id,
            event.properties.get("error"),
        )
        await self._end_turn()

    async def _on_permission_asked(self, event: OpenCodeEvent) -> None:
        """Handle ``permission.v2.asked`` — evaluate policy and reply."""
        request = parse_permission_request(event.properties)
        if request is None:
            return
        if not self.state.mark(self._key("perm", request.request_id)):
            return
        decision = await self._resolve_permission(request_dict=request)
        reply = decision_to_reply(decision)
        if reply is None:
            # Fail closed: an unmapped/ask verdict gets a reject so a
            # headless turn never silently auto-approves a sensitive op.
            reply = "reject"
        try:
            await self._opencode.reply_permission(
                request.request_id, reply_body(reply, message="omnigent-policy")
            )
        except Exception:  # noqa: BLE001 - reply is best effort; log and move on.
            _logger.warning(
                "OpenCode permission reply failed for request=%s",
                request.request_id,
                exc_info=True,
            )

    async def _resolve_permission(self, *, request_dict: Any) -> PolicyDecision:
        """
        Resolve a permission request to a normalized decision.

        :param request_dict: The parsed permission request.
        :returns: The normalized policy decision.
        """
        if self._policy_evaluator is None:
            return self._default_decision
        normalized = normalize_for_policy(
            request_dict,
            omnigent_session_id=self._session_id,
            workspace=self._workspace,
        )
        try:
            verdict = await self._policy_evaluator(normalized)
        except Exception:  # noqa: BLE001 - policy errors fail closed.
            _logger.warning("OpenCode policy evaluation failed", exc_info=True)
            return "ask"
        if verdict is None:
            return self._default_decision
        return map_verdict_to_decision(verdict)


def _tool_output_text(properties: Mapping[str, Any]) -> str:
    """
    Extract a string tool output from a tool-success event.

    :param properties: The ``session.next.tool.success`` properties.
    :returns: A string suitable for ``function_call_output``.
    """
    for key in ("content", "result", "structured"):
        value = properties.get(key)
        if isinstance(value, str) and value:
            return value
        if value is not None and not isinstance(value, str):
            return json.dumps(value, ensure_ascii=True)
    return ""


# Event type → bound handler-name lookup. Built once; ``handle_event``
# resolves the method on the instance. Keys are OpenCode event ``type``
# discriminators (see openapi.json Event* schemas).
_HANDLERS: dict[str, Callable[[OpenCodeNativeForwarder, OpenCodeEvent], Awaitable[None]]] = {
    "session.next.step.started": OpenCodeNativeForwarder._on_step_started,
    "session.next.step.ended": OpenCodeNativeForwarder._on_step_ended,
    "session.next.step.failed": OpenCodeNativeForwarder._on_step_failed,
    "session.next.text.delta": OpenCodeNativeForwarder._on_text_delta,
    "session.next.text.ended": OpenCodeNativeForwarder._on_text_ended,
    "session.next.tool.called": OpenCodeNativeForwarder._on_tool_called,
    "session.next.tool.success": OpenCodeNativeForwarder._on_tool_success,
    "session.next.tool.failed": OpenCodeNativeForwarder._on_tool_failed,
    "session.next.interrupt.requested": OpenCodeNativeForwarder._on_interrupt_requested,
    "session.next.prompt.promoted": OpenCodeNativeForwarder._on_prompt_promoted,
    "session.error": OpenCodeNativeForwarder._on_session_error,
    "permission.v2.asked": OpenCodeNativeForwarder._on_permission_asked,
}
