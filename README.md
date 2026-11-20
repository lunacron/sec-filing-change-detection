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
| 4. Diff | Find added, removed and modified paragraphs; skip date-only changes and bare headings; attach a closest-paragraph hint to added/removed ones | Done |
| 5. Classify | Schema-constrained LLM classification | Done (prompt v002; 37-change sample run; not yet checked against hand labels) |
| 6. Figures | LLM extracts numbers from changed paragraphs; plain code checks each against the source text | Done (prompt extract_v001; all 94 Apple changes with figures; 10 figures hand-checked) |
| 7. Eval | Golden set of hand labels, `edgar eval` with pass/fail thresholds and a saved baseline | Done (57 labeled changes, Apple only) |

See [how_it_works.md](how_it_works.md) for a plain-language overview, [paragraph_diff_explained.md](paragraph_diff_explained.md) for a worked example of the diff step, and [pipeline_flowchart.md](pipeline_flowchart.md) for flowcharts of a first and second run with real example messages.

## Classification (LLM step)

`uv run edgar classify <old> <new> --sample 40 --model claude-sonnet-5-5` sends each changed paragraph to the model and saves the answer in `data/edgar.db`.

### The two messages sent on every call

1. **System prompt**: the whole file `prompts/classify_v002.md` (the default; v001 is kept for comparison), identical for every call. It is the instruction manual: the task, a warning that filing text is data and not instructions, the eight labels with definitions, what "material" means, how to treat added and removed paragraphs, and the rules for `uncertain`, `confidence` and `evidence_quotes`. It contains no specific paragraph.
2. **User message**: built fresh for each change by `build_user_message` in `src/edgar_diff/llm/classify.py`. It holds the section (e.g. `10-K/9A`), the change kind, `<old_text>`, `<new_text>` and `<word_diff>`. An added paragraph has only the new text, and a removed one only the old text. Added and removed paragraphs also get a `<closest_paragraph_in_other_filing>` tag when the other filing has a similar paragraph (similarity 0.70 or higher). It carries the similarity score, the section, and `already_matched_to_another_paragraph`, so the model can check whether the text was only moved or folded into another paragraph. v002 explains this tag; v001 does not.

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

Check it yourself: `sha256sum prompts/classify_v002.md` prints the same code that is in the database.

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

- With v001, 9 of 40 answers were `uncertain`, all removed paragraphs: the model could not tell a real drop from a move or a rewrite elsewhere, and several were bare headings. Both planned fixes are now in. Heading-like blocks are skipped before the LLM (16 on Apple), and added/removed paragraphs carry the closest-paragraph hint (26 on Apple).
- On the same 37 changes, v002 gave 0 `uncertain` against 7 for v001. That could be better judgment or overconfidence: several removed paragraphs with no hint now get confident labels. Only hand labels can tell which, which is what the eval step is for.
- `--from-run RUN_ID` classifies exactly the changes of an earlier run, so two prompts can be compared on the same sample.
- Current models reject `temperature`, so runs cannot be pinned to temperature 0. Repeatability comes from the result cache and from the eval measuring run-to-run variation.
- Refusals, truncated replies and schema failures are saved as `uncertain` with that reason. The optional server-side refusal fallback is not enabled.

## Figure extraction (LLM step plus a code check)

This step turns a changed paragraph into a list of numbers. For "commercial paper was $10.0 billion" (2024) and "$8.0 billion" (2025) it records old = $10.0 billion, new = $8.0 billion, metric = commercial paper outstanding.

1. **Which changes:** only changes that go to the LLM and contain a dollar amount, a percentage, a scaled number ("5 million") or a number with a thousands comma. Years and dates alone do not count. On Apple that is 94 of the 273 changes.
2. **The LLM** (prompt `prompts/extract_v001.md`) returns, for each figure: `value_as_written`, `normalized_value`, `unit`, `metric`, `period`, `source_quote` and `side` (old or new). It applies a scale (million, billion) only when the text states it next to the number.
3. **The code check** (`src/edgar_diff/ground/numbers.py`, no LLM) tests two things for each figure. The `source_quote` must appear in the paragraph on the stated side, and a number with the same value must appear in that paragraph. A figure that fails is kept, with `grounded = 0` and a reason:

