"""Attachment upload type/size enforcement on POST /v1/sessions/{id}/resources/files."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.runtime.content_resolver import (
    MAX_IMAGE_UPLOAD_BYTES,
    MAX_TEXT_UPLOAD_BYTES,
)
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


@pytest.fixture
def upload_client(db_uri: str, tmp_path) -> Iterator[tuple[TestClient, str]]:
    """A sessions route client with file + artifact stores and one session."""
    conversation_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store.create(
        agent_id="ag_test",
        name="test-agent",
        bundle_location="ag_test/bundle",
    )
    conv = conversation_store.create_conversation(title="upload session", agent_id="ag_test")

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            file_store=file_store,
            artifact_store=artifact_store,
        ),
        prefix="/v1",
    )

    with TestClient(app) as client:
        yield client, conv.id


def test_upload_small_text_file_succeeds(upload_client: tuple[TestClient, str]) -> None:
    """A small text file uploads and returns a resource."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["name"] == "notes.txt"


def test_upload_rejects_unsupported_type(upload_client: tuple[TestClient, str]) -> None:
    """A pptx (binary office doc) is rejected with 415, not stored."""
    client, session_id = upload_client
    pptx_mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("deck.pptx", b"PK\x03\x04 fake pptx bytes", pptx_mime)},
    )
    assert resp.status_code == 415, resp.text
    assert "Unsupported attachment type" in resp.text


def test_upload_rejects_oversized_image(upload_client: tuple[TestClient, str]) -> None:
    """An image over the per-type limit is rejected with 413."""
    client, session_id = upload_client
    oversized = b"\x00" * (MAX_IMAGE_UPLOAD_BYTES + 1)
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("huge.png", oversized, "image/png")},
    )
    assert resp.status_code == 413, resp.status_code
    assert "limit" in resp.text.lower()


def test_upload_text_just_under_limit_succeeds(upload_client: tuple[TestClient, str]) -> None:
    """A text file just under the text cap is accepted."""
    client, session_id = upload_client
    payload = b"a" * (MAX_TEXT_UPLOAD_BYTES - 1024)
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("big.txt", payload, "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.status_code
