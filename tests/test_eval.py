import json
import sqlite3

import pytest

from edgar_diff.diff.paragraphs import Change, DiffResult
from edgar_diff.eval import runner
from edgar_diff.llm import classify as clf


def _change(i, needs_llm=True):
    return Change(change_id=f"chg-{i}", kind="modified", old_section="10-K/1A", new_section="10-K/1A",
                  old_paragraph_id=f"o{i}", new_paragraph_id=f"n{i}", old_text="old", new_text="new",
                  needs_llm=needs_llm)


def _gold_dir(tmp_path, practice, holdout, thresholds=None, baseline=None):
    (tmp_path / "gold_labels.json").write_text(json.dumps(practice))
    (tmp_path / "gold_labels_holdout.json").write_text(json.dumps(holdout))
    if thresholds is not None:
        (tmp_path / "thresholds.json").write_text(json.dumps(thresholds))
    if baseline is not None:
        (tmp_path / "baseline.json").write_text(json.dumps(baseline))
    return tmp_path


def _changes_file(tmp_path, changes):
    path = tmp_path / "changes.json"
    path.write_text(DiffResult(old_accession="a", new_accession="b", summary={}, changes=changes).model_dump_json())
    return path


def _save_answer(db, cid, sha, label, material=False, model="m"):
    db.execute("INSERT INTO classifications (change_id, label, is_material, prompt_sha256, model_id) "
               "VALUES (?,?,?,?,?)", (cid, label, int(material), sha, model))
    db.commit()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "classify_t.md").write_text("system prompt")
    db = clf.open_db(tmp_path / "t.db")
    sha = clf.load_prompt("classify_t")[1]
    return tmp_path, db, sha


def test_load_gold_marks_adjudicated_labels_as_not_blind(tmp_path):
    d = _gold_dir(tmp_path,
                  {"chg-1": {"label": "non_material", "secondary": None, "note": "", "status": "confirmed"},
                   "chg-2": {"label": "risk_factor", "secondary": None, "note": "",
                             "status": "adjudicated_after_seeing_model"}},
                  {"chg-3": {"case": 1, "label": "risk_factor", "second": "non_material", "material": True, "note": ""}})
    g = runner.load_gold(d)
    assert g["chg-1"].blind and not g["chg-2"].blind
    assert g["chg-3"].group == "holdout" and g["chg-3"].second == "non_material" and g["chg-3"].material is True


def test_score_counts_exact_lenient_and_material(tmp_path):
    d = _gold_dir(tmp_path, {},
                  {"chg-1": {"case": 1, "label": "risk_factor", "second": "non_material", "material": True},
                   "chg-2": {"case": 2, "label": "risk_factor", "second": "non_material", "material": False},
                   "chg-3": {"case": 3, "label": "financial_results", "second": None, "material": False}})
    rows = {"chg-1": {"label": "risk_factor", "material": True},
            "chg-2": {"label": "non_material", "material": True},
            "chg-3": {"label": "uncertain", "material": False}}
    scores, correct = runner.score(runner.load_gold(d), rows)
    h = scores["holdout"]
    assert (h["strict"], h["lenient"], h["material_agree"], h["uncertain"]) == (1, 2, 2, 1)
    assert correct == {"chg-1": True, "chg-2": False, "chg-3": False}


def test_check_fails_below_threshold_and_on_new_failures():
    scores = {"practice_blind": {"strict_rate": 0.9}, "holdout": {"strict_rate": 0.55, "lenient_rate": 0.8,
                                                                    "material_rate": 0.5}}
    thresholds = {"holdout_strict_min": 0.6, "holdout_lenient_min": 0.75, "max_new_failures": 1}
    fails, regressed = runner.check(scores, {"a": False, "b": False, "c": True},
                                    thresholds, {"correct": {"a": True, "b": True, "c": True}})
    assert regressed == ["a", "b"]
    assert any("holdout set, exact label" in f for f in fails)
    assert any("2 change(s)" in f for f in fails)
    assert not any("lenient" in f for f in fails)


