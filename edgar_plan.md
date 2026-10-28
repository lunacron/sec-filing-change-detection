# EDGAR Material-Change Pipeline: Step-by-Step Plan

Goal: ingest SEC filings, detect material changes between successive filings of the same company, classify each changed paragraph with an LLM, extract figures with grounding checks, and gate prompt changes behind a regression eval.

Guiding principles:
- **Deterministic first, LLM last.** Parsing, alignment, and diffing are plain code. The LLM only sees paragraphs the diff has already flagged as changed.
- **Every artifact is traceable.** Each output carries `paragraph_id`, `filing_accession`, `run_id`, `created_at`, and the prompt/model version that produced it.
- **Each stage writes to disk and is re-runnable.** Stages communicate through versioned files or tables, so you can debug one stage without re-running the others.

---

## Phase 0: Decisions and scaffolding (day 1-2)

1. **Pin the scope for v1.** Start with 10-K and 10-K/A only, for 5-10 companies across different sectors, 3-5 years each. Add 10-Q and 8-K only after v1 works.
2. **Define the six change types now.** The plan assumes a taxonomy like the one below. Replace it with yours, but fix it before building eval data:
   1. `financial_results` (numbers, guidance, metrics)
   2. `risk_factor` (new, removed, or reworded risks)
   3. `legal_regulatory` (litigation, investigations, compliance)
   4. `business_operations` (strategy, products, segments, M&A)
   5. `governance_management` (officers, board, controls, auditors)
   6. `accounting_policy` (policies, estimates, restatements)
   - Plus two non-type outcomes: `non_material` (boilerplate, formatting, date roll-forwards) and `uncertain`. These are not among the six, but the pipeline needs them.
3. **Define "material".** Write a one-page rubric with examples of material vs. non-material changes (e.g. "year rolled from 2024 to 2025" is non-material; "new SEC investigation disclosed" is material). This rubric becomes the spine of the prompt and the eval labels.
4. **Repo layout:**
   ```
   edgar/
     pyproject.toml
     src/edgar_diff/
       ingest/      # EDGAR client, cache
       parse/       # HTML/iXBRL -> section tree
       align/       # section alignment
       diff/        # paragraph diff
       llm/         # classification + extraction + prompts/
       ground/      # figure grounding checks
       store/       # schemas, DB access, provenance
       cli.py
     prompts/       # versioned prompt files (v001.md, ...)
     evals/         # cases, runner, thresholds
     data/          # raw/, parsed/, diffs/, outputs/ (gitignored)
     tests/
   ```
5. **Tooling:** Python 3.12, `uv` or `poetry`, `pydantic` v2 for all schemas, `pytest`, `ruff`, `mypy`, `httpx`, `lxml` + `selectolax`/`BeautifulSoup`, `rapidfuzz`, `scikit-learn` (TF-IDF), `scipy` (Hungarian matching), SQLite (or DuckDB) for storage, the Anthropic SDK for the LLM steps. Initialize git and add a pre-commit hook.

---

## Phase 1: Ingestion (day 3-5)

1. **EDGAR client.**
   - Send a descriptive `User-Agent` with a contact email (SEC requires this).
   - Rate limit to <= 10 requests/sec (aim for ~5).
   - Retry with backoff on 429/5xx.
2. **Discover filings.** Use `https://data.sec.gov/submissions/CIK##########.json`. Filter to form types `10-K`, `10-K405`, `10-K/A` and record `accessionNumber`, `filingDate`, `reportDate`, `primaryDocument`, and the amendment relationship.
3. **Download.** Fetch the primary document from `https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/<primaryDocument>`. Also fetch the filing index to pick up exhibits and XBRL files.
4. **Raw cache.** Store bytes untouched at `data/raw/<cik>/<accession>/...` with a sidecar JSON (URL, fetch time, SHA-256). Never mutate raw files.
5. **Filing registry table:** `filings(accession, cik, form, filing_date, period_of_report, amends_accession NULL, raw_path, sha256, fetched_at)`.
6. **Build the comparison pairs.** For each company, sort by `period_of_report`. Pair each filing with its predecessor of the same base form type (10-K to prior-year 10-K).
   - A 10-K/A pairs against the original 10-K it amends *and* is overlaid onto it (see Phase 3, step 6).
   - Handle gaps (missing years) and 52/53-week fiscal-year drift.
