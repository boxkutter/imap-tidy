# mail-translate

A small headless service that runs next to a self-hosted mail server (built
for poste.io: Haraka + Dovecot + Roundcube) and translates incoming
foreign-language mail into English **on the server**. Roundcube, Thunderbird
and your phone all see the translated version with no plugins or extra steps.

* One container, any number of IMAP accounts (one thread and IMAP IDLE
  connection per account).
* Language is detected locally with `py3langid`; mail you can already read
  never touches a translation API.
* Translators: DeepL, LibreTranslate (self-hosted) or any OpenAI-compatible
  chat endpoint (OpenAI, a local Ollama, ...).
* Originals are never modified (apart from being marked read) or deleted.
* If anything goes wrong the mail is still delivered, untranslated, and you get
  an explanatory message in your inbox.

## How it works

```
sender ──► poste.io (Haraka → Dovecot) ──Sieve──► Pending/
                                                     │
                                         mail-translate (IMAP IDLE)
                                                     │
          ┌──────────────────────────┬───────────────┴───────────────┬──────────────────────────┐
          │ readable (en/skip_langs) │ foreign language              │ failure                  │
          ▼                          ▼                               ▼                          ▼
   INBOX: original, unchanged   INBOX: [de→EN] Translated subject   Originals/:            INBOX: original, unchanged
          tagged $Translated           ┌───────────────────────────┐   untouched original          tagged $TranslateFailed
                                       │ translation (highlighted) │   (marked read)        INBOX: "[mail-translate] Could
                                       └───────────────────────────┘                                not translate: ..." notice
                                       ──── Original message (de) ────
                                       original body
                                       + original attachments
                                       + original.eml
                                       tagged $Translated
```

For each new message in the watched folder:

1. **Detect** the language from the body (subject as fallback).
2. **Readable** (`skip_langs`, which always includes the target language):
   move it unchanged to `deliver` (or leave it if `watch` == `deliver`) and
   tag it `$Translated`.
3. **Foreign**: translate subject and body and append a new message to
   `deliver`:
   * every original header (From, To, Date, Message-ID, In-Reply-To,
     References, ...) is kept byte-for-byte, so replies and threading work;
     only the MIME headers are regenerated;
   * Subject becomes `[de→EN] <translated subject>`; headers
     `X-Original-Subject`, `X-Translated-From` and `X-Translated-By` are added;
   * the body is `multipart/alternative`: plain text (translation, divider,
     original text) and HTML (highlighted translation, `<hr>`, original HTML);
   * original attachments are re-attached, plus the untouched original as
     `original.eml` (`attach_original`);
   * the copy keeps the original's flags (except `\Seen`, so it shows as
     unread) and its received date.

   The original is then marked read and moved to `originals`.
4. **Failure** (translator down, broken MIME, anything): the original is
   moved unchanged to `deliver`, tagged `$TranslateFailed`, and an error
   notice is appended to `deliver`. One bad message never stops the account.

The service only ever *appends* to `deliver` and *moves* the original; it never
deletes anything. The `$Translated` / `$TranslateFailed` keywords also stop it
from processing its own output when `watch` and `deliver` are the same folder.
You can search or filter on these keywords in Thunderbird and Roundcube.

## Setup

You don't need to build anything: GitHub Actions publishes the image to
`ghcr.io/boxkutter/imap-tidy:latest` on every push to `main`. You need two
files, `docker-compose.yml` (with the config inline) and `.env` (secrets).

### Unraid 7 (Compose Manager plugin)

1. **Docker → Compose → Add New Stack**, name it `mail-translate`.
2. **Edit Stack → Compose File**: paste [`docker-compose.yml`](docker-compose.yml)
   and edit the `configs:` block at the bottom: your accounts, folders,
   translator. Leave passwords as `$${PASS_PERSONAL}` (double `$`).
3. **Edit Stack → Env File**: paste [`.env.example`](.env.example) and fill
   in the passwords and translator key, **in single quotes**.
4. **Compose Up**, then open the stack's logs.

State (one small JSON file per account) goes in
`/mnt/user/appdata/mail-translate/state`, owned by `nobody:users` (99:100);
change `PUID`/`PGID` if you want another owner. To change the config later,
edit the stack and Compose Up again.

If the GitHub repo is private, the image is private too: either make the
package public (GitHub → your profile → Packages → imap-tidy → Package
settings → Change visibility) or run once on the Unraid terminal
`docker login ghcr.io -u boxkutter` with a personal access token that has
`read:packages`.

### Any other Docker host

Same two files: `curl -O` them (or clone the repo), adjust the volume path
`/mnt/user/appdata/mail-translate`, `cp .env.example .env`, edit, then
`docker compose up -d && docker compose logs -f`.