def test_check_treats_missing_scores_as_failure():
    scores = {"practice_blind": {"strict_rate": None}, "holdout": {"strict_rate": None, "lenient_rate": None,
                                                                    "material_rate": None}}
    fails, _ = runner.check(scores, {}, {"holdout_strict_min": 0.6}, None)
    assert fails and "no data" in fails[0]


def test_missing_gold_change_stops_the_run(setup):
    tmp, db, sha = setup
    d = _gold_dir(tmp, {"chg-1": {"label": "risk_factor"}, "chg-9": {"label": "risk_factor"}}, {})
    path = _changes_file(tmp, [_change(1)])
    with pytest.raises(runner.EvalError, match="chg-9"):
        runner.run_eval(changes_path=path, prompt="classify_t", model="m", db=db, client=None,
                        gold_dir=d, log=lambda *_: None)


def test_gold_change_that_no_longer_needs_llm_stops_the_run(setup):
    tmp, db, sha = setup
    d = _gold_dir(tmp, {"chg-1": {"label": "risk_factor"}}, {})
    path = _changes_file(tmp, [_change(1, needs_llm=False)])
    with pytest.raises(runner.EvalError, match="no longer go to the LLM"):
        runner.run_eval(changes_path=path, prompt="classify_t", model="m", db=db, client=None,
                        gold_dir=d, log=lambda *_: None)


def test_pass_then_save_baseline_then_regression_fails(setup):
    tmp, db, sha = setup
    practice = {f"chg-{i}": {"label": "risk_factor"} for i in range(1, 4)}
    holdout = {"chg-4": {"case": 1, "label": "risk_factor", "second": None, "material": True}}
    d = _gold_dir(tmp, practice, holdout, thresholds={"max_new_failures": 0})
    path = _changes_file(tmp, [_change(i) for i in range(1, 5)])
    for i in range(1, 5):
        _save_answer(db, f"chg-{i}", sha, "risk_factor", material=True)
    run = lambda: runner.run_eval(changes_path=path, prompt="classify_t", model="m", db=db, client=None,
                                  gold_dir=d, save_baseline=True, log=lambda *_: None)
    assert run() == 0
    assert (d / "baseline.json").exists()
    db.execute("UPDATE classifications SET label='non_material' WHERE change_id='chg-2'")
    db.commit()
    assert runner.run_eval(changes_path=path, prompt="classify_t", model="m", db=db, client=None,
                           gold_dir=d, log=lambda *_: None) == 1


def test_failing_run_does_not_overwrite_the_baseline(setup):
    tmp, db, sha = setup
    d = _gold_dir(tmp, {"chg-1": {"label": "risk_factor"}},
                  {"chg-2": {"case": 1, "label": "risk_factor", "second": None, "material": True}},
                  thresholds={"holdout_strict_min": 1.0})
    path = _changes_file(tmp, [_change(1), _change(2)])
    _save_answer(db, "chg-1", sha, "risk_factor")
    _save_answer(db, "chg-2", sha, "non_material", material=True)
    code = runner.run_eval(changes_path=path, prompt="classify_t", model="m", db=db, client=None,
                           gold_dir=d, save_baseline=True, log=lambda *_: None)
    assert code == 1 and not (d / "baseline.json").exists()


def test_unclassified_changes_need_a_client(setup):
    tmp, db, sha = setup
    d = _gold_dir(tmp, {"chg-1": {"label": "risk_factor"}}, {})
    path = _changes_file(tmp, [_change(1)])
    with pytest.raises(runner.EvalError, match="ANTHROPIC_API_KEY"):
        runner.run_eval(changes_path=path, prompt="classify_t", model="m", db=db, client=None,
                        gold_dir=d, log=lambda *_: None)


def test_missing_changes_file_gives_a_clear_error(setup):
    tmp, db, sha = setup
    d = _gold_dir(tmp, {"chg-1": {"label": "risk_factor"}}, {})
    with pytest.raises(runner.EvalError, match="edgar diff"):
        runner.run_eval(changes_path=tmp / "nope.json", prompt="classify_t", model="m", db=db,
                        client=None, gold_dir=d, log=lambda *_: None)
