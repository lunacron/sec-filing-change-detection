"""Parse a 10-K primary document into Item-level sections of paragraphs."""
import hashlib
import re
import unicodedata
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser

from .models import Paragraph, ParsedFiling, Section

ITEM_RE = re.compile(r"^Item\s+(\d{1,2}[A-C]?)\s*[.:\-–—]\s*(.*)$", re.I | re.S)
PAGE_FOOTER_RE = re.compile(r"^(.{0,60}form 10-k\s*\|\s*)?(\d{1,3}|page \d+)$", re.I)
BLOCK_TAGS = {"div", "p"}
DROP_TAGS = ("script", "style", "ix\\:header")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = t.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                   "–": "-", "—": "-"}))
    return re.sub(r"\s+", " ", t).strip()


def _has_block_child(node) -> bool:
    return any(d.mem_id != node.mem_id for d in node.css("div,p,table"))


def _blocks(root) -> list[tuple[str, str]]:
    """Ordered (kind, text) blocks. Leaf div/p -> paragraph; table -> one block."""
    out: list[tuple[str, str]] = []

    def walk(node):
        for child in node.iter(include_text=False):
            tag = child.tag
            if tag == "table":
                rows = []
                for tr in child.css("tr"):
                    cells = [_clean(td.text()) for td in tr.css("td,th")]
                    cells = [c for c in cells if c]
                    if cells:
                        rows.append(" | ".join(cells))
                if rows:
                    out.append(("table", "\n".join(rows)))
            elif tag in BLOCK_TAGS and not _has_block_child(child):
                text = _clean(child.text())
                if text:
                    out.append(("paragraph", text))
            else:
                walk(child)

    walk(root)
    return out


def _in_table(node) -> bool:
    p = node.parent
    while p is not None:
        if p.tag == "table":
            return True
        p = p.parent
    return False


def parse_filing(path: Path, accession: str) -> ParsedFiling:
    tree = LexborHTMLParser(path.read_text(encoding="utf-8", errors="replace"))
    for tag in DROP_TAGS:
        for n in tree.css(tag):
            n.decompose()
    body = tree.body

    # Table of contents lives in a <table>; real headings are standalone blocks.
    # Mark TOC-ish text by removing tables from heading consideration (handled below
    # because tables become single "table" blocks, never matched as headings).
    sections: list[Section] = []
    current: Section | None = None
    seen: set[str] = set()
    for kind, text in _blocks(body):
        m = ITEM_RE.match(text) if kind == "paragraph" and len(text) < 300 else None
        if m:
            key = f"10-K/{m.group(1).upper()}"
            if key not in seen:
                seen.add(key)
                current = Section(key=key, title=_clean(m.group(2)), paragraphs=[])
                sections.append(current)
                continue
        if current is None or (kind == "paragraph" and PAGE_FOOTER_RE.match(text)):
            continue
        norm = normalize(text)
        ordinal = len(current.paragraphs)
        h = hashlib.sha256(norm.encode()).hexdigest()[:8]
        current.paragraphs.append(Paragraph(
            paragraph_id=f"{accession}:{current.key}:{ordinal:04d}:{h}",
            accession=accession, section_key=current.key, ordinal=ordinal,
            kind=kind, text=text, normalized_text=norm,
        ))
    return ParsedFiling(accession=accession, sections=sections)
