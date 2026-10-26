"""The MCP servers: everything the agent can do, as narrow tools with strict schemas.

Three servers, mounted in the API at /mcp/booking/mcp, /mcp/knowledge/mcp and /mcp/recommender/mcp
(streamable HTTP), so the agent, Claude Desktop or the MCP Inspector all use the same tools.

Every call acts as one faculty member. Over HTTP that is the bearer token the backend mints for the
signed-in user (`mint_token`); for the in-process agent it is `acting_faculty_id`, set by the backend
after it has checked the session; over stdio it is the ACTING_EMAIL environment variable:

    ACTING_EMAIL=you@example.com uv run python -m backend.mcp_servers booking

Booking tools act only on the caller's own bookings and can't approve anything. The agent asks for
an explicit yes before it calls `create_hold`, `request_change` or `cancel_booking`.
"""

import os
import sys
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, datetime, time
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from mcp.server.fastmcp import Context, FastMCP
from sqlalchemy import func, select

from backend import booking as bk
from backend import documents, recommend
from backend.config import get_settings
from backend.db import SessionLocal
from backend.embeddings import get_embedder
from backend.models import ACTIVE_BOOKING_STATUSES, Faculty, UsagePattern, Venue
from backend.recommend import BookingRequest

TOKEN_MAX_AGE = 3600

# Who the in-process agent is acting for. Only the backend sets it, after signing the faculty in.
acting_faculty_id: ContextVar[int | None] = ContextVar("acting_faculty_id", default=None)

# Swappable in tests.
session_factory = SessionLocal
embedder_factory = get_embedder

booking_mcp = FastMCP("venue-booking", instructions="Venues, availability and the caller's own bookings.")
knowledge_mcp = FastMCP("venue-knowledge", instructions="Search circulars and event reports; venue details.")
recommender_mcp = FastMCP("venue-recommender", instructions="Similar past events, seasonal patterns, ranking.")
SERVERS = {"booking": booking_mcp, "knowledge": knowledge_mcp, "recommender": recommender_mcp}


class ToolError(Exception):
    pass


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="mcp")


def mint_token(faculty_id: int) -> str:
    return _serializer().dumps(faculty_id)


def _actor(db, ctx: Context | None) -> Faculty:
    request = getattr(getattr(ctx, "request_context", None), "request", None) if ctx else None
    faculty = None
    if (fid := acting_faculty_id.get()) is not None:
        faculty = db.get(Faculty, fid)
    elif request is not None:
        auth = request.headers.get("authorization", "")
        try:
            faculty = db.get(Faculty, _serializer().loads(auth.removeprefix("Bearer "), max_age=TOKEN_MAX_AGE))
        except BadSignature as e:
            raise ToolError("Not signed in.") from e
    elif email := os.environ.get("ACTING_EMAIL"):
        faculty = db.scalar(select(Faculty).where(func.lower(Faculty.email) == email.lower()))
    if faculty is None or not faculty.active:
        raise ToolError("Not signed in as anyone on the faculty list.")
    return faculty


def _json(value: Any) -> Any:
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def _venue(db, code: str) -> Venue:
    key = code.lower().strip()
    v = next((v for v in db.scalars(select(Venue).where(Venue.active))
              if key in (v.code.lower(), v.name.lower(), *(a.lower() for a in v.aliases))), None)
    if v is None:
        raise ToolError(f"No venue called {code!r}. Use list_venues to see the codes.")
    return v


def _booking_out(b) -> dict:
    return {"booking_id": b.id, "event_id": b.event_id, "title": b.event.title, "venue": b.venue.code,
            "start": bk.local(b.start_ts).isoformat(), "end": bk.local(b.end_ts).isoformat(), "status": b.status,
            "soft_conflict": b.soft_conflict_note, "replaces_booking_id": b.replaces_booking_id}


def _run(fn):
    """Run a booking-rule call, turning refusals into readable tool errors."""
    try:
        return fn()
    except bk.BookingError as e:
        raise ToolError(f"{e.message} {'; '.join(c['detail'] for c in e.details if 'detail' in c)}".strip()) from e


# --- booking ------------------------------------------------------------------------------------------


