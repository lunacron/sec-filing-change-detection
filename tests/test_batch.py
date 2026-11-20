import json
from datetime import datetime, timezone
from types import SimpleNamespace

from edgar_diff.diff.paragraphs import Change, DiffResult
from edgar_diff.llm import batch
from edgar_diff.llm import classify as clf

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
MODEL = "claude-sonnet-5-5"
GOOD = {"label": "legal_regulatory", "secondary_label": None, "is_material": True, "confidence": "high",
        "rationale": "A fine.", "evidence_quotes": ["new text"], "uncertainty_reason": None}


def _change(i):
    return Change(change_id=f"chg-{i}", kind="modified", old_section="10-K/7", new_section="10-K/7",
                  old_paragraph_id=f"o{i}", new_paragraph_id=f"n{i}", old_text="old text here", new_text="new text here")


def _msg(payload=GOOD):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


class FakeBatches:
    def __init__(self, status="ended", results=(), batch_id="msgbatch_1"):
        self.status, self.result_list, self.created, self.batch_id = status, list(results), [], batch_id

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id=self.batch_id, created_at=NOW, processing_status="in_progress")

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status=self.status, ended_at=NOW,
                               request_counts=SimpleNamespace(processing=2, succeeded=0, errored=0))

    def results(self, batch_id):
        return iter(self.result_list)


def _client(**kw):
    return SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches(**kw)))


def _setup(tmp_path, n=2):
    changes = [_change(i) for i in range(n)]
    path = tmp_path / "x.changes.json"
    path.write_text(DiffResult(old_accession="a", new_accession="b", summary={}, changes=changes).model_dump_json())
    return clf.open_db(tmp_path / "t.db"), changes, path


def _ok(cid, payload=GOOD):
    return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="succeeded", message=_msg(payload)))


def test_submit_sends_one_request_per_change_with_the_same_settings_as_normal_calls(tmp_path):
    db, changes, path = _setup(tmp_path)
    client = _client()
    bid = batch.submit(client, db, "classify", changes, changes_path=path, prompt_version="classify_v002",
                       model=MODEL, log=lambda *_: None)
    reqs = client.messages.batches.created[0]
    system, _ = clf.load_prompt("classify_v002")
    assert bid == "msgbatch_1" and [r["custom_id"] for r in reqs] == ["chg-0", "chg-1"]
    assert reqs[0]["params"] == clf.request_params(changes[0], system, MODEL, cache_ttl="1h")
    assert db.execute("SELECT n_requests, collected_at FROM batches").fetchone() == (2, None)


def test_changes_already_saved_or_waiting_are_not_sent_again(tmp_path):
    db, changes, path = _setup(tmp_path, 3)
    kw = dict(changes_path=path, prompt_version="classify_v002", model=MODEL, log=lambda *_: None)
    system, sha = clf.load_prompt("classify_v002")
    cls, raw, usage = clf.parse_message(_msg())
    clf.store_classification(db, changes[0], cls, raw, usage, system=system, prompt_version="classify_v002",
                             sha=sha, model=MODEL, run_id="r")
    client = _client()
    batch.submit(client, db, "classify", changes, **kw)
    assert [r["custom_id"] for r in client.messages.batches.created[0]] == ["chg-1", "chg-2"]
    assert batch.submit(client, db, "classify", changes, **kw) is None     # all saved or waiting
    assert len(client.messages.batches.created) == 1


def test_collect_saves_results_as_batch_calls_with_the_same_checks(tmp_path):
    db, changes, path = _setup(tmp_path)
    kw = dict(changes_path=path, prompt_version="classify_v002", model=MODEL, log=lambda *_: None)
    batch.submit(_client(), db, "classify", changes, **kw)
    done = _client(results=[_ok("chg-0"), _ok("chg-1", {**GOOD, "evidence_quotes": ["invented quote"]})])
    assert batch.collect(done, db, "msgbatch_1", log=lambda *_: None) is True
    rows = db.execute("SELECT change_id, mode, batch_id, input_tokens, evidence_grounded, duration_ms "
                      "FROM classifications ORDER BY change_id").fetchall()
    assert rows == [("chg-0", "batch", "msgbatch_1", 1000, 1, None), ("chg-1", "batch", "msgbatch_1", 1000, 0, None)]
    assert db.execute("SELECT status, succeeded, failed, ended_at IS NOT NULL FROM batches").fetchone() == ("ended", 2, 0, 1)
    assert batch.open_batches(db) == []
    from edgar_diff import cost
    assert [g["mode"] for g in cost.report(db)] == ["batch"]
    assert batch.collect(done, db, "msgbatch_1", log=lambda *_: None) is True   # second collect does nothing
    assert db.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 2


def test_collect_waits_for_a_running_batch_and_does_not_save_failures(tmp_path):
    db, changes, path = _setup(tmp_path)
    kw = dict(changes_path=path, prompt_version="classify_v002", model=MODEL, log=lambda *_: None)
    batch.submit(_client(), db, "classify", changes, **kw)
    assert batch.collect(_client(status="in_progress"), db, "msgbatch_1", log=lambda *_: None) is False
    assert batch.open_batches(db) == ["msgbatch_1"]
    expired = SimpleNamespace(custom_id="chg-1", result=SimpleNamespace(type="expired"))
    batch.collect(_client(results=[_ok("chg-0"), expired]), db, "msgbatch_1", log=lambda *_: None)
    assert [r[0] for r in db.execute("SELECT change_id FROM classifications")] == ["chg-0"]
    assert db.execute("SELECT failed FROM batches").fetchone() == (1,)
    # the expired change is no longer waiting, so the next submit sends it again
    client = _client(batch_id="msgbatch_2")
    batch.submit(client, db, "classify", changes, **kw)
    assert [r["custom_id"] for r in client.messages.batches.created[0]] == ["chg-1"]


def test_extract_works_through_batch_too(tmp_path):
    from edgar_diff.llm import extract as ext
    db, changes, path = _setup(tmp_path, 1)
    kw = dict(changes_path=path, prompt_version="extract_v001", model=MODEL, log=lambda *_: None)
    batch.submit(_client(), db, "extract", changes, **kw)
    fig = {"value_as_written": "$8.0 billion", "normalized_value": 8e9, "unit": "USD", "metric": "x",
           "period": None, "source_quote": "new text", "side": "new"}
    batch.collect(_client(results=[_ok("chg-0", {"figures": [fig]})]), db, "msgbatch_1", log=lambda *_: None)
    assert db.execute("SELECT n_figures, mode, batch_id FROM extractions").fetchone() == (1, "batch", "msgbatch_1")
    assert db.execute("SELECT grounded, reason FROM figures").fetchone() == (0, "not_in_source")
    assert ext.is_extracted(db, "chg-0", clf.load_prompt("extract_v001")[1], MODEL)
