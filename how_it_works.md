# How the EDGAR change-detection pipeline works

Two parts. **Part A** is for business people: the problem and what we deliver. **Part B** is for technical people: how it is built. Both are written in simple language.

---

# Part A: The business problem (for business people)

## The problem

- Every public company files an annual report with the SEC, called a 10-K. It runs 100 pages or more.
- Most of it is the same as last year. The few sentences that matter are buried in it: a new lawsuit, a new risk, a change in an accounting policy, a disclosure that quietly disappeared.
- Analysts, investors, auditors and compliance teams read this year's and last year's reports side by side to find them.
- That takes hours for one company. People get tired, and they miss things.

## Why a normal comparison tool does not solve it

- A plain "compare two documents" tool shows thousands of differences.
- Most are noise. Dates roll forward, sections are renumbered, paragraphs are reordered, and boilerplate is reworded with the same meaning.
- The reader still has to decide which differences matter.

## What we built

- The program reads both reports and finds the paragraphs that really changed.
- It ignores the noise: date-only edits, headings and text that only moved.
- An AI model then reads each real change, one at a time, and says:
  - what kind of change it is (a risk, a legal matter, a financial result, an accounting policy and so on)
  - whether a reasonable investor would want to know about it
  - why, with exact quotes from the filing
- For changes that contain numbers, the AI lists the figures, such as "$10.0 billion last year, $8.0 billion this year".
- Plain code then checks that every quote and every number is really in the filing.

## What you get

A short, sourced list of what changed, instead of 100 pages. For example, one real entry for Apple (2024 vs 2025):

> **Financial results, material, high confidence.** Commercial paper outstanding fell from $10.0 billion to $8.0 billion.

Each entry points back to the exact paragraph in the filing, so any claim can be verified in seconds.

## Why you can trust it

- **Every claim is checked against the source.** If the AI quotes text or a number that is not in the filing, the program flags it.
- **The AI is allowed to say "I don't know".** It must say `uncertain` rather than guess.
- **It is tested against human answers.** We hand-labeled 57 changes and score the AI against them. If a change to the instructions makes the results worse, the test fails and the change is blocked.
- **Everything is traceable.** Each answer records which instructions and which model produced it, and when.

## Cost and speed

- Finding the changes is free and takes seconds.
- The AI part costs money. We measured about $1.64 to extract the figures from all 94 Apple changes that contain numbers. We estimate about $2 to classify all 273 changes that need the AI.
- Results are saved, so asking the same question again costs nothing.

## Honest limits

- It has been tried on one company, Apple, and one pair of years.
- The AI's judgment of "material" is good but not perfect. When we compared it with our own labels, we agreed on most changes, but not all, and some of those were matters of opinion.
- The test set is small (57 changes), and part of it is no longer a blind test.
- It points a person to what to read. It does not replace the person's judgment.

---

# Part B: How it works (for technical people)

## The design in one paragraph

Plain Python code finds the changes. The LLM is used only for judgment (what kind of change, whether it is material, which numbers it contains). The LLM sees one changed paragraph at a time and must answer in a fixed JSON shape. Every answer is checked in code, cached in SQLite, and traceable by prompt hash and model ID. A hand-labeled set scores each prompt version.

## The stages

| Stage | Command | Input | Output | LLM? |
|---|---|---|---|---|
| Ingest | `edgar ingest` | SEC submissions API | `data/raw/<cik>/<accession>/` | no |
| Parse | `edgar parse` | raw iXBRL HTML | `data/parsed/<accession>.json` (Items and paragraphs with stable IDs) | no |
| Align | `edgar align` | two parsed filings | `data/diffs/...alignment.json` (Item pairs) | no |
| Diff | `edgar diff` | alignment + parsed filings | `data/diffs/...changes.json` | no |
| Classify | `edgar classify` | `changes.json` + prompt | `classifications` table | yes |
| Figures | `edgar extract` | `changes.json` + prompt | `extractions` and `figures` tables | yes, then a code check |
| Eval | `edgar eval` | gold labels + `classifications` | PASS or FAIL | no |

Apple's 2024 vs 2025 result: 288 paragraphs unchanged; 333 changes found; 273 of those go to the LLM; 60 are skipped (43 date-only, 16 headings, 1 unchanged move).

---

## 1. Before any API call (free, no LLM)

