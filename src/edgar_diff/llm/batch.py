"""Run classify or extract through Anthropic's Message Batches API: half price, but asynchronous.

`submit` sends one request per change in a single batch and records the batch ID. Nothing is
waited for. `collect` is run later: when the batch has ended it reads the results and saves them
exactly like normal calls (same tables, same checks), marked mode = 'batch' with the batch ID.
A change that errored, expired or was cancelled is not saved, so the next run sends it again.

The custom_id of each request is the change_id, which is how a result finds its change.
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..diff.paragraphs import Change, DiffResult
from . import classify as clf
from . import extract as ext

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS batches (
        batch_id TEXT PRIMARY KEY, step TEXT, prompt_version TEXT, prompt_sha256 TEXT, model_id TEXT,
        changes_path TEXT, n_requests INTEGER, created_at TEXT, ended_at TEXT, status TEXT,
        succeeded INTEGER, failed INTEGER, collected_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS batch_items (batch_id TEXT, change_id TEXT, PRIMARY KEY (batch_id, change_id))",
]

# step -> (is already saved?, request arguments)
STEPS = {
    "classify": (clf.is_classified, clf.request_params),
    "extract": (ext.is_extracted, ext.request_params),
}


def init_tables(db: sqlite3.Connection) -> None:
    ext.init_tables(db)
    for stmt in SCHEMA:
        db.execute(stmt)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def waiting_ids(db: sqlite3.Connection, step: str, sha: str, model: str) -> set[str]:
    """Changes already sent in a batch whose results have not been collected."""
    return {r[0] for r in db.execute(
        "SELECT i.change_id FROM batch_items i JOIN batches b USING (batch_id) "
        "WHERE b.step = ? AND b.prompt_sha256 = ? AND b.model_id = ? AND b.collected_at IS NULL",
        (step, sha, model))}


def submit(client, db: sqlite3.Connection, step: str, changes: list[Change], *, changes_path: Path,
           prompt_version: str, model: str, log=print) -> str | None:
    init_tables(db)
    saved, params = STEPS[step]
    system, sha = clf.load_prompt(prompt_version)
    waiting = waiting_ids(db, step, sha, model)
    todo = [c for c in changes if not saved(db, c.change_id, sha, model) and c.change_id not in waiting]
    log(f"{len(changes)} changes: {len(changes) - len(todo)} already saved or waiting in an open batch, "
        f"{len(todo)} to send")
    if not todo:
        return None
    batch = client.messages.batches.create(
        requests=[{"custom_id": c.change_id, "params": params(c, system, model, cache_ttl="1h")} for c in todo])
    db.execute("INSERT INTO batches (batch_id, step, prompt_version, prompt_sha256, model_id, changes_path, "
               "n_requests, created_at, status) VALUES (?,?,?,?,?,?,?,?,?)",
               (batch.id, step, prompt_version, sha, model, str(changes_path), len(todo),
                _iso(batch.created_at) or datetime.now(timezone.utc).isoformat(), batch.processing_status))
    db.executemany("INSERT INTO batch_items VALUES (?,?)", [(batch.id, c.change_id) for c in todo])
    db.commit()
    log(f"batch {batch.id} submitted with {len(todo)} requests. Collect it later with: edgar batch-collect")
    return batch.id


def open_batches(db: sqlite3.Connection) -> list[str]:
    init_tables(db)
    return [r[0] for r in db.execute("SELECT batch_id FROM batches WHERE collected_at IS NULL ORDER BY created_at")]


def collect(client, db: sqlite3.Connection, batch_id: str, log=print) -> bool:
    """Save the results of a finished batch. Returns False if it is still running."""
    init_tables(db)
    row = db.execute("SELECT step, prompt_version, prompt_sha256, model_id, changes_path, collected_at "
                     "FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown batch {batch_id}: it was not submitted from this database")
    step, prompt_version, sha, model, changes_path, collected = row
    if collected:
        log(f"batch {batch_id} was already collected at {collected}")
        return True
    b = client.messages.batches.retrieve(batch_id)
    rc = b.request_counts
    if b.processing_status != "ended":
        log(f"batch {batch_id} is {b.processing_status}: {rc.processing} still processing, "
            f"{rc.succeeded} succeeded, {rc.errored} errored")
        return False
    by_id = {c.change_id: c for c in DiffResult.model_validate_json(Path(changes_path).read_text()).changes}
    system, _ = clf.load_prompt(prompt_version)
    run_id = uuid.uuid4().hex[:12]
    saved_n = failed = 0
    for r in client.messages.batches.results(batch_id):
        c = by_id.get(r.custom_id)
        if r.result.type != "succeeded" or c is None:
            failed += 1
            log(f"  {r.custom_id}: {r.result.type}, not saved (it will be sent again next time)")
            continue
        kw = dict(system=system, prompt_version=prompt_version, sha=sha, model=model, run_id=run_id,
                  mode="batch", batch_id=batch_id)
        if STEPS[step][0](db, c.change_id, sha, model):
            continue
        if step == "classify":
            cls, raw, usage = clf.parse_message(r.result.message)
            clf.store_classification(db, c, cls, raw, usage, **kw)
        else:
            e, failure, raw, usage = ext.parse_message(r.result.message)
            ext.store_extraction(db, c, e, failure, raw, usage, **kw)
        saved_n += 1
    db.execute("UPDATE batches SET status='ended', ended_at=?, succeeded=?, failed=?, collected_at=? "
               "WHERE batch_id=?", (_iso(b.ended_at), saved_n, failed,
                                    datetime.now(timezone.utc).isoformat(), batch_id))
    db.commit()
    log(f"batch {batch_id} collected: {saved_n} saved, {failed} not saved")
    return True
