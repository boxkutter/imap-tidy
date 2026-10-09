"""Translation backends.

Each translator has ``translate(texts, source=None) -> (translations, source_lang)``
and a ``name``. ``source`` is a language we are already sure of; None means auto-detect. Settings come from environment variables so API keys stay in `.env`.
"""

import logging
import re
import time

import requests

from .config import ConfigError

log = logging.getLogger(__name__)

RETRY_DELAY = 2.0  # seconds before the single retry


class TranslatorError(Exception):
    """The translation service failed (network, HTTP error, bad response)."""


def post_json(url, payload, headers=None, timeout=60, retry_delay=None):
    """POST JSON and return the decoded response, retrying once on transient errors.

    Connection errors, timeouts, 429 and 5xx are retried; other 4xx are not
    (a bad API key won't fix itself in two seconds).
    """
    retry_delay = RETRY_DELAY if retry_delay is None else retry_delay
    for attempt in (1, 2):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            err = TranslatorError(f"{url}: {e.__class__.__name__}: {e}")
            transient = True
        else:
            if r.ok:
                try:
                    return r.json()
                except ValueError:
                    raise TranslatorError(f"{url}: response is not JSON: {r.text[:200]!r}") from None
            err = TranslatorError(f"{url}: HTTP {r.status_code}: {r.text[:300]}")
            transient = r.status_code == 429 or r.status_code >= 500
        if attempt == 1 and transient:
            log.warning("translator request failed (%s), retrying in %.0fs", err, retry_delay)
            time.sleep(retry_delay)
            continue
        raise err
    raise AssertionError("unreachable")


class DeepL:
    name = "deepl"

    def __init__(self, target_lang, env):
        self.key = env.get("DEEPL_API_KEY", "")
        if not self.key:
            raise ConfigError("translator 'deepl' needs DEEPL_API_KEY in the environment")
        self.url = env.get("DEEPL_URL") or "https://api-free.deepl.com/v2/translate"
        self.target = target_lang.upper()

    def translate(self, texts, source=None):
        payload = {"text": texts, "target_lang": self.target}
        if source:
            payload["source_lang"] = source.upper()
        data = post_json(
            self.url,
            payload,
            headers={"Authorization": f"DeepL-Auth-Key {self.key}"},
        )
        try:
            items = data["translations"]
            return [t["text"] for t in items], items[0]["detected_source_language"].lower()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise TranslatorError(f"unexpected DeepL response: {str(data)[:300]}") from None


class LibreTranslate:
    name = "libretranslate"

    def __init__(self, target_lang, env):
        self.url = (env.get("LT_URL") or "http://libretranslate:5000").rstrip("/") + "/translate"
        self.key = env.get("LT_API_KEY", "")
        self.target = target_lang.split("-")[0]

    def translate(self, texts, source=None):
        if source:
            return [self._text(self._post(t, source)) for t in texts], source.lower()
        # LibreTranslate detects the language per request, and is unreliable on short
        # text like a subject line. So translate the longest text (the body) first with
        # auto-detection, then translate the rest *from that language*: the label and
        # the subject translation then agree with the body.
        out, src = [None] * len(texts), ""
        for i in sorted(range(len(texts)), key=lambda i: -len(texts[i])):
            data = self._post(texts[i], src or "auto")
            out[i] = self._text(data)
            src = src or ((data.get("detectedLanguage") or {}).get("language") or "")
        return out, src.lower()

    @staticmethod
    def _text(data):
        try:
            return data["translatedText"]
        except (KeyError, TypeError):
            raise TranslatorError(f"unexpected LibreTranslate response: {str(data)[:300]}") from None

    def _post(self, text, source):
        payload = {"q": text, "source": source, "target": self.target, "format": "text"}
        if self.key:
            payload["api_key"] = self.key
        return post_json(self.url, payload, timeout=120)


SEP = "<<<SEP>>>"


class LLM:
    """Any OpenAI-compatible /chat/completions endpoint (OpenAI, Ollama, vLLM, ...)."""

    name = "llm"

    def __init__(self, target_lang, env):
        self.url = env.get("LLM_URL", "").rstrip("/")
        self.model = env.get("LLM_MODEL", "")
        if not self.url or not self.model:
            raise ConfigError("translator 'llm' needs LLM_URL and LLM_MODEL in the environment "
                              "(e.g. LLM_URL=http://ollama:11434/v1)")
        if not self.url.endswith("/chat/completions"):
            self.url += "/chat/completions"
        self.key = env.get("LLM_API_KEY", "")
        self.target = target_lang

    def translate(self, texts, source=None):
        hint = f"The source language is most likely '{source}'. " if source else ""
        prompt = (
            f"Translate the following email into the language with code '{self.target}'. {hint}"
            f"Preserve line breaks, lists and formatting. Do not add commentary. "
            f"The input has {len(texts)} sections separated by a line containing only {SEP}; "
            f"return exactly {len(texts)} translated sections separated the same way. "
            f"On the very first line, before anything else, write 'LANG: <two-letter ISO 639-1 "
            f"code of the source language>'.\n\n" + f"\n{SEP}\n".join(texts)
        )
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else None
        data = post_json(
            self.url,
            {"model": self.model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]},
            headers=headers,
            timeout=300,
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise TranslatorError(f"unexpected LLM response: {str(data)[:300]}") from None
        return parse_llm_output(content, len(texts))


def parse_llm_output(content, expected):
    content = content.strip()
    # Some models wrap the whole answer in a code fence.
    content = re.sub(r"^```[a-z]*\n(.*)\n```$", r"\1", content, flags=re.S)
    m = re.match(r"\s*LANG:\s*([A-Za-z-]+)[^\n]*\n?", content)
    src = m.group(1).lower() if m else ""
    body = content[m.end():] if m else content
    parts = [p.strip() for p in body.split(SEP)]
    if len(parts) != expected:
        raise TranslatorError(f"LLM returned {len(parts)} sections, expected {expected}")
    return parts, src


PROVIDERS = {"deepl": DeepL, "libretranslate": LibreTranslate, "llm": LLM}


def make_translator(tcfg, env):
    return PROVIDERS[tcfg.provider](tcfg.target_lang, env)