To build from source instead of pulling, replace `image:` with
`build: https://github.com/boxkutter/imap-tidy.git#main` (or `build: .` in a
clone).

If you prefer a separate config file to the inline block, remove `configs:`
from the service and put the file at `<appdata>/config.yml` (see
[`config.example.yml`](config.example.yml)); it is picked up automatically.

### Notes

* `host` is whatever name reaches Dovecot's IMAP port from the container:
  your public mail hostname works everywhere. If poste.io uses a self-signed
  certificate set `verify_tls: false` (prefer fixing the certificate).
* By default only mail arriving **after** the first start is processed. Set
  `process_existing: true` (once) to also process what is already in the
  watched folder.
* Container paths: `/data` (state in `/data/state`), config read from
  `/config.yml` or `/data/config.yml`; override with `CONFIG`, `DATA_DIR`,
  `STATE_DIR`. The container starts as root only to `chown` the state dir,
  then runs as `PUID:PGID`.

### Recommended: Sieve rule so clients never see untranslated mail

With the **Sieve setup** new mail is filed into `Pending` by the server, the
service processes it and puts the result in `INBOX`. Your clients only ever
see the finished message (one notification, no flicker).

1. Start the service once; it creates the `Pending` and `Originals` folders.
2. In Roundcube: **Settings → Filters**, add a filter to your filter set:
   * Filter name: `Hold for translation`
   * Scope: **all messages**
   * Action: **Move message to** → `Pending`
3. Move it to the **top** of the filter list and make sure it is enabled.
   (Rules you want applied to the *translated* mail, such as moving
   newsletters to a folder, will not see it: they run before translation.
   Spam rules that file to Junk should stay above this one so spam is not
   translated.)
4. In the config keep `watch: Pending` and `deliver: INBOX` (the defaults).

The equivalent raw Sieve script, if you prefer to edit it directly:

```sieve
require ["fileinto"];
fileinto "Pending";
stop;
```

If the service is stopped, mail waits in `Pending` until it is back (anything
that arrived while it was down is picked up on restart). Remove the filter to
go back to normal delivery.

### Simple setup: no Sieve

Set `watch: INBOX` and `deliver: INBOX` for the account. New mail lands in
INBOX as usual; within a second or two foreign mail gets a translated copy in
INBOX and the original is moved to `Originals`. Your phone may notify twice.

## Configuration reference

The `configs:` block in `docker-compose.yml` (or `config.yml`, see `config.example.yml`). Unknown keys are rejected, so typos
fail at startup instead of being silently ignored.

| Key | Where | Default | Meaning |
|---|---|---|---|
| `translator.provider` | translator | `deepl` | `deepl`, `libretranslate` or `llm` |
| `translator.target_lang` | translator | `en` | Language to translate into |
| `translator.max_chars` | translator | `30000` | Longer bodies are truncated before translation |
| `name` | account | required | Label used in logs; must be unique |
| `host` | account | required | IMAP server |
| `port` | account / defaults | `993` | IMAPS port (TLS is always used) |
| `user`, `password` | account | required | Login; use `${VAR}` for the password |
| `verify_tls` | account / defaults | `true` | Set `false` only for self-signed certificates |
| `watch` | account / defaults | `Pending` | Folder watched for new mail |
| `deliver` | account / defaults | `INBOX` | Where translated, readable and failed mail goes |
| `originals` | account / defaults | `Originals` | Where originals of translated mail are kept |
| `skip_langs` | account / defaults | `[en]` | ISO 639-1 codes you read; the target language is always included |
| `attach_original` | account / defaults | `true` | Attach the untouched original as `original.eml` |
| `process_existing` | account / defaults | `false` | On first run, also process mail already in `watch` |

Any key under `defaults:` applies to every account unless the account sets
it. Any string may contain `${VAR}`, expanded from the environment (`.env`);
an undefined variable is a startup error. Inside the compose file write
`$${VAR}` so compose passes it through untouched.

Environment variables (`.env`):

| Variable | Used for |
|---|---|
| `LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |
| `PUID`, `PGID` | User/group the service runs as and owns the state files (default 99/100) |
| `DEEPL_API_KEY`, `DEEPL_URL` | DeepL key; URL defaults to the free API (`https://api-free.deepl.com/v2/translate`), Pro is `https://api.deepl.com/v2/translate` |
| `LT_URL`, `LT_API_KEY` | LibreTranslate base URL (default `http://libretranslate:5000`), optional key |
| `LLM_URL`, `LLM_MODEL`, `LLM_API_KEY` | OpenAI-compatible base URL and model (both required), optional key |
| your `PASS_*` names | IMAP passwords referenced from `config.yml` |

