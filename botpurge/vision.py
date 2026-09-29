"""On-device avatar checks: perceptual hash and "is there a face?".

Runs locally; photos are never uploaded anywhere and never stored — only the
64-bit hash and the labels are kept.

Labels:
  * ``blank``   — a flat single colour (or nearly): a default or placeholder picture
  * ``no_face`` — no human face found (back of the head, a car, a logo, a sunset...)
  * ``hidden_face`` — a person is in the photo but their face isn't: turned away, a phone held
    in front of it (mirror selfies), or too far away to see. Fake accounts use these so the
    stolen photo can't be traced to a face.

"No face" is weak evidence on its own (plenty of real people use a pet or a
view as their picture); it only adds up alongside bot-like messages or other
signals. Needs Pillow for hashing and OpenCV (4.x, which bundles the face
models) for the face check; without them this module does nothing.
"""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional

from . import imagehash

try:  # optional
    from PIL import Image, ImageStat
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

_cascades = None
_bodies = None


def _detectors():
    global _cascades
    if _cascades is None:
        try:
            import cv2

            base = cv2.data.haarcascades
            _cascades = [cv2.CascadeClassifier(base + n) for n in
                         ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")]
            _cascades = [c for c in _cascades if not c.empty()]
        except Exception:  # OpenCV missing or without cascades
            _cascades = []
    return _cascades


def _body_detectors():
    global _bodies
    if _bodies is None:
        try:
            import cv2

            base = cv2.data.haarcascades
            _bodies = [cv2.CascadeClassifier(base + n) for n in ("haarcascade_upperbody.xml", "haarcascade_fullbody.xml")]
            _bodies = [c for c in _bodies if not c.empty()]
        except Exception:
            _bodies = []
    return _bodies


def has_body(img) -> Optional[bool]:
    """Is there a person (upper body or full figure) in the photo? None when no detector is available."""
    dets = _body_detectors()
    if not dets:
        return None
    import numpy as np

    gray = np.array(img.convert("L").resize((256, 256)))
    return any(len(d.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=3, minSize=(40, 40))) for d in dets)


def available() -> dict:
    return {"hash": Image is not None, "faces": bool(_detectors())}


def has_face(img) -> Optional[bool]:
    """True/False when a detector is available, None when it isn't."""
    dets = _detectors()
    if not dets:
        return None
    import numpy as np

    gray = np.array(img.convert("L").resize((256, 256)))
    for det in dets:
        if len(det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))):
            return True
        # profile cascade only finds left-facing profiles; check the mirror too
        if len(det.detectMultiScale(gray[:, ::-1].copy(), scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))):
            return True
    return False


def analyze(data: bytes, face_detector: Optional[Callable] = None, body_detector: Optional[Callable] = None) -> dict:
    """{"hash": hex or None, "labels": [...]} for an encoded image."""
    if Image is None:
        return {"hash": None, "labels": []}
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return {"hash": None, "labels": []}
    gray = img.convert("L")
    small = gray.resize((9, 8))
    px = list(small.getdata())
    h = imagehash.dhash_from_pixels([px[i * 9:(i + 1) * 9] for i in range(8)])
    labels = []
    if ImageStat.Stat(gray.resize((64, 64))).stddev[0] < 8:
        labels.append("blank")
    face = (face_detector or has_face)(img)
    if face is False or (face is None and "blank" in labels):
        labels.append("no_face")
        if face is False and "blank" not in labels and (body_detector or has_body)(img):
            labels.append("hidden_face")
    return {"hash": h, "labels": labels}


def enrich(conns: Iterable, fetch: Callable[[str], Optional[bytes]], max_images: int = 5000, workers: int = 8) -> int:
    """Download each connection's avatar (``avatar_url``), hash it and label it. Returns how many were processed."""
    todo = [c for c in conns if getattr(c, "avatar_url", None) and not c.avatar_hash][:max_images]
    if Image is None or not todo:
        return 0

    def one(c):
        data = fetch(c.avatar_url)
        if data:
            out = analyze(data)
            c.avatar_hash = out["hash"] or c.avatar_hash
            c.avatar_labels = out["labels"]
        return c

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    return len(todo)
