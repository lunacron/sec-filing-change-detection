from pydantic import BaseModel


class Paragraph(BaseModel):
    paragraph_id: str
    accession: str
    section_key: str  # e.g. "10-K/1A"
    ordinal: int
    kind: str  # "paragraph" | "table"
    text: str  # as written, whitespace-collapsed
    normalized_text: str  # lowercased, punctuation-normalized, for matching


class Section(BaseModel):
    key: str
    title: str
    paragraphs: list[Paragraph]


class ParsedFiling(BaseModel):
    accession: str
    sections: list[Section]
