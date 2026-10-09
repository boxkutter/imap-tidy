import datetime as dt
from email.message import EmailMessage

import pytest

from translate_mail.config import Account, TranslatorConfig
from translate_mail.message import to_bytes
from translate_mail.translators import TranslatorError

GERMAN = ("Hallo Lee, vielen Dank für deine Nachricht. Ich wollte dir nur sagen, dass das Treffen "
          "am Donnerstag um zehn Uhr im Büro stattfindet. Bitte bring die Unterlagen mit.")
ENGLISH = ("Hi Lee, thanks for your message. I just wanted to let you know that the meeting is "
           "on Thursday at ten o'clock in the office. Please bring the documents with you.")


def make_mail(body=GERMAN, subject="Treffen am Donnerstag", html=None, attachments=(), **headers):
    m = EmailMessage()
    m["From"] = headers.get("From", "Hans Müller <hans@example.de>")
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = "Mon, 1 Jan 2024 10:00:00 +0100"
    m["Message-ID"] = "<orig-1@example.de>"
    m["In-Reply-To"] = "<parent@example.com>"
    m["References"] = "<root@example.com> <parent@example.com>"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    for name, data, mime in attachments:
        maintype, subtype = mime.split("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return to_bytes(m)


class FakeIMAP:
    """Just enough of IMAPClient for AccountWorker, backed by dicts."""

    def __init__(self, folders=("INBOX",), capabilities=("IDLE", "MOVE", "UIDPLUS")):
        self.folders = {f: {} for f in folders}   # folder -> uid -> dict(raw, flags, date)
        self.next_uid = {f: 1 for f in folders}
        self.uidvalidity = 42
        self.caps = set(capabilities)
        self.selected = None
        self.subscribed = set()
        self.fail_on = set()                      # method names that raise (simulating IMAP errors)

    # helpers for tests
    def deliver(self, folder, raw, flags=()):
        uid = self.next_uid[folder]
        self.next_uid[folder] += 1
        self.folders[folder][uid] = {"raw": raw, "flags": set(flags),
                                     "date": dt.datetime(2024, 1, 1, 10, 0)}
        return uid

    def msgs(self, folder):
        return list(self.folders[folder].values())

    def _check(self, name):
        if name in self.fail_on:
            from imapclient import IMAPClient
            raise IMAPClient.Error(f"simulated failure in {name}")

    # IMAPClient API
    def has_capability(self, cap):
        return cap in self.caps

    def folder_exists(self, f):
        return f in self.folders

    def create_folder(self, f):
        self.folders[f] = {}
        self.next_uid[f] = 1

    def subscribe_folder(self, f):
        self.subscribed.add(f)

    def select_folder(self, f):
        self.selected = f
        return {b"UIDVALIDITY": self.uidvalidity, b"UIDNEXT": self.next_uid[f], b"EXISTS": len(self.folders[f])}

    def search(self, criteria):
        uids = sorted(self.folders[self.selected])
        if criteria == ["ALL"]:
            return uids
        assert criteria[0] == "UID"
        lo = int(criteria[1].split(":")[0])
        hits = [u for u in uids if u >= lo]
        # Real servers return the highest UID for "N:*" even when it is below N.
        return hits or uids[-1:]

    def get_flags(self, uids):
        box = self.folders[self.selected]
        return {u: tuple(f.encode() for f in box[u]["flags"]) for u in uids if u in box}

    def fetch(self, uids, items):
        self._check("fetch")
        box = self.folders[self.selected]
        return {u: {b"BODY[]": box[u]["raw"], b"FLAGS": tuple(f.encode() for f in box[u]["flags"]),
                    b"INTERNALDATE": box[u]["date"]} for u in uids if u in box}

    def add_flags(self, uids, flags):
        self._check("add_flags")
        for u in uids:
            self.folders[self.selected][u]["flags"].update(flags)

    def append(self, folder, msg, flags=(), msg_time=None):
        self._check("append")
        uid = self.deliver(folder, msg, flags)
        self.folders[folder][uid]["date"] = msg_time

    def move(self, uids, folder):
        self._check("move")
        for u in uids:
            m = self.folders[self.selected].pop(u)
            uid = self.deliver(folder, m["raw"], m["flags"])
            self.folders[folder][uid]["date"] = m["date"]

    def copy(self, uids, folder):
        for u in uids:
            m = self.folders[self.selected][u]
            uid = self.deliver(folder, m["raw"], m["flags"])
            self.folders[folder][uid]["date"] = m["date"]

    def delete_messages(self, uids):
        self.add_flags(uids, ["\\Deleted"])

    def uid_expunge(self, uids):
        for u in uids:
            self.folders[self.selected].pop(u)

    def expunge(self):
        box = self.folders[self.selected]
        for u in [u for u, m in box.items() if "\\Deleted" in m["flags"]]:
            box.pop(u)

    def logout(self):
        pass


class FakeTranslator:
    name = "fake"

    def __init__(self, src="de", fail=False):
        self.src = src
        self.fail = fail
        self.calls = []

    def translate(self, texts):
        self.calls.append(texts)
        if self.fail:
            raise TranslatorError("service unavailable")
        return [f"EN({t})" for t in texts], self.src


@pytest.fixture
def account():
    return Account(name="test", host="mail.example.com", user="me@example.com", password="x",
                   watch="Pending", deliver="INBOX", originals="Originals", skip_langs=["en"],
                   attach_original=True, process_existing=False)


@pytest.fixture
def tcfg():
    return TranslatorConfig(provider="deepl", target_lang="en", max_chars=30000)
