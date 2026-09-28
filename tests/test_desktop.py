import os

from botpurge import desktop


def test_data_dir_and_env(tmp_path, monkeypatch):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOTPURGE_")}
    env.update(HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path / "data"))
    monkeypatch.setattr(os, "environ", env)  # a private copy: nothing leaks into other tests
    d = desktop.data_dir()
    assert d.exists() and d.name == "Bot Purge"
    desktop.configure_env(d)
    assert os.environ["BOTPURGE_DB"].startswith(str(d)) and os.environ["BOTPURGE_DESKTOP"] == "1"


def test_finds_iphone_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.Path, "home", staticmethod(lambda: tmp_path))
    old = tmp_path / "Library/Application Support/MobileSync/Backup/aaa/3d/3d0d7e5fb2ce288813306e4d4636395e047a3d28"
    new = tmp_path / "Library/Application Support/MobileSync/Backup/bbb/3d/3d0d7e5fb2ce288813306e4d4636395e047a3d28"
    for i, p in enumerate((old, new)):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"SQLite format 3\x00")
        os.utime(p, (1000 + i, 1000 + i))
    assert desktop.iphone_sms_backups() == [str(new), str(old)]


def test_local_endpoints_are_desktop_only(client, user):
    from conftest import hdr

    assert client.get("/api/local/iphone-backups", headers=hdr(user)).status_code == 404


def test_serves_without_a_console(tmp_path, monkeypatch):
    """Windowed builds on Windows have no stdout/stderr; the server must still start."""
    import sys
    import urllib.request

    monkeypatch.setattr(desktop, "data_dir", lambda: tmp_path)
    monkeypatch.setenv("BOTPURGE_DB", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("BOTPURGE_WORKER", "0")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    port = desktop.free_port()
    srv = desktop.serve(port)
    try:
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5).read() == b'{"ok":true}'
    finally:
        srv.should_exit = True
    assert (tmp_path / "botpurge.log").exists()


def test_app_manifest_and_worker_are_served(client):
    import json

    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    man = json.loads(m.text)
    assert man["start_url"].startswith("/") and man["icons"]
    sw = client.get("/sw.js")
    assert sw.headers["content-type"].startswith("text/javascript") and "/api/" in sw.text   # API calls never cached
    for i in man["icons"]:
        assert client.get(i["src"]).status_code == 200


def test_update_check(monkeypatch):
    import httpx

    from botpurge import __version__, release

    release._cache.clear()
    monkeypatch.delenv("BOTPURGE_DESKTOP", raising=False)
    assert release.check_for_update()["update_available"] is False        # servers don't phone home
    monkeypatch.setenv("BOTPURGE_UPDATE_CHECK", "1")
    seen = []

    def gh(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"tag_name": "v99.0.0", "html_url": "https://github.com/x/y/releases/tag/v99.0.0"})

    http = httpx.Client(transport=httpx.MockTransport(gh))
    info = release.check_for_update(http, now=1000.0)
    assert info == {"version": __version__, "latest": "99.0.0", "update_available": True,
                    "download_url": "https://github.com/x/y/releases/tag/v99.0.0"}
    release.check_for_update(http, now=2000.0)
    assert len(seen) == 1                                                  # cached for 12 hours
    release._cache.clear()
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert release.check_for_update(down)["update_available"] is False     # offline or GitHub down: no news
    release._cache.clear()