7. **Tests:** mocked HTTP for the client; a fixture of 3-4 real filings checked in (small ones) for downstream tests.

---

## Phase 2: Parsing into a canonical section tree (week 2)

1. **Normalize the HTML first.**
   - Decode with the declared charset, normalize unicode (NFKC, but keep a mapping for non-breaking spaces and smart quotes).
   - Strip `<script>`/`<style>`, hidden elements (`display:none`, which iXBRL uses for header data), and page headers/footers/page numbers.
2. **Handle Inline XBRL.**
   - Unwrap `ix:nonNumeric`/`ix:nonFraction` tags so text flows normally, but **record** each tagged fact (concept, context, unit, scale, sign, value) as a side table keyed to the paragraph that contains it.
   - These facts feed the grounding check in Phase 6.
3. **Block extraction.** Walk the DOM into ordered blocks: `heading`, `paragraph`, `list_item`, `table`. Filings use `<div>`/`<font>`/`<span>` soup, so detect blocks by block-level elements and `<br>` runs, not by `<p>` alone.
   - Tables: keep as a structured grid, plus a flattened text rendering ("row label | col header: value") so they can be diffed and shown to the LLM.
4. **Heading detection** (the hard part). Combine signals, score them, and don't rely on any one:
   - Regex for 10-K structure: `^(PART\s+[IV]+)`, `^Item\s+\d+[A-C]?\.` (case-insensitive, tolerate nbsp and a trailing period/dash).
   - Style: bold-only blocks, larger font, all-caps, short length, centered.
   - Table-of-contents links: the TOC's internal `href="#..."` anchors give the *true* heading positions. Use them to validate or override style heuristics.
   - Drop the TOC itself from the tree (it duplicates heading text).
5. **Canonical section tree.**
   ```
   Document
     Part I
       Item 1 Business            (canonical_key = "10-K/1")
         [subheadings...]
       Item 1A Risk Factors       (canonical_key = "10-K/1A")
     ...
   ```
   - Top-level nodes get **canonical keys** from the Item number (stable across years and independent of page layout).
   - Sub-sections below the Item level get only their heading text plus a normalized slug, because their titles are free-form.
6. **Paragraph IDs.** Make them stable and unique:
   `paragraph_id = <accession>:<canonical_section_path>:<ordinal>:<hash8(normalized_text)>`
   - Store `raw_text`, `normalized_text` (lowercased, whitespace-collapsed, quotes/dashes normalized), `char_offsets` into the cleaned HTML, and `html_xpath` for UI click-through.
7. **Output:** `data/parsed/<accession>.json` (validated by pydantic) plus rows in `sections` and `paragraphs` tables.
8. **Validation checks** (fail loudly): every 10-K has Items 1, 1A, 7, 8; paragraph count is within 3x of the company's other years; no empty sections; text coverage (parsed chars / visible chars in source) > 95%. Log these per filing as a quality report.
9. **Fallback.** For old or malformed filings with no usable structure, produce a flat tree with a `parse_confidence: low` flag rather than silently guessing.

---

## Phase 3: Section alignment (week 3)

Aligning old-to-new sections must survive **renumbering** (Item 1A to 1B, new sub-sections shifting numbers), **reordering**, and **amendments**.