| Reason | Meaning |
|---|---|
| `quote_mismatch` | the quote is not in the paragraph |
| `not_in_source` | no number in the paragraph has that value |
| `scale_mismatch` | the digits are there, but the model applied a scale the paragraph does not state |

Tables are the usual case for `scale_mismatch`: a cell "12,430" in a table headed "in millions" stays 12,430, as the prompt says.

### Results on Apple (2024 vs 2025)

| Item | Value |
|---|---|
| Changes processed | 94 |
| Figures extracted | 1,315 |
| Failed the code check | 0 |
| Model failures (refusal, cut off, bad format) | 0 |
| Cost on claude-sonnet-5-5 | about $1.64 for all 94 (the dry-run estimate for 10 was $0.09; the real cost was $0.135) |

### What the check cannot catch

- The model attaching a real number to the wrong description. Only a person can see that. 10 figures were checked by hand ([eval/figure_check_sheet.md](eval/figure_check_sheet.md)) and all were right.
- A wrong unit label. In a table "in millions" the unit may say `USD` for a value that is really millions of dollars.
- A lost minus sign. A value in parentheses is compared as a positive number.
- Numbers in words are found only up to nineteen, the tens and "hundred" ("five-year", "three million", "twenty-five").

### Running it

```bash
# Cost estimate first. No API call, free.
uv run edgar extract 0000320193-24-000123 0000320193-25-000079 --sample 10 --dry-run

# 10 changes (about $0.14), then all of them (--sample 0, about $1.60).
uv run edgar extract 0000320193-24-000123 0000320193-25-000079 --sample 10
uv run edgar extract 0000320193-24-000123 0000320193-25-000079 --sample 0
```

Results are cached by change, prompt hash and model, so repeating a run costs nothing, including for changes that had no figures. The command prints how many figures failed the code check and lists them.

### What is in the database

Two tables in `data/edgar.db`:

| Table | Columns |
|---|---|
| `extractions` (one row per change) | `change_id`, `old_paragraph_id`, `new_paragraph_id`, `n_figures`, `failure`, `prompt_version`, `prompt_sha256`, `model_id`, `run_id`, `created_at`, `input_hash`, `raw_response`, `input_tokens`, `output_tokens` |
| `figures` (one row per figure) | `change_id`, `idx`, `side`, `paragraph_id`, `value_as_written`, `normalized_value`, `unit`, `metric`, `period`, `source_quote`, `char_start`, `char_end`, `grounded`, `reason`, plus the same provenance columns |

## Cost, latency and batch mode

Every LLM call (classify and extract) saves what it used, so real cost can be added up and the next run can be estimated from it.

| Saved per call | Column | Notes |
|---|---|---|
| Tokens used | `input_tokens`, `output_tokens` | from the API response |
| How it ran | `mode` | `standard` or `batch` |
| Time taken | `duration_ms` | normal calls only; empty for batch calls and for calls saved before this tracking began |
| Size of the input | `input_chars` | system prompt plus message; the estimator uses it |
| Cached tokens | `cache_read_tokens`, `cache_write_tokens` | prompt-cache use, priced separately (see below); `input_tokens` counts only the uncached part |
| Which batch | `batch_id` | batch calls only |

### See what you spent

```bash
uv run edgar cost
```

It prints calls, tokens, dollars and median / 95th-percentile latency for each step, prompt, model and mode. Batch calls are priced at 50%. Prices are typed into `src/edgar_diff/cost.py` (`PRICES`), so update them when Anthropic changes them.

### Estimate before you run

`--dry-run` on `edgar classify` and `edgar extract` prints the cost both ways, normal and batch, plus the expected time:

```bash
uv run edgar classify OLD NEW --sample 0 --model claude-sonnet-5-5 --dry-run
```

The estimate learns from earlier calls of the same step. It counts cached prompt tokens too, and when earlier calls used the cache it assumes one cache write for the run and reads for the rest. Methods, best first:

1. tokens per character, once at least 10 earlier calls saved their input size;
2. the average tokens per call from all earlier calls (less exact);
3. a rough guess (4 characters per token), only when there is no history.

