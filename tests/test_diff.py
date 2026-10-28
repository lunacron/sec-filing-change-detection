from edgar_diff.align.sections import align_sections
from edgar_diff.diff.paragraphs import diff_filings, mask_dates
from edgar_diff.parse.html import normalize
from edgar_diff.parse.models import Paragraph, ParsedFiling, Section


def _filing(acc: str, sections: dict[str, list[str]]) -> ParsedFiling:
    secs = []
    for key, texts in sections.items():
        paras = [
            Paragraph(paragraph_id=f"{acc}:{key}:{i:04d}:x", accession=acc, section_key=key,
                      ordinal=i, kind="paragraph", text=t, normalized_text=normalize(t))
            for i, t in enumerate(texts)
        ]
        secs.append(Section(key=key, title=key, paragraphs=paras))
    return ParsedFiling(accession=acc, sections=secs)


def _diff(old, new):
    return diff_filings(old, new, align_sections(old, new))


def test_fictitious_legal_proceedings_example():
    old = _filing("o", {"10-K/3": [
        "The Company is subject to various legal proceedings.",
        "In 2023, the Company was sued by Acme over a patent. A trial is scheduled for 2025.",
        "The Company has a dispute with a supplier in Taiwan.",
    ]})
    new = _filing("n", {"10-K/3": [
        "The Company is subject to various legal proceedings.",
        "In 2023, the Company was sued by Acme over a patent. The court ruled against the Company and awarded $50 million.",
        "The EU opened an investigation into the App Store.",
    ]})
    r = _diff(old, new)
    kinds = {c.kind: c for c in r.changes}
    assert r.summary["unchanged"] == 1  # A
    assert kinds["modified"].old_text.startswith("In 2023")  # B
    assert "awarded $50 million." in kinds["modified"].word_diff[0].new
    assert kinds["removed"].old_text.endswith("Taiwan.")  # C
    assert kinds["removed"].new_paragraph_id is None
    assert kinds["added"].new_text.startswith("The EU")  # D
    assert kinds["added"].old_paragraph_id is None
    assert r.summary["needs_llm"] == 3


def test_date_roll_forward_is_cosmetic():
    old = _filing("o", {"10-K/7": ["For the fiscal year ended September 28, 2024, sales rose in 2024."]})
    new = _filing("n", {"10-K/7": ["For the fiscal year ended September 27, 2025, sales rose in 2025."]})
    (c,) = _diff(old, new).changes
    assert c.cosmetic and not c.needs_llm


def test_one_digit_figure_change_is_not_cosmetic():
    base = "Net sales were $391 billion during the year, driven by strong demand across all of the Company's products and services."
    r = _diff(_filing("o", {"10-K/7": [base]}), _filing("n", {"10-K/7": [base.replace("391", "381")]}))
    (c,) = r.changes
    assert c.kind == "modified" and not c.cosmetic and c.needs_llm


def test_paragraph_moved_across_sections():
    text = "The Company relies on a limited number of suppliers for key components used in its products."
    old = _filing("o", {"10-K/1": [text, "Other."], "10-K/1A": ["Risk."]})
    new = _filing("n", {"10-K/1": ["Other."], "10-K/1A": ["Risk.", text]})
    r = _diff(old, new)
    (c,) = r.changes
    assert c.kind == "moved" and (c.old_section, c.new_section) == ("10-K/1", "10-K/1A")
    assert not c.needs_llm  # same text, just relocated


def test_mask_keeps_dollar_amounts():
    assert mask_dates("sold $2024 million on march 3, 2025") == "sold $2024 million on <date>"
    assert mask_dates("net sales of 2,024 units in 2024") == "net sales of 2,024 units in <yr>"
