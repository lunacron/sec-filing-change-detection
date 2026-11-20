"""Real cost and latency, measured from what every call saves, plus an estimator built on that.

Every LLM call stores its input and output tokens, whether it ran as a normal call or in a
batch, and (for normal calls) how long it took. `edgar cost` adds them up. The estimator uses
the same history: it predicts a new run from the tokens that earlier runs of the same step
actually used, and it prices the run both ways, normal and batch (50% cheaper, but slower).

Rows saved before this tracking existed have no timing and no `mode`. They count as normal
calls with unknown latency.
"""
import sqlite3
import statistics
from dataclasses import dataclass

# Cached list prices, USD per 1M tokens: (input, output, cache read). Typed in by hand: update when
# prices change. Cache read is 0.05x input on Opus 5.5 and Sonnet 5.5; the Haiku value is 0.1x, unchecked.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
BATCH_DISCOUNT = 0.5          # the Batches API bills everything at half price
CACHE_WRITE_5M = 1.25         # writing to the cache costs this many times the input price
CACHE_WRITE_1H = 2.0          # same, for the 1-hour lifetime (used for batch requests)
BATCH_USUALLY = "usually within an hour, at most 24 hours (Anthropic's stated limits)"
STEP_TABLES = {"classify": "classifications", "extract": "extractions"}
NEW_COLUMNS = {"mode": "TEXT", "batch_id": "TEXT", "duration_ms": "INTEGER", "input_chars": "INTEGER",
               "cache_read_tokens": "INTEGER", "cache_write_tokens": "INTEGER"}
MIN_SIZED_CALLS = 10          # fewer than this and tokens-per-character is too noisy to trust
GUESS_OUTPUT_TOKENS = 700     # only used when there is no history at all


@dataclass
class Usage:
    """Tokens one call used. `input` is the uncached part only; cached tokens are counted apart."""
    input: int
    output: int
    cache_read: int = 0
    cache_write: int = 0

    @classmethod
    def from_api(cls, u) -> "Usage":
        return cls(u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0,
                   getattr(u, "cache_creation_input_tokens", 0) or 0)


def add_columns(db: sqlite3.Connection, table: str) -> None:
    """Add the tracking columns to a table created before they existed."""
    have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    for name, typ in NEW_COLUMNS.items():
        if name not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def call_cost(model: str, input_tokens: int, output_tokens: int, mode: str = "standard",
              cache_read: int = 0, cache_write: int = 0) -> float | None:
    p = PRICES.get(model)
    if p is None:
        return None
    write_x = CACHE_WRITE_1H if mode == "batch" else CACHE_WRITE_5M
    usd = (input_tokens * p[0] + output_tokens * p[1] + cache_read * p[2] + cache_write * p[0] * write_x) / 1e6
    return usd * (BATCH_DISCOUNT if mode == "batch" else 1.0)


def _rows(db: sqlite3.Connection, step: str, model: str | None = None, prompt_version: str | None = None):
    """(prompt, model, mode, input, output, duration_ms, input_chars, cache_read, cache_write) per call."""
    sql = ("SELECT prompt_version, model_id, COALESCE(mode, 'standard'), input_tokens, output_tokens, "
           "duration_ms, input_chars, COALESCE(cache_read_tokens, 0), COALESCE(cache_write_tokens, 0) "
           f"FROM {STEP_TABLES[step]} WHERE input_tokens IS NOT NULL")
    args = []
    if model:
        sql, args = sql + " AND model_id = ?", args + [model]
    if prompt_version:
        sql, args = sql + " AND prompt_version = ?", args + [prompt_version]
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:  # table not created yet
        return []


def batch_seconds(db: sqlite3.Connection, step: str) -> list[float]:
    """How long finished batches took, from creation to the end."""
    from datetime import datetime
    try:
        rows = db.execute("SELECT created_at, ended_at FROM batches WHERE step = ? AND ended_at IS NOT NULL",
                          (step,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(datetime.fromisoformat(e) - datetime.fromisoformat(c)).total_seconds() for c, e in rows]


def _p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.95 * len(xs)))]


