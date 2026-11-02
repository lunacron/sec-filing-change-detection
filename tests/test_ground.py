import json
from types import SimpleNamespace

from edgar_diff.diff.paragraphs import Change
from edgar_diff.ground.numbers import find_numbers, ground_figure, looks_like_figure, number_check
from edgar_diff.llm import extract as ext


def test_find_numbers_reads_scales_commas_and_percent():
    vals = {(n.value, n.percent) for n in find_numbers("Paper of $8.0 billion, 12,430 units and 5.1% growth.")}
    assert vals == {(8e9, False), (12430.0, False), (5.1, True)}


def test_number_check_accepts_stated_scale_and_percent_forms():
    t = "Commercial paper was $8.0 billion, up 5.1%."
    assert number_check(8e9, t) is None
    assert number_check(5.1, t) is None
    assert number_check(0.051, t) is None


def test_number_check_flags_invented_and_wrong_scale():
    t = "Total lease liabilities were 13,720 at year end."
    assert number_check(13720, t) is None
    assert number_check(9999, t) == "not_in_source"
    assert number_check(13.72e9, t) == "scale_mismatch"   # table header never said "millions"
    assert number_check(1.2, "grew 12%") == "not_in_source"  # near-miss 1.2 vs 12


def test_ground_figure_checks_quote_first():
    t = "Commercial paper was $8.0 billion."
    assert ground_figure(8e9, "was $8.0  billion", t) == (True, None)
    assert ground_figure(8e9, "was $9.0 billion", t) == (False, "quote_mismatch")
    assert ground_figure(7e9, "was $8.0 billion", t) == (False, "not_in_source")


def test_looks_like_figure_ignores_years_and_dates():
    assert not looks_like_figure("As of September 28, 2024, the Company filed in 2025.")
    assert looks_like_figure("fell to $8.0 billion") and looks_like_figure("up 5%")


def _change(old, new):
    return Change(change_id="chg-1", kind="modified", old_section="10-K/7", new_section="10-K/7",
                  old_paragraph_id="o1", new_paragraph_id="n1", old_text=old, new_text=new)


def _resp(payload):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _fig(**kw):
    base = dict(value_as_written="$8.0 billion", normalized_value=8e9, unit="USD", metric="commercial paper",
                period=None, source_quote="was $8.0 billion", side="new")
    return {**base, **kw}


def test_extract_stores_grounded_and_flagged_figures_and_caches(tmp_path):
    from edgar_diff.llm import classify as clf
    c = _change("Paper was $10.0 billion.", "Paper was $8.0 billion.")
    good, bad = _fig(), _fig(value_as_written="$9.0 billion", normalized_value=9e9, source_quote="was $8.0 billion")
    client = FakeClient(_resp({"figures": [good, bad]}))
    db = clf.open_db(tmp_path / "t.db")
    kw = dict(prompt_version="extract_v001", model="m", log=lambda *_: None)
    ext.extract_figures(client, [c], db, **kw)
    rows = db.execute("SELECT grounded, reason, char_start FROM figures ORDER BY idx").fetchall()
    assert rows == [(1, None, 6), (0, "not_in_source", 6)]
    ext.extract_figures(client, [c], db, **kw)          # second run: cached, no API call
    assert len(client.calls) == 1


def test_extract_caches_a_change_with_no_figures(tmp_path):
    from edgar_diff.llm import classify as clf
    c = _change("a $5 fee", "a $6 fee")
    client = FakeClient(_resp({"figures": []}))
    db = clf.open_db(tmp_path / "t.db")
    kw = dict(prompt_version="extract_v001", model="m", log=lambda *_: None)
    ext.extract_figures(client, [c], db, **kw)
    ext.extract_figures(client, [c], db, **kw)
    assert len(client.calls) == 1
    assert db.execute("SELECT n_figures FROM extractions").fetchone() == (0,)
