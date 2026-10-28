import argparse

import json
from pathlib import Path

from .ingest.client import EdgarClient
from .align.sections import align_sections
from .diff.paragraphs import diff_filings
from .parse.html import parse_filing
from .parse.models import ParsedFiling


def main() -> None:
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


if __name__ == "__main__":
    main()
