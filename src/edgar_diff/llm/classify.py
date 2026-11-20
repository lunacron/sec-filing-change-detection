"""Classify changed paragraphs with an LLM; store every result with provenance."""
import hashlib
import json
import random
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from ..cost import PRICES, Usage, add_columns
from ..diff.paragraphs import Change, DiffResult
from .schema import Classification, strict_schema

PROMPT_DIR = Path("prompts")
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
EST_OUTPUT_TOKENS = 700  # JSON answer plus low-effort thinking; a guess, see dry-run


def load_prompt(version: str) -> tuple[str, str]:
    text = (PROMPT_DIR / f"{version}.md").read_text()
    return text, hashlib.sha256(text.encode()).hexdigest()


def sample_changes(changes: list[Change], n: int, seed: int = 0,
                   always_sections: tuple[str, ...] = ("10-K/3",)) -> list[Change]:
    """Reproducible sample that spreads over (section, kind) groups.

    Changes in `always_sections` (Legal Proceedings) are always included, then
    groups are visited round-robin so small sections are not crowded out by big ones.
    """
    pool = [c for c in changes if c.needs_llm]
    rng = random.Random(seed)
    forced = [c for c in pool if (c.new_section or c.old_section) in always_sections]
    chosen = list(forced)
    groups: dict[tuple, list[Change]] = defaultdict(list)
    for c in pool:
        if c not in forced:
            groups[(c.new_section or c.old_section, c.kind)].append(c)
    for g in groups.values():
        rng.shuffle(g)
    keys = sorted(groups)
    while len(chosen) < n and any(groups.values()):
        for k in keys:
            if groups[k] and len(chosen) < n:
                chosen.append(groups[k].pop())
    return chosen


def build_user_message(c: Change) -> str:
    section = c.new_section or c.old_section
    parts = [f"Section: {section}", f"Change: {c.kind}"]
    if c.kind == "added":
        parts.append("There was no equivalent paragraph in the earlier filing.")
    elif c.kind == "removed":
        parts.append("There is no equivalent paragraph in the later filing.")
    elif c.kind == "moved":
        parts.append(f"Moved from {c.old_section} to {c.new_section}.")
    if c.old_text is not None:
        parts.append(f"<old_text>\n{c.old_text}\n</old_text>")
    if c.new_text is not None:
        parts.append(f"<new_text>\n{c.new_text}\n</new_text>")
    if c.hint:
        h = c.hint
        parts.append(
            f'<closest_paragraph_in_other_filing similarity="{h.similarity}" section="{h.section}" '
            f'already_matched_to_another_paragraph="{str(h.already_matched).lower()}">\n{h.text}\n'
            f"</closest_paragraph_in_other_filing>")
    if c.word_diff:
        diff = "\n".join(f"{d.op}: {d.old!r} -> {d.new!r}" for d in c.word_diff)
        parts.append(f"<word_diff>\n{diff}\n</word_diff>")
    return "\n\n".join(parts)


