import argparse
import os

import json
from pathlib import Path

from .ingest.client import EdgarClient
from .align.sections import align_sections
from .diff.paragraphs import DiffResult, diff_filings
from .llm import classify as clf
from .parse.html import parse_filing
from .parse.models import ParsedFiling


def _load_env(path: Path = Path(".env")) -> None:
    """Minimal .env loader so commands work without sourcing the file first."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def main() -> None:
    _load_env()
    p = argparse.ArgumentParser(prog="edgar")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="fetch recent 10-K filings for a CIK")
    ing.add_argument("cik", type=int)
    ing.add_argument("--n", type=int, default=2, help="number of 10-Ks (not /A)")
    par = sub.add_parser("parse", help="parse a cached filing into sections/paragraphs")
    par.add_argument("cik", type=int)
    par.add_argument("accession")
    al = sub.add_parser("align", help="align sections of two parsed filings")
    al.add_argument("old_accession")
    al.add_argument("new_accession")
    df = sub.add_parser("diff", help="paragraph-level diff of two parsed filings")
    df.add_argument("old_accession")
    df.add_argument("new_accession")
    cl = sub.add_parser("classify", help="LLM-classify changed paragraphs from a diff")
    cl.add_argument("old_accession")
    cl.add_argument("new_accession")
    cl.add_argument("--sample", type=int, default=40, help="number of changes (0 = all)")
    cl.add_argument("--seed", type=int, default=0)
    cl.add_argument("--model", default=clf.DEFAULT_MODEL)
    cl.add_argument("--prompt", default="classify_v002")
    cl.add_argument("--from-run", help="classify the same changes as this earlier run_id")
    cl.add_argument("--ids-file", help="JSON file with a change_ids list (e.g. eval/holdout_ids.json)")
    cl.add_argument("--dry-run", action="store_true", help="estimate cost; make no API calls")
    ex = sub.add_parser("extract", help="LLM-extract numeric figures from changes, then check them in code")
    ex.add_argument("old_accession")
    ex.add_argument("new_accession")
    ex.add_argument("--sample", type=int, default=10, help="number of changes (0 = all with figures)")
    ex.add_argument("--seed", type=int, default=0)
    ex.add_argument("--model", default="claude-sonnet-5-5")
    ex.add_argument("--prompt", default="extract_v001")
    ex.add_argument("--dry-run", action="store_true", help="estimate cost; make no API calls")
    ev = sub.add_parser("eval", help="score a prompt against the golden set; exit 1 on regression")
    ev.add_argument("old_accession")
    ev.add_argument("new_accession")
    ev.add_argument("--prompt", default="classify_v002")
    ev.add_argument("--model", default="claude-sonnet-5-5")
    ev.add_argument("--save-baseline", action="store_true", help="record a passing run as the new baseline")
    ev.add_argument("--dry-run", action="store_true", help="show what would run and its cost; make no API calls")
    args = p.parse_args()

    if args.cmd == "ingest":
        client = EdgarClient()
        filings = client.list_filings(args.cik)
        keep = [f for f in filings if f["form"] == "10-K"][: args.n]
        for f in keep:
            path = client.fetch_filing(f)
            print(f"{f['company']} {f['form']} {f['report_date']} {f['accession']} -> {path}")

    if args.cmd == "parse":
        src_dir = Path("data/raw") / str(args.cik) / args.accession
        meta = json.loads((src_dir / "meta.json").read_text())
        pf = parse_filing(src_dir / meta["primary_document"], args.accession)
        out = Path("data/parsed") / f"{args.accession}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(pf.model_dump_json(indent=1))
        n = sum(len(s.paragraphs) for s in pf.sections)
        print(f"{args.accession}: {len(pf.sections)} sections, {n} blocks -> {out}")

    if args.cmd == "align":
        load = lambda a: ParsedFiling.model_validate_json(Path(f"data/parsed/{a}.json").read_text())
        al = align_sections(load(args.old_accession), load(args.new_accession))
        out = Path("data/diffs") / f"{args.old_accession}__{args.new_accession}.alignment.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(al.model_dump_json(indent=1))
        for s in al.sections:
            print(f"{s.kind:8} {s.key:8} old={s.old_paragraphs:4} new={s.new_paragraphs:4}  {(s.new_title or s.old_title)[:45]}")
        print(f"-> {out}")

    if args.cmd == "diff":
        load = lambda a: ParsedFiling.model_validate_json(Path(f"data/parsed/{a}.json").read_text())
        old, new = load(args.old_accession), load(args.new_accession)
        res = diff_filings(old, new, align_sections(old, new))
        out = Path("data/diffs") / f"{args.old_accession}__{args.new_accession}.changes.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(res.model_dump_json(indent=1))
        print(res.summary)
        print(f"-> {out}")

    if args.cmd == "classify":
        path = Path("data/diffs") / f"{args.old_accession}__{args.new_accession}.changes.json"
        res = DiffResult.model_validate_json(path.read_text())
        todo = [c for c in res.changes if c.needs_llm]
        if args.from_run:
            ids = {r[0] for r in clf.open_db(Path("data/edgar.db")).execute(
                "SELECT change_id FROM classifications WHERE run_id=?", (args.from_run,))}
            picked = [c for c in todo if c.change_id in ids]
            print(f"{len(ids) - len(picked)} of the {len(ids)} changes in run {args.from_run} "
                  f"no longer need the LLM (headings, date-only)")
        elif args.ids_file:
            wanted = set(json.loads(Path(args.ids_file).read_text())["change_ids"])
            picked = [c for c in todo if c.change_id in wanted]
            print(f"{len(picked)} of the {len(wanted)} change_ids in {args.ids_file} found")
        else:
            picked = clf.sample_changes(todo, args.sample or len(todo), args.seed)
        system, _ = clf.load_prompt(args.prompt)
        est = clf.estimate_cost([clf.build_user_message(c) for c in picked], system)
        print(f"{len(picked)} of {len(todo)} changes; ~{est['input_tokens']:,} input / "
              f"~{est['output_tokens']:,} output tokens (rough estimate)")
        print("estimated USD by model:", est["usd"])
        if args.dry_run:
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set (add it to .env)")
        import anthropic

        db = clf.open_db(Path("data/edgar.db"))
        clf.classify_changes(anthropic.Anthropic(), picked, db,
                             prompt_version=args.prompt, model=args.model)

    if args.cmd == "extract":
        from .llm import extract as ext

        path = Path("data/diffs") / f"{args.old_accession}__{args.new_accession}.changes.json"
        res = DiffResult.model_validate_json(path.read_text())
        pool = [c for c in res.changes if ext.has_figures(c)]
        picked = clf.sample_changes(pool, args.sample or len(pool), args.seed)
        system, sha = clf.load_prompt(args.prompt)
        est = clf.estimate_cost([ext.build_extract_message(c) for c in picked], system)
        print(f"{len(picked)} of {len(pool)} changes with figures; ~{est['input_tokens']:,} input / "
              f"~{est['output_tokens']:,} output tokens (rough estimate)")
        print("estimated USD by model:", est["usd"])
        if args.dry_run:
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set (add it to .env)")
        import anthropic

        db = clf.open_db(Path("data/edgar.db"))
        ext.extract_figures(anthropic.Anthropic(), picked, db, prompt_version=args.prompt, model=args.model)
        rep = ext.report(db, sha, args.model, {c.change_id for c in picked})
        print(f"{rep['figures']} figures, {rep['ungrounded']} not grounded")
        for cid, side, written, metric, _, reason in rep["bad"]:
            print(f"  NOT GROUNDED {cid} {side}: {written} ({metric}) -> {reason}")

    if args.cmd == "eval":
        from .eval import runner

        path = Path("data/diffs") / f"{args.old_accession}__{args.new_accession}.changes.json"
        client = None
        if os.environ.get("ANTHROPIC_API_KEY") and not args.dry_run:
            import anthropic

            client = anthropic.Anthropic()
        try:
            code = runner.run_eval(changes_path=path, prompt=args.prompt, model=args.model,
                                   db=clf.open_db(Path("data/edgar.db")), client=client,
                                   save_baseline=args.save_baseline, dry_run=args.dry_run)
        except runner.EvalError as e:
            raise SystemExit(str(e))
        raise SystemExit(code)


if __name__ == "__main__":
    main()