@booking_mcp.tool()
def list_venues(type: str | None = None, min_capacity: int | None = None, software: str | None = None,
                ctx: Context = None) -> list[dict]:
    """List bookable venues. type is computer_lab, seminar_hall or coe_lab; software filters lab computers."""
    with session_factory() as db:
        _actor(db, ctx)
        q = select(Venue).where(Venue.active).order_by(Venue.type, Venue.id)
        if type:
            q = q.where(Venue.type == type)
        if min_capacity:
            q = q.where(Venue.capacity >= min_capacity)
        rows = [v for v in db.scalars(q) if not software or any(software.lower() in s.lower() for s in v.software)]
        return [{"code": v.code, "name": v.name, "type": v.type, "capacity": v.capacity,
                 "computers": v.n_computers, "byod": v.byod, "equipment": v.equipment,
                 "in_charge": v.in_charge.name if v.in_charge else None} for v in rows]


@booking_mcp.tool()
def check_availability(venue_codes: list[str], day: date, start_time: time, end_time: time,
                       ctx: Context = None) -> list[dict]:
    """Whether each venue is free on a day between two times, with any clashes and seasonal warnings."""
    with session_factory() as db:
        _actor(db, ctx)
        out = []
        for code in venue_codes:
            v = _venue(db, code)
            start, end = datetime.combine(day, start_time, bk._tz()), datetime.combine(day, end_time, bk._tz())
            clashes = bk.find_clashes(db, v.id, start, end)
            out.append({"venue": v.code, "free": not clashes, "clashes": [asdict(c) for c in clashes],
                        "warnings": [asdict(c) for c in bk.find_soft_conflicts(db, v.id, start, end)]})
        return _json(out)


@booking_mcp.tool()
def find_free_venues(request: BookingRequest, ctx: Context = None) -> dict:
    """Venues that suit the event and are free on every day, and why the others were left out."""
    with session_factory() as db:
        _actor(db, ctx)
        free, excluded = recommend.find_free_venues(db, request)
        return {"free": [v.code for v in free], "excluded": excluded}


@booking_mcp.tool()
def create_hold(request: BookingRequest, venue_code: str, ctx: Context = None) -> dict:
    """Request a venue for every day of the event. The booking waits for the venue in-charge's approval."""
    if request.missing():
        raise ToolError(f"Still need: {', '.join(request.missing())}.")
    with session_factory.begin() as db:
        actor = _actor(db, ctx)
        v = _venue(db, venue_code)
        event = _run(lambda: bk.request_booking(
            db, actor, title=request.title or (request.category or "Event").replace("_", " ").title(),
            category=request.category or "other", format=request.format,
            expected_headcount=request.expected_headcount, significance=request.significance or 1,
            guest_profile=request.guest_profile, audience=request.audience, requirements=request.equipment,
            slots=[bk.Slot(v.id, s, e) for s, e in request.slots()]))
        return {"event_id": event.id, "bookings": [_booking_out(b) for b in event.bookings]}


@booking_mcp.tool()
def list_my_bookings(include_past: bool = False, ctx: Context = None) -> list[dict]:
    """The caller's bookings, newest first."""
    with session_factory() as db:
        actor = _actor(db, ctx)
        now = bk._now()
        return [_booking_out(b) for b in bk.mine(db, actor)
                if include_past or (b.status in ACTIVE_BOOKING_STATUSES and b.end_ts > now)]


@booking_mcp.tool()
def edit_booking(event_id: int, title: str | None = None, expected_headcount: int | None = None,
                 description: str | None = None, ctx: Context = None) -> dict:
    """Minor edits that need no approval: title, description, headcount within the venue's capacity."""
    changes = {k: v for k, v in (("title", title), ("expected_headcount", expected_headcount),
                                 ("description", description)) if v is not None}
    with session_factory.begin() as db:
        actor = _actor(db, ctx)
        event = _run(lambda: bk.edit_event(db, actor, event_id, **changes))
        return {"event_id": event.id, "bookings": [_booking_out(b) for b in event.bookings]}


