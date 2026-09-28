import io

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from botpurge import vision  # noqa: E402
from botpurge.models import Connection, Direction, Platform  # noqa: E402
from botpurge.scoring import Scorer  # noqa: E402


def png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def test_blank_placeholder_and_hash():
    out = vision.analyze(png(Image.new("RGB", (200, 200), (180, 180, 180))))
    assert out["hash"] and len(out["hash"]) == 16
    assert "blank" in out["labels"] and "no_face" in out["labels"]


def test_scenery_has_no_face_when_detector_available():
    img = Image.new("RGB", (200, 200), (40, 120, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 130, 200, 200], fill=(30, 140, 60))   # grass
    d.ellipse([140, 20, 180, 60], fill=(250, 220, 60))     # sun
    out = vision.analyze(png(img))
    assert "blank" not in out["labels"]
    if vision.available()["faces"]:
        assert "no_face" in out["labels"]


def test_face_detector_hook_and_same_photo_hash():
    img = Image.new("RGB", (200, 200))
    d = ImageDraw.Draw(img)
    for i in range(0, 200, 20):
        d.rectangle([i, 0, i + 10, 200], fill=(i, 255 - i, 100))
    a = vision.analyze(png(img), face_detector=lambda _: True)
    b = vision.analyze(png(img.resize((120, 120))), face_detector=lambda _: True)
    assert a["labels"] == [] and b["labels"] == []
    from botpurge.imagehash import hamming

    assert hamming(a["hash"], b["hash"]) <= 4  # resized copy is recognised as the same photo


def test_enrich_and_scoring_signal():
    face = Image.new("RGB", (200, 200), (10, 10, 10))
    ImageDraw.Draw(face).rectangle([50, 50, 150, 150], fill=(200, 200, 200))
    c = Connection(platform=Platform.x, account_id="1", handle="h", name="H", direction=Direction.follower,
                   avatar_url="https://pbs.twimg.com/profile_images/1/a_200x200.jpg")
    n = vision.enrich([c], fetch=lambda url: png(face))
    assert n == 1 and c.avatar_hash
    c.avatar_labels = ["no_face"]
    [r] = Scorer().score([c])
    assert "faceless_avatar" in {x.code for x in r.reasons} and r.score < 40  # weak alone