It says which method it used. Changes that are already saved are not counted, because they cost nothing. The old estimate was about 2x too low for figure extraction, so treat estimates from method 2 as rough.

### Prompt caching (classify only)

The classify system prompt is identical on every call, so it is marked for Anthropic's prompt caching (`cache_control` on the system block in `classify.request_params`). The first call of a run writes it to the cache; later calls read it.

| Token kind | Price on claude-sonnet-5-5 |
|---|---|
| normal input | $2.00 per million |
| cache write | 1.25x input for the default 5-minute lifetime, 2x for the 1-hour lifetime (used for batch requests) |
| cache read | $0.10 per million (0.05x input) |

- Measured on 6 real calls: 2,124 tokens are cached per call (more than the prompt text alone). Those 6 calls cost $0.019 against $0.038 without caching (50% less, including the write). Once the cache is warm, a call costs about $0.0023 against $0.0064.
- The cache lives 5 minutes and every read refreshes it, so a normal run, with calls seconds apart, keeps it warm. In a batch the requests run concurrently, so hits are best-effort and the estimate for batch assumes they hit.
- `prompt_sha256` is unchanged, so earlier saved results stay valid: caching changes the price, not the answers.
- `extract_v001` is not cached. It is about 500 tokens, at the edge of the 512-token minimum, and only about 6% of a call's cost.
- Check it worked: `edgar cost` shows `cache rd` and `cache wr` columns. If `cache rd` stays at 0 across a run, the prefix is not being cached.
- The prices of cache reads are in `PRICES` in `src/edgar_diff/cost.py` (the Haiku value is an unchecked 0.1x).

### Batch mode: half price, but not immediate

Add `--batch` to `classify` or `extract`. All changes go to Anthropic in one batch and the command returns at once. Batches usually finish within an hour and always within 24 hours.

```bash
uv run edgar classify OLD NEW --sample 0 --model claude-sonnet-5-5 --batch   # submit
uv run edgar batch-status                                                   # list batches
uv run edgar batch-collect                                                  # later: save finished batches
```

- `batch-collect` saves the results into the same tables as normal calls (same checks, `mode = 'batch'`). Without an ID it checks every open batch.
- Changes already saved, or already waiting in an open batch, are not sent again.
- A change that errored or expired is not saved, so the next run sends it again.
- Batch latency is the time from creation to the end of the batch, kept in the `batches` table and used by the estimator.
- Use normal calls when you need an answer now (for example while working on a prompt). Use batch for large, non-urgent runs.

## Evaluating a prompt

The golden set is 57 changes whose correct labels were written by hand: `eval/gold_labels.json` (37, the practice set) and `eval/gold_labels_holdout.json` (20, never used to tune a prompt). See [golden_set.md](golden_set.md) for how they were made.

```bash
uv run edgar eval <old accession> <new accession> --dry-run   # what would run, and what it costs
uv run edgar eval <old accession> <new accession>             # run and score; exit code 1 if it fails
uv run edgar eval <old accession> <new accession> --prompt classify_v003 --save-baseline
```

It sends only the golden-set changes that have no saved answer for this prompt and model, so a repeat run costs $0. `--model` defaults to `claude-sonnet-5-5`.

**What it prints:** the exact-label score on the practice set (blind labels only) and on the holdout set, the holdout score counting your second label, how often `is_material` agrees, and the number of `uncertain` answers.

**Pass or fail.** The run fails if any minimum in `eval/thresholds.json` is not met, or if more than `max_new_failures` changes that the saved baseline got right are now wrong. The current minimums are set just below v002's scores and are a starting point, not a standard:

| Setting | Value |
|---|---|
| `practice_blind_strict_min` | 85% |
| `holdout_strict_min` | 60% |
| `holdout_lenient_min` | 75% |
| `holdout_material_min` | 45% |
| `max_new_failures` | 2 |

**The baseline** (`eval/baseline.json`) records which changes a passing prompt got right. `--save-baseline` writes it only when the run passes. It is the ratchet: later prompts may not break what an earlier one got right. Currently it holds v002.

