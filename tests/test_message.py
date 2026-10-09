from translate_mail.message import (_walk_own_parts, unflow, unwrap, build_error_notice, build_translated, detect_lang, detect_message_lang,
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
    assert html_s.index("TRANSLATED BODY") < html_s.index("<hr>") < html_s.index("white-space:pre-wrap")
    assert "Hallo Lee" in html_s


def test_original_html_embedded():
    raw = make_mail(html="<html><head><title>t</title></head><body><p><b>Hallo</b> Welt</p></body></html>")
    _, new = build(raw)
    html_s = new.get_body(("html",)).get_content()
    assert "<p><b>Hallo</b> Welt</p>" in html_s
    assert "white-space:pre-wrap" not in html_s and "<title>" not in html_s


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


# ---- inline (cid:) images ---------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\nfake-logo"


def outlook_style_mail():
    """alternative[ plain, related[ html, image ] ] plus a real attachment, like Outlook sends."""
    html_body = '<html><body><p>Hallo</p><img src="cid:image001.png@01DA0000.11223344"></body></html>'
    m2 = parse(make_mail())
    m2.set_content(GERMAN)
    m2.add_alternative(html_body, subtype="html")
    m2.get_body(("html",)).add_related(PNG, maintype="image", subtype="png",
                                       cid="<image001.png@01DA0000.11223344>", filename="image001.png")
    m2.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="rechnung.pdf")
    return to_bytes(m2)


def test_inline_images_are_embedded():
    raw = outlook_style_mail()
    _, new = build(raw)
    html_part = new.get_body(("html",))
    assert 'src="cid:image001.png@01DA0000.11223344"' in html_part.get_content()
    # The HTML sits in a multipart/related together with the image, same Content-ID.
    related = [p for p in _walk_own_parts(new) if p.get_content_type() == "multipart/related"]
    assert len(related) == 1
    images = [p for p in related[0].iter_parts() if p.get_content_maintype() == "image"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<image001.png@01DA0000.11223344>"
    assert images[0].get_content() == PNG
    # Not duplicated as a regular attachment; the real attachment is still there.
    names = [a.get_filename() for a in new.iter_attachments()]
    assert names == ["rechnung.pdf", "original.eml"]
    assert to_bytes(parse(to_bytes(new))) == to_bytes(new)


def test_inline_image_at_top_level_apple_style():
    # Some clients put the image straight in multipart/mixed with a Content-ID.
    m = parse(make_mail(html='<p>Hallo <img src="cid:logo"></p>'))
    m.add_attachment(PNG, maintype="image", subtype="png", cid="<logo>", filename="logo.png", disposition="inline")
    raw = to_bytes(m)
    _, new = build(raw)
    related = [p for p in _walk_own_parts(new) if p.get_content_type() == "multipart/related"]
    assert related and any(p["Content-ID"] == "<logo>" for p in related[0].iter_parts())
    assert [a.get_filename() for a in new.iter_attachments()] == ["original.eml"]


def test_unreferenced_image_stays_an_attachment():
    raw = make_mail(html="<p>Hallo</p>", attachments=[("photo.jpg", b"jpeg", "image/jpeg")])
    _, new = build(raw)
    assert not [p for p in _walk_own_parts(new) if p.get_content_type() == "multipart/related"]
    assert "photo.jpg" in [a.get_filename() for a in new.iter_attachments()]


# ---- likely languages -------------------------------------------------------

def test_likely_langs_break_ties_on_uncertain_text():
    # Real py3langid results: short Swedish lines detected as exotic languages.
    assert detect_lang("Hej! Se bifogad fil.") == "pcm"            # Nigerian Pidgin
    assert detect_lang("Hej! Se bifogad fil.", likely=["sv"]) == "sv"
    assert detect_lang("Faktura nr 2024-03-15, OCR 12345", likely=["sv"]) == "sv"


def test_likely_langs_do_not_override_confident_detection():
    assert detect_lang("Merci beaucoup pour votre aide, à demain", likely=["sv"]) == "fr"
    assert detect_lang(GERMAN, likely=["sv"]) == "de"


# ---- hard-wrapped plain text --------------------------------------------------

WRAPPED = """Hej Lee,

tack för ditt mejl. Jag ville bara berätta att mötet på torsdag har
flyttats till fredag klockan tio, eftersom flera av oss är bortresta i
början av veckan. Hoppas att det fungerar för dig också.

Det här behöver vi gå igenom:
- budgeten för nästa kvartal
- planeringen av sommarens resa

Med vänliga hälsningar
Anna
--
Anna Svensson, Exempelbolaget AB"""


def test_unwrap_joins_wrapped_paragraph_only():
    out = unwrap(WRAPPED).split("\n")
    assert out[2] == ("tack för ditt mejl. Jag ville bara berätta att mötet på torsdag har flyttats till "
                      "fredag klockan tio, eftersom flera av oss är bortresta i början av veckan. Hoppas "
                      "att det fungerar för dig också.")
    # Lists, greetings, signature and short lines keep their breaks.
    assert out[4:] == WRAPPED.split("\n")[6:]
    assert out[:2] == ["Hej Lee,", ""]


def test_unwrap_leaves_short_line_text_alone():
    text = "Adress:\nStorgatan 1\n123 45 Stockholm\nTel 08-123 45"
    assert unwrap(text) == text


def test_unwrap_respects_quote_depth():
    text = ("> Detta är en citerad rad som är ganska lång och bröts av avsändarens\n"
            "> e-postprogram vid ungefär sjuttio tecken för att passa.\n"
            "Mitt svar kommer här på en egen rad som inte ska slås ihop med citatet.")
    out = unwrap(text).split("\n")
    assert len(out) == 2 and out[0].startswith("> Detta") and out[0].endswith("passa.")


def test_unflow_format_flowed():
    text = ("Det h\u00e4r \u00e4r en l\u00e5ng rad som \n"
            "forts\u00e4tter h\u00e4r.\n"
            "\n"
            "> citerad text som \n"
            "> forts\u00e4tter\n"
            "-- \n"
            "Anna")
    assert unflow(text).split("\n") == ["Det här är en lång rad som fortsätter här.", "",
                                         "> citerad text som fortsätter", "-- ", "Anna"]
    assert unflow("ab \ncd", delsp=True) == "abcd"


def test_get_bodies_unwraps_plain_and_flowed():
    m = parse(make_mail(body=WRAPPED))
    text, _ = get_bodies(m)
    assert "torsdag har flyttats" in text
    flowed = parse(make_mail(body="Rad ett som \nfortsätter.\n"))
    flowed.get_body(("plain",)).set_param("format", "flowed")
    assert get_bodies(flowed)[0] == "Rad ett som fortsätter."


def test_html_keeps_original_styles_and_viewport():
    raw = make_mail(html="<html><head><style>.wrap{width:100%}</style></head><body><div class=wrap>Hallo</div></body></html>")
    _, new = build(raw)
    h = new.get_body(("html",)).get_content()
    assert "<style>.wrap{width:100%}</style>" in h
    assert 'name="viewport"' in h
