# EDGAR Material-Change Pipeline

Automatically spot what changed between a company's consecutive SEC filings, and say which changes matter.

## The problem

- A 10-K runs 100+ pages, and most of it is the same as last year's. The sentences that matter are buried: a new lawsuit, a new risk, a changed accounting policy, a dropped disclosure.
- Analysts and investors read both years side by side to find these. That is slow, and people miss things.
- A plain text diff doesn't work. Companies renumber sections, reorder paragraphs, reword boilerplate and roll dates forward, so a raw diff is mostly noise.

## What the project does

1. **Pulls the filings** from SEC EDGAR.
2. **Breaks them into sections and paragraphs** and lines up this year's against last year's, even when things moved.
3. **Finds the paragraphs that really changed** (added, removed or reworded) and ignores cosmetic edits.
4. **Has an LLM classify each change** into one of six types, such as legal or risk factor, and say whether it is material. When it can't tell, it must say `uncertain` rather than guess.
5. **Extracts the dollar figures** and checks in code that each number is actually in the source text, so invented numbers get flagged.
6. **Traces every output** back to a paragraph ID and timestamp, so any claim can be verified against the filing.

## Evaluation

The LLM steps have a regression test set. When a prompt changes, the set re-runs and blocks the change if the results get worse. That keeps the classifier from quietly degrading as prompts are tweaked.

## The result

For a company, a short, sourced list of what changed between filings and why it might matter, instead of a pile of pages.

## Who it's for

Anyone who reads filings year after year and needs to know what changed.

| Who | What they'd use it for |
|---|---|
| Equity and credit analysts | Spot new risks, legal matters or accounting changes in each covered company without reading every filing side by side. |
| Investors and portfolio managers | Screen many holdings at once for material disclosure changes after each filing season. |
| Auditors and accounting teams | Track changes in accounting policies, estimates and internal-control language. |
| Compliance and legal teams | Watch competitors' or counterparties' disclosures, or compare their own filing against last year's before it is filed. |
| Risk and due-diligence teams | Check a target or vendor for new litigation, investigations or going-concern language. |
| Journalists and researchers | Find stories in quiet line changes, or study disclosure trends. |
| Developers and data teams | Reuse the parser and diff as a base for their own filing tools. |

Compared with the alternatives:
- **Manual comparison** is slow, and people miss things.
- **Plain diff tools** drown in renumbering and date noise.
- **Asking a generic LLM about a filing** can invent numbers and gives no way to check them. This pipeline flags figures missing from the source, says `uncertain` instead of guessing, and ties every claim to a paragraph ID.

It flags and sorts changes. It does not judge investment value. The "material" label is a first pass for a human to review.

## Pipeline

| Step | What | Status |
|---|---|---|
| 1. Ingest | Fetch filings from EDGAR into a raw cache | Done |
| 2. Parse | HTML/iXBRL into Items and paragraphs with stable IDs | Done |
| 3. Align | Pair sections by Item key | Done |
| 4. Diff | Find added, removed and modified paragraphs | Next |
| 5. Classify | Schema-constrained LLM classification | Planned |
| 6. Ground | Check extracted figures against the source text | Planned |
| 7. Eval | Regression set with pass/fail thresholds | Planned |

See [edgar_plan.md](edgar_plan.md) for the full plan and [paragraph_diff_explained.md](paragraph_diff_explained.md) for a worked example of the diff step.

## Usage

The SEC requires a User-Agent that identifies you. Copy `.env.example` to `.env`, set `EDGAR_USER_AGENT`, then load it:

```bash
set -a && . ./.env && set +a
uv sync
uv run edgar ingest 320193 --n 2                    # fetch Apple's two latest 10-Ks
uv run edgar parse 320193 <accession>               # parse a cached filing
uv run edgar align <old_accession> <new_accession>  # align sections
uv run pytest
```

Data is written under `data/` (git-ignored): `raw/`, `parsed/`, `diffs/`.