1. **Tier 1: canonical-key match.** Pair top-level sections with equal canonical keys (`10-K/1A` to `10-K/1A`). This resolves most of the tree.
2. **Tier 2: content-based match for the rest** (sub-sections, and any top-level sections whose keys differ).
   - Build a similarity matrix between unmatched old and new sections: weighted blend of (a) title similarity (`rapidfuzz` token-set ratio on normalized titles with leading numbers stripped), (b) TF-IDF cosine of section body, and (c) a small positional prior (relative order in parent).
   - Solve with the Hungarian algorithm (`scipy.optimize.linear_sum_assignment`).
   - Accept pairs above a threshold. The remainder are `added` or `removed` sections.
3. **Strip numbering before comparing titles.** "3.2 Liquidity" and "4.1 Liquidity" should match. Treat numbering changes as metadata, not content.
4. **Detect splits and merges.** If one old section matches two or more new sections (or the reverse) with combined coverage, record a 1:N or N:1 alignment instead of forcing 1:1.
5. **Output table:** `section_alignments(old_section_id, new_section_id, kind: matched|moved|split|merged|added|removed, score, method)`.
6. **Amended filings (10-K/A).**
   - A 10-K/A is usually either *partial* (only Part III, or only specific Items or exhibits) or a *full restatement* of the document.
   - Detect type from the explanatory note and from which Items are present.
   - For a partial amendment, build an **effective document**: the original 10-K with the amended Items replaced by the amendment's versions. Diff the effective document against the prior-year filing, and *also* diff amendment against original to surface "what the amendment changed".
   - Record `amends_accession` and tag every resulting change with `source = original | amendment`.
7. **Tests:** synthetic trees where you programmatically renumber, reorder, split, and merge sections and assert the aligner recovers the ground-truth mapping. Compute alignment accuracy on a hand-labeled real pair.

---

## Phase 4: Paragraph-level diffing (week 3-4)

1. **Within each aligned section pair**, match paragraphs old-to-new:
   1. **Exact match** on `normalized_text` hash: `unchanged`.
   2. **Candidate generation** for the rest: character-shingle MinHash/LSH or TF-IDF nearest neighbors (limit to the aligned section first; then widen to the whole document to catch paragraphs moved across sections).
   3. **Optimal assignment** via Hungarian on similarity (e.g. `rapidfuzz` ratio or token-level Jaccard). Thresholds: >= 0.95 near-identical (`unchanged_cosmetic`), 0.60-0.95 `modified`, below that treated as unmatched.
   4. Unmatched old paragraphs are `removed`; unmatched new paragraphs are `added`.
   5. A matched pair in a different section or order is `moved` (with `modified` too if text differs).
2. **Normalize before comparing** to suppress noise: fiscal-year/date roll-forwards (replace dates with `<DATE>` for similarity only, but keep the originals), whitespace and punctuation, smart quotes, footnote markers, defined-term capitalization.
3. **Within modified pairs, compute a word-level diff** (`difflib` or `diff-match-patch`) and store the spans. Also compute a **numeric delta**: extract numbers from both versions and flag which changed.
4. **Cheap pre-filter for materiality.** Before spending LLM calls, mark changes that are clearly cosmetic (only dates, only punctuation, only numbering) as `cosmetic` and skip the LLM. Log how many get skipped so you can audit it.
5. **Table diffing.** Compare tables cell-by-cell after aligning row labels and column headers; emit one change record per changed row, not per table.
6. **Output table:** `paragraph_changes(change_id, old_paragraph_id, new_paragraph_id, kind: added|removed|modified|moved, similarity, word_diff_json, numeric_delta_json, cosmetic bool)`.
7. **Tests:** property tests (shuffle paragraphs, expect only `moved`), golden diffs on fixtures, and a measurement of recall on a small hand-built set of known real changes.

---

## Phase 5: Schema-constrained LLM classification (week 4-5)