1. We download Apple's two 10-K filings from the SEC. They are saved in `data/raw/`.
2. Our code splits each filing into sections (Items) and paragraphs. This is saved in `data/parsed/`.
3. Our code compares the 2024 and 2025 paragraphs.
4. It writes a list of every paragraph that changed: `data/diffs/<old>__<new>.changes.json`.
5. Each item in that list is one change, with a `change_id`, the old text and the new text.
6. 273 of the changes need the LLM. The rest are skipped (date-only edits, headings, unchanged moves).

## 2. One API call for one change

7. An API call is a message we send to Claude over the internet. Claude sends an answer back.
8. We send one call for each change.
9. Each call has two parts.
10. The **system prompt** is a file in `prompts/`, like `classify_v002.md`. It holds the rules and is the same for every call.
11. The **user message** holds the section name, the old text and the new text of this one change.
12. Claude answers with a small piece of JSON: a label, whether the change is material, a confidence level, a reason and some quotes.

## 3. What we check on the answer

13. The answer must have exactly the fields we asked for. Pydantic checks this.
14. If the answer is broken, we save it as `uncertain`.
15. Our code checks that each quote really appears in the filing text.

## 4. What is saved in the database

16. The database is one file, `data/edgar.db`.
17. Each answer becomes one row in the table `classifications`.
18. The row holds the `change_id`, the label, the reason and the quotes.
19. It also holds which prompt file and which model gave the answer.
20. It also holds the full raw reply and how many tokens were used.
21. A row is unique for one change, one prompt and one model.
22. Before a call, we look for that row. If it exists, we skip the call, so running it again costs $0.

## 5. The second kind of call (figures)

23. A change with numbers gets a second call, with a different prompt, `extract_v001.md`.
24. Claude lists the numbers it found.
25. Our code checks each number against the paragraph.
26. The results go into the tables `extractions` and `figures`.

## Where everything lives

| What | Where |
|---|---|
| Downloaded filings | `data/raw/` |
| Parsed paragraphs | `data/parsed/` |
| The list of changes, with the paragraph text | `data/diffs/...changes.json` |
| The prompts (the rules we send) | `prompts/` |
| The model's answers | `data/edgar.db`, table `classifications` |
| Numbers found | `data/edgar.db`, tables `extractions` and `figures` |
| Your correct labels | `eval/gold_labels*.json` |

`data/` is not in git, so it can be rebuilt from the SEC. The `prompts/` and `eval/` folders are in git. The database holds answers only. The paragraph text stays in the `changes.json` file.

---

## 6. Who decides what is in `changes.json`?

Our own code decides everything in it. The SEC does not, and the LLM does not.

1. `changes.json` is written by the diff step, in `src/edgar_diff/diff/paragraphs.py`.
2. It is plain Python code with fixed rules. It has no LLM in it, so the same two filings always give the same file.
3. The shape of each record is the `Change` class in that file. That class lists the fields.

| Field | Who or what sets it |
|---|---|
| `kind` (modified, added, removed, moved) | the diff code, by comparing paragraphs |
| `old_text`, `new_text` | copied from the parsed filings, which came from the SEC's HTML |
| `old_section`, `new_section` | the parse step, from the Item headings in the filing |
| `old_paragraph_id`, `new_paragraph_id` | the parse step (see below) |
| `similarity` | the diff code, a score of how alike the paired paragraphs are |
| `word_diff` | the diff code, the exact words that changed |
| `hint` | the diff code, the closest paragraph in the other filing |
| `needs_llm`, `skip_reason` | the diff code, from the rules `_skip_reason` (`date_only`, `heading`, `moved_unchanged`) |

### How `change_id` is made

4. `change_id` is not chosen by anyone. The code computes it.
5. It takes the old paragraph ID and the new paragraph ID and joins them as `old|new`.
6. It runs a SHA-256 hash on that text and keeps the first 12 characters, with `chg-` in front.
7. The same two paragraphs always give the same `change_id`. This is why saved answers and gold labels can find their change again.

### How a paragraph ID is made

8. The parse step builds it in `src/edgar_diff/parse/html.py`.
9. It looks like `<accession>:<section>:<position>:<8-character hash of the text>`.
10. If the text of a paragraph changes, its ID changes. If a paragraph is moved, its position part changes.

### What the LLM adds

11. The LLM adds nothing to `changes.json`. Its answers go to the database.
12. The only link between the two is the `change_id`.

---

## 7. The logic of `paragraphs.py` (the diff step)

The main function is `diff_filings`. It runs in one pass. There is no LLM in it.

### Step 1: Go through the sections that exist in both years