def estimate_cost(messages: list[str], system: str) -> dict:
    """Rough, offline: ~4 characters per token."""
    in_tok = sum((len(system) + len(m)) // 4 for m in messages)
    out_tok = EST_OUTPUT_TOKENS * len(messages)
    return {
        "calls": len(messages), "input_tokens": in_tok, "output_tokens": out_tok,
        "usd": {m: round(in_tok / 1e6 * p[0] + out_tok / 1e6 * p[1], 2) for m, p in PRICES.items()},
    }


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def evidence_grounded(quotes: list[str], c: Change) -> bool:
    """Every quote must appear verbatim (modulo whitespace/quote style) in old or new text."""
    source = _squash((c.old_text or "") + " " + (c.new_text or ""))
    if c.hint:  # the model may rely on the closest paragraph, so allow quotes from it
        source += " " + _squash(c.hint.text)
    return all(_squash(q) in source for q in quotes)


SCHEMA = """
CREATE TABLE IF NOT EXISTS classifications (
    change_id TEXT, old_paragraph_id TEXT, new_paragraph_id TEXT,
    label TEXT, secondary_label TEXT, is_material INTEGER, confidence TEXT,
    rationale TEXT, evidence_quotes TEXT, evidence_grounded INTEGER, uncertainty_reason TEXT,
    prompt_version TEXT, prompt_sha256 TEXT, model_id TEXT, run_id TEXT,
    created_at TEXT, input_hash TEXT, raw_response TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    PRIMARY KEY (change_id, prompt_sha256, model_id)
)"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    add_columns(db, "classifications")
    return db


def _uncertain(reason: str) -> Classification:
    return Classification(label="uncertain", secondary_label=None, is_material=False,
                          confidence="low", rationale="No classification was produced.",
                          evidence_quotes=[], uncertainty_reason=reason)


def request_params(c: Change, system: str, model: str, cache_ttl: str | None = None) -> dict:
    """The arguments of one API call. Used by normal calls and by batch requests alike.

    The system prompt is the same for every call, so it is marked for prompt caching: later calls
    read it from the cache at a fraction of the input price. `cache_ttl` is None for the default
    5 minutes (normal calls run seconds apart) or "1h" for batch requests, which can sit far apart.
    """
    control = {"type": "ephemeral", **({"ttl": cache_ttl} if cache_ttl else {})}
    return dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": control}],
        messages=[{"role": "user", "content": build_user_message(c)}],
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": strict_schema(Classification)}},
    )


def parse_message(resp):
    """Returns (Classification, raw_text, Usage) from an API message."""
    usage = Usage.from_api(resp.usage)
    if resp.stop_reason in ("refusal", "max_tokens"):
        return _uncertain(resp.stop_reason), "", usage
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return Classification.model_validate_json(text), text, usage
    except ValidationError:
        return _uncertain("schema_failure"), text, usage


def classify_one(client, c: Change, system: str, model: str):
    """Returns (Classification, raw_text, Usage, duration_ms)."""
    t0 = time.perf_counter()
    resp = client.messages.create(**request_params(c, system, model))
    ms = round((time.perf_counter() - t0) * 1000)
    return (*parse_message(resp), ms)


def is_classified(db: sqlite3.Connection, change_id: str, sha: str, model: str) -> bool:
    return db.execute("SELECT 1 FROM classifications WHERE change_id=? AND prompt_sha256=? AND model_id=?",
                      (change_id, sha, model)).fetchone() is not None


def store_classification(db: sqlite3.Connection, c: Change, cls: Classification, raw: str, usage: Usage, *,
                         system: str, prompt_version: str, sha: str, model: str, run_id: str,
                         mode: str = "standard", batch_id: str | None = None,
                         duration_ms: int | None = None) -> bool:
    """Save one answer with its provenance, cost inputs and timing. Returns evidence_grounded."""
    if cls.label == "uncertain" and not cls.uncertainty_reason:
        cls.uncertainty_reason = "model gave no reason"
    grounded = evidence_grounded(cls.evidence_quotes, c)
    msg = build_user_message(c)
    db.execute(
        "INSERT INTO classifications (change_id, old_paragraph_id, new_paragraph_id, label, secondary_label, "
        "is_material, confidence, rationale, evidence_quotes, evidence_grounded, uncertainty_reason, "
        "prompt_version, prompt_sha256, model_id, run_id, created_at, input_hash, raw_response, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, mode, batch_id, duration_ms, "
        "input_chars) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (c.change_id, c.old_paragraph_id, c.new_paragraph_id, cls.label, cls.secondary_label,
         int(cls.is_material), cls.confidence, cls.rationale, json.dumps(cls.evidence_quotes),
         int(grounded), cls.uncertainty_reason, prompt_version, sha, model, run_id,
         datetime.now(timezone.utc).isoformat(), hashlib.sha256(msg.encode()).hexdigest(),
         raw, usage.input, usage.output, usage.cache_read, usage.cache_write, mode, batch_id, duration_ms,
         len(system) + len(msg)))
    db.commit()
    return grounded


def classify_changes(client, changes: list[Change], db: sqlite3.Connection, *,
                     prompt_version: str, model: str, log=print) -> str:
    system, sha = load_prompt(prompt_version)
    run_id = uuid.uuid4().hex[:12]
    done = skipped = 0
    for c in changes:
        if is_classified(db, c.change_id, sha, model):
            skipped += 1
            continue
        cls, raw, usage, ms = classify_one(client, c, system, model)
        grounded = store_classification(db, c, cls, raw, usage, system=system, prompt_version=prompt_version,
                                        sha=sha, model=model, run_id=run_id, duration_ms=ms)
        done += 1
        log(f"[{done}] {c.change_id} {cls.label} ({cls.confidence}) grounded={grounded} {ms / 1000:.1f}s")
    log(f"run {run_id}: classified {done}, skipped {skipped} already done")
    return run_id
