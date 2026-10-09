"""Entrypoint: python -m translate_mail [config.yml]

Paths (env): DATA_DIR (default /data), STATE_DIR (default $DATA_DIR/state), CONFIG.
"""

import logging
import os
import signal
import sys
import threading

from . import __version__
from .config import ConfigError, load_config
from .translators import make_translator
from .worker import AccountWorker

log = logging.getLogger("translate_mail")


def setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    # Each account runs in a thread named after it, so %(threadName)s is the account name.
    logging.basicConfig(
        level=level if level in logging.getLevelNamesMapping() else "INFO",
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("imapclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    threading.current_thread().name = "main"


def find_config(argv, data_dir):
    """Explicit argument or $CONFIG, else /config.yml (inline compose config), else <data>/config.yml."""
    if argv:
        return argv[0]
    if os.getenv("CONFIG"):
        return os.environ["CONFIG"]
    for path in ("/config.yml", os.path.join(data_dir, "config.yml")):
        if os.path.exists(path):
            return path
    return os.path.join(data_dir, "config.yml")


def drop_privileges(state_dir):
    """When started as root (the image default), make the state dir ours and switch to PUID:PGID.

    Defaults are Unraid's nobody:users (99:100) so files in appdata get the usual owner.
    Started as non-root (compose `user:`), this does nothing.
    """
    if os.getuid() != 0:
        return
    uid, gid = int(os.getenv("PUID", "99")), int(os.getenv("PGID", "100"))
    if uid == 0:
        log.warning("PUID=0: running as root")
        return
    os.makedirs(state_dir, exist_ok=True)
    for root, dirs, files in os.walk(state_dir):
        for name in [root] + [os.path.join(root, f) for f in dirs + files]:
            os.chown(name, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    log.debug("running as uid %s gid %s", uid, gid)


def main(argv=None):
    setup_logging()
    argv = sys.argv[1:] if argv is None else argv
    data_dir = os.getenv("DATA_DIR", "/data")
    state_dir = os.getenv("STATE_DIR") or os.path.join(data_dir, "state")
    path = find_config(argv, data_dir)

    try:
        drop_privileges(state_dir)
    except OSError as e:
        log.error("could not prepare %s or switch to PUID/PGID: %s", state_dir, e)
        return 2

    try:
        os.makedirs(state_dir, exist_ok=True)
        probe = os.path.join(state_dir, ".write-test")
        with open(probe, "w"):
            pass
        os.remove(probe)
    except OSError as e:
        log.error("state directory %s is not writable as uid %s (%s); fix the owner of the "
                  "host folder or set PUID/PGID", state_dir, os.getuid(), e)
        return 2

    try:
        cfg = load_config(path)
        translator = make_translator(cfg.translator, os.environ)
    except ConfigError as e:
        log.error("config error: %s", e)
        return 2

    t = cfg.translator
    log.info("mail-translate %s starting: translator=%s target=%s max_chars=%s, %s account(s)",
             __version__, t.provider, t.target_lang, t.max_chars, len(cfg.accounts))
    for a in cfg.accounts:
        log.info("account %s: user=%s host=%s:%s watch=%s deliver=%s originals=%s skip_langs=%s "
                 "likely_langs=%s attach_original=%s process_existing=%s%s",
                 a.name, a.user, a.host, a.port, a.watch, a.deliver, a.originals, ",".join(a.skip_langs),
                 ",".join(a.likely_langs) or "-", a.attach_original, a.process_existing,
                 "" if a.verify_tls else " verify_tls=false")

    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    for a in cfg.accounts:
        worker = AccountWorker(a, translator, t, state_dir=state_dir, stop_event=stop)
        # Daemon threads: a worker blocked in IDLE must not hold up shutdown.
        threading.Thread(target=worker.run, name=a.name, daemon=True).start()

    while not stop.wait(1):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
