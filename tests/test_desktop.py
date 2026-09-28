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
