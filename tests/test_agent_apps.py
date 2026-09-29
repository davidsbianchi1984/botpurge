"""The agent reads connected apps from the platform's settings page (browser test) and cleans names (unit)."""
import os

import pytest

from botpurge.agent.apps import _clean

browser = pytest.mark.skipif(os.environ.get("BOTPURGE_UI_TESTS") != "1", reason="set BOTPURGE_UI_TESTS=1")

X_APPS = """<!doctype html><html><body><nav><a href="/home">Home</a><a href="/settings">Settings</a></nav>
<main><h2>Connected apps</h2><p>These are the apps you've connected to your account.</p>
<div role="list">
  <div role="listitem"><h3>InstaFollowers Pro Boost</h3><span>Permissions: read and write. Approved on Jan 1, 2025</span><button>Revoke</button></div>
  <div role="listitem"><h3>Who viewed my profile</h3><span>Permissions: read. Approved on Feb 1, 2025</span><button>Revoke</button></div>
  <div role="listitem"><h3>Buffer</h3><span>Permissions: read and write. Approved on Jun 1, 2025</span><button>Revoke</button></div>
</div><a href="/i/flow/login" style="display:none">Log in</a></main></body></html>"""


def test_clean_drops_page_furniture():
    assert _clean(["Settings", "Revoke", " InstaFollowers Pro Boost ", "Buffer", "buffer", "See all"]) == ["InstaFollowers Pro Boost", "Buffer"]


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        exe = os.environ.get("CHROMIUM_PATH")
        br = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield br.new_context(viewport={"width": 1200, "height": 700}).new_page()
        br.close()


@browser
def test_agent_reads_connected_apps_from_the_settings_page(page):
    from botpurge.agent.apps import read_apps
    from botpurge.agent.cockpit import Cockpit

    page.route("https://x.com/**", lambda r: r.fulfill(body=X_APPS, content_type="text/html; charset=utf-8"))
    out = read_apps(page, "x", None, Cockpit(page.context, help_timeout=5).install(), settle_ms=300)
    assert out["names"] == ["InstaFollowers Pro Boost", "Who viewed my profile", "Buffer"]
    assert "Revoke" not in out["candidates"] and "Home" not in out["candidates"]
