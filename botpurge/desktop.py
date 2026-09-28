"""Bot Purge desktop app.

Starts Bot Purge privately on this computer (127.0.0.1 only; nothing is
reachable from outside), keeps its data in the user's app-data folder, and
opens it in its own window (or the default browser if no window toolkit is
available).

    botpurge-desktop            # what the packaged app runs
    python -m botpurge.desktop
"""
from __future__ import annotations

import glob
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "Bot Purge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def configure_env(d: Path) -> None:
    os.environ.setdefault("BOTPURGE_DB", str(d / "botpurge.sqlite3"))
    os.environ.setdefault("BOTPURGE_KEYFILE", str(d / "botpurge.key"))
    os.environ.setdefault("BOTPURGE_BROWSER_PROFILE", str(d / "agent-browser"))
    os.environ.setdefault("BOTPURGE_WORKER", "1")
    os.environ["BOTPURGE_DESKTOP"] = "1"


def iphone_sms_backups() -> list[str]:
    """sms.db files inside unencrypted iPhone backups made with Finder/iTunes, newest first.

    Backups store files under hashed names; the Messages database is always
    3d/3d0d7e5fb2ce288813306e4d4636395e047a3d28.
    """
    roots = [Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup",
             Path(os.environ.get("APPDATA", "")) / "Apple Computer" / "MobileSync" / "Backup",
             Path.home() / "Apple" / "MobileSync" / "Backup",
             Path(os.environ.get("USERPROFILE", "")) / "Apple" / "MobileSync" / "Backup"]
    found = []
    for r in roots:
        found += glob.glob(str(r / "*" / "3d" / "3d0d7e5fb2ce288813306e4d4636395e047a3d28"))
    return sorted(set(found), key=lambda p: os.path.getmtime(p), reverse=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_stdio(d: Path) -> None:
    """Windowed apps (no console) have no stdout/stderr; logging to None crashes the server.
    Send output to a log file in the app-data folder instead (handy for support, too)."""
    if sys.stdout is None or sys.stderr is None:
        log = open(d / "botpurge.log", "a", buffering=1, encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


def serve(port: int):
    import uvicorn

    from .api import create_app

    _ensure_stdio(data_dir())
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning", log_config=None))
    threading.Thread(target=server.run, daemon=True, name="botpurge-server").start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server


def main(open_window: bool = True) -> None:
    configure_env(data_dir())
    port = int(os.environ.get("BOTPURGE_PORT", "0")) or free_port()
    server = serve(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Bot Purge is running at {url}", flush=True)
    if not open_window:
        return
    try:
        import webview  # pywebview: a native window

        webview.create_window("Bot Purge", url, width=1280, height=860, min_size=(380, 600))
        webview.start()
    except Exception:  # no window toolkit on this system: use the default browser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    server.should_exit = True


def cli() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="botpurge-desktop")
    p.add_argument("--no-window", action="store_true", help="serve only (for testing); press Ctrl+C to stop")
    a = p.parse_args()
    if a.no_window:
        main(open_window=False)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        main()


if __name__ == "__main__":
    cli()
