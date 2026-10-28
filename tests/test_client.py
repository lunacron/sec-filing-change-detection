from edgar_diff.ingest.client import EdgarClient


def test_list_filings_filters_forms(monkeypatch, tmp_path):
    monkeypatch.setenv("EDGAR_USER_AGENT", "test test@example.com")
    c = EdgarClient(tmp_path)
    sub = {
        "name": "Acme",
        "filings": {"recent": {
            "form": ["8-K", "10-K", "10-K/A"],
            "accessionNumber": ["a", "b", "c"],
            "filingDate": ["2024-01-01"] * 3,
            "reportDate": ["2023-12-31"] * 3,
            "primaryDocument": ["x.htm"] * 3,
        }},
    }
    monkeypatch.setattr(c, "submissions", lambda cik: sub)
    assert [f["form"] for f in c.list_filings(1)] == ["10-K", "10-K/A"]
