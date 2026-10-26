from datetime import datetime
from zoneinfo import ZoneInfo

from backend import agent_eval, patterns, seed
from backend import booking as bk
from backend.agent import Turn
from backend.recommend import BookingRequest
from conftest import FakeEmbedder


class AnswerKey:
    """Reads every request perfectly, to check the scoring itself."""

    def __init__(self, cases):
        self.by_text = {c["text"]: c["request"] for c in cases}

    def understand(self, context, history, message):
        return Turn(intent="book", request=BookingRequest(**self.by_text[message]))


def test_request_set_meets_the_phase_3_bar(db, monkeypatch):
    monkeypatch.setattr(bk, "_now", lambda: datetime(2026, 10, 22, 9, tzinfo=ZoneInfo("Asia/Kolkata")))
    seed.run(db, admin_email="admin@example.com", with_past_events=True, embedder=FakeEmbedder())
    db.flush()
    patterns.compute(db, agent_eval.TODAY)
    cases = agent_eval.load()
    out = agent_eval.evaluate(db, FakeEmbedder(), cases, AnswerKey(cases))
    assert out["valid_rate"] >= 0.8, out["failures"]
    assert out["soft_reported"] == out["soft_planted"] == 9, out["failures"]
    assert set(out["understanding"].values()) == {1.0}