def report(db: sqlite3.Connection) -> list[dict]:
    """Actual spend, grouped by step, prompt, model and mode."""
    groups: dict[tuple, dict] = {}
    for step in STEP_TABLES:
        for pv, model, mode, tin, tout, ms, _, cread, cwrite in _rows(db, step):
            g = groups.setdefault((step, pv, model, mode), {
                "step": step, "prompt": pv, "model": model, "mode": mode, "calls": 0, "input": 0,
                "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0, "priced": True, "ms": []})
            g["calls"] += 1
            g["input"] += tin
            g["output"] += tout
            g["cache_read"] += cread
            g["cache_write"] += cwrite
            c = call_cost(model, tin, tout, mode, cread, cwrite)
            g["priced"] &= c is not None
            g["usd"] += c or 0.0
            if ms is not None:
                g["ms"].append(ms)
    return sorted(groups.values(), key=lambda g: (g["step"], g["prompt"], g["model"], g["mode"]))


def estimate(db: sqlite3.Connection, step: str, model: str, prompt_version: str, chars: list[int]) -> dict:
    """Predict a run of len(chars) calls. `chars` is the length of system prompt + message per call.

    Basis, best first: tokens per character from at least MIN_SIZED_CALLS earlier calls that saved
    their input size; then the average tokens per call of earlier calls of this step; then a
    rough guess (4 characters per token, 700 output tokens).

    "Prompt tokens" are uncached input + cache reads + cache writes, so a run that used the cache
    is comparable with one that did not. If earlier calls used the cache, the estimate assumes
    the same cached prefix: one cache write for the run, then cache reads (best case for batch,
    where hits are only best-effort).
    """
    n = len(chars)
    rows = _rows(db, step, model, prompt_version) or _rows(db, step, model)
    other_model = False
    if not rows:  # tokens per call barely depend on the model, so another model's history beats a guess
        rows, other_model = _rows(db, step), True
    cached = [r for r in rows if r[7] + r[8] > 0]
    if cached:      # the cache is in use now: learn only from calls that used it
        rows = cached
    sized = [r for r in rows if r[6]]
    prompt = lambda r: r[3] + r[7] + r[8]  # noqa: E731
    if len(sized) >= MIN_SIZED_CALLS:
        ci = sum(r[6] for r in sized)
        in_tok = round(sum(chars) * sum(prompt(r) for r in sized) / ci)
        out_tok = round(sum(chars) * sum(r[4] for r in sized) / ci)
        basis = f"tokens per character from {len(sized)} earlier calls"
        basis += " of another model" if other_model else ""
    elif rows:
        in_tok = round(n * statistics.mean(prompt(r) for r in rows))
        out_tok = round(n * statistics.mean(r[4] for r in rows))
        basis = f"average tokens per call from {len(rows)} earlier calls (less exact)"
        basis += ", of another model" if other_model else ""
    else:
        in_tok, out_tok = sum(chars) // 4, GUESS_OUTPUT_TOKENS * n
        basis = "rough guess, no earlier calls to learn from"
    prefix = round(statistics.median(r[7] + r[8] for r in cached)) if cached else 0
    prefix = min(prefix, in_tok // n) if n else 0

    def usd(mode: str) -> float | None:
        # first call writes the cached prefix, the other n-1 read it; the rest is normal input
        return call_cost(model, in_tok - n * prefix, out_tok, mode, cache_read=(n - 1) * prefix if prefix else 0,
                         cache_write=prefix if prefix else 0)

    timed = [r[5] for r in rows if r[5] is not None and r[2] == "standard" and not other_model]
    batches = batch_seconds(db, step)
    return {
        "calls": n, "input_tokens": in_tok, "output_tokens": out_tok, "basis": basis, "cached_prefix": prefix,
        "usd_standard": usd("standard"), "usd_batch": usd("batch"),
        "seconds_standard": n * statistics.median(timed) / 1000 if timed else None,
        "seconds_batch": statistics.median(batches) if batches else None,
    }


def _dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _usd(x: float | None) -> str:
    return "unknown (no price for this model)" if x is None else f"${x:.2f}"


def format_estimate(est: dict) -> list[str]:
    if est["calls"] == 0:
        return ["nothing to send: every change is already saved, so this run costs $0.00"]
    std = (f"about {_dur(est['seconds_standard'])} (one call after another)"
           if est["seconds_standard"] is not None else "time not known yet (no timed calls)")
    bat = (f"typically {_dur(est['seconds_batch'])} from earlier batches"
           if est["seconds_batch"] is not None else BATCH_USUALLY)
    cache = (f", {est['cached_prefix']:,} of them per call cached" if est["cached_prefix"] else "")
    return [
        f"{est['calls']} calls, ~{est['input_tokens']:,} prompt{cache} and ~{est['output_tokens']:,} output "
        f"tokens ({est['basis']})",
        f"  normal: {_usd(est['usd_standard'])}, {std}",
        f"  batch:  {_usd(est['usd_batch'])} (50% off), {bat}",
    ]


def format_report(groups: list[dict]) -> list[str]:
    if not groups:
        return ["no LLM calls saved yet"]
    lines = [f"{'step':8} {'prompt':14} {'model':18} {'mode':9} {'calls':>5} {'input':>9} {'cache rd':>9} "
             f"{'cache wr':>8} {'output':>9} {'USD':>7}  latency per call (median / 95th)"]
    total = 0.0
    for g in groups:
        total += g["usd"]
        lat = (f"{_dur(statistics.median(g['ms']) / 1000)} / {_dur(_p95(g['ms']) / 1000)} "
               f"({len(g['ms'])} timed)" if g["ms"] else "not recorded")
        usd = f"{g['usd']:.2f}" + ("" if g["priced"] else "?")
        lines.append(f"{g['step']:8} {g['prompt']:14} {g['model']:18} {g['mode']:9} {g['calls']:>5} "
                     f"{g['input']:>9,} {g['cache_read']:>9,} {g['cache_write']:>8,} {g['output']:>9,} "
                     f"{usd:>7}  {lat}")
    lines.append(f"{'total':>101} {total:>7.2f}")
    return lines
