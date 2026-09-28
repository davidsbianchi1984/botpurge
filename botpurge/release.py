"""Settings baked into a release build, and the desktop app's update check.

``release.json`` next to this file is written by the release workflow (from
repository variables), so a downloaded app knows where its store is and
where updates come from without anyone setting environment variables.
Environment variables still win, for testing.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from . import __version__

CONFIG = Path(__file__).with_name("release.json")
DEFAULT_REPO = "davidsbianchi1984/botpurge"
_cache: dict = {}


def _baked() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


def store_url() -> str:
    return (os.environ.get("BOTPURGE_STORE_URL") or _baked().get("store_url") or "").rstrip("/")


def update_repo() -> str:
    return os.environ.get("BOTPURGE_UPDATE_REPO") or _baked().get("update_repo") or DEFAULT_REPO


def _parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def check_for_update(http=None, now: Optional[float] = None) -> dict:
    """Is a newer release out? Asks GitHub at most every 12 hours; any failure means "no news"."""
    now = now or time.time()
    info = {"version": __version__, "latest": None, "update_available": False, "download_url": None}
    if os.environ.get("BOTPURGE_DESKTOP") != "1" and os.environ.get("BOTPURGE_UPDATE_CHECK") != "1":
        return info
    if "at" in _cache and _cache["at"] > now - 12 * 3600:
        return _cache["info"]
    try:
        import httpx

        r = (http or httpx.Client(timeout=8)).get(f"https://api.github.com/repos/{update_repo()}/releases/latest",
                                                  headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            rel = r.json()
            latest = rel.get("tag_name", "").lstrip("v")
            info.update(latest=latest, download_url=rel.get("html_url"),
                        update_available=bool(latest) and _parse(latest) > _parse(__version__))
    except Exception:
        pass
    _cache.update(at=now, info=info)
    return info
