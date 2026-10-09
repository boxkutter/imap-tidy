"""Reading mail bodies, detecting language, and building the messages we append."""

import html
import re
from email import message_from_bytes, policy
from email.message import EmailMessage
from datetime import datetime, timezone
from email.utils import format_datetime, formataddr, make_msgid

import py3langid as langid

# Headers regenerated on the translated copy; everything else is copied verbatim.
REGENERATED = {"content-type", "content-transfer-encoding", "content-disposition",
               "content-id", "content-description", "mime-version", "subject"}

# CRLF line endings, as IMAP APPEND expects; no line-length refolding.
WIRE = policy.SMTP.clone(max_line_length=None)


def parse(raw: bytes) -> EmailMessage:
    return message_from_bytes(raw, policy=policy.default)


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table|blockquote)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def html_body_inner(s: str) -> str:
    """Strip <html>/<head>/<body> wrappers so the original can be embedded."""
    s = re.sub(r"(?is)<!doctype[^>]*>", "", s)
    s = re.sub(r"(?is)<head\b.*?</head>", "", s)
    s = re.sub(r"(?is)</?(html|body)\b[^>]*>", "", s)
    return s


def get_bodies(msg) -> tuple[str, str | None]:
    """Return (plain_text, html_or_None). Plain text is derived from HTML if needed."""
    plain = msg.get_body(preferencelist=("plain",))
    htmlp = msg.get_body(preferencelist=("html",))
    html_s = htmlp.get_content() if htmlp is not None else None
    if plain is not None:
        text = plain.get_content()
    elif html_s:
        text = html_to_text(html_s)
    else:
        text = ""
    return text.strip(), html_s


def detect_lang(text: str) -> str | None:
    """ISO 639-1 code of `text`, or None if there is too little text to tell."""
    sample = " ".join(text[:4000].split())
    if len(sample) < 20:
        return None
    lang, _ = langid.classify(sample)
    return lang.lower()


def detect_message_lang(text: str, subject: str) -> str | None:
    """Body first, subject as fallback."""
    return detect_lang(text) or detect_lang(subject)


def build_translated(orig, raw: bytes, subject_tr: str, body_tr: str, src: str, target: str,
                     translator_name: str, attach_original: bool = True) -> EmailMessage:
    text, html_s = get_bodies(orig)
    new = EmailMessage()

    # Keep every header (From, To, Date, Message-ID, References, ...) byte-for-byte so
    # replies and threading behave exactly as for the original. Only MIME headers are
    # rebuilt. raw_items() + _headers avoids re-parsing/re-rendering (which would e.g.
    # rewrite "1 Jan" as "01 Jan" or choke on slightly malformed headers).
    for k, v in orig.raw_items():
        if k.lower() not in REGENERATED:
            new._headers.append((k, v))
    new["Subject"] = f"[{src}→{target.split('-')[0].upper()}] {subject_tr}".strip()
    new["X-Original-Subject"] = orig.get("Subject", "") or ""
    new["X-Translated-From"] = src
    new["X-Translated-By"] = f"mail-translate/{translator_name}"

    divider = f"──── Original message ({src}) ────"
    new.set_content(f"{body_tr}\n\n{divider}\n\n{text}\n")

    html_tr = "<br>\n".join(html.escape(line) for line in body_tr.splitlines())
    original_html = (html_body_inner(html_s) if html_s
                     else f"<pre style=\"white-space:pre-wrap\">{html.escape(text)}</pre>")
    html_out = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
        "<div style=\"border-left:4px solid #3b82f6;padding:8px 12px;margin-bottom:16px;"
        "background:#f3f6fb;color:#111;font-family:sans-serif\">"
        f"<div style=\"font-size:12px;color:#555;margin-bottom:6px\">"
        f"Translated {html.escape(src)} → {html.escape(target)} by mail-translate</div>"
        f"<div>{html_tr}</div></div>"
        f"<hr><p style=\"color:#777;font-size:12px\"><i>{html.escape(divider)}</i></p>"
        f"{original_html}</body></html>"
    )
    new.add_alternative(html_out, subtype="html")

    # Carry over attachments so they stay one click away.
    for part in orig.iter_attachments():
        filename = part.get_filename() or "attachment"
        if part.get_content_type() == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else None
            if inner is not None:
                new.add_attachment(inner, filename=filename)
            continue
        new.add_attachment(
            part.get_payload(decode=True) or b"",
            maintype=part.get_content_maintype(),
            subtype=part.get_content_subtype(),
            filename=filename,
        )
    if attach_original:
        # Re-parse with compat32 so the embedded copy is serialised as close to the raw bytes as possible.
        new.add_attachment(message_from_bytes(raw, policy=policy.compat32), filename="original.eml")
    return new


def build_error_notice(account, uid, orig_from, orig_subject, orig_date, error, tb, outage=False):
    """A plain-text notice appended to the account's deliver folder."""
    msg = EmailMessage()
    msg["From"] = formataddr(("mail-translate", f"mail-translate@{account.host}"))
    msg["To"] = account.user
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid("mail-translate", domain=account.host)
    if outage:
        msg["Subject"] = "[mail-translate] Translator is failing; notices paused"
    else:
        msg["Subject"] = f"[mail-translate] Could not translate: {orig_subject or '(no subject)'}"

    lines = []
    if outage:
        lines += [
            "The translator has failed 3 times in a row for this account.",
            "Further failure notices are suppressed until a translation succeeds again.",
            "Mail keeps being delivered untranslated (tagged $TranslateFailed) in the meantime.",
            "",
            "Most recent failure:",
            "",
        ]
    lines += [
        f"Account:  {account.name} ({account.user})",
        f"UID:      {uid} in {account.watch}",
        f"From:     {orig_from}",
        f"Subject:  {orig_subject}",
        f"Date:     {orig_date}",
        "",
        f"Error:    {error}",
        "",
        "The original message was delivered to "
        f"{account.deliver} untranslated and tagged with the IMAP keyword $TranslateFailed.",
        "",
        "Traceback:",
        tb,
    ]
    msg.set_content("\n".join(lines))
    return msg


def to_bytes(msg: EmailMessage) -> bytes:
    return msg.as_bytes(policy=WIRE)
