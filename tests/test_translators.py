from unittest import mock

import pytest
import requests

from translate_mail.config import ConfigError, TranslatorConfig
from translate_mail.translators import (LLM, DeepL, LibreTranslate, TranslatorError, make_translator,
                                        parse_llm_output, post_json)


def resp(status=200, json_data=None, text=""):
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    r.ok = status < 400
    r.text = text or str(json_data)
    r.json.return_value = json_data
    return r


@pytest.fixture(autouse=True)
def no_sleep():
    with mock.patch("translate_mail.translators.time.sleep") as s:
        yield s


def test_deepl_request_and_parse():
    t = DeepL("en", {"DEEPL_API_KEY": "k:fx"})
    data = {"translations": [{"detected_source_language": "DE", "text": "Hello"},
                             {"detected_source_language": "DE", "text": "World"}]}
    with mock.patch("requests.post", return_value=resp(json_data=data)) as post:
        assert t.translate(["Hallo", "Welt"]) == (["Hello", "World"], "de")
    url, = post.call_args.args
    assert url == "https://api-free.deepl.com/v2/translate"
    assert post.call_args.kwargs["headers"] == {"Authorization": "DeepL-Auth-Key k:fx"}
    assert post.call_args.kwargs["json"] == {"text": ["Hallo", "Welt"], "target_lang": "EN"}
    assert post.call_args.kwargs["timeout"]


def test_deepl_needs_key():
    with pytest.raises(ConfigError, match="DEEPL_API_KEY"):
        DeepL("en", {})


def test_deepl_custom_url():
    assert DeepL("en", {"DEEPL_API_KEY": "k", "DEEPL_URL": "https://api.deepl.com/v2/translate"}).url \
        == "https://api.deepl.com/v2/translate"


def test_deepl_bad_response():
    with mock.patch("requests.post", return_value=resp(json_data={"oops": 1})):
        with pytest.raises(TranslatorError, match="unexpected DeepL"):
            DeepL("en", {"DEEPL_API_KEY": "k"}).translate(["x"])


def test_libretranslate():
    t = LibreTranslate("en", {"LT_URL": "http://lt:5000/", "LT_API_KEY": "abc"})
    replies = [resp(json_data={"translatedText": "Hello", "detectedLanguage": {"language": "de", "confidence": 90}}),
               resp(json_data={"translatedText": "World"})]
    with mock.patch("requests.post", side_effect=replies) as post:
        assert t.translate(["Hallo", "Welt"]) == (["Hello", "World"], "de")
    assert post.call_args_list[0].args == ("http://lt:5000/translate",)
    assert post.call_args_list[0].kwargs["json"] == {"q": "Hallo", "source": "auto", "target": "en",
                                                     "format": "text", "api_key": "abc"}


def test_libretranslate_default_url_no_key():
    t = LibreTranslate("en-gb", {})
    with mock.patch("requests.post", return_value=resp(json_data={"translatedText": "x"})) as post:
        t.translate(["y"])
    assert post.call_args.args == ("http://libretranslate:5000/translate",)
    assert "api_key" not in post.call_args.kwargs["json"]
    assert post.call_args.kwargs["json"]["target"] == "en"


def test_llm_ollama_style():
    t = LLM("en", {"LLM_URL": "http://ollama:11434/v1", "LLM_MODEL": "qwen2.5"})
    content = "LANG: de\nMeeting\n<<<SEP>>>\nHello there"
    with mock.patch("requests.post", return_value=resp(json_data={"choices": [{"message": {"content": content}}]})) as post:
        assert t.translate(["Treffen", "Hallo"]) == (["Meeting", "Hello there"], "de")
    assert post.call_args.args == ("http://ollama:11434/v1/chat/completions",)
    assert post.call_args.kwargs["headers"] is None  # no key -> no Authorization header
    assert post.call_args.kwargs["json"]["model"] == "qwen2.5"


def test_llm_with_key():
    t = LLM("en", {"LLM_URL": "https://api.openai.com/v1/", "LLM_MODEL": "m", "LLM_API_KEY": "sk"})
    assert t.url == "https://api.openai.com/v1/chat/completions"
    content = "LANG: fr\na\n<<<SEP>>>\nb"
    with mock.patch("requests.post", return_value=resp(json_data={"choices": [{"message": {"content": content}}]})) as post:
        t.translate(["x", "y"])
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer sk"}


def test_llm_needs_url_and_model():
    with pytest.raises(ConfigError, match="LLM_URL"):
        LLM("en", {"LLM_URL": "http://x"})


def test_parse_llm_output_variants():
    assert parse_llm_output("```\nLANG: es\nhola\n<<<SEP>>>\nmundo\n```", 2) == (["hola", "mundo"], "es")
    assert parse_llm_output("no lang line\n<<<SEP>>>\nb", 2) == (["no lang line", "b"], "")
    with pytest.raises(TranslatorError, match="1 sections, expected 2"):
        parse_llm_output("LANG: de\nonly one", 2)


def test_retry_once_on_5xx_then_success(no_sleep):
    with mock.patch("requests.post", side_effect=[resp(503, text="busy"), resp(json_data={"ok": 1})]) as post:
        assert post_json("http://x", {}) == {"ok": 1}
    assert post.call_count == 2
    no_sleep.assert_called_once()


def test_retry_once_on_timeout_then_fail():
    with mock.patch("requests.post", side_effect=requests.Timeout("slow")) as post:
        with pytest.raises(TranslatorError, match="Timeout"):
            post_json("http://x", {})
    assert post.call_count == 2


def test_no_retry_on_403():
    with mock.patch("requests.post", return_value=resp(403, text="Forbidden")) as post:
        with pytest.raises(TranslatorError, match="HTTP 403"):
            post_json("http://x", {})
    assert post.call_count == 1


def test_non_json_response():
    r = resp(200, text="<html>")
    r.json.side_effect = ValueError("no json")
    with mock.patch("requests.post", return_value=r):
        with pytest.raises(TranslatorError, match="not JSON"):
            post_json("http://x", {})


def test_make_translator():
    t = make_translator(TranslatorConfig(provider="libretranslate"), {})
    assert t.name == "libretranslate"
