"""Uploads as downloaded: the app works out which network (or message type) an export is from."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from botpurge.api import create_app
from botpurge.importers import detect_platform
from botpurge.messages import detect_message_source
from botpurge.models import Platform


def zipped(files: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, v in files.items():
            z.writestr(n, v)
    return b.getvalue()


IG = json.dumps([{"string_list_data": [{"value": "friend.1", "timestamp": 1700000000}]}])
TT = json.dumps({"Activity": {"Follower List": {"FansList": [{"Date": "2026-01-01 10:00:00", "UserName": "abc"}]}}})
FB = json.dumps({"friends_v2": [{"name": "Jen Walsh", "timestamp": 1700000000}]})
LI = "Notes:\n\nFirst Name,Last Name,URL,Email Address,Company,Position,Connected On\nJen,Walsh,https://www.linkedin.com/in/jw,,,,01 Jan 2026\n"
XJS = 'window.YTD.follower.part0 = [{"follower": {"accountId": "1", "userLink": "https://twitter.com/intent/user?user_id=1"}}]'


@pytest.mark.parametrize("name,data,expect", [
    ("instagram-me-2026.zip", zipped({"connections/followers_and_following/followers_1.json": IG}), Platform.instagram),
    ("TikTok_Data_1759132800.zip", zipped({"user_data_tiktok.json": TT}), Platform.tiktok),
    ("facebook-me-2026.zip", zipped({"connections/friends/your_friends.json": FB}), Platform.facebook),
    ("Basic_LinkedInDataExport.zip", zipped({"Connections.csv": LI}), Platform.linkedin),
    ("archive.zip", zipped({"data/follower.js": XJS}), Platform.x),
    ("TikTok_Data_1759132800.json", TT.encode(), Platform.tiktok),
    ("upload.json", IG.encode(), Platform.instagram),
    ("upload.json", FB.encode(), Platform.facebook),
    ("Connections.csv", LI.encode(), Platform.linkedin),
    ("follower.js", XJS.encode(), Platform.x),
    ("Follower.txt", b"Date: 2026-01-01 10:00:00\nUsername: abc\n", Platform.tiktok),
    ("notes.txt", b"hello", None),
])
def test_detect_platform(name, data, expect):
    assert detect_platform(name, data) == expect


@pytest.mark.parametrize("name,data,expect", [
    ("takeout.zip", zipped({"Takeout/Mail/All mail Including Spam and Trash.mbox": "From x\n"}), "email"),
    ("Inbox.mbox", b"From MAILER-DAEMON Mon Sep 28\nFrom: a@b.co\n", "email"),
    ("msg.eml", b"Received: from x\nFrom: a@b.co\n", "email"),
    ("sms-20260929.xml", b"<?xml version='1.0'?><smses count='0'></smses>", "sms"),
    ("sms.db", b"SQLite format 3\x00", "sms"),
    ("instagram-me.zip", zipped({"your_instagram_activity/messages/inbox/a/message_1.json": "{}"}), "instagram"),
    ("facebook-me.zip", zipped({"your_facebook_activity/messages/inbox/a/message_1.json": "{}"}), "facebook"),
    ("TikTok_Data_1.zip", zipped({"user_data_tiktok.json": "{}"}), "tiktok"),
    ("twitter-2026.zip", zipped({"data/direct-messages.js": ""}), "x"),
    ("comments.csv", b"author,text\n", "csv"),
    ("photo.png", b"\x89PNG", None),
])
def test_detect_message_source(name, data, expect):
    assert detect_message_source(name, data) == expect


def test_auto_upload_routes(tmp_path):
    client = TestClient(create_app(str(tmp_path / "a.sqlite3"), worker=False))
    h = {"Authorization": "Bearer " + client.post("/api/signup", json={"email": "a@b.co"}).json()["token"]}
    r = client.post("/api/import/auto", files={"file": ("TikTok_Data_1759132800.zip", zipped({"user_data_tiktok.json": TT}))}, headers=h)
    assert r.status_code == 200 and r.json()["platform"] == "tiktok" and r.json()["imported"] == 1
    assert client.post("/api/import/auto", files={"file": ("notes.txt", b"hello")}, headers=h).status_code == 400
    # Picking the network yourself still works exactly as before.
    assert client.post("/api/import/facebook", files={"file": ("x.json", FB.encode())}, headers=h).json()["imported"] == 1
    mail = b"From: \"PayPal\" <service@paypa1-secure.top>\nSubject: Account limited\nMessage-ID: <1@x>\n\nVerify now https://paypa1-secure.top/login\n"
    r = client.post("/api/inbox/import/auto", files={"file": ("msg.eml", mail)}, headers=h)
    assert r.status_code == 200 and r.json()["source"] == "email" and r.json()["imported"] == 1
    assert client.post("/api/inbox/import/auto", files={"file": ("photo.png", b"\x89PNG")}, headers=h).status_code == 400
