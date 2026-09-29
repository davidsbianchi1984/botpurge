"""Tells from real bot accounts: random-letter names, names never set, and photos that hide the face."""
import io
from datetime import datetime, timedelta, timezone

import pytest

from botpurge import rules
from botpurge.liveguard import ChatMessage, Judge, Policy
from botpurge.models import Connection, Direction, Platform
from botpurge.scoring import Scorer

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("name", ["yznnkdcp", "ynvpxs", "cbeanjhgccf", "@qxtrvmwl"])
def test_random_letter_names(name):
    assert rules.keyboard_mash(name)


@pytest.mark.parametrize("name", ["kelsey.m", "chris_p", "brb_chris", "schmidt", "rhythm", "cathyhillarye", "Malugen-Ilixadu",
                                  "angels1st", "tom_b", "marco88", "lily_rose", "Grace Patel"])
def test_real_looking_names_are_not_random(name):
    assert not rules.keyboard_mash(name)


def _score(**kw):
    base = dict(platform=Platform.tiktok, account_id=kw.get("handle", "x"), direction=Direction.follower,
                connected_at=NOW - timedelta(days=400), created_at=NOW - timedelta(days=900))
    base.update(kw)
    return Scorer(now=NOW).score([Connection(**base)])[0]


def test_short_keyboard_mash_handle_is_flagged():
    r = _score(handle="ynvpxs", name="ynvpxs")
    assert any(x.code == "handle_pattern" and "randomly generated" in x.text for x in r.reasons)


def test_default_name_never_set():
    r = _score(handle="cbeanjhgccf", name="user45418458969")
    codes = {x.code: x.text for x in r.reasons}
    assert "default_name" in codes and "user45418458969" in codes["default_name"]
    assert "handle_pattern" in codes                          # and the handle is random letters too
    assert "default_name" not in {x.code for x in _score(handle="jen.walsh", name="Jen Walsh").reasons}


def test_person_with_hidden_face_is_its_own_signal():
    hidden = _score(handle="felicity.m", name="Felicity", avatar_labels=["no_face", "hidden_face"])
    scenery = _score(handle="felicity.m", name="Felicity", avatar_labels=["no_face"])
    assert "hidden_face" in {x.code for x in hidden.reasons} and "faceless_avatar" not in {x.code for x in hidden.reasons}
    assert hidden.score > scenery.score


def test_vision_labels_a_person_without_a_face():
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    from botpurge import vision

    noisy = Image.effect_noise((200, 200), 60).convert("RGB")   # not a blank placeholder
    b2 = io.BytesIO(); noisy.save(b2, "PNG")
    out = vision.analyze(b2.getvalue(), face_detector=lambda i: False, body_detector=lambda i: True)
    assert out["labels"] == ["no_face", "hidden_face"]
    out = vision.analyze(b2.getvalue(), face_detector=lambda i: False, body_detector=lambda i: False)
    assert out["labels"] == ["no_face"]


def test_random_live_name_alone_never_gets_a_viewer_removed():
    j = Judge(Policy(mode="protect"))
    t = datetime(2026, 6, 1, 20, tzinfo=timezone.utc)
    m = lambda who, text, s: ChatMessage(platform="tiktok", author_id=who, author_name=who, text=text, at=t + timedelta(seconds=s))
    calm = j.judge(m("ynvpxs", "hi from Ohio!", 0))
    assert calm.action == "none" and any("random letters" in r for r in calm.reasons)
    scam = j.judge(m("qxtrvmwl", "I made $8,400 this week with my trading coach, message me on WhatsApp", 5))
    plain = Judge(Policy(mode="protect")).judge(m("dana.r", "I made $8,400 this week with my trading coach, message me on WhatsApp", 5))
    assert scam.score > plain.score and scam.action != "none"
