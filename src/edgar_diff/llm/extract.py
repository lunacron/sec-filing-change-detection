"""Extract numeric figures from changed paragraphs with an LLM, then check each one in code.

Only changes that go to the LLM and contain a figure are sent. Every figure is stored with
`grounded` and `reason` (see ground/numbers.py); ungrounded figures are flagged, not dropped.
Results are cached by (change_id, prompt hash, model), like classifications, so repeat runs
cost nothing, and a change with no figures is cached too.
"""
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError

from ..cost import Usage, add_columns
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
    add_columns(db, "extractions")


def has_figures(c: Change) -> bool:
    return c.needs_llm and looks_like_figure((c.old_text or "") + " " + (c.new_text or ""))


def build_extract_message(c: Change) -> str:
    parts = [f"Section: {c.new_section or c.old_section}", f"Change: {c.kind}"]
    if c.old_text is not None:
        parts.append(f"<old_text>\n{c.old_text}\n</old_text>")
    if c.new_text is not None:
        parts.append(f"<new_text>\n{c.new_text}\n</new_text>")
    return "\n\n".join(parts)


def request_params(c: Change, system: str, model: str, cache_ttl: str | None = None) -> dict:
    """The arguments of one API call. Used by normal calls and by batch requests alike.

    Not cached: extract_v001 is about 500 tokens, at the edge of the 512-token cache minimum, and
    only ~6% of the cost of a call, so caching it would save almost nothing. `cache_ttl` is
    accepted so batch.py can call both steps the same way.
    """
    return dict(
        model=model, max_tokens=MAX_TOKENS, system=system,
        messages=[{"role": "user", "content": build_extract_message(c)}],
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": strict_schema(Extraction)}},
    )


def parse_message(resp):
    """Returns (Extraction | None, failure | None, raw_text, Usage)."""
    usage = Usage.from_api(resp.usage)
    if resp.stop_reason in ("refusal", "max_tokens"):
        return None, resp.stop_reason, "", usage
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return Extraction.model_validate_json(text), None, text, usage
    except ValidationError:
        return None, "schema_failure", text, usage


def extract_one(client, c: Change, system: str, model: str):
    """Returns (Extraction | None, failure | None, raw_text, Usage, duration_ms)."""
    t0 = time.perf_counter()
    resp = client.messages.create(**request_params(c, system, model))
    ms = round((time.perf_counter() - t0) * 1000)
    return (*parse_message(resp), ms)


def is_extracted(db: sqlite3.Connection, change_id: str, sha: str, model: str) -> bool:
    return db.execute("SELECT 1 FROM extractions WHERE change_id=? AND prompt_sha256=? AND model_id=?",
                      (change_id, sha, model)).fetchone() is not None


def store_extraction(db: sqlite3.Connection, c: Change, ext: Extraction | None, failure: str | None, raw: str,
                     usage: Usage, *, system: str, prompt_version: str, sha: str, model: str, run_id: str,
                     mode: str = "standard", batch_id: str | None = None, duration_ms: int | None = None) -> int:
    """Save one extraction and its figures, checking each figure in code. Returns the figure count."""
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
    msg = build_extract_message(c)
    db.execute(
        "INSERT INTO extractions (change_id, old_paragraph_id, new_paragraph_id, n_figures, failure, "
        "prompt_version, prompt_sha256, model_id, run_id, created_at, input_hash, raw_response, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, mode, batch_id, duration_ms, "
        "input_chars) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (c.change_id, c.old_paragraph_id, c.new_paragraph_id, len(figures), failure, prompt_version, sha, model,
         run_id, now, hashlib.sha256(msg.encode()).hexdigest(), raw, usage.input, usage.output, usage.cache_read,
         usage.cache_write, mode, batch_id, duration_ms, len(system) + len(msg)))
    db.commit()
    return len(figures)


def extract_figures(client, changes: list[Change], db: sqlite3.Connection, *,
                    prompt_version: str, model: str, log=print) -> str:
    init_tables(db)
    system, sha = load_prompt(prompt_version)
    run_id = uuid.uuid4().hex[:12]
    done = skipped = 0
    for c in changes:
        if is_extracted(db, c.change_id, sha, model):
            skipped += 1
            continue
        ext, failure, raw, usage, ms = extract_one(client, c, system, model)
        n = store_extraction(db, c, ext, failure, raw, usage, system=system, prompt_version=prompt_version,
                             sha=sha, model=model, run_id=run_id, duration_ms=ms)
        done += 1
        log(f"[{done}] {c.change_id} {n} figures" + (f" ({failure})" if failure else "") + f" {ms / 1000:.1f}s")
    log(f"run {run_id}: extracted {done}, skipped {skipped} already done")
    return run_id


def report(db: sqlite3.Connection, sha: str, model: str, ids: set[str]) -> dict:
    rows = [r for r in db.execute(
        "SELECT change_id, side, value_as_written, metric, grounded, reason FROM figures "
        "WHERE prompt_sha256=? AND model_id=? ORDER BY change_id, idx", (sha, model)) if r[0] in ids]
    bad = [r for r in rows if not r[4]]
    return {"figures": len(rows), "ungrounded": len(bad), "rows": rows, "bad": bad}
