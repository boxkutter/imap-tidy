"""Reading mail bodies, detecting language, and building the messages we append."""

import html
import re
from email import message_from_bytes, policy
from email.message import EmailMessage
from datetime import datetime, timezone
from email.utils import format_datetime, formataddr, make_msgid

from urllib.parse import unquote

from py3langid.langid import MODEL_FILE, LanguageIdentifier

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


def html_head_styles(s: str) -> str:
    """The original's <style> blocks, so its layout survives being embedded."""
    head = re.search(r"(?is)<head\b.*?</head>", s)
    return "".join(re.findall(r"(?is)<style\b.*?</style>", head.group(0))) if head else ""


# --- undoing hard line wraps ----------------------------------------------------
# Plain-text mail is usually wrapped at ~72-78 characters by the sender's client.
# Mail apps reflow it to the screen width, but our translated copy would keep the
# breaks (in the translation and in the embedded original), so text only filled
# part of the screen. Translators also do better on whole sentences.

_QUOTE = re.compile(r"^((?:>\s?)*)")
_LIST_ITEM = re.compile(r"^\s*([-*•–]|\d+[.)]|[a-zA-Z][.)])\s")


def unflow(text: str, delsp: bool = False) -> str:
    """Decode RFC 3676 format=flowed: a line ending in a space continues on the next."""
    out, cur, cur_depth = [], None, 0
    for line in text.split("\n"):
        quote = _QUOTE.match(line).group(1)
        depth = quote.count(">")
        body = line[len(quote):]
        if body.startswith(" "):           # space-stuffing
            body = body[1:]
        if cur is not None and depth == cur_depth:
            cur += body
        else:
            if cur is not None:
                out.append(cur)
            cur, cur_depth = ("> " * depth if depth else "") + body, depth
        soft = body.endswith(" ") and body.rstrip() != "--"
        if soft:
            if delsp:
                cur = cur[:-1]
        else:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return "\n".join(out)


def unwrap(text: str) -> str:
    """Join lines that were broken only because they reached the sender's wrap width.

    A line counts as wrapped if the next line's first word would not have fitted on
    it (classic greedy wrapping). Short lines, blank lines, list items, quotes of a
    different depth and signatures keep their breaks.
    """
    lines = text.split("\n")
    # Lines of prose longer than 100 characters mean the sender did not hard-wrap.
    # (Long lines without spaces are URLs and the like; wrapping can't break those.)
    if any(len(l) > 100 and " " in l.strip() for l in lines):
        return text
    lengths = [len(l.rstrip()) for l in lines if l.strip() and len(l.rstrip()) <= 100]
    if len(lengths) < 2:
        return text
    width = max(lengths)                     # the wrap width the sender used
    if width < 50:
        return text                          # only short lines: addresses, lists...
    out = [lines[0]]
    for i, nxt in enumerate(lines[1:]):
        prev = lines[i]                      # the original line before `nxt`, for the fit test
        pq, nq = _QUOTE.match(prev).group(1), _QUOTE.match(nxt).group(1)
        nbody = nxt[len(nq):]
        first_word = nbody.split(" ", 1)[0] if nbody.strip() else ""
        joinable = (
            prev.strip() and nbody.strip()
            and pq.replace(" ", "") == nq.replace(" ", "")
            and prev.rstrip() != "--" and not prev.startswith("-- ")
            and not _LIST_ITEM.match(nbody)
            and len(prev.rstrip()) + 1 + len(first_word) > width
            and len(prev.rstrip()) <= width
        )
        if joinable:
            out[-1] = out[-1].rstrip() + " " + nbody.lstrip()
        else:
            out.append(nxt)
    return "\n".join(out)


def get_bodies(msg) -> tuple[str, str | None]:
    """Return (plain_text, html_or_None). Plain text is derived from HTML if needed,
    with the sender's hard line wraps undone."""
    plain = msg.get_body(preferencelist=("plain",))
    htmlp = msg.get_body(preferencelist=("html",))
    html_s = htmlp.get_content() if htmlp is not None else None
    if plain is not None:
        text = plain.get_content().replace("\r\n", "\n")
        if (plain.get_param("format") or "").lower() == "flowed":
            text = unflow(text, (plain.get_param("delsp") or "").lower() == "yes")
        else:
            text = unwrap(text)
    elif html_s:
        text = html_to_text(html_s)
    else:
        text = ""
    return text.strip(), html_s


