from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend import agent, mcp_servers, patterns, seed
from backend import booking as bk
from backend.agent import Turn
from backend.models import Booking, ChatSession, Faculty
from backend.recommend import BookingRequest
from conftest import FakeEmbedder

pytestmark = pytest.mark.anyio
IST = ZoneInfo("Asia/Kolkata")


class ScriptedLLM:
    """Stands in for Groq: each message is understood as the next scripted Turn."""

    def __init__(self, *turns: Turn):
        self.turns = list(turns)
        self.contexts = []
        self.asked = []

    def understand(self, context, history, message):
        self.contexts.append(context)
        return self.turns.pop(0)

    def write(self, instructions, data, history):
        raise RuntimeError("Groq is down")  # exercise the plain-text fallback

    async def answer(self, question, history, tools, call):
        self.asked.append(question)
        assert {t["name"] for t in tools} >= {"search_knowledge", "get_venue_rules"}
        hits = await call("search_knowledge", {"query": question, "k": 2})
        return "From " + ", ".join(h["document"] for h in hits)


@pytest.fixture
def w(db, monkeypatch):
    monkeypatch.setattr(bk, "_now", lambda: datetime(2026, 10, 22, 9, tzinfo=IST))
    seed.run(db, admin_email="admin@example.com", with_past_events=True, embedder=FakeEmbedder())
    db.flush()
    patterns.compute(db, date(2026, 10, 22))
    monkeypatch.setattr(mcp_servers, "session_factory",
                        sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint",
                                     expire_on_commit=False))
    monkeypatch.setattr(mcp_servers, "embedder_factory", FakeEmbedder)
    db.me = db.scalar(select(Faculty).where(Faculty.email == "sekar.a@example.com"))
    return db


def book(**kw):
    return Turn(intent="book", request=BookingRequest(**kw))


async def say(db, me, llm, message, sid=None, action=None):
    return await agent.chat(db, me, llm, message, sid, action)


async def test_asks_follow_ups_then_recommends_then_holds_on_yes(w):
    llm = ScriptedLLM(
        book(title="Guest lecture on cloud computing", category="guest_lecture"),
        book(expected_headcount=80, format="talk"),
        book(dates=["2026-11-07"], start_time="10:00", end_time="12:00"),
        Turn(intent="book", venue="D-Block Seminar Hall"),
        Turn(intent="yes"),
    )
    r = await say(w, w.me, llm, "I need a hall for a guest lecture on cloud computing")
    assert "how many people" in r["reply"] and "talk or a hands-on" in r["reply"] and "which date" in r["reply"]
    sid = r["session_id"]

    r = await say(w, w.me, llm, "about 80, it's a talk", sid)
    assert "which date" in r["reply"] and "how many" not in r["reply"]

    r = await say(w, w.me, llm, "7 Nov, 10 to 12", sid)
    codes = [o["code"] for o in r["data"]["options"]]
    assert codes and all(c.endswith("SH") for c in codes) and "Which one should I request?" in r["reply"]

    assert w.scalar(select(Booking).where(Booking.status == "pending")) is None

    r = await say(w, w.me, llm, "the D block hall please", sid)
    assert r["pending"] and r["reply"].startswith("Shall I request D Block")
    assert w.scalar(select(Booking).where(Booking.status == "pending")) is None  # nothing until yes

    r = await say(w, w.me, llm, "yes", sid)
    assert r["reply"].startswith("Done.") and not r["pending"]
    held = w.scalars(select(Booking).where(Booking.status == "pending")).all()
    assert [b.venue.code for b in held] == ["D Block SH"] and held[0].event.created_by_id == w.me.id
    assert bk.local(held[0].start_ts).hour == 10

    cs = w.get(ChatSession, sid)
    assert len(cs.messages) == 10 and not cs.state.get("draft")
    assert "Today is Thursday 22 October 2026" in llm.contexts[0]
    assert "CC1 = CC1 Laboratory, computer lab, 60 seats" in llm.contexts[0]