- The align step already paired the sections, for example Item 1A with Item 1A.
- For each pair, `_match` tries to pair the old paragraphs with the new ones, from strictest to loosest.
- Paragraphs that cannot be paired are set aside.

### Pairing, in three rounds (`_match`)

- **Round 1: exact match.** The text must be the same word for word. Such a pair is unchanged. It is only counted.
- **Round 2: match after hiding dates.**
  - `mask_dates` replaces full dates and bare years with a placeholder.
  - If the paragraphs match after that, only the date changed. The change is marked `cosmetic` and later skipped as `date_only`.
  - Dollar amounts are left alone on purpose, so a one-digit change in a number still counts.
- **Round 3: fuzzy match.**
  - `_pair_fuzzy` scores how alike each old and new paragraph are, from 0 to 1.
  - It picks the best one-to-one pairing overall, so two old paragraphs cannot claim the same new one.
  - A pair counts only if the score is at least 0.60 (`MODIFIED_MIN`).
  - These become `modified` changes, with the exact changed words saved as `word_diff`.

### Step 2: Look at everything left over

- Leftovers from all sections go into one pool.
- `_match` runs again on the whole pool with a stricter bar of 0.85 (`MOVED_MIN`).
- This finds paragraphs that moved to a different section.
- Identical text in a different section is `moved` and needs no LLM. Similar text in a different section is `moved` and does go to the LLM.

### Step 3: What is still left is added or removed

- An old paragraph with no partner is `removed`.
- A new paragraph with no partner is `added`.

### Step 4: Add a hint to added and removed paragraphs

- `_hints` looks in the other filing for the most similar paragraph.
- If the similarity is at least 0.70 (`HINT_MIN`), it is attached as the `hint`. It also says whether that paragraph is already paired with another one.
- For long paragraphs it also checks whether the text was folded into a longer paragraph. A short heading can't count as a hint for a long paragraph.
- The hint helps the LLM tell a real removal from text that was only reworded.

### Step 5: Decide which changes skip the LLM (`_skip_reason`)

- `date_only`: the change was cosmetic, only a date or year.
- `moved_unchanged`: the same text moved to another section.
- `heading`: the old and new text are only short heading-like fragments.
- A skipped change gets `needs_llm = false`. Everything else stays `true`.

### Step 6: Make the change records and the summary

- `_change` builds each record and computes the `change_id`.
- At the end the code adds a `summary` of counts and returns a `DiffResult`.

### The key numbers

| Setting | Value | Meaning |
|---|---|---|
| `MODIFIED_MIN` | 0.60 | how alike two paragraphs in the same section must be to count as modified |
| `MOVED_MIN` | 0.85 | how alike they must be across sections to count as moved |
| `HINT_MIN` | 0.70 | how alike a paragraph must be to be offered as a hint |

If you change one of these numbers, the list of changes can change, and some change IDs change with it. Saved gold labels may then no longer match. That is why the eval stops with an error when a gold `change_id` is missing.

---

## 8. How we keep the LLM honest

- **Small input.** The model sees one changed paragraph, never a whole filing.
- **Code finds the changes.** The model is never asked to find them or to recall anything.
- **Fixed answer shape.** A Pydantic schema, sent as a JSON schema, makes every field required. A broken or cut-off reply becomes `uncertain`.
- **Quotes must exist.** The model must give exact quotes. Code checks they are in the source.
- **Numbers must exist.** For figures, code checks the quote and the number against the paragraph. Failures are flagged, not dropped.
- **"I don't know" is allowed.** `uncertain` has a required reason.
- **Filing text is data.** The prompt says to ignore instructions inside it.
- **Everything is traceable.** Prompt hash, model ID and the raw reply are stored with each answer.

## 9. How we measure it

- A hand-labeled **golden set**: 37 practice changes and 20 holdout changes.
- `edgar eval` scores the saved answers against it and compares to minimums in `eval/thresholds.json`.
- Results so far: v002 matched 30 of 30 blind practice labels (v001: 23 of 30). On the 20 holdout changes it matched 13 of 20 at first. We then changed nine holdout labels after reviewing the model's reasons, so that set is no longer a blind test.
- Figure extraction has no gold set. We only have the ungrounded count (0 of 1,315) and 10 figures checked by hand.

## 10. Known limits

- Apple only, one pair of years.
- Thresholds are provisional guesses set just below the v002 scores.
- `prompt_sha256` covers the prompt file only, not the message-building code or the schema.
- Models do not accept `temperature`, so scores vary a little between runs.
- A failed quote check (`evidence_grounded = 0`) is stored but nothing acts on it yet.

