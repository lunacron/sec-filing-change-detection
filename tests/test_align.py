from edgar_diff.align.sections import align_sections
from edgar_diff.parse.models import ParsedFiling, Section


def _filing(acc, keys):
    return ParsedFiling(accession=acc, sections=[Section(key=k, title=k, paragraphs=[]) for k in keys])


def test_matched_added_removed():
    a = align_sections(_filing("old", ["10-K/1", "10-K/1A", "10-K/2"]),
                       _filing("new", ["10-K/1", "10-K/1A", "10-K/1C"]))
    kinds = {s.key: s.kind for s in a.sections}
    assert kinds == {"10-K/1": "matched", "10-K/1A": "matched",
                     "10-K/1C": "added", "10-K/2": "removed"}
    assert [s.key for s in a.sections] == ["10-K/1", "10-K/1A", "10-K/1C", "10-K/2"]
