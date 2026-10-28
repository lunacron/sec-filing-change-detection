"""Paragraph-level diff between two aligned filings."""
import difflib
import hashlib
import re
from collections import defaultdict
from typing import Callable, Literal

import numpy as np
from pydantic import BaseModel
from rapidfuzz import fuzz, process
from scipy.optimize import linear_sum_assignment

from ..align.sections import FilingAlignment
from ..parse.models import Paragraph, ParsedFiling

MODIFIED_MIN = 0.60  # within an aligned section
MOVED_MIN = 0.85  # across sections: stricter, less context to go on

_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
DATE_RE = re.compile(rf"\b(?:{_MONTHS})\.? \d{{1,2}}(?:, (?:19|20)\d{{2}})?")
YEAR_RE = re.compile(r"(?<![\d,.$])(?:19|20)\d{2}(?!\d|[,.]\d)")


def mask_dates(norm: str) -> str:
    """Hide full dates and bare years so a rolled-forward date is not a change.

    Dollar amounts and other numbers are left alone on purpose: a one-digit
    change in a figure must still register as a change.
    """
    return YEAR_RE.sub("<yr>", DATE_RE.sub("<date>", norm))


class WordOp(BaseModel):
    op: Literal["replace", "delete", "insert"]
    old: str
    new: str


class Change(BaseModel):
    change_id: str
    kind: Literal["modified", "added", "removed", "moved"]
    old_section: str | None = None
    new_section: str | None = None
    old_paragraph_id: str | None = None
    new_paragraph_id: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    similarity: float | None = None
    cosmetic: bool = False  # identical once dates/years are masked
    needs_llm: bool = True
    word_diff: list[WordOp] = []


class DiffResult(BaseModel):
    old_accession: str
    new_accession: str
    summary: dict[str, int]
    changes: list[Change]


def word_diff(old: str, new: str) -> list[WordOp]:
    a, b = old.split(), new.split()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    return [
        WordOp(op=op, old=" ".join(a[i1:i2]), new=" ".join(b[j1:j2]))
        for op, i1, i2, j1, j2 in sm.get_opcodes()
        if op != "equal"
    ]


def _pair_by_key(olds: list[Paragraph], news: list[Paragraph], key: Callable[[Paragraph], str]):
    """Pair paragraphs with equal keys, in order. Returns (pairs, old_left, new_left)."""
    by_key: dict[str, list[Paragraph]] = defaultdict(list)
    for n in news:
        by_key[key(n)].append(n)
    pairs, old_left, used = [], [], set()
    for o in olds:
        bucket = by_key.get(key(o))
        if bucket:
            n = bucket.pop(0)
            used.add(n.paragraph_id)
            pairs.append((o, n))
        else:
            old_left.append(o)
    return pairs, old_left, [n for n in news if n.paragraph_id not in used]


def _pair_fuzzy(olds: list[Paragraph], news: list[Paragraph], threshold: float):
    """Optimal one-to-one pairing by similarity. Returns (pairs_with_score, old_left, new_left)."""
    if not olds or not news:
        return [], olds, news
    sim = process.cdist(
        [mask_dates(o.normalized_text) for o in olds],
        [mask_dates(n.normalized_text) for n in news],
        scorer=fuzz.ratio, dtype=np.float32, workers=-1,
    ) / 100.0
    rows, cols = linear_sum_assignment(-sim)
    pairs, used_o, used_n = [], set(), set()
    for r, c in zip(rows, cols):
        if sim[r, c] >= threshold:
            pairs.append((olds[r], news[c], float(sim[r, c])))
            used_o.add(r)
            used_n.add(c)
    return (
        pairs,
        [o for i, o in enumerate(olds) if i not in used_o],
        [n for i, n in enumerate(news) if i not in used_n],
    )


def _change(kind, o: Paragraph | None, n: Paragraph | None, sim, cosmetic=False) -> Change:
    ids = f"{o.paragraph_id if o else ''}|{n.paragraph_id if n else ''}"
    return Change(
        change_id="chg-" + hashlib.sha256(ids.encode()).hexdigest()[:12],
        kind=kind,
        old_section=o.section_key if o else None,
        new_section=n.section_key if n else None,
        old_paragraph_id=o.paragraph_id if o else None,
        new_paragraph_id=n.paragraph_id if n else None,
        old_text=o.text if o else None,
        new_text=n.text if n else None,
        similarity=sim,
        cosmetic=cosmetic,
        needs_llm=not cosmetic,
        word_diff=word_diff(o.text, n.text) if o and n and o.text != n.text and not cosmetic else [],
    )


def _match(olds, news, threshold, moved: bool):
    """Exact -> date-masked exact -> fuzzy. Returns (changes, unchanged_count, old_left, new_left)."""
    changes: list[Change] = []
    exact, olds, news = _pair_by_key(olds, news, lambda p: p.normalized_text)
    masked, olds, news = _pair_by_key(olds, news, lambda p: mask_dates(p.normalized_text))
    fuzzy, olds, news = _pair_fuzzy(olds, news, threshold)

    unchanged = 0
    for o, n in exact:
        if moved and o.section_key != n.section_key:
            # Same text in a different section: a pure move, nothing to classify.
            c = _change("moved", o, n, 1.0)
            c.needs_llm = False
            changes.append(c)
        else:
            unchanged += 1
    for o, n in masked:
        changes.append(_change("moved" if moved and o.section_key != n.section_key else "modified",
                               o, n, 1.0, cosmetic=True))
    for o, n, s in fuzzy:
        changes.append(_change("moved" if moved and o.section_key != n.section_key else "modified",
                               o, n, round(s, 3)))
    return changes, unchanged, olds, news


def diff_filings(old: ParsedFiling, new: ParsedFiling, alignment: FilingAlignment) -> DiffResult:
    old_by_key = {s.key: s for s in old.sections}
    new_by_key = {s.key: s for s in new.sections}
    changes: list[Change] = []
    unchanged = 0
    old_pool: list[Paragraph] = []
    new_pool: list[Paragraph] = []

    # Pass 1: within each aligned section.
    for a in alignment.sections:
        o = old_by_key[a.key].paragraphs if a.key in old_by_key else []
        n = new_by_key[a.key].paragraphs if a.key in new_by_key else []
        if a.kind == "matched":
            c, u, o_left, n_left = _match(o, n, MODIFIED_MIN, moved=False)
            changes += c
            unchanged += u
            o, n = o_left, n_left
        old_pool += o
        new_pool += n

    # Pass 2: leftovers against the whole document, so a paragraph that moved
    # sections is a move, not a removal here plus an addition there.
    c, u, old_pool, new_pool = _match(old_pool, new_pool, MOVED_MIN, moved=True)
    changes += c
    unchanged += u

    changes += [_change("removed", o, None, None) for o in old_pool]
    changes += [_change("added", None, n, None) for n in new_pool]

    summary = {
        "unchanged": unchanged,
        "modified": sum(c.kind == "modified" and not c.cosmetic for c in changes),
        "added": sum(c.kind == "added" for c in changes),
        "removed": sum(c.kind == "removed" for c in changes),
        "moved": sum(c.kind == "moved" for c in changes),
        "cosmetic": sum(c.cosmetic for c in changes),
        "needs_llm": sum(c.needs_llm for c in changes),
    }
    return DiffResult(old_accession=old.accession, new_accession=new.accession,
                      summary=summary, changes=changes)
