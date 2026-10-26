import json
from datetime import datetime

import pytest
from sqlalchemy import select

from backend import booking as bk
from backend import documents, mcp_servers, notify
from backend.agent import Turn
from backend.config import REPO_ROOT, get_settings
from backend.models import Booking, Faculty, Notification, SourceDocument
from backend.recommend import BookingRequest
from conftest import FakeEmbedder
from test_agent import IST, ScriptedLLM, say, w  # noqa: F401  (w is the seeded-campus fixture)

pytestmark = pytest.mark.anyio
THREADS = json.loads((REPO_ROOT / "data" / "mock" / "mails" / "mock_mails.json").read_text())["threads"]


class FakeMail:
    """A mailbox holding the mock threads."""

    def __init__(self):
        self.read_ids = []

    def search(self, faculty_id, query, max_results=5):
        words = [x for x in query.lower().split() if not x.startswith("newer_than")]
        hits = [t for t in THREADS if all(x in json.dumps(t).lower() for x in words)]
        return [{"id": t["id"], "thread_id": t["id"], "sender": t["messages"][0]["from"],
                 "subject": t["messages"][0]["subject"], "date": t["messages"][0]["date"], "snippet": "..."}
                for t in hits[:max_results]]

    def read(self, faculty_id, message_id):
        self.read_ids.append(message_id)
        t = next(t for t in THREADS if t["id"] == message_id)
        return {"id": message_id, "subject": t["messages"][0]["subject"],
                "text": "\n\n---\n\n".join(m["body"] for m in t["messages"])}


class FakeSender:
    def __init__(self, fail=False):
        self.emails, self.events, self.deleted, self.fail = [], {}, [], fail

    def send_email(self, to, subject, body):
        if self.fail:
            raise ConnectionError("Gmail unreachable")
        self.emails.append((to, subject, body))

    def upsert_event(self, event_id, **event):
        event_id = event_id or f"ev{len(self.events) + 1}"
        self.events[event_id] = event
        return event_id

    def delete_event(self, event_id):
        self.deleted.append(event_id)
        self.events.pop(event_id, None)


@pytest.fixture
def mode(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "admin_email", "admin@example.com")
    monkeypatch.setattr(s, "notify_mode", "admin")
    return s


@pytest.fixture
def mail(monkeypatch):
    box = FakeMail()
    monkeypatch.setattr(mcp_servers, "mail_reader", box)
    return box


def person(db, email):
    return db.scalar(select(Faculty).where(Faculty.email == email))


async def test_a_booking_starts_from_a_mail_and_ends_with_an_invite(w, mail, mode):
    t01 = THREADS[0]["expected_booking"]
    llm = ScriptedLLM(
        Turn(intent="mail", mail_query="guest lecture generative"),
        Turn(intent="book", request=BookingRequest(
            title="Guest lecture on Generative AI", category="guest_lecture", format=t01["format"],
            expected_headcount=t01["headcount"], dates=[t01["date"]], start_time=t01["start"], end_time=t01["end"],
            guest_profile="external_academic")))
    r = await say(w, w.me, llm, "The guest lecture details are in my mail")
    assert [m["id"] for m in r["data"]["mails"]] == ["T01"] and mail.read_ids == []  # nothing read yet

    r = await say(w, w.me, llm, "Use this mail", r["session_id"], {"use_mail": "T01"})
    assert mail.read_ids == ["T01"]
    assert r["reply"].startswith('From "Confirmation: Guest lecture on Generative AI for III year CSE" I read: '
                                 '"Guest lecture on Generative AI", 90 people, talk, Mon 23 Nov 2026, 10:00 to 12:30')
    codes = [o["code"] for o in r["data"]["options"]]
    assert "MBA SH" in codes

    r = await say(w, w.me, llm, "Request MBA SH", r["session_id"], {"pick": "MBA SH"})
    r = await say(w, w.me, llm, "Yes", r["session_id"], {"confirm": True})
    b = w.scalar(select(Booking).where(Booking.status == "pending"))
    assert b.venue.code == "MBA SH"

    sender = FakeSender()
    notify.deliver(w, sender)
    assert sender.events == {}  # no invite while it waits for approval
    assert {to for to, *_ in sender.emails} == {"admin@example.com"}  # test mode: only the admin gets mail
    assert any("meant for vijayalakshmi.p@example.com" in body and "approve or reject" in body
               for _, _, body in sender.emails)

    in_charge = person(w, "vijayalakshmi.p@example.com")
    bk.approve(w, in_charge, b.id)
    notify.deliver(w, sender)
    (event_id, event), = sender.events.items()
    assert event["summary"] == "Guest lecture on Generative AI" and event["location"] == "MBA Seminar Hall"
    assert event["start"] == datetime(2026, 11, 23, 10, tzinfo=IST) and event["attendees"] == ["admin@example.com"]
    assert "sekar.a@example.com" in event["description"] and b.calendar_event_id == event_id
    assert not w.scalars(select(Notification).where(Notification.status == "queued",
                                                    Notification.channel != "in_app")).all()


