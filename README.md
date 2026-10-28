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
| 4. Diff | Find added, removed and modified paragraphs | Done |
| 5. Classify | Schema-constrained LLM classification | Done (first 40-change run; not yet checked against hand labels) |
| 6. Ground | Check extracted figures against the source text | Planned |
| 7. Eval | Regression set with pass/fail thresholds | Planned |

See [edgar_plan.md](edgar_plan.md) for the full plan and [paragraph_diff_explained.md](paragraph_diff_explained.md) for a worked example of the diff step.

## Classification (LLM step)

`uv run edgar classify <old> <new> --sample 40 --model claude-sonnet-5-5` sends each changed paragraph to the model and saves the answer in `data/edgar.db`.

### The two messages sent on every call

1. **System prompt**: the whole file `prompts/classify_v001.md`, identical for every call. It is the instruction manual: the task, a warning that filing text is data and not instructions, the eight labels with definitions, what "material" means, how to treat added and removed paragraphs, and the rules for `uncertain`, `confidence` and `evidence_quotes`. It contains no specific paragraph.
2. **User message**: built fresh for each change by `build_user_message` in `src/edgar_diff/llm/classify.py`. It holds the section (e.g. `10-K/9A`), the change kind, `<old_text>`, `<new_text>` and `<word_diff>`. An added paragraph has only the new text, and a removed one only the old text.

The reply is forced into a fixed JSON shape by `src/edgar_diff/llm/schema.py`: `label`, `secondary_label`, `is_material`, `confidence`, `rationale`, `evidence_quotes`, `uncertainty_reason`. The prompt only explains what the fields mean; the schema itself is sent separately through the API's structured-output setting.

The eight labels are the six topic types (`financial_results`, `risk_factor`, `legal_regulatory`, `business_operations`, `governance_management`, `accounting_policy`) plus `non_material` and `uncertain`.

### What is in the database

One table, `classifications`, one row per classified change.

| Group | Columns |
|---|---|
| Which change | `change_id`, `old_paragraph_id`, `new_paragraph_id` |
| The answer | `label`, `secondary_label`, `is_material`, `confidence` |
| The explanation | `rationale`, `evidence_quotes`, `uncertainty_reason` |
| The check | `evidence_grounded` (1 = every quote was found verbatim in the source text) |
| Provenance | `prompt_version`, `prompt_sha256`, `model_id`, `run_id`, `created_at` (UTC) |
| Audit and cost | `input_hash`, `raw_response`, `input_tokens`, `output_tokens` |

The paragraph text is not in the database. Join on `change_id` to `data/diffs/<old>__<new>.changes.json` to read it.

Open the file with the "SQLite Viewer" VS Code extension (click `data/edgar.db`), or from a terminal:

```bash
sqlite3 -header -column data/edgar.db
```

```sql
SELECT label, COUNT(*) FROM classifications GROUP BY label;
SELECT change_id, label, confidence, rationale FROM classifications WHERE is_material = 1;
SELECT change_id, uncertainty_reason FROM classifications WHERE label = 'uncertain';
```

### Prompt versioning and `prompt_sha256`

`prompt_sha256` is a SHA-256 fingerprint of the system prompt file's text: a 64-character code that is always the same for the same text and completely different after any edit, even one comma. It is computed fresh from the file on every run.

Why it is saved:
- **Traceability.** A row proves which exact wording produced it. The filename (`classify_v001`) is only a label you choose, and an edited file could keep its old name. The hash cannot be fooled that way.
- **Caching.** A result is reused only when `(change_id, prompt_sha256, model_id)` all match. Edit the prompt and the hash changes, so the next run classifies everything again and saves new rows beside the old ones. Nothing is overwritten.
- **The eval gate.** Comparing results from the old hash with the new one is how a prompt change gets checked for regressions.

Check it yourself: `sha256sum prompts/classify_v001.md` prints the same code that is in the database.

What the hash does **not** cover: the user message is built by code and the output schema is a separate file, so a change to either is not captured by `prompt_sha256`. Git history records those for now. When the eval gate is built, all three should count as one prompt version.

### Changing the prompt

Copy, don't edit in place, so v001 stays intact and you can compare:

```bash
cp prompts/classify_v001.md prompts/classify_v002.md   # edit the copy
uv run edgar classify <old> <new> --sample 40 --model claude-sonnet-5-5 --prompt classify_v002
```

Compare the two sets of rows:

```sql
SELECT prompt_sha256, label, COUNT(*) FROM classifications GROUP BY prompt_sha256, label;
```

Don't change the prompt blind. The intended order: hand-label the 40 results (start with the `uncertain` and low-confidence ones), see where the model disagrees, write v002 with examples taken from those errors, run it on the same 40 and compare. A 40-change run on Sonnet 5.5 costs about $0.27.

### Known behavior

- All 9 `uncertain` answers in the first run were removed paragraphs. The model cannot tell whether a removal is a real drop, a move, or a rewrite elsewhere. Several were bare headings. Planned fixes: skip heading-like blocks before the LLM, and show the model the closest paragraph in the other filing as a hint.
- Current models reject `temperature`, so runs cannot be pinned to temperature 0. Repeatability comes from the result cache and from the eval measuring run-to-run variation.
- Refusals, truncated replies and schema failures are saved as `uncertain` with that reason. The optional server-side refusal fallback is not enabled.

## Setup

```bash
cd edgar    # run everything from the project folder
uv sync                              # install dependencies (first time only)
cp .env.example .env                 # then edit .env
```

`.env` holds your settings. It is git-ignored and the program reads it by itself, so there is no need to source it.