1. **Output schema (pydantic, exported as JSON Schema):**
   ```python
   class Classification(BaseModel):
       change_id: str
       label: Literal["financial_results","risk_factor","legal_regulatory",
                      "business_operations","governance_management",
                      "accounting_policy","non_material","uncertain"]
       secondary_label: Optional[<same Literal>]
       is_material: bool
       confidence: Literal["high","medium","low"]   # or 0-1 float, see step 5
       rationale: str            # <= 2 sentences, must cite words from the text
       evidence_quotes: list[str]  # verbatim substrings of old/new text
       uncertainty_reason: Optional[str]   # required when label == "uncertain"
   ```
2. **Force the schema.** Use the Anthropic SDK's tool use with a single tool whose `input_schema` is the JSON Schema and `tool_choice` forcing that tool (check the current docs for structured-output support, via the `claude-api` skill, before implementing). Validate the returned object with pydantic; on validation failure, retry once with the error message, then record `label="uncertain"` with `uncertainty_reason="schema_failure"`. Never fix outputs by hand.
3. **Prompt design.** Keep prompts in `prompts/` as versioned files (`classify_v001.md`) with the rubric from Phase 0, definitions for all labels, 2-3 examples per label (including hard negatives), and an explicit instruction to prefer `uncertain` over guessing.
   - Input to the model: section title/path, old text, new text, word-diff highlights, change kind (added/removed/modified), and nothing else. Include no filing-level metadata that could bias it (company name, stock moves).
4. **Explicit uncertainty handling.**
   - The model may answer `uncertain` with a reason. That is a valid, expected output, not an error.
   - Calibrate `confidence` against eval accuracy (Phase 7): measure accuracy per confidence bucket. If "high" isn't meaningfully more accurate than "low", fix the prompt or drop the field.
   - Route policy: `uncertain` or `low` goes to a human review queue; optionally re-run those with a stronger model or a second sample, and flag disagreement between samples as uncertain.
   - Do not average away uncertainty. Report `uncertain` counts as a first-class metric.
5. **Operational details.** Temperature 0; pin the exact model ID in config (not an alias) and record it per call; batch changes by section for caching and throughput; cache results keyed by `(change_id, prompt_hash, model_id)` so re-runs of unchanged inputs are free; log token usage and cost.
6. **Provenance record per output** (table `classifications`):
   `change_id, paragraph_ids (old/new), accession(s), label..., prompt_version, prompt_sha256, model_id, run_id, created_at (UTC ISO-8601), input_hash, raw_response`.

---

## Phase 6: Grounded figure extraction (week 5)

1. **Extraction schema.** For each changed paragraph (new and old text), the LLM returns:
   ```python
   class Figure(BaseModel):
       value_as_written: str     # e.g. "$1.2 billion"
       normalized_value: float   # 1.2e9
       unit: str                 # USD, %, shares, ...
       metric: str               # "total revenue", "litigation reserve"
       period: Optional[str]
       source_quote: str         # verbatim sentence/span containing the figure
       side: Literal["old","new"]
   ```
2. **Deterministic grounding check** (no LLM involved). For each extracted figure:
   - **Quote check:** `source_quote` must be a substring of the source paragraph after whitespace/quote normalization (fuzzy ratio >= 0.97 as a fallback).
   - **Number check:** parse all numbers in the source paragraph with a robust parser covering `$`, commas, parentheses for negatives, `%`, "million/billion/thousand", "1.2bn", words ("three million"), and ranges. Normalize to canonical values. The figure is *grounded* only if its `normalized_value` equals some parsed source number, within rounding tolerance and after applying scale.
   - **XBRL cross-check (bonus):** if the paragraph has tagged iXBRL facts, confirm the value matches the tagged fact, scale, and sign.
