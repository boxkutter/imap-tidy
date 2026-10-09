import dataclasses
import json
import logging

import pytest
from imapclient import IMAPClient

from translate_mail.message import parse
from translate_mail.worker import FLAG_DONE, FLAG_FAILED, AccountWorker

from .conftest import ENGLISH, FakeIMAP, FakeTranslator, make_mail


def make(account, tcfg, tmp_path, translator=None, folders=("INBOX", "Pending"), **acct_changes):
    acct = dataclasses.replace(account, **acct_changes)
    imap = FakeIMAP(folders=folders)
    w = AccountWorker(acct, translator or FakeTranslator(), tcfg, state_dir=tmp_path)
    return w, imap


def run_once(w, imap):
    w.setup(imap)
    return w


def new_mail(w, imap, raw, flags=()):
    uid = imap.deliver(w.a.watch, raw, flags)
    imap.select_folder(w.a.watch)
    w.process_pending(imap)
    return uid


# ---- setup / state -------------------------------------------------------

def test_setup_creates_and_subscribes_folders(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path, folders=("INBOX",))
    w.setup(imap)
    assert {"Pending", "Originals", "INBOX"} <= set(imap.folders)
    assert imap.subscribed == {"Pending", "INBOX", "Originals"}
    assert imap.selected == "Pending"


def test_first_run_skips_existing_mail(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path)
    imap.deliver("Pending", make_mail())
    w.setup(imap)
    w.process_pending(imap)
    assert len(imap.msgs("Pending")) == 1 and imap.msgs("INBOX") == []
    assert w.last_uid == 1


def test_process_existing(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path, process_existing=True)
    imap.deliver("Pending", make_mail())
    w.setup(imap)
    w.process_pending(imap)
    assert imap.msgs("Pending") == [] and len(imap.msgs("INBOX")) == 1


def test_state_persists_across_restarts(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path, process_existing=True)
    w.setup(imap)
    new_mail(w, imap, make_mail())
    state_files = list(tmp_path.glob("*.json"))
    assert [f.name for f in state_files] == ["mail.example.com_me@example.com.json"]
    assert json.loads(state_files[0].read_text()) == {"uidvalidity": 42, "last_uid": 1}

    # A restarted worker resumes from the state file, even with process_existing on.
    imap.deliver("Pending", make_mail())  # uid 2
    w2 = AccountWorker(w.a, w.translator, tcfg, state_dir=tmp_path)
    w2.setup(imap)
    assert w2.last_uid == 1
    w2.process_pending(imap)
    assert len(imap.msgs("INBOX")) == 2


def test_uidvalidity_change_resets(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path)
    (tmp_path / "mail.example.com_me@example.com.json").write_text(json.dumps({"uidvalidity": 1, "last_uid": 999}))
    imap.deliver("Pending", make_mail())
    w.setup(imap)
    assert w.last_uid == 1  # reset to "only new mail", not 999
    assert w.uidvalidity == 42


# ---- decisions -----------------------------------------------------------

def test_pass_through_english(account, tcfg, tmp_path):
    tr = FakeTranslator()
    w, imap = make(account, tcfg, tmp_path, translator=tr)
    w.setup(imap)
    raw = make_mail(body=ENGLISH, subject="Meeting")
    new_mail(w, imap, raw)
    assert tr.calls == []                                   # no API call
    assert imap.msgs("Pending") == [] and imap.msgs("Originals") == []
    (m,) = imap.msgs("INBOX")
    assert m["raw"] == raw and m["flags"] == {FLAG_DONE}     # unchanged, unread, tagged


def test_pass_through_skip_langs(account, tcfg, tmp_path):
    tr = FakeTranslator()
    w, imap = make(account, tcfg, tmp_path, translator=tr, skip_langs=["en", "de"])
    w.setup(imap)
    new_mail(w, imap, make_mail())
    assert tr.calls == [] and len(imap.msgs("INBOX")) == 1


