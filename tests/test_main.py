import os

import pytest

from translate_mail.__main__ import find_config, main


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    # The test runner may be root: never let main() switch uid under pytest.
    monkeypatch.setenv("PUID", "0")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CONFIG", raising=False)


def test_missing_config_exits_nonzero(tmp_path, caplog):
    assert main([str(tmp_path / "missing.yml")]) == 2
    assert "config error" in caplog.text and "not found" in caplog.text


def test_translator_env_checked_at_startup(tmp_path, monkeypatch, caplog):
    cfg = tmp_path / "config.yml"
    cfg.write_text("translator: {provider: deepl}\naccounts:\n  - {name: a, host: h, user: u, password: pw}\n")
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    assert main([str(cfg)]) == 2
    assert "DEEPL_API_KEY" in caplog.text


def test_find_config(tmp_path, monkeypatch):
    assert find_config(["x.yml"], str(tmp_path)) == "x.yml"
    assert find_config([], str(tmp_path)) == os.path.join(tmp_path, "config.yml")
    monkeypatch.setenv("CONFIG", "/elsewhere.yml")
    assert find_config([], str(tmp_path)) == "/elsewhere.yml"


def test_state_dir_not_writable(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("STATE_DIR", str(blocker / "state"))
    assert main([str(tmp_path / "c.yml")]) == 2
    assert "not writable" in caplog.text
