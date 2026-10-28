# Paragraph Diff Explained (Step 5)

A worked example of how the pipeline decides which paragraphs changed between two filings, and what happens to each one.

**The text below is made up for illustration.** It is not from Apple's real filings.

---

## Where this step sits

1. **Ingest:** download the 2024 and 2025 10-Ks.
2. **Parse:** cut each into Items (sections) of paragraphs.
3. **Align:** pair the sections. Item 3 (Legal Proceedings) in 2025 is paired with Item 3 in 2024.
4. **Paragraph diff (this document):** inside each pair, find the paragraphs that changed.
5. **LLM classification:** classify only the changed paragraphs.

The diff decides what the LLM ever sees. About 570 paragraphs per filing go in, and only the changed ones come out.

---

## Example section: Legal Proceedings (Item 3)

| # | 2024 filing | 2025 filing |
|---|---|---|
| A | "The Company is subject to various legal proceedings." | "The Company is subject to various legal proceedings." |
| B | "In 2023, the Company was sued by Acme over a patent. A trial is scheduled for 2025." | "In 2023, the Company was sued by Acme over a patent. The court ruled against the Company and awarded $50 million." |
| C | "The Company has a dispute with a supplier in Taiwan." | *(not there)* |
| D | *(not there)* | "The EU opened an investigation into the App Store." |

---

## How the diff works

For each aligned section pair:

1. **Exact match.** Normalize each paragraph's text (lowercase, collapse whitespace, straighten quotes) and hash it. Identical hashes are paired as **unchanged**.
2. **Fuzzy match the leftovers.** Score the similarity of every unmatched old paragraph against every unmatched new one (`rapidfuzz`). Pair them one-to-one with Hungarian assignment (`scipy`).
   - Similarity about 0.95 or higher: cosmetic edit.
   - Similarity about 0.60 to 0.95: **modified**.
   - Below about 0.60: not a match.
3. **Still unmatched.**
   - An old paragraph with no match is **removed**.
   - A new paragraph with no match is **added**.
4. **Noise filter.** Mask dates and fiscal years before comparing, so "fiscal 2024" becoming "fiscal 2025" is not flagged. Changes that are only dates, punctuation or numbering are marked cosmetic and skipped.

---

## What happens to A, B, C and D

| # | Result | Why | Sent to the LLM? |
|---|---|---|---|
| **A** | **Unchanged** | Text is identical in both years, so the hashes match. | No |
| **B** | **Modified** | The first sentence is the same, so similarity is high and the paragraphs are paired. The second sentence changed. | Yes: old text, new text and word diff |
| **C** | **Removed** | In 2024 only. Nothing in 2025 is similar enough to pair with it. | Yes: old text only |
| **D** | **Added** | In 2025 only. Nothing in 2024 is similar enough to pair with it. | Yes: new text only |

### A: unchanged
Skipped. Most paragraphs end up here, which is why the LLM bill stays small.

### B: modified
The word diff shows:

- Removed words: `A trial is scheduled for 2025.`
- Added words: `The court ruled against the Company and awarded $50 million.`

This is material: a lawsuit outcome with a dollar amount. Expected label: `legal_regulatory`, high confidence. The figure $50 million is also extracted and checked against the source text.

### C: removed
The company stopped disclosing the supplier dispute. That may mean it was resolved, or it may mean the company stopped disclosing it. The filing does not say which.

The LLM sees only the old text, with a note that nothing replaced it. It can say what the paragraph was about (`legal_regulatory`), but the materiality is often unknowable from the filing alone. A response of `uncertain` with a reason is a valid, expected answer here. This is the case the explicit uncertainty handling is for.

### D: added
A new disclosure appears: an EU investigation. This is likely the most material change in the section. Expected label: `legal_regulatory`, high confidence.

---

## What the LLM is told for each kind

- **Added:** "This paragraph is new in the 2025 filing. There was no equivalent in 2024."
- **Removed:** "This paragraph was in the 2024 filing and has no equivalent in 2025."
- **Modified:** old text, new text and the word diff.

Each call also gets the section name (here, Legal Proceedings) and nothing else about the company.

---

## Output of the diff step

One record per changed paragraph:

| Field | B | C | D |
|---|---|---|---|
| kind | modified | removed | added |
| old paragraph ID | `…:10-K/3:0001:…` | `…:10-K/3:0002:…` | none |
| new paragraph ID | `…:10-K/3:0001:…` | none | `…:10-K/3:0002:…` |
| similarity | about 0.72 | none | none |
| word diff | saved | none | none |

(The real IDs have the form `<accession>:<section key>:<ordinal>:<hash>`.)

---

## A caveat: moved paragraphs

A paragraph that moved to a different section is not really added or removed. If the diff only compares within aligned sections, it would report the same text as removed in one place and added in another.

To avoid this, before calling anything added or removed, the diff compares the leftovers against the whole document. A match across sections is recorded as **moved**, not as two separate changes.

---

## How we will check it

If the diff misses a real change, no later step can recover it. If it flags too much noise, we pay for LLM calls on nothing. So before step 6, the diff output gets checked by hand against Apple's real changes in the two filings. One known candidate to look for: the Digital Markets Act investigation text in Item 3.