3. **Flag, don't drop.** Ungrounded figures are kept with `grounded=false` and a `reason` (`not_in_source`, `quote_mismatch`, `scale_mismatch`, `derived_value`). Derived values (e.g. "revenue grew 12%" computed by the model) get their own flag, since they're legitimate but not literally in the source.
4. **Also check the classifier.** `evidence_quotes` from Phase 5 go through the same substring check; a failed quote lowers the record to `uncertain` or sets `evidence_grounded=false`.
5. **Metric:** ungrounded-figure rate per prompt version. This is a top-level quality metric and an eval gate (Phase 7).
6. **Provenance:** every figure row carries `paragraph_id`, `char_span` in the source, `run_id`, `created_at`, `prompt_version`, `model_id`.

---

## Phase 7: Evaluation regression set and gating (week 5-7)

1. **Build the gold set.**
   - Sample ~200-300 changed paragraphs from the Phase 4 output, stratified over the six labels, `non_material`, added/removed/modified, companies, and years. Deliberately oversample hard cases: boilerplate rewording, risk-factor reordering, number-only changes, 10-K/A changes, and paragraphs that straddle two labels.
   - Label with a written guideline from the Phase 0 rubric. Have two labelers (or you, twice, a week apart) on a ~20% overlap to measure agreement (Cohen's kappa) and fix ambiguous label definitions. Add genuine `uncertain` gold cases.
   - Store as `evals/cases/*.jsonl`, each case with: the input (old/new text, section path), expected label, acceptable alternates, expected figures (with spans), and tags (e.g. `hard`, `amendment`, `numeric`).
   - Split into `dev` (used while iterating on prompts) and `holdout` (touched only by the gate, and only occasionally) to avoid overfitting prompts to the eval.
2. **Separate suites per LLM step:**
   - **Classification:** accuracy and macro-F1, per-label recall (material labels matter more than `non_material`), confusion matrix, `uncertain` rate, accuracy per confidence bucket.
   - **Extraction:** figure precision/recall against gold figures, ungrounded-figure rate, and **hallucination test cases** (paragraphs with no numbers, where the correct output is an empty list; and cases with near-miss numbers, like 1.2 vs 12).
   - **Adversarial/robustness cases:** prompt-injection text inside a filing paragraph ("ignore previous instructions..."), extremely long paragraphs, tables, empty or garbled text.
3. **Runner.** `python -m evals.run --suite classify --prompt prompts/classify_v002.md` that:
   - runs all cases at temperature 0 (optionally N=3 samples to measure flakiness);
   - writes a results JSON (per-case outputs, metrics, prompt hash, model ID, timestamp);
   - compares to the stored **baseline** (`evals/baseline.json`, the last approved run) and prints a diff of which cases flipped pass-to-fail and fail-to-pass.
4. **Thresholds (set from measured baseline, then ratchet).** Example starting gates, to be calibrated after the first real run:
   | Metric | Gate |
   |---|---|
   | Macro-F1 | >= baseline - 0.02 and >= absolute floor (e.g. 0.80) |
   | Recall on each material label | >= baseline - 0.03 and >= floor (e.g. 0.75) |
   | Ungrounded-figure rate | <= baseline + 0.005 and <= ceiling (e.g. 2%) |
   | Hallucination cases (should-be-empty) | 100% pass |
   | Schema-valid output rate | >= 99.5% |
   | "High" confidence bucket accuracy | >= floor (e.g. 0.90) |
   | Previously-passing "critical" cases | 0 regressions allowed (tag ~30 must-never-break cases) |
   - Use bootstrap confidence intervals, or at least a minimum case count per label, so small-sample noise doesn't cause false blocks or false passes.
5. **Block regressions automatically.**
   - Exit code non-zero when any gate fails.
   - CI (GitHub Actions or similar) triggers on changes to `prompts/**`, the schema, the model ID in config, or the LLM module. It runs the dev suite on every PR, and the holdout suite before merge or release.
   - Add a pre-commit or Makefile target (`make eval`) so you can run it locally.
   - Updating the baseline requires an explicit, reviewed commit (`evals/baseline.json` changed deliberately), never an automatic overwrite.
6. **Eval-driven prompt workflow.** Every prompt change: bump the version file, run the dev suite, inspect flipped cases, then run the gate. Record the outcome in `evals/CHANGELOG.md` (what changed, which metrics moved).
7. **Cost control.** Cache by `(case_id, prompt_hash, model_id)`, run the cheap deterministic checks (grounding, schema) without any LLM calls, and use the full suite only on prompt/model changes.
8. **Grow the set from production.** Every human-review correction and every bug becomes a new eval case tagged with its origin.

---

## Phase 8: Orchestration, storage, and reporting (week 7-8)

1. **CLI:** `edgar ingest <cik>`, `edgar parse <accession>`, `edgar diff <old> <new>`, `edgar classify <pair>`, `edgar report <pair>`, `edgar run <cik> --years N`. Each stage is idempotent and skips work when inputs and versions are unchanged (hash-based).
2. **Storage:** SQLite/DuckDB tables for `filings, sections, paragraphs, section_alignments, paragraph_changes, classifications, figures, runs`. Foreign keys make the paragraph-ID trace queryable: any output row joins back to its source paragraphs and accession in one query.
3. **Run metadata:** `runs(run_id, started_at, git_sha, config_json, prompt_versions, model_ids)`.
4. **Report:** per filing pair, a Markdown/HTML report of material changes grouped by label, each with old/new text side by side, highlighted word diff, extracted figures with a grounded/ungrounded badge, confidence, a link to the SEC source URL, and the paragraph ID plus timestamp. Include a separate "needs review" section for `uncertain` and ungrounded items.
5. **Observability:** structured logs, per-stage counts (paragraphs parsed to changes found to LLM-classified to uncertain), cost and latency per run.

---

## Milestones

| Week | Deliverable | Exit criterion |
|---|---|---|
| 1 | Scaffolding + ingestion | Can fetch and cache 10-K/10-K/A for 5 companies x 5 years |
| 2 | Parser | Section tree passes validation on all fetched filings |
| 3 | Alignment | Synthetic-perturbation tests pass; hand-labeled real pair aligned correctly |
| 4 | Paragraph diff | Known real changes recalled; cosmetic noise filtered |
| 5 | LLM classify + extract + grounding | Schema-valid outputs, grounding checks working end to end |
| 6-7 | Gold set + eval runner + CI gate | A deliberately bad prompt is blocked by CI; a good one passes |
| 8 | Orchestration + reports | `edgar run <cik>` produces a full traceable report |

## Key risks and mitigations

- **Heading detection is brittle across filers.** Mitigate with the TOC-anchor signal, per-filing quality reports, and a low-confidence fallback. Budget extra time here; it's usually the biggest time sink.
- **Alignment errors cascade into false "added/removed" noise.** Evaluate alignment separately and allow cross-section paragraph matching in Phase 4.
- **Eval set too small or overfit to prompts.** Keep a holdout split, stratify, and grow it from real corrections.
- **LLM nondeterminism and model drift.** Pin model IDs, use temperature 0, cache, and re-run the baseline whenever the model ID changes. Treat a model upgrade like a prompt change that must pass the gate.
- **Label ambiguity.** Measure inter-labeler agreement early; accept `acceptable alternates` for genuinely ambiguous cases rather than forcing one answer.
- **Prompt injection via filing text.** Delimit filing text clearly, tell the model it is untrusted data, rely on schema-constrained output, and include injection cases in the eval.
- **SEC access limits.** Respect rate limits and the User-Agent policy; cache everything so you never refetch.

## Open questions to settle before Phase 5

1. Is the six-label taxonomy above the one you want? (Provide yours if it differs.)
2. Which model(s) for classification vs. the uncertain-case re-check? (Decide using cost/accuracy measured on the dev set.)
3. Human review: is there a reviewer, or is `uncertain` only reported?
4. Scope beyond 10-K: add 10-Q/8-K after v1?