def test_pass_through_in_place_when_watch_is_deliver(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path, watch="INBOX")
    w.setup(imap)
    uid = new_mail(w, imap, make_mail(body=ENGLISH))
    assert imap.folders["INBOX"][uid]["flags"] == {FLAG_DONE}


def test_translate(account, tcfg, tmp_path):
    tr = FakeTranslator()
    w, imap = make(account, tcfg, tmp_path, translator=tr)
    w.setup(imap)
    raw = make_mail()
    new_mail(w, imap, raw, flags=["\\Flagged", "$Label1"])
    assert len(tr.calls) == 1 and tr.calls[0][0] == "Treffen am Donnerstag"

    assert imap.msgs("Pending") == []
    (orig,) = imap.msgs("Originals")
    assert orig["raw"] == raw                                       # untouched
    assert "\\Seen" in orig["flags"]

    (tr_msg,) = imap.msgs("INBOX")
    assert tr_msg["flags"] == {"\\Flagged", "$Label1", FLAG_DONE}   # no \Seen
    assert tr_msg["date"] is not None
    m = parse(tr_msg["raw"])
    assert m["Subject"] == "[de→EN] EN(Treffen am Donnerstag)"
    assert m["Message-ID"] == "<orig-1@example.de>"


def test_translate_simple_setup_watch_inbox(account, tcfg, tmp_path):
    tr = FakeTranslator()
    w, imap = make(account, tcfg, tmp_path, translator=tr, watch="INBOX", deliver="INBOX")
    w.setup(imap)
    new_mail(w, imap, make_mail())
    (m,) = imap.msgs("INBOX")
    assert parse(m["raw"])["X-Translated-From"] == "de"
    assert len(imap.msgs("Originals")) == 1
    # Our own appended copy has a higher UID in the watched folder; it must not be re-translated.
    w.process_pending(imap)
    assert len(tr.calls) == 1 and len(imap.msgs("INBOX")) == 1


def test_translator_says_readable_language(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path, translator=FakeTranslator(src="EN"))
    w.setup(imap)
    raw = make_mail()
    new_mail(w, imap, raw)
    (m,) = imap.msgs("INBOX")
    assert m["raw"] == raw and imap.msgs("Originals") == []


def test_truncation(account, tcfg, tmp_path):
    tr = FakeTranslator()
    w, imap = make(account, tcfg, tmp_path, translator=tr)
    w.tcfg = dataclasses.replace(tcfg, max_chars=200)
    w.setup(imap)
    new_mail(w, imap, make_mail(body=" ".join(["Hallo Welt, wie geht es dir heute?"] * 50)))
    body = tr.calls[0][1]
    assert len(body) < 260 and body.endswith("[… truncated for translation …]")


def test_without_move_capability(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path)
    imap.caps = {"IDLE"}
    w.setup(imap)
    new_mail(w, imap, make_mail())
    assert imap.msgs("Pending") == [] and len(imap.msgs("Originals")) == 1 and len(imap.msgs("INBOX")) == 1


# ---- failures ------------------------------------------------------------

def notices(imap):
    return [parse(m["raw"]) for m in imap.msgs("INBOX") if parse(m["raw"])["From"].startswith("mail-translate")]


def test_translator_failure_delivers_original_and_notifies(account, tcfg, tmp_path, caplog):
    w, imap = make(account, tcfg, tmp_path, translator=FakeTranslator(fail=True))
    w.setup(imap)
    raw = make_mail()
    with caplog.at_level(logging.ERROR):
        uid = new_mail(w, imap, raw)
    assert imap.msgs("Pending") == [] and imap.msgs("Originals") == []
    originals = [m for m in imap.msgs("INBOX") if m["raw"] == raw]
    assert len(originals) == 1 and originals[0]["flags"] == {FLAG_FAILED}
    (n,) = notices(imap)
    assert n["Subject"] == "[mail-translate] Could not translate: Treffen am Donnerstag"
    body = n.get_content()
    assert "service unavailable" in body and "Traceback" in body and f"UID:      {uid}" in body
    assert "FAILED" in caplog.text and "Traceback" in caplog.text
    assert w.last_uid == uid


