"""Align sections of two parsed filings by canonical Item key."""
from typing import Literal

from pydantic import BaseModel

from ..parse.models import ParsedFiling


class SectionAlignment(BaseModel):
    key: str
    kind: Literal["matched", "added", "removed"]
    old_title: str | None = None
    new_title: str | None = None
    old_paragraphs: int = 0
    new_paragraphs: int = 0


class FilingAlignment(BaseModel):
    old_accession: str
    new_accession: str
    sections: list[SectionAlignment]


def align_sections(old: ParsedFiling, new: ParsedFiling) -> FilingAlignment:
    old_by_key = {s.key: s for s in old.sections}
    new_by_key = {s.key: s for s in new.sections}
    # Keep the new filing's order, then append removed keys in old order.
    keys = [s.key for s in new.sections] + [k for k in old_by_key if k not in new_by_key]
    out = []
    for key in keys:
        o, n = old_by_key.get(key), new_by_key.get(key)
        out.append(SectionAlignment(
            key=key,
            kind="matched" if o and n else "added" if n else "removed",
            old_title=o.title if o else None,
            new_title=n.title if n else None,
            old_paragraphs=len(o.paragraphs) if o else 0,
            new_paragraphs=len(n.paragraphs) if n else 0,
        ))
    return FilingAlignment(old_accession=old.accession, new_accession=new.accession, sections=out)
