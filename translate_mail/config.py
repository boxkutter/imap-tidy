"""Load and validate config.yml.

Secrets never live in the YAML itself: any string may contain ``${VAR}``,
which is expanded from the environment (docker compose loads `.env` into it).
"""

import os
import re
from dataclasses import dataclass, field

import yaml

PROVIDERS = ("deepl", "libretranslate", "llm")

DEFAULTS = {
    "watch": "Pending",
    "deliver": "INBOX",
    "originals": "Originals",
    "skip_langs": ["en"],
    "likely_langs": [],
    "attach_original": True,
    "process_existing": False,
}
ACCOUNT_KEYS = {"name", "host", "port", "user", "password", "verify_tls"} | set(DEFAULTS)
TRANSLATOR_KEYS = {"provider", "target_lang", "max_chars"}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


@dataclass
class TranslatorConfig:
    provider: str = "deepl"
    target_lang: str = "en"
    max_chars: int = 30000


@dataclass
class Account:
    name: str
    host: str
    user: str
    password: str = field(repr=False)
    port: int = 993
    verify_tls: bool = True
    watch: str = "Pending"
    deliver: str = "INBOX"
    originals: str = "Originals"
    skip_langs: list[str] = field(default_factory=lambda: ["en"])
    likely_langs: list[str] = field(default_factory=list)
    attach_original: bool = True
    process_existing: bool = False


@dataclass
class Config:
    translator: TranslatorConfig
    accounts: list[Account]


def expand_vars(value, env):
    """Recursively replace ${VAR} in every string. Unknown variables are an error."""
    if isinstance(value, str):
        def sub(m):
            name = m.group(1)
            if name not in env:
                raise ConfigError(f"environment variable ${{{name}}} is not set (check your .env)")
            return env[name]
        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [expand_vars(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_vars(v, env) for k, v in value.items()}
    return value


def _check_type(where, key, value, typ):
    # bool is a subclass of int; don't let "port: true" slip through.
    if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
        raise ConfigError(f"{where}: '{key}' must be {typ.__name__}, got {value!r}")


def _str(where, key, value):
    _check_type(where, key, value, str)
    if not value.strip():
        raise ConfigError(f"{where}: '{key}' must not be empty")
    return value.strip()


def _bool(where, key, value):
    _check_type(where, key, value, bool)
    return value


def _langs(where, key, value):
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where}: '{key}' must be a list of language codes, e.g. [en, sv]")
    out = []
    for v in value:
        v = v.strip().lower()
        if v not in out:
            out.append(v)
    return out


def _translator(raw) -> TranslatorConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("'translator' must be a mapping")
    unknown = set(raw) - TRANSLATOR_KEYS
    if unknown:
        raise ConfigError(f"translator: unknown key(s): {', '.join(sorted(unknown))}")
    t = TranslatorConfig()
    if "provider" in raw:
        t.provider = _str("translator", "provider", raw["provider"]).lower()
    if t.provider not in PROVIDERS:
        raise ConfigError(f"translator: provider must be one of {', '.join(PROVIDERS)}, got {t.provider!r}")
    if "target_lang" in raw:
        t.target_lang = _str("translator", "target_lang", raw["target_lang"]).lower()
    if "max_chars" in raw:
        _check_type("translator", "max_chars", raw["max_chars"], int)
        if raw["max_chars"] < 100:
            raise ConfigError("translator: max_chars must be at least 100")
        t.max_chars = raw["max_chars"]
    return t


def _account(i, raw, defaults, target_lang) -> Account:
    if not isinstance(raw, dict):
        raise ConfigError(f"accounts[{i}] must be a mapping")
    where = f"accounts[{i}]" + (f" ({raw['name']})" if isinstance(raw.get("name"), str) else "")
    unknown = set(raw) - ACCOUNT_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    for key in ("name", "host", "user", "password"):
        if key not in raw:
            raise ConfigError(f"{where}: '{key}' is required")
    merged = {**defaults, **raw}

    port = merged.get("port", 993)
    _check_type(where, "port", port, int)
    if not 0 < port < 65536:
        raise ConfigError(f"{where}: port {port} is out of range")

    skip_langs = sorted(set(_langs(where, "skip_langs", merged["skip_langs"])))
    likely_langs = _langs(where, "likely_langs", merged["likely_langs"])
    # Mail already in the target language never needs translating.
    target_base = target_lang.split("-")[0]
    if target_base not in skip_langs:
        skip_langs.append(target_base)

    # Validate the password but keep it verbatim (leading/trailing spaces are legal).
    _str(where, "password", merged["password"])

    acct = Account(
        name=_str(where, "name", merged["name"]),
        host=_str(where, "host", merged["host"]),
        user=_str(where, "user", merged["user"]),
        password=merged["password"],
        port=port,
        verify_tls=_bool(where, "verify_tls", merged.get("verify_tls", True)),
        watch=_str(where, "watch", merged["watch"]),
        deliver=_str(where, "deliver", merged["deliver"]),
        originals=_str(where, "originals", merged["originals"]),
        skip_langs=skip_langs,
        likely_langs=likely_langs,
        attach_original=_bool(where, "attach_original", merged["attach_original"]),
        process_existing=_bool(where, "process_existing", merged["process_existing"]),
    )
    if acct.originals in (acct.watch, acct.deliver):
        raise ConfigError(f"{where}: 'originals' must be a different folder from 'watch' and 'deliver'")
    return acct


def parse_config(data, env=None) -> Config:
    env = os.environ if env is None else env
    if not isinstance(data, dict):
        raise ConfigError("config must be a YAML mapping with 'translator', 'defaults' and 'accounts'")
    unknown = set(data) - {"translator", "defaults", "accounts"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    data = expand_vars(data, env)

    translator = _translator(data.get("translator"))

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("'defaults' must be a mapping")
    unknown = set(defaults) - set(DEFAULTS) - {"port", "verify_tls"}
    if unknown:
        raise ConfigError(f"defaults: unknown key(s): {', '.join(sorted(unknown))}")
    defaults = {**DEFAULTS, **defaults}

    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ConfigError("'accounts' must be a non-empty list")
    accounts = [_account(i, a, defaults, translator.target_lang) for i, a in enumerate(raw_accounts)]

    names, logins = set(), set()
    for a in accounts:
        if a.name in names:
            raise ConfigError(f"duplicate account name {a.name!r}")
        login = (a.host.lower(), a.user.lower())
        if login in logins:
            raise ConfigError(f"account {a.name!r}: {a.user}@{a.host} is configured twice")
        names.add(a.name)
        logins.add(login)
    return Config(translator=translator, accounts=accounts)


def load_config(path, env=None) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (see config.example.yml; on Unraid put the config inline in compose.yaml)") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    return parse_config(data, env)
