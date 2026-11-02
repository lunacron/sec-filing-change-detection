"""Extract numeric figures from changed paragraphs with an LLM, then check each one in code.

Only changes that go to the LLM and contain a figure are sent. Every figure is stored with
`grounded` and `reason` (see ground/numbers.py); ungrounded figures are flagged, not dropped.
Results are cached by (change_id, prompt hash, model), like classifications, so repeat runs
cost nothing, and a change with no figures is cached too.
"""
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError

from ..diff.paragraphs import Change
from ..ground.numbers import ground_figure, looks_like_figure, quote_span
from .classify import MAX_TOKENS, load_prompt
from .schema import Extraction, strict_schema

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS extractions (
        change_id TEXT, old_paragraph_id TEXT, new_paragraph_id TEXT, n_figures INTEGER,
        failure TEXT, prompt_version TEXT, prompt_sha256 TEXT, model_id TEXT, run_id TEXT,
        created_at TEXT, input_hash TEXT, raw_response TEXT, input_tokens INTEGER, output_tokens INTEGER,
        PRIMARY KEY (change_id, prompt_sha256, model_id))""",
    """CREATE TABLE IF NOT EXISTS figures (
        change_id TEXT, idx INTEGER, side TEXT, paragraph_id TEXT, value_as_written TEXT,
        normalized_value REAL, unit TEXT, metric TEXT, period TEXT, source_quote TEXT,
        char_start INTEGER, char_end INTEGER, grounded INTEGER, reason TEXT,
        prompt_version TEXT, prompt_sha256 TEXT, model_id TEXT, run_id TEXT, created_at TEXT,
        PRIMARY KEY (change_id, idx, prompt_sha256, model_id))""",
]


def init_tables(db: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        db.execute(stmt)


def has_figures(c: Change) -> bool:
    return c.needs_llm and looks_like_figure((c.old_text or "") + " " + (c.new_text or ""))


def build_extract_message(c: Change) -> str:
    parts = [f"Section: {c.new_section or c.old_section}", f"Change: {c.kind}"]
    if c.old_text is not None:
        parts.append(f"<old_text>\n{c.old_text}\n</old_text>")
    if c.new_text is not None:
        parts.append(f"<new_text>\n{c.new_text}\n</new_text>")
    return "\n\n".join(parts)


def extract_one(client, c: Change, system: str, model: str):
    """Returns (Extraction | None, failure | None, raw_text, input_tokens, output_tokens)."""
    resp = client.messages.create(
        model=model, max_tokens=MAX_TOKENS, system=system,
        messages=[{"role": "user", "content": build_extract_message(c)}],
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": strict_schema(Extraction)}},
    )
    usage = (resp.usage.input_tokens, resp.usage.output_tokens)
    if resp.stop_reason in ("refusal", "max_tokens"):
        return None, resp.stop_reason, "", *usage
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return Extraction.model_validate_json(text), None, text, *usage
    except ValidationError:
        return None, "schema_failure", text, *usage


def extract_figures(client, changes: list[Change], db: sqlite3.Connection, *,
                    prompt_version: str, model: str, log=print) -> str:
    init_tables(db)
    system, sha = load_prompt(prompt_version)
    run_id = uuid.uuid4().hex[:12]
    done = skipped = 0
    for c in changes:
        if db.execute("SELECT 1 FROM extractions WHERE change_id=? AND prompt_sha256=? AND model_id=?",
                      (c.change_id, sha, model)).fetchone():
            skipped += 1
            continue
        ext, failure, raw, tin, tout = extract_one(client, c, system, model)
        now = datetime.now(timezone.utc).isoformat()
        figures = ext.figures if ext else []
        for i, f in enumerate(figures):
            text = (c.old_text if f.side == "old" else c.new_text) or ""
            grounded, reason = ground_figure(f.normalized_value, f.source_quote, text)
            span = quote_span(f.source_quote, text) or (None, None)
            db.execute("INSERT INTO figures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (c.change_id, i, f.side, c.old_paragraph_id if f.side == "old" else c.new_paragraph_id,
                        f.value_as_written, f.normalized_value, f.unit, f.metric, f.period, f.source_quote,
                        span[0], span[1], int(grounded), reason, prompt_version, sha, model, run_id, now))
        db.execute("INSERT INTO extractions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (c.change_id, c.old_paragraph_id, c.new_paragraph_id, len(figures), failure,
                    prompt_version, sha, model, run_id, now,
                    hashlib.sha256(build_extract_message(c).encode()).hexdigest(), raw, tin, tout))
        db.commit()
        done += 1
        log(f"[{done}] {c.change_id} {len(figures)} figures" + (f" ({failure})" if failure else ""))
    log(f"run {run_id}: extracted {done}, skipped {skipped} already done")
    return run_id


def report(db: sqlite3.Connection, sha: str, model: str, ids: set[str]) -> dict:
    rows = [r for r in db.execute(
        "SELECT change_id, side, value_as_written, metric, grounded, reason FROM figures "
        "WHERE prompt_sha256=? AND model_id=? ORDER BY change_id, idx", (sha, model)) if r[0] in ids]
    bad = [r for r in rows if not r[4]]
    return {"figures": len(rows), "ungrounded": len(bad), "rows": rows, "bad": bad}