def test_failure_in_place_when_watch_is_deliver(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path, translator=FakeTranslator(fail=True), watch="INBOX")
    w.setup(imap)
    uid = new_mail(w, imap, make_mail())
    assert imap.folders["INBOX"][uid]["flags"] == {FLAG_FAILED}
    assert len(notices(imap)) == 1
    w.process_pending(imap)                 # neither the failed mail nor the notice is reprocessed
    assert len(imap.msgs("INBOX")) == 2


def test_malformed_mail_fails_cleanly(account, tcfg, tmp_path, monkeypatch):
    w, imap = make(account, tcfg, tmp_path)
    w.setup(imap)
    monkeypatch.setattr("translate_mail.worker.build_translated",
                        lambda *a, **k: (_ for _ in ()).throw(LookupError("unknown charset")))
    new_mail(w, imap, make_mail())
    assert len(notices(imap)) == 1
    assert "unknown charset" in notices(imap)[0].get_content()


def test_outage_notices_are_rate_limited(account, tcfg, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    tr = FakeTranslator(fail=True)
    w, imap = make(account, tcfg, tmp_path, translator=tr)
    w.setup(imap)
    for _ in range(6):
        new_mail(w, imap, make_mail())
    subjects = [n["Subject"] for n in notices(imap)]
    assert len(subjects) == 3                                    # 2 per-message + 1 outage notice
    assert subjects[2] == "[mail-translate] Translator is failing; notices paused"
    assert "notice suppressed" in caplog.text
    assert sum(1 for m in imap.msgs("INBOX") if m["flags"] == {FLAG_FAILED}) == 6  # all still delivered

    # Recovery resets the counter; the next failure notifies again.
    tr.fail = False
    new_mail(w, imap, make_mail())
    assert "translator recovered" in caplog.text
    tr.fail = True
    new_mail(w, imap, make_mail())
    assert len(notices(imap)) == 4


def test_imap_error_propagates_then_gives_up(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path)
    w.setup(imap)
    imap.fail_on = {"append"}
    uid = imap.deliver("Pending", make_mail())
    for _ in range(3):
        with pytest.raises(IMAPClient.Error):
            w.process_pending(imap)
    assert w.last_uid < uid
    w.process_pending(imap)   # 4th attempt: logged and skipped, loop keeps going
    assert w.last_uid == uid
    assert len(imap.msgs("Pending")) == 1   # never lost


def test_no_duplicate_after_partial_failure(account, tcfg, tmp_path):
    w, imap = make(account, tcfg, tmp_path)
    w.setup(imap)
    imap.fail_on = {"move"}
    imap.deliver("Pending", make_mail())
    with pytest.raises(IMAPClient.Error):
        w.process_pending(imap)          # translated copy appended, original not yet moved
    imap.fail_on = set()
    w.process_pending(imap)              # after "reconnect": only the move is retried
    assert len(imap.msgs("INBOX")) == 1 and len(imap.msgs("Originals")) == 1


def test_log_lines_carry_account_name(account, tcfg, tmp_path, caplog):
    # main() names each worker thread after its account and logs %(threadName)s.
    import threading
    w, imap = make(account, tcfg, tmp_path)
    w.setup(imap)
    imap.deliver("Pending", make_mail(body=ENGLISH))
    with caplog.at_level(logging.INFO):
        t = threading.Thread(target=w.process_pending, args=(imap,), name="test")
        t.start()
        t.join()
    rec = [r for r in caplog.records if "pass-through" in r.getMessage()]
    assert rec and rec[0].threadName == "test"
    assert "language=en" in rec[0].getMessage() and "Treffen" in rec[0].getMessage()
