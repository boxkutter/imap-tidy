from translate_mail.message import (build_error_notice, build_translated, detect_lang, detect_message_lang,
                                    get_bodies, html_to_text, parse, to_bytes)

from .conftest import ENGLISH, GERMAN, make_mail


def build(raw, attach_original=True):
    orig = parse(raw)
    new = build_translated(orig, raw, "Meeting on Thursday", "TRANSLATED BODY", "de", "en", "deepl",
                           attach_original=attach_original)
    return orig, parse(to_bytes(new))


# ---- language detection ---------------------------------------------------

def test_detect_languages():
    assert detect_lang(GERMAN) == "de"
    assert detect_lang(ENGLISH) == "en"
    assert detect_lang("Bonjour, je voulais vous dire que la réunion aura lieu jeudi à dix heures.") == "fr"


def test_detect_too_short_is_none():
    assert detect_lang("ok") is None
    assert detect_lang("   \n  ") is None


def test_detect_falls_back_to_subject():
    assert detect_message_lang("", "Die Rechnung für den Monat Januar ist angekommen") == "de"
    assert detect_message_lang(ENGLISH, "Die Rechnung für den Monat Januar") == "en"  # body wins


def test_html_to_text():
    assert html_to_text("<html><head><style>p{}</style></head><p>Hallo&nbsp;Welt</p><br>x") == "Hallo Welt\n\nx"


def test_get_bodies_html_only():
    raw = make_mail(body="ignored")
    m = parse(raw)
    m.set_content("<p>Guten Tag</p>", subtype="html")
    text, html_s = get_bodies(m)
    assert text == "Guten Tag" and "<p>" in html_s


# ---- translated message ---------------------------------------------------

def test_headers_preserved():
    raw = make_mail().replace(b"Date: Mon, 01 Jan", b"Date: Mon, 1 Jan")
    orig, new = build(raw)
    for h in ("From", "To", "Date", "Message-ID", "In-Reply-To", "References"):
        assert new[h] == orig[h], h
    # Date must stay byte-identical (not re-rendered as "01 Jan").
    assert b"Date: Mon, 1 Jan 2024 10:00:00 +0100" in to_bytes(parse(to_bytes(new)))


def test_subject_tagging_and_x_headers():
    _, new = build(make_mail())
    assert new["Subject"] == "[de→EN] Meeting on Thursday"
    assert new["X-Original-Subject"] == "Treffen am Donnerstag"
    assert new["X-Translated-From"] == "de"
    assert new["X-Translated-By"] == "mail-translate/deepl"
    assert len(new.get_all("Subject")) == 1


def test_body_plain_and_html():
    _, new = build(make_mail())
    plain = new.get_body(("plain",)).get_content()
    assert plain.index("TRANSLATED BODY") < plain.index("──── Original message (de) ────") < plain.index("Hallo Lee")
    html_s = new.get_body(("html",)).get_content()
    assert html_s.index("TRANSLATED BODY") < html_s.index("<hr>") < html_s.index("<pre")
    assert "Hallo Lee" in html_s


def test_original_html_embedded():
    raw = make_mail(html="<html><head><title>t</title></head><body><p><b>Hallo</b> Welt</p></body></html>")
    _, new = build(raw)
    html_s = new.get_body(("html",)).get_content()
    assert "<p><b>Hallo</b> Welt</p>" in html_s
    assert "<pre" not in html_s and "<title>" not in html_s


def test_html_translation_is_escaped():
    raw = make_mail()
    orig = parse(raw)
    new = parse(to_bytes(build_translated(orig, raw, "s", "<script>x</script>", "de", "en", "deepl")))
    assert "<script>x" not in new.get_body(("html",)).get_content()


def test_attachments_carried_over_and_original_eml():
    pdf = b"%PDF-1.4 fake"
    raw = make_mail(attachments=[("rechnung.pdf", pdf, "application/pdf")])
    _, new = build(raw)
    atts = {a.get_filename(): a for a in new.iter_attachments()}
    assert set(atts) == {"rechnung.pdf", "original.eml"}
    assert atts["rechnung.pdf"].get_content() == pdf
    assert atts["original.eml"].get_content_type() == "message/rfc822"
    inner = atts["original.eml"].get_payload(0)
    assert inner["Message-ID"] == "<orig-1@example.de>"
    assert inner["Subject"] == "Treffen am Donnerstag"
    assert "Hallo Lee" in inner.get_body(("plain",)).get_content()


def test_attach_original_off():
    _, new = build(make_mail(), attach_original=False)
    assert [a.get_filename() for a in new.iter_attachments()] == []


def test_attached_message_rfc822_kept():
    inner = make_mail(body="Weitergeleitete Nachricht", subject="Fwd")
    m = parse(make_mail())
    m.add_attachment(parse(inner), filename="forwarded.eml")
    raw = to_bytes(m)
    _, new = build(raw)
    names = [a.get_filename() for a in new.iter_attachments()]
    assert names == ["forwarded.eml", "original.eml"]


def test_round_trip_is_stable():
    raw = make_mail(html="<p>Hallo</p>", attachments=[("a.txt", b"abc", "text/plain")])
    _, new = build(raw)
    again = parse(to_bytes(new))
    assert to_bytes(again) == to_bytes(new)
    assert b"\r\n" in to_bytes(new) and b"\n" not in to_bytes(new).replace(b"\r\n", b"")


def test_non_utf8_and_encoded_subject():
    raw = ("From: a@example.fr\r\nSubject: =?iso-8859-1?q?R=E9union?=\r\nDate: Mon, 1 Jan 2024 10:00:00 +0100\r\n"
           "Content-Type: text/plain; charset=iso-8859-1\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
           ).encode() + "La réunion aura lieu jeudi à dix heures.\r\n".encode("latin-1")
    orig = parse(raw)
    assert orig["Subject"] == "Réunion"
    new = parse(to_bytes(build_translated(orig, raw, "Meeting", "The meeting", "fr", "en", "deepl")))
    assert new["X-Original-Subject"] == "Réunion"
    assert "La réunion aura lieu" in new.get_body(("plain",)).get_content()


def test_error_notice(account):
    n = parse(to_bytes(build_error_notice(account, 7, "hans@example.de", "Hallo", "Mon, 1 Jan 2024",
                                          RuntimeError("boom"), "Traceback...")))
    assert n["From"] == "mail-translate <mail-translate@mail.example.com>"
    assert n["Subject"] == "[mail-translate] Could not translate: Hallo"
    body = n.get_content()
    for s in ("test", "7", "hans@example.de", "boom", "Traceback...", "$TranslateFailed", "INBOX"):
        assert s in body
