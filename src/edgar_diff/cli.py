import argparse

import json
from pathlib import Path

from .ingest.client import EdgarClient
from .parse.html import parse_filing


def main() -> None:
    p = argparse.ArgumentParser(prog="edgar")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="fetch recent 10-K filings for a CIK")
    ing.add_argument("cik", type=int)
    ing.add_argument("--n", type=int, default=2, help="number of 10-Ks (not /A)")
    par = sub.add_parser("parse", help="parse a cached filing into sections/paragraphs")
    par.add_argument("cik", type=int)
    par.add_argument("accession")
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


if __name__ == "__main__":
    main()
