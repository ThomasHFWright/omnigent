// Thin one-shot spam/abuse classifier via the LLM gateway. Kept separate from
// abuse-triage.js so the triage gating logic stays unit-testable offline (this
// module is exercised only by a live smoke test -- it makes a network call).
//
// Endpoint: GATEWAY_BASE_URL is the OpenAI-compatible surface
// (<host>/serving-endpoints; the same value the e2e workflows pass as
// OPENAI_BASE_URL), so we POST to <GATEWAY_BASE_URL>/chat/completions with a
// Databricks model name and a Bearer token.
//
// ADVISORY ONLY: the PR title/body/diff are attacker-controlled and therefore
// prompt-injectable, so the verdict is used solely to add a triage label that a
// human reviews -- never to auto-close. Caller treats any throw as "no verdict".
const MODEL = "databricks-claude-sonnet-4-6";

module.exports = async function classify({ title, body, diff, env, fetchImpl }) {
  const base = (env && env.GATEWAY_BASE_URL || "").replace(/\/$/, "");
  const key = env && env.LLM_API_KEY;
  if (!base || !key) throw new Error("gateway env (GATEWAY_BASE_URL/LLM_API_KEY) missing");
  const doFetch = fetchImpl || globalThis.fetch;
  if (!doFetch) throw new Error("no fetch available");

  const prompt = [
    "You triage pull requests for an open-source software project.",
    "Decide whether this PR is SPAM/ABUSE rather than a good-faith contribution.",
    "Spam includes: nonsensical or AI-slop changes, SEO/link/crypto/advertising",
    "content, empty or contentless diffs, or changes unrelated to the project.",
    "A short but genuine bug fix or doc fix is NOT spam. When unsure, say not spam.",
    "",
    "Respond with ONLY a JSON object, no prose:",
    '{"spam": <true|false>, "confidence": <0..1>, "reason": "<=120 chars"}',
    "",
    "--- PR TITLE ---",
    title || "(empty)",
    "--- PR BODY ---",
    (body || "(empty)").slice(0, 4000),
    "--- PR DIFF ---",
    (diff || "(empty)").slice(0, 32000),
  ].join("\n");

  const res = await doFetch(`${base}/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: MODEL,
      messages: [{ role: "user", content: prompt }],
      max_tokens: 200,
      temperature: 0,
    }),
  });
  if (!res.ok) throw new Error(`gateway HTTP ${res.status}`);
  const data = await res.json();
  const text = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || "";
  const m = text.match(/\{[\s\S]*\}/);
  if (!m) throw new Error("no JSON object in model response");
  const v = JSON.parse(m[0]);
  return { spam: v.spam === true, confidence: Number(v.confidence) || 0, reason: String(v.reason || "").slice(0, 120) };
};