## Translator options

| `provider` | Needs | Notes |
|---|---|---|
| `deepl` | `DEEPL_API_KEY` (free tier: 500k chars/month) | Best quality for the effort. Default. |
| `libretranslate` | the `libretranslate` service in `docker-compose.yml` (uncomment it) | Fully self-hosted; nothing leaves your server. Needs a few GB of RAM/disk for models. |
| `llm` | `LLM_URL`, `LLM_MODEL`, optional `LLM_API_KEY` | Any `/v1/chat/completions` endpoint. Local Ollama: `LLM_URL=http://ollama:11434/v1`, `LLM_MODEL=qwen2.5:7b`, no key. |

Every translator call has a timeout and is retried once (after 2 s) on
network errors, timeouts, HTTP 429 and 5xx. Other errors (e.g. a wrong API
key, HTTP 403) fail immediately.

With DeepL you can set `target_lang: en-gb` or `en-us` for a specific variant.

## Reading the logs

Everything goes to stdout: `docker compose logs -f mail-translate`. Each line
carries the account name (`[main]` for startup):

```
INFO    [main] mail-translate 1.0.0 starting: translator=deepl target=en max_chars=30000, 2 account(s)
INFO    [main] account personal: user=me@example.com host=mail.example.com:993 watch=Pending deliver=INBOX originals=Originals skip_langs=en ...
INFO    [personal] connecting to mail.example.com:993 as me@example.com
INFO    [personal] connected; watching Pending (last uid 1203, uidvalidity 1791537148)
INFO    [personal] uid 1204: deepl responded in 0.8s
INFO    [personal] uid 1204: translated de→en: Treffen am Donnerstag
INFO    [personal] uid 1205: language=en, pass-through: Meeting Thursday
WARNING [personal] translator request failed (...: HTTP 503: ...), retrying in 2s
ERROR   [personal] uid 1206: FAILED (...: HTTP 503: ...): Rechnung März
                   <full traceback>
ERROR   [work] IMAP error: abort: socket error: EOF; reconnecting in 5s
```

* `pass-through`: readable language, delivered unchanged.
* `translated`: translated copy delivered, original filed in `Originals`.
* `FAILED`: delivered untranslated with `$TranslateFailed`; a notice was
  appended to the inbox (see below).
* `LOG_LEVEL=DEBUG` adds IMAP IDLE responses and connection tracebacks.

## Error reporting

Errors that concern a message reach your inbox, not just the logs:

* **Per message**: a message from `mail-translate <mail-translate@<host>>`
  with subject `[mail-translate] Could not translate: <subject>` is appended
  to the account's `deliver` folder. It contains the account, UID, the
  original From/Subject/Date, the error and traceback.
* **Translator outage**: after 3 consecutive translator failures on an
  account, one "Translator is failing; notices paused" message is sent and
  further notices for that account are suppressed (and logged as such) until a
  translation succeeds again. Mail keeps flowing, untranslated and tagged.
* **Startup / config errors** are logged and the process exits with status 2;
  `restart: unless-stopped` retries. Fix the config and it starts.
* **IMAP connection errors** (server down, wrong password) are logged and
  retried with exponential backoff from 5 s up to 5 minutes. They obviously
  cannot be reported by mail.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
STATE_DIR=./state python -m translate_mail config.yml     # run locally
```

The tests cover language detection, message building, config loading and
validation, translator HTTP clients (mocked) and the pass-through / translate
/ failure logic against an in-memory fake IMAP server.

Layout: `translate_mail/config.py` (config + validation),
`translators.py`, `message.py` (detection, building messages), `worker.py`
(one IMAP account), `__main__.py` (entrypoint, logging, threads).

## Known limitations

* **Inline images** referenced by `cid:` in HTML mail are not shown in the
  translated copy (broken image icons). Open `original.eml` or the original in
  `Originals/` to see them.
* **Truncation**: bodies longer than `max_chars` are cut before translation;
  the translation says `[… truncated for translation …]` and the full original
  is still shown below it.
* Only the **text** of the mail is translated (the plain-text part, or text
  extracted from HTML); the translated block loses the original formatting.
* Very **short mail** (under ~20 characters of body and subject) cannot be
  identified reliably and is treated as readable.
* Language detection decides per message; a mostly-English mail quoting a
  long foreign text may be passed through, and vice versa.
* Only implicit TLS (IMAPS, usually port 993) is supported, not STARTTLS on 143.
* With the Sieve setup, rules that should act on the *translated* mail
  must be applied in your client, because server-side filters run first.
* Mail is processed in UID order, one message at a time per account; a slow
  translator delays the rest of that account's queue.
