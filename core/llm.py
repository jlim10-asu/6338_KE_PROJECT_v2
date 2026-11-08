"""Cached, schema-constrained Claude calls. Cache key = SHA-256(model | version | effort | tag | system | user), where
version is the calling stage's prompt version (e.g. EXTRACT_VERSION in s4_extract/prompts.py): editing one stage's prompts
invalidates only that stage's cached responses."""
import hashlib
import anthropic
from config import EXTRACT_EFFORT, LLM_MODEL
from core.db import db, lock

_anthropic = None


def _client():
    """Created on first use, so importing this module does not need ANTHROPIC_API_KEY (tests, dry runs)."""
    global _anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic()
    return _anthropic


class LLMStop(RuntimeError):
    def __init__(self, reason, category=None):
        super().__init__(f"LLM stop_reason={reason}" + (f" category={category}" if category else ""))
        self.reason, self.category = reason, category


def llm_json(schema_model, system, user, tag, version, effort=EXTRACT_EFFORT, use_cache=True, model=None):
    # Cache key = SHA-256(model ID + prompt version + tag + prompt text); re-runs are free and identical.
    # model: per-call override (GraphRAG generator); None = LLM_MODEL, so pipeline cache keys are unchanged.
    model = model or LLM_MODEL
    key = hashlib.sha256(f"{model}|{version}|{effort}|{tag}|{system}|{user}".encode()).hexdigest()
    with lock:
        row = db.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
    if row and use_cache:
        return row[0], True
    resp = _client().messages.create(
        model=model, max_tokens=16000, system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema_model)}},
    )
    if resp.stop_reason in ("refusal", "max_tokens"):
        category = getattr(resp.stop_details, "category", None) if resp.stop_details else None
        raise LLMStop(resp.stop_reason, category)
    text = next(b.text for b in resp.content if b.type == "text")
    with lock:
        db.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?)", (key, model, version, tag, text))
        db.commit()
    return text, False
