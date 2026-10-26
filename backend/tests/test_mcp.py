import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy.orm import sessionmaker

from backend import booking as bk
from backend import mcp_servers, patterns, seed
from conftest import FakeEmbedder

IST = ZoneInfo("Asia/Kolkata")
pytestmark = pytest.mark.anyio
FACULTY = "sekar.a@example.com"


@pytest.fixture
def servers(db, monkeypatch):
    monkeypatch.setattr(bk, "_now", lambda: datetime(2026, 10, 22, 9, tzinfo=IST))
    seed.run(db, admin_email="admin@example.com", with_past_events=True, embedder=FakeEmbedder())
    db.flush()
    patterns.compute(db, date(2026, 10, 22))
    monkeypatch.setattr(mcp_servers, "session_factory",
                        sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint",
                                     expire_on_commit=False))
    monkeypatch.setattr(mcp_servers, "embedder_factory", FakeEmbedder)
    monkeypatch.setenv("ACTING_EMAIL", FACULTY)
    return db


async def call(server, tool, **args):
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        res = await client.call_tool(tool, args)
    text = res.content[0].text if res.content else ""
    if res.isError:
        raise RuntimeError(text)
    if res.structuredContent is not None:
        return res.structuredContent.get("result", res.structuredContent)
    return json.loads(text)


REQUEST = {"title": "Guest lecture on cloud computing", "category": "guest_lecture", "format": "talk",
           "expected_headcount": 80, "dates": ["2026-11-07"], "start_time": "10:00", "end_time": "12:00"}


async def test_tools_are_listed(servers):
    async with create_connected_server_and_client_session(mcp_servers.booking_mcp._mcp_server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"list_venues", "check_availability", "create_hold", "cancel_booking"} <= names
    assert not any("approve" in n for n in names)  # the agent can never confirm a booking


async def test_hold_change_cancel(servers):
    halls = await call(mcp_servers.booking_mcp, "list_venues", type="seminar_hall", min_capacity=100)
    assert {"A Block SH", "D Block SH"} <= {v["code"] for v in halls}

    ranked = await call(mcp_servers.recommender_mcp, "rank_venues", request=REQUEST)
    best = ranked["options"][0]["code"]
    held = await call(mcp_servers.booking_mcp, "create_hold", request=REQUEST, venue_code=best)
    booking = held["bookings"][0]
    assert booking["status"] == "pending" and booking["venue"] == best

    avail = await call(mcp_servers.booking_mcp, "check_availability", venue_codes=[best], day="2026-11-07",
                       start_time="11:00", end_time="13:00")
    assert avail[0]["free"] is False

    mine = await call(mcp_servers.booking_mcp, "list_my_bookings")
    assert [b["booking_id"] for b in mine] == [booking["booking_id"]]

    moved = await call(mcp_servers.booking_mcp, "request_change", booking_id=booking["booking_id"],
                       start_time="14:00", end_time="16:00")
    assert moved["start"].startswith("2026-11-07T14:00")

    cancelled = await call(mcp_servers.booking_mcp, "cancel_booking", booking_id=moved["booking_id"])
    assert cancelled["status"] == "cancelled"


async def test_refusals_are_readable(servers):
    with pytest.raises(RuntimeError, match="Still need"):
        await call(mcp_servers.booking_mcp, "create_hold", request={"title": "x"}, venue_code="CC1")
    with pytest.raises(RuntimeError, match="No venue"):
        await call(mcp_servers.knowledge_mcp, "get_venue_rules", venue_code="Moon Base")


async def test_unknown_caller_is_refused(servers, monkeypatch):
    monkeypatch.setenv("ACTING_EMAIL", "stranger@example.com")
    with pytest.raises(RuntimeError, match="faculty list"):
        await call(mcp_servers.booking_mcp, "list_my_bookings")


async def test_cannot_cancel_someone_elses_booking(servers, monkeypatch):
    held = await call(mcp_servers.booking_mcp, "create_hold", request=REQUEST, venue_code="D Block SH")
    monkeypatch.setenv("ACTING_EMAIL", "babu.g@example.com")
    with pytest.raises(RuntimeError):
        await call(mcp_servers.booking_mcp, "cancel_booking", booking_id=held["bookings"][0]["booking_id"])


async def test_recommender_tools(servers):
    similar = await call(mcp_servers.recommender_mcp, "find_similar_events", request=REQUEST, k=5)
    assert len(similar) == 5 and all(s["venue"] for s in similar)
    pats = await call(mcp_servers.recommender_mcp, "get_usage_patterns", venue_codes=["A Block SH"])
    assert any(p["category"] == "admissions" for p in pats)