| Setting | Needed for | Example |
|---|---|---|
| `EDGAR_USER_AGENT` | downloading from the SEC (the SEC requires a name and contact email) | `EDGAR_USER_AGENT="edgar-research you@example.com"` |
| `ANTHROPIC_API_KEY` | the LLM classification step only | `ANTHROPIC_API_KEY="sk-ant-..."` |

## Testing (unit tests)

```bash
uv run pytest -q
```

Expected: `17 passed`. The tests need no internet, no API key and cost nothing: they use small made-up filings and a fake API client, never the SEC or Anthropic.

| Test file | What it checks |
|---|---|
| `tests/test_client.py` | the EDGAR client keeps only 10-K, 10-K405 and 10-K/A filings |
| `tests/test_parse.py` | Item sections and paragraphs are found; the hidden XBRL header and page footers are dropped; paragraph IDs are stable; small tables survive |
| `tests/test_align.py` | sections are matched, added or removed by Item key |
| `tests/test_diff.py` | unchanged / modified / removed / added (the A, B, C, D example), date-only changes skipped, a one-digit figure change is not skipped, a paragraph moved between sections is a move, years followed by a comma or period are masked |
| `tests/test_classify.py` | the output schema is strict, the sample is reproducible and always includes Item 3, the user message is built correctly, evidence quotes are checked against the source, results are stored with provenance and cached, refusals and bad JSON become `uncertain` |
| `tests/test_env.py` | the `.env` loader accepts `export KEY = value` |

Run one file or one test:

```bash
uv run pytest tests/test_diff.py -q
uv run pytest tests/test_diff.py::test_date_roll_forward_is_cosmetic -q
```

If you change the code, run the tests before running the pipeline.

## Running the pipeline

Two kinds of ID appear in the commands:

- **CIK** (Central Index Key): the SEC's permanent ID for a company. Apple is `320193`.
- **Accession number**: the ID of one specific filing, like `0000320193-25-000079`. Do not read the company out of it.

The example values below are Apple's two latest 10-Ks. Keep them to reproduce the example, or replace them with your own. For another company, find its CIK at https://www.sec.gov/edgar/searchedgar/companysearch, run step 1, and copy the two accession numbers it prints.

The **old** filing always comes first. Swap them and added and removed flip.

```bash
# 1. Download the filings from the SEC (network). Prints each accession number.
uv run edgar ingest 320193 --n 2
#                   ^ <company CIK>

# 2. Parse each filing into sections and paragraphs (local).
uv run edgar parse 320193 0000320193-24-000123      # old filing
uv run edgar parse 320193 0000320193-25-000079      # new filing
#                  ^ <company CIK>  ^ <accession number>

# 3. Align sections by Item key (local; the diff step also does this internally).
uv run edgar align 0000320193-24-000123 0000320193-25-000079
#                  ^ <old accession>    ^ <new accession>

# 4. Diff paragraphs (local). Writes data/diffs/<old>__<new>.changes.json
uv run edgar diff 0000320193-24-000123 0000320193-25-000079
#                 ^ <old accession>    ^ <new accession>

# 5. See the cost estimate first. No API call, free.
uv run edgar classify 0000320193-24-000123 0000320193-25-000079 --sample 40 --dry-run

# 6. Classify with the LLM (network, needs ANTHROPIC_API_KEY). About $0.27 for 40 on Sonnet 5.5.
uv run edgar classify 0000320193-24-000123 0000320193-25-000079 --sample 40 --model claude-sonnet-5-5
#                     ^ <old accession>    ^ <new accession>
```

Notes:
- Only step 1 contacts the SEC and only step 6 contacts Anthropic. Steps 2-5 are local and free, so re-run them as often as you like.
- Downloads are cached, and classifications are cached by change, prompt hash and model, so repeating a step does not repeat the cost.
- `--sample 0` classifies every changed paragraph, not just a sample.
- The pipeline has only been tried on Apple.

What each step leaves behind (all under `data/`, which is git-ignored):

| Step | Output |
|---|---|
| 1 ingest | `data/raw/<CIK>/<accession>/` with the filing `.htm` and `meta.json` |
| 2 parse | `data/parsed/<accession>.json` |
| 3 align | `data/diffs/<old>__<new>.alignment.json` |
| 4 diff | `data/diffs/<old>__<new>.changes.json` |
| 6 classify | rows in `data/edgar.db` |

## Checking the database

After step 6, the model's answers are in `data/edgar.db` (SQLite). The table is `classifications`; the columns are described in the Classification section above.

**In VS Code:** install the "SQLite Viewer" extension, then click `data/edgar.db` in the file explorer and open the `classifications` table.

**In a terminal:**

```bash
sqlite3 -header -column data/edgar.db
```

```sql
SELECT COUNT(*) FROM classifications;                                   -- rows saved
SELECT label, COUNT(*) FROM classifications GROUP BY label;             -- label mix
SELECT change_id, label, confidence, rationale
  FROM classifications WHERE is_material = 1;                           -- material changes
SELECT change_id, uncertainty_reason
  FROM classifications WHERE label = 'uncertain';                       -- what it could not decide
SELECT COUNT(*) FROM classifications WHERE evidence_grounded = 0;       -- quotes not found in the source (should be 0)
```

Leave with `.quit`. Without `sqlite3`: `sudo apt install sqlite3`.

The database stores IDs and answers, not the paragraph text. To read the original paragraphs for a row, find its `change_id` in `data/diffs/<old>__<new>.changes.json`.

Quick sanity check that a run worked: the row count should equal the number of changes you classified (40 for `--sample 40`), `evidence_grounded = 0` should return 0 rows, and `created_at`, `model_id` and `prompt_sha256` should be filled in.
