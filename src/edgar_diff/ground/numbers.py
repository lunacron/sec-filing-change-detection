"""Find numbers in filing text and check that a figure the model reports is really in the source.

No LLM is involved here. The model says "this paragraph contains $8.0 billion"; this module
looks for 8.0 followed by "billion" in the paragraph itself. A figure that fails is kept and
flagged (never dropped) with one of these reasons:

  quote_mismatch  the model's source_quote is not in the paragraph on the side it named
  not_in_source   no number in that paragraph has the value the model gave
  scale_mismatch  a number with the right digits is there, but the model applied a scale
                  (thousand, million, billion) that the paragraph does not state, e.g. a
                  table cell "12,430" that the model turned into 12.43 billion

Numbers written as words ("five-year", "three million", "twenty-five") are found too, up to
nineteen, the tens, and "hundred". Limits of this first version: the sign is ignored (a value in parentheses is compared as a positive number).
"""
import math
import re
from dataclasses import dataclass

SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12, "bn": 1e9}

_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
WORDS = {**_ONES, **_TENS, "hundred": 100}

_WORD = (r"(?:(?:" + "|".join(_TENS) + r")(?:[-\s](?:" + "|".join(list(_ONES)[:9]) + r"))?|"
         + "|".join(sorted(list(_ONES) + ["hundred"], key=len, reverse=True)) + r")")
_NUMBER = re.compile(
    r"(?:(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)|\b(?P<word>" + _WORD + r")\b)"
    r"(?:\s*(?P<scale>thousand|million|billion|trillion|bn)\b)?"
    r"(?P<pct>\s*%|\s+percent\b)?",
    re.IGNORECASE)


def _word_value(w: str) -> float:
    parts = re.split(r"[-\s]", w.lower())
    return float(sum(WORDS[p] for p in parts))


@dataclass(frozen=True)
class FoundNumber:
    digits: float          # the number as printed, e.g. 8.0 for "$8.0 billion"
    scale: float           # 1e9 for "billion", 1 when no scale word follows
    percent: bool
    start: int
    end: int

    @property
    def value(self) -> float:
        return self.digits * self.scale


def find_numbers(text: str) -> list[FoundNumber]:
    out = []
    for m in _NUMBER.finditer(text):
        scale = SCALES[m.group("scale").lower()] if m.group("scale") else 1.0
        digits = float(m.group("num").replace(",", "")) if m.group("num") else _word_value(m.group("word"))
        out.append(FoundNumber(digits, scale, bool(m.group("pct")), m.start(), m.end()))
    return out


_FIGURE_HINT = re.compile(
    r"\$\s?\d|\d[\d,]*\.?\d*\s?(?:%|percent|million|billion|thousand)|\b\d{1,3}(?:,\d{3})+\b",
    re.IGNORECASE)


def looks_like_figure(text: str) -> bool:
    """Cheap filter: does the text have a dollar amount, a percentage, a scaled or comma number?
    Years and dates alone do not count, so most paragraphs without real figures are skipped."""
    return bool(_FIGURE_HINT.search(text))


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)


def number_check(normalized_value: float, text: str) -> str | None:
    """None when the value is in the text, otherwise the failure reason."""
    nums = find_numbers(text)
    v = abs(normalized_value)
    for n in nums:
        candidates = [n.value] + ([n.value / 100] if n.percent else [])
        if any(_same(v, c) for c in candidates):
            return None
    for n in nums:  # right digits, scale the paragraph never states
        if any(_same(v, n.digits * k) for k in (1e3, 1e6, 1e9, 1e12)):
            return "scale_mismatch"
    return "not_in_source"


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def quote_check(quote: str, text: str) -> bool:
    return bool(quote.strip()) and _squash(quote) in _squash(text)


def quote_span(quote: str, text: str) -> tuple[int, int] | None:
    """Character span of the quote in the original text, when it is there exactly."""
    i = text.find(quote)
    return (i, i + len(quote)) if i >= 0 and quote else None


def ground_figure(normalized_value: float, quote: str, text: str) -> tuple[bool, str | None]:
    """Returns (grounded, reason). `text` is the paragraph on the side the figure names."""
    if not quote_check(quote, text):
        return False, "quote_mismatch"
    reason = number_check(normalized_value, text)
    return reason is None, reason
