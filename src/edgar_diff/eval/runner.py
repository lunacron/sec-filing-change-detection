"""Score a prompt against the golden set and decide pass or fail.

The gold labels are the answer key (eval/gold_labels*.json). The model's answers come from the
classifications table, keyed by (change_id, prompt hash, model). A prompt passes when every
threshold in eval/thresholds.json is met and it has not broken more than `max_new_failures`
changes that the saved baseline got right.

Groups that are scored (sizes as of the current 57-change golden set):

  group           changes  labels in it                                  checks scored
  --------------  -------  --------------------------------------------  ---------------------------
  practice_blind  30       practice set, written before seeing the        strict
                           model's answer (status "confirmed")
  practice_all    37       the 30 above plus 7 changed after seeing       strict (reported only)
                           the model's answer ("adjudicated_...")
  holdout         20       holdout set, all written blind                 strict, lenient, material

What each check means:

  strict    the model's label equals the first gold label
  lenient   the model's label equals the first or the second gold label (holdout only)
  material  the model's is_material equals the gold MATERIAL answer (holdout only)

Thresholds in eval/thresholds.json:

  threshold                   group           check     current minimum
  --------------------------  --------------  --------  ---------------
  practice_blind_strict_min   practice_blind  strict    85%
  holdout_strict_min          holdout         strict    60%
  holdout_lenient_min         holdout         lenient   75%
  holdout_material_min        holdout         material  45%
  max_new_failures            all             strict    2 (changes the baseline got right, now wrong)
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..diff.paragraphs import Change, DiffResult
from ..llm import classify as clf

GOLD_FILES = {"practice": "gold_labels.json", "holdout": "gold_labels_holdout.json"}

DEFAULT_THRESHOLDS = {
    "practice_blind_strict_min": 0.85,
    "holdout_strict_min": 0.60,
    "holdout_lenient_min": 0.75,
    "holdout_material_min": 0.45,
    "max_new_failures": 2,
}


class EvalError(Exception):
    """The golden set cannot be scored (missing changes, missing files)."""


@dataclass
class Gold:
    change_id: str
    group: str            # practice | holdout
    label: str
    second: str | None
    material: bool | None
    blind: bool           # False when the label was changed after seeing the model's answer


def load_gold(gold_dir: Path = Path("eval")) -> dict[str, Gold]:
    gold: dict[str, Gold] = {}
    for group, name in GOLD_FILES.items():
        path = gold_dir / name
        if not path.exists():
            raise EvalError(f"gold label file not found: {path}")
        for cid, g in json.loads(path.read_text()).items():
            gold[cid] = Gold(
                change_id=cid, group=group, label=g["label"],
                second=g.get("second") or g.get("secondary"),
                material=g.get("material"),
                blind=g.get("status", "confirmed") == "confirmed")
    return gold


def check_gold_present(gold: dict[str, Gold], changes: list[Change]) -> None:
    """Stop if a gold label no longer matches a change that goes to the LLM."""
    needs = {c.change_id for c in changes if c.needs_llm}
    missing = sorted(set(gold) - {c.change_id for c in changes})
    skipped = sorted((set(gold) & {c.change_id for c in changes}) - needs)
    if missing or skipped:
        lines = ["The golden set does not match the current changes.json, so nothing was scored."]
        if missing:
            lines.append(f"  {len(missing)} gold change_id(s) not found (the diff rules or the filings changed): "
                         + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""))
        if skipped:
            lines.append(f"  {len(skipped)} gold change(s) no longer go to the LLM: "
                         + ", ".join(skipped[:8]) + (" ..." if len(skipped) > 8 else ""))
        lines.append("  The label sheets in eval/ hold the paragraph text, so labels can be re-attached by hand.")
        raise EvalError("\n".join(lines))


def model_rows(db: sqlite3.Connection, sha: str, model: str, ids) -> dict[str, dict]:
    out = {}
    for cid, label, mat in db.execute(
            "SELECT change_id, label, is_material FROM classifications WHERE prompt_sha256=? AND model_id=?",
            (sha, model)):
        if cid in ids:
            out[cid] = {"label": label, "material": bool(mat)}
    return out


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def score(gold: dict[str, Gold], rows: dict[str, dict]) -> tuple[dict, dict[str, bool]]:
    """Returns (scores, correct) where correct maps change_id to strict correctness."""
    correct = {cid: rows[cid]["label"] == g.label for cid, g in gold.items() if cid in rows}
    groups = {
        "practice_blind": [g for g in gold.values() if g.group == "practice" and g.blind],
        "practice_all": [g for g in gold.values() if g.group == "practice"],
        "holdout": [g for g in gold.values() if g.group == "holdout"],
    }
    scores = {}
    for name, gs in groups.items():
        gs = [g for g in gs if g.change_id in rows]
        strict = sum(rows[g.change_id]["label"] == g.label for g in gs)
        lenient = sum(rows[g.change_id]["label"] in (g.label, g.second) for g in gs)
        mat = [g for g in gs if g.material is not None]
        mat_ok = sum(rows[g.change_id]["material"] == g.material for g in mat)
        scores[name] = {
            "n": len(gs), "strict": strict, "lenient": lenient,
            "material_n": len(mat), "material_agree": mat_ok,
            "uncertain": sum(rows[g.change_id]["label"] == "uncertain" for g in gs),
            "strict_rate": _rate(strict, len(gs)), "lenient_rate": _rate(lenient, len(gs)),
            "material_rate": _rate(mat_ok, len(mat)),
        }
    return scores, correct


def check(scores: dict, correct: dict[str, bool], thresholds: dict, baseline: dict | None) -> tuple[list[str], list[str]]:
    """Returns (failures, regressed change_ids). Empty failures means pass."""
    failures = []
    rules = [
        ("practice_blind_strict_min", "practice_blind", "strict_rate", "practice set (blind labels), exact label"),
        ("holdout_strict_min", "holdout", "strict_rate", "holdout set, exact label"),
        ("holdout_lenient_min", "holdout", "lenient_rate", "holdout set, first or second label"),
        ("holdout_material_min", "holdout", "material_rate", "holdout set, is_material agrees"),
    ]
    for key, group, metric, text in rules:
        floor, value = thresholds.get(key), scores[group][metric]
        if floor is not None and (value is None or value < floor):
            shown = "no data" if value is None else f"{value:.0%}"
            failures.append(f"{text}: {shown} is below the minimum {floor:.0%}")
    regressed = []
    if baseline:
        regressed = sorted(c for c, ok in correct.items() if not ok and baseline["correct"].get(c) is True)
        limit = thresholds.get("max_new_failures")
        if limit is not None and len(regressed) > limit:
            failures.append(f"{len(regressed)} change(s) the baseline got right are now wrong "
                            f"(allowed: {limit})")
    return failures, regressed


def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def run_eval(*, changes_path: Path, prompt: str, model: str, db: sqlite3.Connection, client,
             gold_dir: Path = Path("eval"), save_baseline: bool = False, dry_run: bool = False,
             log=print) -> int:
    """Returns the process exit code: 0 pass, 1 fail."""
    gold = load_gold(gold_dir)
    if not changes_path.exists():
        raise EvalError(f"{changes_path} not found. Run `edgar diff <old> <new>` first, "
                        "and check both accession numbers.")
    res = DiffResult.model_validate_json(changes_path.read_text())
    check_gold_present(gold, res.changes)
    thresholds = {**DEFAULT_THRESHOLDS, **load_json(gold_dir / "thresholds.json", {})}
    baseline = load_json(gold_dir / "baseline.json")

    system, sha = clf.load_prompt(prompt)
    by_id = {c.change_id: c for c in res.changes}
    cached = set(model_rows(db, sha, model, set(gold)))
    todo = [by_id[cid] for cid in gold if cid not in cached]
    log(f"golden set: {len(gold)} changes; prompt {prompt} ({sha[:12]}), model {model}; "
        f"{len(gold) - len(todo)} already classified, {len(todo)} to run")
    if todo:
        est = clf.estimate_cost([clf.build_user_message(c) for c in todo], system)
        log(f"estimated cost of the new calls: ${est['usd'].get(model, '?')}")
    if dry_run:
        return 0
    if todo:
        if client is None:
            raise EvalError("ANTHROPIC_API_KEY is not set (add it to .env)")
        clf.classify_changes(client, [by_id[cid] for cid in gold], db,
                             prompt_version=prompt, model=model, log=lambda *_: None)
    rows = model_rows(db, sha, model, set(gold))
    scores, correct = score(gold, rows)
    failures, regressed = check(scores, correct, thresholds, baseline)

    for name, s in scores.items():
        parts = [f"{s['strict']}/{s['n']} exact"]
        if name == "holdout":
            parts += [f"{s['lenient']}/{s['n']} first-or-second label",
                      f"{s['material_agree']}/{s['material_n']} is_material agrees"]
        parts.append(f"{s['uncertain']} uncertain")
        log(f"  {name:15} " + ", ".join(parts))
    if baseline:
        log(f"baseline: {baseline['prompt_version']} ({baseline['prompt_sha256'][:12]}), "
            f"{baseline['model_id']}; changes it got right that are now wrong: {len(regressed)}")
        for cid in regressed:
            log(f"    {cid}: gold {gold[cid].label}, model {rows[cid]['label']}")
    else:
        log("no baseline saved yet (use --save-baseline to record this run as the baseline)")

    if save_baseline:
        if failures:
            log("not saving the baseline: this run failed the thresholds")
        else:
            (gold_dir / "baseline.json").write_text(json.dumps({
                "prompt_version": prompt, "prompt_sha256": sha, "model_id": model,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "scores": scores, "correct": correct}, indent=1))
            log(f"saved {gold_dir / 'baseline.json'}")
    if failures:
        log("FAIL")
        for f in failures:
            log(f"  - {f}")
        return 1
    log("PASS")
    return 0