@booking_mcp.tool()
def request_change(booking_id: int, venue_code: str | None = None, day: date | None = None,
                   start_time: time | None = None, end_time: time | None = None, ctx: Context = None) -> dict:
    """Move a booking to another venue, day or time. A confirmed booking stays until the change is approved."""
    with session_factory.begin() as db:
        actor = _actor(db, ctx)
        b = db.get(bk.Booking, booking_id)
        if b is None:
            raise ToolError(f"There is no booking {booking_id}.")
        start, end = bk.local(b.start_ts), bk.local(b.end_ts)
        d = day or start.date()
        new_start = datetime.combine(d, start_time or start.time(), bk._tz())
        new_end = datetime.combine(d, end_time or end.time(), bk._tz())
        venue_id = _venue(db, venue_code).id if venue_code else None
        return _booking_out(_run(lambda: bk.request_change(db, actor, booking_id, venue_id=venue_id,
                                                           start=new_start, end=new_end)))


@booking_mcp.tool()
def cancel_booking(booking_id: int, reason: str | None = None, ctx: Context = None) -> dict:
    """Cancel one of the caller's bookings. Everyone involved is told."""
    with session_factory.begin() as db:
        actor = _actor(db, ctx)
        return _booking_out(_run(lambda: bk.cancel(db, actor, booking_id, reason)))


# --- knowledge ----------------------------------------------------------------------------------------


@knowledge_mcp.tool()
def search_knowledge(query: str, kind: str | None = None, k: int = 6, ctx: Context = None) -> list[dict]:
    """Search circulars and event reports. kind is circular or report. Cite the document names you use."""
    with session_factory() as db:
        _actor(db, ctx)
        hits = documents.search(db, query, embedder_factory(), min(k, 12), kind=kind)
        return _json([{"document": h.document, "kind": h.kind, "date": h.doc_date, "text": h.text} for h in hits])


@knowledge_mcp.tool()
def get_venue_rules(venue_code: str, ctx: Context = None) -> dict:
    """Details for one venue: capacity, equipment, software, typical use and who approves bookings."""
    with session_factory() as db:
        _actor(db, ctx)
        v = _venue(db, venue_code)
        return {"code": v.code, "name": v.name, "type": v.type, "capacity": v.capacity, "computers": v.n_computers,
                "byod": v.byod, "equipment": v.equipment, "software": v.software, "typical_use": v.typical_use,
                "approver": {"name": v.in_charge.name, "email": v.in_charge.email} if v.in_charge else None,
                "rules": ["Every booking needs the in-charge's approval.",
                          f"Requests not approved within {get_settings().hold_hours} hours are released.",
                          "Seasonal patterns only warn; they never block a booking."]}


# --- recommender --------------------------------------------------------------------------------------


@recommender_mcp.tool()
def find_similar_events(request: BookingRequest, k: int = 8, ctx: Context = None) -> list[dict]:
    """Past events most like this one, with the venue each used."""
    with session_factory() as db:
        _actor(db, ctx)
        return [asdict(s) for s in recommend.similar_events(db, embedder_factory(), request, min(k, 20))]


@recommender_mcp.tool()
def get_usage_patterns(venue_codes: list[str], ctx: Context = None) -> list[dict]:
    """Seasonal patterns (soft conflicts) for venues: what each is usually used for, and when."""
    with session_factory() as db:
        _actor(db, ctx)
        ids = [_venue(db, c).id for c in venue_codes]
        return [{"venue": p.venue.code, "category": p.category, "week_start": p.week_start, "week_end": p.week_end,
                 "weekdays": p.weekdays, "years_seen": p.years_seen, "years_observed": p.years_observed,
                 "evidence": p.evidence} for p in db.scalars(select(UsagePattern).where(UsagePattern.venue_id.in_(ids)))]


@recommender_mcp.tool()
def rank_venues(request: BookingRequest, ctx: Context = None) -> dict:
    """The full recommendation: suitable free venues ranked with reasons and soft conflicts, or other dates."""
    if request.missing():
        raise ToolError(f"Still need: {', '.join(request.missing())}.")
    with session_factory() as db:
        _actor(db, ctx)
        return _json(recommend.recommend(db, embedder_factory(), request))


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "booking"
    SERVERS[name].run("stdio")


if __name__ == "__main__":
    main()
