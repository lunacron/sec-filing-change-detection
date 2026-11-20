import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from edgar_diff import cost
from edgar_diff.diff.paragraphs import Change
from edgar_diff.llm import classify as clf
from edgar_diff.llm import extract as ext


def _change(i, old="old text here", new="new text here"):
    return Change(change_id=f"chg-{i}", kind="modified", old_section="10-K/7", new_section="10-K/7",
                  old_paragraph_id=f"o{i}", new_paragraph_id=f"n{i}", old_text=old, new_text=new)


def _resp(payload, tin=1000, tout=200):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=tin, output_tokens=tout))


GOOD = {"label": "legal_regulatory", "secondary_label": None, "is_material": True, "confidence": "high",
        "rationale": "A fine.", "evidence_quotes": ["new text"], "uncertainty_reason": None}


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = SimpleNamespace(create=lambda **kw: self.responses.pop(0))


def test_batch_is_exactly_half_price_and_unknown_model_has_no_price():
    std = cost.call_cost("claude-sonnet-5-5", 1_000_000, 100_000)
    assert std == 2.00 + 1.00
    assert cost.call_cost("claude-sonnet-5-5", 1_000_000, 100_000, "batch") == std / 2
    assert cost.call_cost("no-such-model", 10, 10) is None


def test_normal_calls_save_mode_latency_and_input_size(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    clf.classify_changes(FakeClient(_resp(GOOD)), [_change(1)], db, prompt_version="classify_v002",
                         model="claude-sonnet-5-5", log=lambda *_: None)
    mode, ms, chars = db.execute("SELECT mode, duration_ms, input_chars FROM classifications").fetchone()
    system, _ = clf.load_prompt("classify_v002")
    assert mode == "standard" and ms >= 0
    assert chars == len(system) + len(clf.build_user_message(_change(1)))


def test_old_database_without_tracking_columns_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE classifications (change_id TEXT, prompt_sha256 TEXT, model_id TEXT, "
                "prompt_version TEXT, input_tokens INTEGER, output_tokens INTEGER)")
    old.execute("INSERT INTO classifications VALUES ('c','s','claude-sonnet-5-5','classify_v001',1000,100)")
    old.commit()
    db = clf.open_db(path)  # adds the columns
    g = cost.report(db)
    assert g[0]["mode"] == "standard" and g[0]["calls"] == 1 and g[0]["ms"] == []
    assert "not recorded" in "\n".join(cost.format_report(g))


def test_report_groups_by_mode_and_prices_batch_at_half(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    for i, mode in enumerate(["standard", "batch"]):
        db.execute("INSERT INTO classifications (change_id, prompt_sha256, model_id, prompt_version, "
                   "input_tokens, output_tokens, mode, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
                   (f"c{i}", "s", "claude-sonnet-5-5", "classify_v002", 1_000_000, 0, mode, 500 if i == 0 else None))
    by_mode = {g["mode"]: g for g in cost.report(db)}
    assert by_mode["standard"]["usd"] == 2.0 and by_mode["batch"]["usd"] == 1.0
    assert by_mode["standard"]["ms"] == [500] and by_mode["batch"]["ms"] == []


def test_estimate_uses_tokens_per_character_from_history(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    for i in range(cost.MIN_SIZED_CALLS):
        db.execute("INSERT INTO classifications (change_id, prompt_sha256, model_id, prompt_version, "
                   "input_tokens, output_tokens, input_chars, duration_ms) VALUES (?,'s','claude-sonnet-5-5',"
                   "'classify_v002',500,100,1000,2000)", (f"c{i}",))
    e = cost.estimate(db, "classify", "claude-sonnet-5-5", "classify_v002", [2000, 2000])
    assert (e["input_tokens"], e["output_tokens"]) == (2000, 400)   # 0.5 and 0.1 tokens per character
    assert "per character" in e["basis"] and e["seconds_standard"] == 4.0
    assert e["usd_batch"] == e["usd_standard"] / 2


def test_a_few_sized_calls_are_not_enough_for_tokens_per_character(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    for i in range(3):
        db.execute("INSERT INTO classifications (change_id, prompt_sha256, model_id, prompt_version, "
                   "input_tokens, output_tokens, input_chars) VALUES (?,'s','claude-sonnet-5-5','classify_v002',"
                   "500,100,1000)", (f"c{i}",))
    assert "per call" in cost.estimate(db, "classify", "claude-sonnet-5-5", "classify_v002", [1000])["basis"]


def test_estimate_falls_back_to_per_call_average_then_a_guess(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    guess = cost.estimate(db, "classify", "claude-sonnet-5-5", "classify_v002", [4000])
    assert "rough guess" in guess["basis"] and guess["input_tokens"] == 1000
    db.execute("INSERT INTO classifications (change_id, prompt_sha256, model_id, prompt_version, input_tokens, "
               "output_tokens) VALUES ('c','s','claude-sonnet-5-5','classify_v001',900,150)")
    e = cost.estimate(db, "classify", "claude-sonnet-5-5", "classify_v002", [4000, 4000])
    assert (e["input_tokens"], e["output_tokens"]) == (1800, 300) and "per call" in e["basis"]
    other = cost.estimate(db, "classify", "claude-opus-5-5", "classify_v002", [4000])
    assert "another model" in other["basis"] and other["seconds_standard"] is None


def test_batch_time_comes_from_finished_batches(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    ext.init_tables(db)
    from edgar_diff.llm import batch
    batch.init_tables(db)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.execute("INSERT INTO batches (batch_id, step, created_at, ended_at) VALUES ('b','classify',?,?)",
               (t0.isoformat(), (t0 + timedelta(minutes=30)).isoformat()))
    assert cost.batch_seconds(db, "classify") == [1800.0]
    est = cost.estimate(db, "classify", "claude-sonnet-5-5", "classify_v002", [100])
    assert est["seconds_batch"] == 1800.0 and "30 min" in "\n".join(cost.format_estimate(est))


def test_nothing_to_send_says_so():
    est = {"calls": 0}
    assert "costs $0.00" in cost.format_estimate(est)[0]


def _api_usage(tin, tout, read=None, write=None):
    return SimpleNamespace(input_tokens=tin, output_tokens=tout, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write)


def test_cache_tokens_are_priced_at_their_own_rates():
    # sonnet 5.5: input $2, output $10, cache read $0.10 per million; writing costs 1.25x input
    base = cost.call_cost("claude-sonnet-5-5", 0, 0, "standard", cache_read=1_000_000)
    assert base == 0.10
    assert cost.call_cost("claude-sonnet-5-5", 0, 0, "standard", cache_write=1_000_000) == 2.5
    assert cost.call_cost("claude-sonnet-5-5", 0, 0, "batch", cache_write=1_000_000) == 2.0   # 2x write, half price
    assert cost.call_cost("claude-sonnet-5-5", 0, 0, "batch", cache_read=1_000_000) == 0.05


def test_usage_reads_cache_fields_and_treats_missing_ones_as_zero():
    u = cost.Usage.from_api(_api_usage(100, 20, read=900, write=None))
    assert (u.input, u.output, u.cache_read, u.cache_write) == (100, 20, 900, 0)
    plain = cost.Usage.from_api(SimpleNamespace(input_tokens=5, output_tokens=6))   # an older response
    assert (plain.cache_read, plain.cache_write) == (0, 0)


def test_classify_marks_the_system_prompt_for_caching_and_batch_uses_one_hour():
    system, _ = clf.load_prompt("classify_v002")
    normal = clf.request_params(_change(1), system, "m")
    assert normal["system"] == [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    batch = clf.request_params(_change(1), system, "m", cache_ttl="1h")
    assert batch["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in str(ext.request_params(_change(1), system, "m"))   # extract is not cached


def test_cache_tokens_are_saved_and_the_report_prices_them(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    client = FakeClient(SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(GOOD))],
                                        stop_reason="end_turn", usage=_api_usage(300, 50, read=1000, write=0)))
    clf.classify_changes(client, [_change(1)], db, prompt_version="classify_v002", model="claude-sonnet-5-5",
                         log=lambda *_: None)
    assert db.execute("SELECT input_tokens, cache_read_tokens, cache_write_tokens FROM classifications"
                      ).fetchone() == (300, 1000, 0)
    g = cost.report(db)[0]
    assert (g["input"], g["cache_read"]) == (300, 1000)
    assert round(g["usd"], 6) == round((300 * 2 + 50 * 10 + 1000 * 0.10) / 1e6, 6)


def test_estimate_assumes_one_cache_write_then_reads(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    for i in range(cost.MIN_SIZED_CALLS):   # each call: 400 uncached + 1000 cached prompt tokens, 100 output
        db.execute("INSERT INTO classifications (change_id, prompt_sha256, model_id, prompt_version, input_tokens, "
                   "output_tokens, input_chars, cache_read_tokens, cache_write_tokens) VALUES "
                   "(?,'s','claude-sonnet-5-5','classify_v002',400,100,5600,?,?)",
                   (f"c{i}", 0 if i else 0, 1000 if i == 0 else 0))
        if i:
            db.execute("UPDATE classifications SET cache_read_tokens = 1000 WHERE change_id = ?", (f"c{i}",))
    e = cost.estimate(db, "classify", "claude-sonnet-5-5", "classify_v002", [5600] * 100)
    assert e["cached_prefix"] == 1000 and e["input_tokens"] == 140_000
    expected = (100 * 400 * 2 + 100 * 100 * 10 + 99 * 1000 * 0.10 + 1000 * 2 * 1.25) / 1e6
    assert round(e["usd_standard"], 6) == round(expected, 6)
    assert e["usd_batch"] < e["usd_standard"]
    assert "1,000 of them per call cached" in "\n".join(cost.format_estimate(e))
