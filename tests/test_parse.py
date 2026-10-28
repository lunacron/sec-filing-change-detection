from pathlib import Path

from edgar_diff.parse.html import parse_filing

HTML = """<html><body>
<ix:header><div>hidden xbrl</div></ix:header>
<table><tr><td>Item 1.</td><td>Business</td><td>1</td></tr></table>
<div>Item 1.    Business</div>
<div>We make things.</div>
<div>Apple Inc. | 2025 Form 10-K | 3</div>
<div>Item 1A.    Risk Factors</div>
<div>Things can go wrong.</div>
<table><tr><td>a</td><td>1</td></tr></table>
</body></html>"""


def test_sections_paragraphs_and_cleanup(tmp_path: Path):
    f = tmp_path / "x.htm"
    f.write_text(HTML)
    pf = parse_filing(f, "acc-1")
    assert [s.key for s in pf.sections] == ["10-K/1", "10-K/1A"]
    assert [p.text for p in pf.sections[0].paragraphs] == ["We make things."]  # footer + hidden dropped
    risk = pf.sections[1].paragraphs
    assert [p.kind for p in risk] == ["paragraph", "table"]
    assert risk[0].paragraph_id.startswith("acc-1:10-K/1A:0000:")


def test_paragraph_id_stable(tmp_path: Path):
    f = tmp_path / "x.htm"
    f.write_text(HTML)
    a = parse_filing(f, "acc-1").sections[0].paragraphs[0].paragraph_id
    b = parse_filing(f, "acc-1").sections[0].paragraphs[0].paragraph_id
    assert a == b
