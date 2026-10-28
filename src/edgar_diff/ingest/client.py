"""Minimal EDGAR client: rate-limited, cached, descriptive User-Agent."""
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

DATA_DIR = Path(os.environ.get("EDGAR_DATA_DIR", "data"))
MIN_INTERVAL = 0.2  # <= 5 req/s, well under SEC's 10/s limit
FORM_TYPES = {"10-K", "10-K405", "10-K/A"}


def _user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        raise RuntimeError("Set EDGAR_USER_AGENT (see .env.example)")
    return ua


class EdgarClient:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self._last = 0.0
        self._http = httpx.Client(
            headers={"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"},
            timeout=30,
            follow_redirects=True,
        )

    def _get(self, url: str) -> bytes:
        for attempt in range(4):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            resp = self._http.get(url)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp.content
        resp.raise_for_status()
        return b""

    def submissions(self, cik: int) -> dict:
        url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        return json.loads(self._get(url))

    def list_filings(self, cik: int) -> list[dict]:
        """10-K family filings from the 'recent' block, newest first."""
        sub = self.submissions(cik)
        recent = sub["filings"]["recent"]
        out = []
        for i, form in enumerate(recent["form"]):
            if form in FORM_TYPES:
                out.append({
                    "cik": cik,
                    "company": sub["name"],
                    "form": form,
                    "accession": recent["accessionNumber"][i],
                    "filing_date": recent["filingDate"][i],
                    "report_date": recent["reportDate"][i],
                    "primary_document": recent["primaryDocument"][i],
                })
        return out

    def fetch_filing(self, filing: dict) -> Path:
        """Download primary document into the raw cache; return its path."""
        acc_nodash = filing["accession"].replace("-", "")
        dest_dir = self.data_dir / "raw" / str(filing["cik"]) / filing["accession"]
        dest = dest_dir / filing["primary_document"]
        if dest.exists():
            return dest
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{filing['cik']}/"
            f"{acc_nodash}/{filing['primary_document']}"
        )
        body = self._get(url)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        meta = {
            **filing,
            "url": url,
            "sha256": hashlib.sha256(body).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        (dest_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return dest
