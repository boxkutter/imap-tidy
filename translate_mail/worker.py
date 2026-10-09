"""One IMAP account: watch a folder, translate or pass through each new message."""

import json
import logging
import re
import ssl
import threading
import time
import traceback
from pathlib import Path

from imapclient import IMAPClient

from .message import (build_error_notice, build_translated, detect_message_lang, get_bodies, parse,
                      to_bytes)
from .translators import TranslatorError

log = logging.getLogger(__name__)

FLAG_DONE = "$Translated"
FLAG_FAILED = "$TranslateFailed"
SEEN = "\\Seen"

IDLE_TIMEOUT = 300           # re-issue IDLE (and re-scan the folder) at least this often
POLL_INTERVAL = 60           # for servers without IDLE
BACKOFF_START, BACKOFF_MAX = 5, 300
OUTAGE_THRESHOLD = 3         # consecutive translator failures before notices are paused
MAX_ATTEMPTS = 3             # IMAP-level retries of a single message before giving up on it


def _s(flag):
    return flag.decode() if isinstance(flag, bytes) else str(flag)


def short(text, n=60):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class AccountWorker:
    def __init__(self, account, translator, tcfg, state_dir="/state", stop_event=None,
                 client_factory=None):
        self.a = account
        self.translator = translator
        self.tcfg = tcfg
        self.stop = stop_event or threading.Event()
        self.client_factory = client_factory or self._connect
        safe = re.sub(r"[^A-Za-z0-9._@-]", "_", f"{account.host}_{account.user}")
        self.state_file = Path(state_dir) / f"{safe}.json"
        self.last_uid = 0
        self.uidvalidity = None
        self.translator_failures = 0     # consecutive, for outage detection
        self.attempts = {}               # uid -> attempts that died on an IMAP error
        self.delivered = set()           # uids whose translated copy is already appended

    # ------------------------------------------------------------ main loop --

    def run(self):
        """Connect, process, IDLE, reconnect with backoff. Never returns until stopped."""
        backoff = BACKOFF_START
        while not self.stop.is_set():
            client = None
            try:
                log.info("connecting to %s:%s as %s", self.a.host, self.a.port, self.a.user)
                client = self.client_factory()
                self.setup(client)
                backoff = BACKOFF_START
                while not self.stop.is_set():
                    self.process_pending(client)
                    self.wait_for_mail(client)
            except Exception as e:
                if self.stop.is_set():
                    break
                log.error("IMAP error: %s: %s; reconnecting in %ss", e.__class__.__name__, e, backoff,
                          exc_info=log.isEnabledFor(logging.DEBUG))
                self.stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            finally:
                if client is not None:
                    try:
                        client.logout()
                    except Exception:
                        pass
        log.info("stopped")

    def _connect(self):
        ctx = ssl.create_default_context()
        if not self.a.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        client = IMAPClient(self.a.host, port=self.a.port, ssl=True, ssl_context=ctx, timeout=60)
        client.login(self.a.user, self.a.password)
        return client

    def setup(self, client):
        for folder in dict.fromkeys((self.a.watch, self.a.deliver, self.a.originals)):
            if not client.folder_exists(folder):
                log.info("creating folder %s", folder)
                client.create_folder(folder)
            try:
                client.subscribe_folder(folder)
            except Exception as e:  # already subscribed, or server doesn't care
                log.debug("subscribe %s: %s", folder, e)
        info = client.select_folder(self.a.watch)
        self.load_state(client, info)
        log.info("connected; watching %s (last uid %s, uidvalidity %s)",
                 self.a.watch, self.last_uid, self.uidvalidity)

    def wait_for_mail(self, client):
        if client.has_capability("IDLE"):
            client.idle()
            try:
                responses = client.idle_check(timeout=IDLE_TIMEOUT)
            finally:
                client.idle_done()
            log.debug("idle responses: %s", responses)
        else:
            self.stop.wait(POLL_INTERVAL)

    # ---------------------------------------------------------------- state --

    def load_state(self, client, select_info):
        uidvalidity = select_info.get(b"UIDVALIDITY")
        state = {}
        if self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text())
            except (ValueError, OSError) as e:
                log.warning("state file %s unreadable (%s); starting fresh", self.state_file, e)
        if state and state.get("uidvalidity") == uidvalidity:
            self.last_uid = int(state.get("last_uid", 0))
        else:
            if state:
                log.warning("UIDVALIDITY of %s changed (%s -> %s); resetting position",
                            self.a.watch, state.get("uidvalidity"), uidvalidity)
            if self.a.process_existing:
                self.last_uid = 0
                log.info("process_existing is on: will process everything already in %s", self.a.watch)
            else:
                uidnext = select_info.get(b"UIDNEXT")
                if uidnext:
                    self.last_uid = int(uidnext) - 1
                else:
                    uids = client.search(["ALL"])
                    self.last_uid = max(uids) if uids else 0
                log.info("first run: skipping existing mail in %s, only new mail from now on", self.a.watch)
        self.uidvalidity = uidvalidity
        self.save_state()

    def save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"uidvalidity": self.uidvalidity, "last_uid": self.last_uid}))
        tmp.replace(self.state_file)

    def advance(self, uid):
        self.attempts.pop(uid, None)
        self.delivered.discard(uid)
        if uid > self.last_uid:
            self.last_uid = uid
            self.save_state()

    # ----------------------------------------------------------- processing --

    def process_pending(self, client):
        # "N:*" always matches the highest UID even if it is below N, so filter again.
        uids = sorted(u for u in client.search(["UID", f"{self.last_uid + 1}:*"]) if u > self.last_uid)
        if not uids:
            return
        flags = client.get_flags(uids)
        for uid in uids:
            if self.stop.is_set():
                return
            fl = {_s(f) for f in flags.get(uid, ())}
            if uid not in flags:
                self.advance(uid)  # vanished meanwhile
                continue
            if FLAG_DONE in fl or FLAG_FAILED in fl:
                # Our own output (translated copies, notices) or already handled.
                self.advance(uid)
                continue
            self.process_uid(client, uid)

    def process_uid(self, client, uid):
        """Process one message. IMAP errors propagate (we reconnect and retry the uid);
        everything else is handled as a per-message failure."""
        self.attempts[uid] = self.attempts.get(uid, 0) + 1
        if self.attempts[uid] > MAX_ATTEMPTS:
            log.error("uid %s: giving up after %s IMAP errors; leaving it in %s",
                      uid, MAX_ATTEMPTS, self.a.watch)
            self.advance(uid)
            return
        data = client.fetch([uid], ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"]).get(uid)
        if not data or b"BODY[]" not in data:
            log.warning("uid %s: vanished before it could be fetched", uid)
            self.advance(uid)
            return
        try:
            self.handle(client, uid, data)
        except (IMAPClient.Error, OSError):
            raise  # connection-level problem: reconnect and retry this uid
        except Exception as e:
            self.fail(client, uid, data, e)
        self.advance(uid)

    def handle(self, client, uid, data):
        raw = data[b"BODY[]"]
        orig = parse(raw)
        subject = str(orig.get("Subject", "") or "")

        if uid in self.delivered:
            # Translated copy went out, then IMAP broke before the original was moved.
            self._file_original(client, uid)
            return

        text, _ = get_bodies(orig)
        lang = detect_message_lang(text, subject)
        if lang is None or lang in self.a.skip_langs:
            self.pass_through(client, uid, lang, subject)
            return

        if len(text) > self.tcfg.max_chars:
            text = text[: self.tcfg.max_chars] + "\n\n[… truncated for translation …]"
        t0 = time.monotonic()
        try:
            (subject_tr, body_tr), src = self.translator.translate(
                [subject or "(no subject)", text or "(empty message)"])
        except Exception as e:
            self.translator_failures += 1
            if isinstance(e, TranslatorError):
                raise
            raise TranslatorError(f"{e.__class__.__name__}: {e}") from e
        log.info("uid %s: %s responded in %.1fs", uid, self.translator.name, time.monotonic() - t0)
        if self.translator_failures >= OUTAGE_THRESHOLD:
            log.info("translator recovered after %s consecutive failures; error notices resumed",
                     self.translator_failures)
        self.translator_failures = 0

        src = (src or lang).lower()
        if src.split("-")[0] in self.a.skip_langs:
            # Local detection was wrong; the translator knows better.
            self.pass_through(client, uid, src, subject, note=f"(translator says {src}, local guess {lang})")
            return

        new = build_translated(orig, raw, subject_tr, body_tr, src, self.tcfg.target_lang,
                               self.translator.name, self.a.attach_original)
        new_flags = [_s(f) for f in data.get(b"FLAGS", ())
                     if _s(f) not in (SEEN, "\\Recent", FLAG_DONE, FLAG_FAILED)] + [FLAG_DONE]
        client.append(self.a.deliver, to_bytes(new), flags=new_flags, msg_time=data.get(b"INTERNALDATE"))
        self.delivered.add(uid)
        self._file_original(client, uid)
        log.info("uid %s: translated %s→%s: %s", uid, src, self.tcfg.target_lang, short(subject))

    def _file_original(self, client, uid):
        client.add_flags([uid], [SEEN])
        self.move(client, uid, self.a.originals)

    def pass_through(self, client, uid, lang, subject, note=""):
        client.add_flags([uid], [FLAG_DONE])
        if self.a.watch != self.a.deliver:
            self.move(client, uid, self.a.deliver)
        log.info("uid %s: language=%s, pass-through%s: %s", uid, lang or "unknown",
                 f" {note}" if note else "", short(subject))

    def fail(self, client, uid, data, exc):
        """Deliver the original untranslated, tag it, and tell the user (unless rate-limited)."""
        tb = "".join(traceback.format_exception(exc))
        orig_from = orig_subject = orig_date = "?"
        try:
            orig = parse(data[b"BODY[]"])
            orig_from, orig_subject, orig_date = (str(orig.get(h, "") or "") for h in ("From", "Subject", "Date"))
        except Exception:
            pass
        log.error("uid %s: FAILED (%s): %s\n%s", uid, exc, short(orig_subject), tb)

        client.add_flags([uid], [FLAG_FAILED])
        if self.a.watch != self.a.deliver:
            self.move(client, uid, self.a.deliver)

        outage = False
        if isinstance(exc, TranslatorError):
            if self.translator_failures > OUTAGE_THRESHOLD:
                log.warning("uid %s: translator still failing (%s in a row); error notice suppressed",
                            uid, self.translator_failures)
                return
            if self.translator_failures == OUTAGE_THRESHOLD:
                outage = True
                log.warning("translator failed %s times in a row; sending one notice and suppressing "
                            "further notices until a translation succeeds", OUTAGE_THRESHOLD)
        notice = build_error_notice(self.a, uid, orig_from, orig_subject, orig_date, exc, tb, outage=outage)
        client.append(self.a.deliver, to_bytes(notice), flags=[FLAG_DONE])

    def move(self, client, uid, folder):
        if client.has_capability("MOVE"):
            client.move([uid], folder)
        else:
            client.copy([uid], folder)
            client.delete_messages([uid])
            if client.has_capability("UIDPLUS"):
                client.uid_expunge([uid])
            else:
                client.expunge()