# Normalised probabilities, so "how sure" can be compared across languages.
_IDENTIFIER = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)

# Below this confidence the top guess is considered uncertain, and a language from
# `likely` wins if it is a reasonably close runner-up (at least LIKELY_RATIO of the top).
UNCERTAIN = 0.6
LIKELY_RATIO = 0.2


def detect_lang(text: str, likely=()) -> str | None:
    """ISO 639-1 code of `text`, or None if there is too little text to tell.

    `likely` lists languages you expect to receive: when detection is unsure,
    one of those is preferred over an exotic or similar-looking alternative.
    """
    sample = " ".join(text[:4000].split())
    if len(sample) < 20:
        return None
    ranked = _IDENTIFIER.rank(sample)
    top, p = ranked[0]
    if likely and top not in likely and p < UNCERTAIN:
        probs = dict(ranked)
        best = max(likely, key=lambda lang: probs.get(lang, 0.0))
        if probs.get(best, 0.0) >= LIKELY_RATIO * p:
            return best
    return top.lower()


def detect_message_lang(text: str, subject: str, likely=()) -> str | None:
    """Body first, subject as fallback."""
    return detect_lang(text, likely) or detect_lang(subject, likely)


def _referenced_cids(html_s: str) -> set[str]:
    return {unquote(m).strip("<>").lower() for m in re.findall(r"""cid:([^"'\s>)]+)""", html_s, flags=re.I)}


def _walk_own_parts(msg):
    """Like msg.walk(), but without descending into attached messages (message/rfc822)."""
    yield msg
    if msg.is_multipart() and msg.get_content_maintype() != "message":
        for sub in msg.iter_parts():
            yield from _walk_own_parts(sub)


def _inline_parts(orig, html_s):
    """Parts (anywhere in the message) whose Content-ID the HTML body references."""
    if not html_s:
        return []
    wanted = _referenced_cids(html_s)
    found = []
    for part in _walk_own_parts(orig):
        cid = str(part.get("Content-ID", "") or "").strip().strip("<>").lower()
        if cid and cid in wanted and not part.is_multipart():
            found.append(part)
    return found


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
    # Plain-text originals are shown in the reader's normal font and reflow to the
    # screen width, as the mail app shows them, rather than as a monospace <pre> block.
    original_html = (html_body_inner(html_s) if html_s
                     else f"<div style=\"white-space:pre-wrap;overflow-wrap:anywhere\">"
                          f"{html.escape(text)}</div>")
    # Keep the original's <style> blocks and a mobile viewport so a newsletter's
    # responsive layout still works once it is embedded below the translation.
    head = ("<meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            + (html_head_styles(html_s) if html_s else ""))
    html_out = (
        f"<!DOCTYPE html><html><head>{head}</head><body>"
        "<div style=\"border-left:4px solid #3b82f6;padding:8px 12px;margin-bottom:16px;"
        "background:#f3f6fb;color:#111\">"
        f"<div style=\"font-size:12px;color:#555;margin-bottom:6px\">"
        f"Translated {html.escape(src)} → {html.escape(target)} by mail-translate</div>"
        f"<div>{html_tr}</div></div>"
        f"<hr><p style=\"color:#777;font-size:12px\"><i>{html.escape(divider)}</i></p>"
        f"{original_html}</body></html>"
    )
    new.add_alternative(html_out, subtype="html")

    # Inline images (<img src="cid:...">) go into a multipart/related next to the HTML,
    # with their original Content-ID, so the embedded original renders as received.
    inline = _inline_parts(orig, html_s)
    if inline:
        html_part = new.get_body(("html",))
        for part in inline:
            html_part.add_related(
                part.get_payload(decode=True) or b"",
                maintype=part.get_content_maintype(),
                subtype=part.get_content_subtype(),
                cid=part["Content-ID"].strip(),
                filename=part.get_filename(),
            )
    inline_ids = {id(p) for p in inline}

    # Carry over attachments so they stay one click away.
    for part in orig.iter_attachments():
        if id(part) in inline_ids:
            continue  # already embedded above
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
