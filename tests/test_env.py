import os

from edgar_diff.cli import _load_env


def test_load_env_handles_export_and_spaces(tmp_path, monkeypatch):
    monkeypatch.delenv("K1", raising=False)
    monkeypatch.delenv("K2", raising=False)
    f = tmp_path / ".env"
    f.write_text('# c\nK1="a b"\n\nexport K2 = \'xyz\'\n')
    _load_env(f)
    assert os.environ["K1"] == "a b" and os.environ["K2"] == "xyz"
