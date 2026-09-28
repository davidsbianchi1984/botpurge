"""Accuracy gates on the labelled message corpus (tests/corpus/messages.jsonl).

The corpus holds real scam templates collected in research (toll and USPS texts,
code theft, "reply Y", fake giveaways, growth-service spam, recovery-scam
testimonials, AI refusal leaks...) and the legitimate look-alikes that must not
be flagged (real 2FA codes, real carrier texts, bank YES/NO alerts, streamer
raffles, fans chanting, people talking about scams). Add every new miss or false
alarm here before fixing it.
"""
import json
from pathlib import Path

from botpurge.messages import Message, MessageScorer

CORPUS = [json.loads(l) for l in (Path(__file__).parent / "corpus" / "messages.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def verdicts():
    msgs = [Message(kind=r["kind"], platform=r["platform"], sender_id=f"s{i}", text=r["text"], sender_name=r["sender_name"])
            for i, r in enumerate(CORPUS)]
    res = {x.sender_id: x for x in MessageScorer().score(msgs)}
    return [(r, res[f"s{i}"]) for i, r in enumerate(CORPUS)]


def test_corpus_gates():
    v = verdicts()
    flagged = lambda x: x.label in ("likely_bot", "suspicious")  # noqa: E731
    tp = sum(1 for r, x in v if r["label"] == "scam" and flagged(x))
    fp = [(r["text"], x.score, [c["code"] for c in x.reasons]) for r, x in v if r["label"] == "benign" and flagged(x)]
    fn = [(r["text"], x.score, [c["code"] for c in x.reasons]) for r, x in v if r["label"] == "scam" and not flagged(x)]
    scams = sum(1 for r, _ in v if r["label"] == "scam")
    precision = tp / (tp + len(fp)) if tp + len(fp) else 1.0
    recall = tp / scams
    assert not fp, f"false alarms on legitimate messages: {fp}"
    assert recall >= 0.95, f"recall {recall:.2f}; missed: {fn}"
    assert precision >= 0.98