---

## 11. The evaluation (golden set), step by step

### What the golden set is

- It is our answer key: changed paragraphs where a person wrote the correct answer.
- We use it to find out whether the LLM's labels are right, and whether a new prompt is better or worse than the old one.
- There are 57 entries, in two groups: a **practice set** (37) and a **holdout set** (20).

### Where the cases come from

- Every case is one change from `changes.json` (Apple 2024 vs 2025).
- Only changes with `needs_llm = true` can be picked. Date-only edits, headings and unchanged moves never go to the LLM, so there is nothing to grade.
- **Practice set (37):** a reproducible sample (seed 0) spread across sections and kinds, with Item 3 (legal) always included.
- **Holdout set (20):** a second draw (seed 7) that excludes the 37, with a fixed mix of 13 modified, 4 removed and 3 added changes. The picked `change_id`s are saved in `eval/holdout_ids.json`.

### What is in each case

| Part | What it is | Where it is |
|---|---|---|
| **Input** | the old and new paragraph text, the section, the kind of change | `data/diffs/...changes.json`, found by `change_id` |
| **Expected answer** | your correct `label`, plus (holdout only) an optional second label and a material yes/no | `eval/gold_labels.json` and `eval/gold_labels_holdout.json` |
| **Source** | the Apple 2024 and 2025 10-Ks; the `change_id` points to the exact paragraphs | `data/raw/.../meta.json` has the SEC link |
| **Notes on how the label was made** | `status`, and `original_label` if it was changed later | the same gold files |

- The gold files hold only the answer, never the paragraph text. The text stays in `changes.json`.
- The label sheets (`eval/label_sheet.md`, `eval/label_sheet_2.md`) are the pages you filled in by hand. The program reads only the JSON files.

### How the cases were labeled

- We printed each case on a label sheet: old text, new text and word diff, with no model answer.
- You wrote the label (and for the holdout, the second label and material yes/no).
- A label written without seeing the model's answer is called **blind**. It is marked `status: confirmed`.
- If you changed a label after seeing the model's answer, it is marked `adjudicated_after_seeing_model`, and the old answer is kept in `original_label`.

### How scoring works

- For each of the 57 changes, the code gets the model's saved answer from `classifications` (same `change_id`, the prompt and model being tested).
- If an answer is missing, it calls the API for it, checks it against the Pydantic schema and saves it.
- It then compares the model's answer with yours.

| Check | Right when | Used on |
|---|---|---|
| Strict | the model's `label` equals your first label | practice (blind part) and holdout |
| Lenient | the model's `label` equals your first or second label | holdout |
| Material | the model's `is_material` equals your yes/no | holdout |

- The score for a group is the number right divided by the number in the group.
- Each score must reach its minimum in `eval/thresholds.json`. If one does not, `edgar eval` prints FAIL and exits with code 1.
- A change the saved baseline got right and the new prompt gets wrong is a **regression**. More than 2 regressions also fails the run.
- If the run passes, `--save-baseline` records it as the new baseline in `eval/baseline.json`.

### What we are measuring

- Only the classification step: does the model pick the right label, and does it agree on whether the change is material?
- We are **not** measuring: figure extraction, the diff step, other companies, speed or cost.
- The real question each time: **did this prompt change make the labels better, the same or worse?**

### Why we always keep a holdout

- A prompt can be tuned until it gets every practice case right. That only shows it fits those cases.
- The holdout is made of cases the prompt was never tuned on, so its score shows how the prompt does on new changes.
- The proof is in our own results. v002 matched 30 of 30 blind practice labels, but only 13 of 20 on the first holdout run.
- Without a holdout we would have believed the practice score.
- Once you tune a prompt using the holdout's misses, it becomes practice. Then you need a new holdout.

### Where we stand today

| Group | Cases | Score now | Minimum |
|---|---|---|---|
| Practice, blind labels, strict | 30 | 30 of 30 | 85% |
| Holdout, strict | 20 | 15 of 20 | 60% |
| Holdout, lenient | 20 | 18 of 20 | 75% |
| Holdout, material | 20 | 18 of 20 | 45% |
| Regressions vs baseline | all | 0 | at most 2 |

- 16 labels (7 practice and 9 holdout) were changed after seeing the model's answers. They are marked in the files, but they still count in the scores.
- So the holdout scores look better than they honestly are. To measure a new prompt fairly, label a new set of about 20 unseen changes without looking at the model's answers.