async def test_button_presses_skip_the_model(w):
    llm = ScriptedLLM(book(title="Python workshop", format="hands-on", expected_headcount=50,
                           dates=["2026-11-07"], start_time="09:30", end_time="12:30"))
    r = await say(w, w.me, llm, "Python workshop for 50 on 7 Nov 9:30-12:30, hands-on")
    assert set(o["code"] for o in r["data"]["options"]) <= {"CC1", "CC2"}
    r = await say(w, w.me, llm, "Request CC1", r["session_id"], {"pick": "CC1"})
    assert r["pending"]
    r = await say(w, w.me, llm, "No", r["session_id"], {"confirm": False})
    assert r["reply"] == "Okay, I haven't changed anything." and not r["pending"]
    assert w.scalar(select(Booking).where(Booking.status == "pending")) is None


async def test_seasonal_warning_is_reported_with_a_clean_alternative(w):
    llm = ScriptedLLM(book(title="Alumni talk", format="talk", expected_headcount=80, dates=["2027-03-10"],
                           start_time="10:00", end_time="12:00", preferred_venue="A Block SH"),
                      Turn(intent="book", venue="A Block SH"))
    r = await say(w, w.me, llm, "Alumni talk for 80 on 10 March 2027, 10-12, in A Block SH")
    opts = r["data"]["options"]
    assert "admissions" in r["reply"].lower() and any(not o["soft_conflicts"] for o in opts)
    assert opts[-1]["code"] == "A Block SH" and opts[-1]["soft_conflicts"]
    r = await say(w, w.me, llm, "A Block SH anyway", r["session_id"])
    assert r["pending"] and "Heads-up" in r["reply"] and "only a warning" in r["reply"]


async def test_busy_pick_is_refused_with_alternatives(w):
    llm = ScriptedLLM(book(title="Lab session", format="hands-on", expected_headcount=25, dates=["2026-11-02"],
                           start_time="09:00", end_time="10:00"),
                      Turn(intent="book", venue="CC1"))
    r = await say(w, w.me, llm, "Hands-on lab for 25 on 2 Nov 9-10")
    r = await say(w, w.me, llm, "CC1", r["session_id"])
    assert r["reply"].startswith("CC1 won't work: busy") and not r["pending"] and r["data"]["options"]


async def test_cancel_and_change_need_a_yes(w):
    me = w.me
    bk.request_booking(w, me, title="Club meeting", category="meeting", format="talk", expected_headcount=40,
                       slots=[bk.Slot(w.scalar(select(bk.Venue.id).where(bk.Venue.code == "CSE PG SH")),
                                      datetime(2026, 11, 7, 14, tzinfo=IST), datetime(2026, 11, 7, 15, tzinfo=IST))])
    w.flush()
    llm = ScriptedLLM(Turn(intent="change", new_start_time="15:00", new_end_time="16:00"), Turn(intent="yes"),
                      Turn(intent="list"), Turn(intent="cancel", reason="Postponed"), Turn(intent="yes"))
    r = await say(w, me, llm, "move my meeting to 3 pm")
    assert r["reply"].startswith('Shall I move "Club meeting" in CSE PG SH on Sat 07 Nov, 14:00 to from 15:00')
    r = await say(w, me, llm, "yes", r["session_id"])
    assert r["reply"].startswith("Done.")
    r = await say(w, me, llm, "what have I booked?", r["session_id"])
    assert "Club meeting, CSE PG SH, Sat 07 Nov 15:00" in r["reply"]
    r = await say(w, me, llm, "cancel it, postponed", r["session_id"])
    assert r["reply"].startswith("Shall I cancel")
    r = await say(w, me, llm, "yes", r["session_id"])
    assert "cancelled" in r["reply"]
    assert w.scalar(select(Booking.status).where(Booking.event.has(title="Club meeting"))) == "cancelled"


async def test_questions_go_through_the_knowledge_tools(w):
    llm = ScriptedLLM(Turn(intent="question", question="Which labs have MATLAB?"))
    r = await say(w, w.me, llm, "which labs have matlab")
    assert llm.asked == ["Which labs have MATLAB?"] and r["reply"].startswith("From")


async def test_sessions_are_private(w):
    llm = ScriptedLLM(book(title="x"), book(title="y"))
    r = await say(w, w.me, llm, "hello")
    other = w.scalar(select(Faculty).where(Faculty.email == "babu.g@example.com"))
    r2 = await say(w, other, llm, "hi", r["session_id"])
    assert r2["session_id"] != r["session_id"]