async def test_live_mode_invites_everyone_and_changes_move_the_same_event(w, mode):
    mode.notify_mode = "live"
    me, in_charge = w.me, person(w, "rajendran.p@example.com")  # CSE PG SH
    venue = w.scalar(select(bk.Venue.id).where(bk.Venue.code == "CSE PG SH"))
    event = bk.request_booking(w, me, title="Club meeting", category="meeting", format="talk", expected_headcount=40,
                               slots=[bk.Slot(venue, datetime(2026, 11, 7, 14, tzinfo=IST),
                                              datetime(2026, 11, 7, 15, tzinfo=IST))],
                               co_organisers=[("co@example.com", "Co")])
    b = event.bookings[0]
    bk.approve(w, in_charge, b.id)
    sender = FakeSender()
    notify.deliver(w, sender)
    (event_id, ev), = sender.events.items()
    assert set(ev["attendees"]) == {"sekar.a@example.com", "co@example.com", "rajendran.p@example.com"}
    assert {to for to, *_ in sender.emails} >= {"sekar.a@example.com", "co@example.com"}

    change = bk.request_change(w, me, b.id, start=datetime(2026, 11, 7, 16, tzinfo=IST),
                               end=datetime(2026, 11, 7, 17, tzinfo=IST))
    notify.deliver(w, sender)
    assert sender.events[event_id]["start"].hour == 14  # unchanged until approved
    bk.approve(w, in_charge, change.id)
    notify.deliver(w, sender)
    assert list(sender.events) == [event_id] and sender.events[event_id]["start"].hour == 16
    assert change.calendar_event_id == event_id and w.get(Booking, b.id).calendar_event_id is None

    bk.cancel(w, me, change.id)
    notify.deliver(w, sender)
    assert sender.deleted == [event_id] and sender.events == {}


async def test_failures_are_retried_then_given_up(w, mode):
    venue = w.scalar(select(bk.Venue.id).where(bk.Venue.code == "CC1"))
    bk.request_booking(w, w.me, title="Lab", category="training", format="hands-on", expected_headcount=20,
                       slots=[bk.Slot(venue, datetime(2026, 11, 7, 14, tzinfo=IST),
                                      datetime(2026, 11, 7, 15, tzinfo=IST))])
    for _ in range(notify.MAX_ATTEMPTS):
        notify.deliver(w, FakeSender(fail=True))
    rows = w.scalars(select(Notification).where(Notification.channel == "email")).all()
    assert rows and all(n.status == "failed" and "Gmail unreachable" in n.last_error for n in rows)


async def test_off_mode_sends_nothing(w, mode):
    mode.notify_mode = "off"
    venue = w.scalar(select(bk.Venue.id).where(bk.Venue.code == "CC1"))
    bk.request_booking(w, w.me, title="Lab", category="training", format="hands-on", expected_headcount=20,
                       slots=[bk.Slot(venue, datetime(2026, 11, 7, 14, tzinfo=IST),
                                      datetime(2026, 11, 7, 15, tzinfo=IST))])
    sender = FakeSender()
    assert notify.deliver(w, sender) == {"emails": 0, "calendar": 0} and sender.emails == []


async def test_without_a_linked_gmail_the_agent_asks_to_link_it(w, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "secrets_dir", tmp_path)
    llm = ScriptedLLM(Turn(intent="mail", mail_query="hackathon"))
    r = await say(w, w.me, llm, "check my gmail for the hackathon request")
    assert r["data"] == {"link_gmail": True} and "isn't linked" in r["reply"]


async def test_drive_tools_show_the_source(w):
    from mcp.shared.memory import create_connected_server_and_client_session

    doc = SourceDocument(name="CIRCULAR 7 - Admission counselling.pdf", kind="circular", drive_file_id="abc123")
    w.add(doc)
    w.flush()
    text = "\n".join(f"Line {i}: admission counselling in A Block Seminar Hall" for i in range(80))
    documents.index_document(w, doc, text, "Admission counselling", FakeEmbedder())
    token = mcp_servers.acting_faculty_id.set(w.me.id)
    try:
        async with create_connected_server_and_client_session(mcp_servers.drive_mcp._mcp_server) as client:
            listed = (await client.call_tool("list_documents", {"kind": "circular"})).structuredContent["result"]
            read = json.loads((await client.call_tool("read_document", {"name": "Admission counselling"}))
                              .content[0].text)
    finally:
        mcp_servers.acting_faculty_id.reset(token)
    assert listed[0]["link"] == "https://drive.google.com/file/d/abc123/view"
    assert read["text"] == text  # chunks put back together without the repeated overlap
