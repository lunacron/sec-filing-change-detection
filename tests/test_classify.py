import json
from types import SimpleNamespace

from edgar_diff.diff.paragraphs import Change
from edgar_diff.llm import classify as clf
from edgar_diff.llm.schema import Classification, strict_schema


def _change(i, section="10-K/1A", kind="modified", old="old text here", new="new text here"):
    return Change(change_id=f"chg-{i}", kind=kind, old_section=section, new_section=section,
                  old_paragraph_id=f"o{i}", new_paragraph_id=f"n{i}", old_text=old, new_text=new)


def _resp(payload: dict | str, stop="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20))


GOOD = {"label": "legal_regulatory", "secondary_label": None, "is_material": True,
        "confidence": "high", "rationale": "A fine.", "evidence_quotes": ["new text"],
        "uncertainty_reason": None}


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def test_strict_schema_forbids_extra_and_requires_all():
    s = strict_schema(Classification)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(Classification.model_fields)


def test_sample_is_reproducible_includes_item3_and_spreads():
    changes = [_change(i, section=f"10-K/{s}") for i, s in enumerate(["1", "1A", "7", "8"] * 20)]
    changes += [_change(100 + i, section="10-K/3") for i in range(3)]
    a = clf.sample_changes(changes, 10, seed=1)
    assert [c.change_id for c in a] == [c.change_id for c in clf.sample_changes(changes, 10, seed=1)]
    assert {"chg-100", "chg-101", "chg-102"} <= {c.change_id for c in a}
    assert len({c.new_section for c in a}) == 5  # all four big sections plus Item 3


def test_user_message_marks_added_and_wraps_text():
    m = clf.build_user_message(_change(1, kind="added", old=None, new="brand new"))
    assert "no equivalent paragraph in the earlier filing" in m
    assert "<new_text>\nbrand new\n</new_text>" in m and "<old_text>" not in m


def test_evidence_grounding():
    c = _change(1, old="The Company was fined.", new="The  Company\nwas fined €500 million.")
    assert clf.evidence_grounded(["was fined €500 million"], c)
    assert not clf.evidence_grounded(["was fined $900 million"], c)


def test_classify_stores_provenance_and_caches(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    client = FakeClient(_resp(GOOD))
    c = _change(1)
    clf.classify_changes(client, [c], db, prompt_version="classify_v001", model="m", log=lambda *_: None)
    row = db.execute("SELECT label, evidence_grounded, prompt_sha256, model_id, run_id, created_at, "
                     "old_paragraph_id FROM classifications").fetchone()
    assert row[0] == "legal_regulatory" and row[1] == 1 and row[3] == "m" and row[6] == "o1"
    assert len(row[2]) == 64 and row[4] and row[5].endswith("+00:00")
    # second run: cached, no new API call
    clf.classify_changes(client, [c], db, prompt_version="classify_v001", model="m", log=lambda *_: None)
    assert len(client.calls) == 1
    assert "temperature" not in client.calls[0]


def test_schema_failure_and_refusal_become_uncertain(tmp_path):
    db = clf.open_db(tmp_path / "t.db")
    client = FakeClient(_resp("not json"), _resp({}, stop="refusal"))
    clf.classify_changes(client, [_change(1), _change(2)], db, prompt_version="classify_v001",
                         model="m", log=lambda *_: None)
    rows = dict(db.execute("SELECT change_id, uncertainty_reason FROM classifications"))
    assert rows == {"chg-1": "schema_failure", "chg-2": "refusal"}
