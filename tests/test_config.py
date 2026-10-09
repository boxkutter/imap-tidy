import pytest
import yaml

from translate_mail.config import ConfigError, expand_vars, load_config, parse_config

EXAMPLE = """
translator:
  provider: deepl
  target_lang: en
  max_chars: 30000
defaults:
  watch: Pending
  deliver: INBOX
  originals: Originals
  skip_langs: [en]
  attach_original: true
  process_existing: false
accounts:
  - name: personal
    host: mail.example.com
    port: 993
    user: me@example.com
    password: ${PASS_PERSONAL}
  - name: work
    host: mail.example.com
    user: me@work-domain.com
    password: ${PASS_WORK}
    skip_langs: [en, SV]
    watch: INBOX
    deliver: INBOX
"""
ENV = {"PASS_PERSONAL": "p1", "PASS_WORK": "p w 2"}


def cfg(text=EXAMPLE, env=ENV):
    return parse_config(yaml.safe_load(text), env)


def test_example_loads():
    c = cfg()
    assert c.translator.provider == "deepl" and c.translator.max_chars == 30000
    p, w = c.accounts
    assert (p.name, p.password, p.watch, p.deliver, p.port) == ("personal", "p1", "Pending", "INBOX", 993)
    assert (w.password, w.watch, w.deliver, w.skip_langs, w.port) == ("p w 2", "INBOX", "INBOX", ["en", "sv"], 993)
    assert "p1" not in repr(p)  # passwords stay out of logs


def test_shipped_example_file_is_valid(tmp_path):
    from pathlib import Path
    example = Path(__file__).parent.parent / "config.example.yml"
    c = load_config(example, {"PASS_PERSONAL": "a", "PASS_WORK": "b"})
    assert len(c.accounts) == 2


def test_expand_vars_nested():
    assert expand_vars({"a": ["x${A}y", 3], "b": "${B}"}, {"A": "1", "B": "2"}) == {"a": ["x1y", 3], "b": "2"}


def test_missing_env_var():
    with pytest.raises(ConfigError, match=r"PASS_WORK"):
        cfg(env={"PASS_PERSONAL": "p1"})


def test_target_lang_always_skipped():
    c = cfg(EXAMPLE.replace("skip_langs: [en, SV]", "skip_langs: [sv]"))
    assert c.accounts[1].skip_langs == ["sv", "en"]


@pytest.mark.parametrize("change, message", [
    (("provider: deepl", "provider: google"), "provider must be one of"),
    (("max_chars: 30000", "max_chars: lots"), "max_chars"),
    (("port: 993", "port: 99999"), "out of range"),
    (("port: 993", "port: '993'"), "port"),
    (("user: me@work-domain.com", "usr: me@work-domain.com"), "unknown key"),
    (("    watch: INBOX\n", "    watch: INBOX\n    originals: INBOX\n"), "originals"),
    (("skip_langs: [en, SV]", "skip_langs: en"), "skip_langs"),
    (("attach_original: true", "attach_original: yes please"), "attach_original"),
    (("name: work", "name: personal"), "duplicate account name"),
    (("user: me@work-domain.com", "user: me@example.com"), "configured twice"),
    (("  - name: personal\n", "  - nme: personal\n"), "unknown key"),
])
def test_validation_errors(change, message):
    old, new = change
    assert old in EXAMPLE
    with pytest.raises(ConfigError, match=message):
        cfg(EXAMPLE.replace(old, new))


def test_no_accounts():
    with pytest.raises(ConfigError, match="non-empty"):
        cfg("translator: {provider: deepl}\naccounts: []\n")


def test_missing_required():
    with pytest.raises(ConfigError, match="'password' is required"):
        cfg("accounts:\n  - {name: a, host: h, user: u}\n")


def test_empty_password():
    with pytest.raises(ConfigError, match="password"):
        cfg("accounts:\n  - {name: a, host: h, user: u, password: '${P}'}\n", env={"P": ""})


def test_minimal_uses_defaults():
    c = cfg("accounts:\n  - {name: a, host: h, user: u, password: pw}\n", env={})
    a = c.accounts[0]
    assert c.translator.provider == "deepl"
    assert (a.watch, a.deliver, a.originals, a.attach_original, a.process_existing) == \
        ("Pending", "INBOX", "Originals", True, False)


def test_file_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("accounts: [\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