**It stops instead of scoring** when a gold `change_id` is not in the current `changes.json`, or no longer goes to the LLM. That happens if the filings or the diff rules change, and scoring the rest would be misleading.

### Testing the eval runner

None of these cost money.

```bash
# 1. Unit tests (no internet, no API key). Expect "60 passed".
uv run pytest -q
uv run pytest tests/test_eval.py -q          # only the eval runner's tests

# 2. Score the real golden set. Every answer is already saved for v002,
#    so this makes no API calls.
uv run edgar eval <old accession> <new accession> --dry-run   # "57 already classified, 0 to run"
uv run edgar eval <old accession> <new accession>             # scores, then PASS
echo $?                                                       # 0 = pass, 1 = fail
```

**See it fail on purpose.** A gate you have only seen pass is not proven.
1. In `eval/thresholds.json`, change `"holdout_strict_min": 0.60` to `0.90`.
2. Run `uv run edgar eval ...` again. Expect `FAIL`, a line saying the holdout exact-label score is below the minimum, and `echo $?` printing `1`.
3. Change it back to `0.60`.

**See it refuse a wrong input.** Run it with a second accession number that has no diff (for example `0000320193-25-000000`). It stops with "... not found. Run `edgar diff <old> <new>` first" and scores nothing.

A new prompt (`--prompt classify_v003`) runs the model on all 57 changes, about $0.50. Use `--dry-run` first to see the estimate.

Limits to keep in mind:
- Models don't accept `temperature`, so the same prompt can score slightly differently on a fresh run. That is why a small number of new failures is allowed.
- The practice set was used to write v002, so its score is optimistic. Use the holdout score. When a prompt is tuned using the holdout set's mistakes, draw and label a new holdout set.
- Seven practice labels were changed after seeing the model's answer. They count in `practice_all` but not in the blind score the thresholds use.
- Apple only, one year pair.

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

Expected: `60 passed`. The tests need no internet, no API key and cost nothing: they use small made-up filings and a fake API client, never the SEC or Anthropic.

| Test file | What it checks |
|---|---|
| `tests/test_client.py` | the EDGAR client keeps only 10-K, 10-K405 and 10-K/A filings |
| `tests/test_parse.py` | Item sections and paragraphs are found; the hidden XBRL header and page footers are dropped; paragraph IDs are stable; small tables survive |
| `tests/test_align.py` | sections are matched, added or removed by Item key |
| `tests/test_diff.py` | unchanged / modified / removed / added (the A, B, C, D example), date-only changes skipped, a one-digit figure change is not skipped, a paragraph moved between sections is a move, years followed by a comma or period are masked, heading-like blocks are skipped, the closest-paragraph hint is attached (and a short paragraph inside a long one is not a false hint) |
| `tests/test_classify.py` | the output schema is strict, the sample is reproducible and always includes Item 3, the user message is built correctly (including the hint tag), evidence quotes are checked against the source, results are stored with provenance and cached, refusals and bad JSON become `uncertain` |
| `tests/test_eval.py` | gold labels load (blind or not), exact / lenient / materiality scoring, thresholds fail below the minimum, a change the baseline got right and is now wrong counts as a regression, a failing run never overwrites the baseline, a gold change missing from `changes.json` stops the run |
| `tests/test_ground.py` | numbers are found with scales, commas, percent and number words; a figure is checked against the paragraph (invented values, wrong scale and near-misses like 1.2 vs 12 are flagged); extraction stores grounded and flagged figures and caches results, including changes with no figures |
| `tests/test_cost.py` | batch is exactly half price, calls save mode, latency and input size, old databases are migrated, the report groups by mode, the estimator uses tokens per character only with enough history and falls back to averages and then a guess, batch time comes from finished batches, cache reads and writes are priced at their own rates and saved with each call, the classify system prompt is marked for caching (1-hour for batch) and extract is not, the estimate assumes one cache write then reads |
| `tests/test_batch.py` | a batch has one request per change with the same settings as a normal call, saved or waiting changes are not sent again, collect saves results as batch calls with the same checks, a running batch is left alone, failed or expired changes are not saved and are sent again, extraction works through batch too |
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
| 7 extract | rows in the `extractions` and `figures` tables of `data/edgar.db` |

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
