"""The HTTP API: venues, availability, the booking lifecycle, approvals and the faculty allowlist.

    uv run uvicorn backend.app:app --reload
"""

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime, time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from backend import booking as bk
from backend import timetable
from backend.auth import _out as faculty_out
from backend.auth import current_user, require_admin
from backend.auth import router as auth_router
from backend.config import get_settings
from backend.db import SessionLocal, get_db
from backend.models import (
    FORMATS,
    REVIEW_STATUSES,
    ROLES,
    Booking,
    BookingHistory,
    Faculty,
    Notification,
    RecurringSlot,
    SourceDocument,
    Venue,
)

ERROR_STATUS = {"not_found": 404, "forbidden": 403, "clash": 409, "overlaps_current": 409, "bad_state": 409}


def run_jobs() -> None:
    with SessionLocal.begin() as db:
        bk.expire_holds(db)
        bk.remind_in_charges(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_jobs, "interval", minutes=10, next_run_time=datetime.now())
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Venue Booking Agent", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=get_settings().session_secret, same_site="lax",
                   max_age=7 * 24 * 3600)
app.include_router(auth_router)


@app.exception_handler(bk.BookingError)
def booking_error(request: Request, e: bk.BookingError):
    return JSONResponse({"code": e.code, "detail": e.message, "details": e.details},
                        status_code=ERROR_STATUS.get(e.code, 422))


# --- schemas --------------------------------------------------------------------------------------


class SlotIn(BaseModel):
    venue_id: int
    start: datetime
    end: datetime


class PartyIn(BaseModel):
    email: str
    name: str | None = None


class EventIn(BaseModel):
    title: str = Field(min_length=3)
    category: str
    format: str = Field(pattern="|".join(FORMATS))
    expected_headcount: int = Field(gt=0)
    slots: list[SlotIn] = Field(min_length=1)
    description: str | None = None
    significance: int = Field(1, ge=1, le=3)
    guest_profile: str | None = None
    audience: str | None = None
    requirements: list[str] = []
    co_organisers: list[PartyIn] = []


class EventEdit(BaseModel):
    title: str | None = Field(None, min_length=3)
    description: str | None = None
    expected_headcount: int | None = Field(None, gt=0)
    audience: str | None = None
    requirements: list[str] | None = None
    guest_profile: str | None = None


class ChangeIn(BaseModel):
    venue_id: int | None = None
    start: datetime | None = None
    end: datetime | None = None


class ReasonIn(BaseModel):
    reason: str | None = None


class FacultyIn(BaseModel):
    email: str
    name: str
    department: str | None = None
    role: str = Field("faculty", pattern="|".join(ROLES))


class SlotEdit(BaseModel):
    venue_id: int | None = None
    weekday: int | None = Field(None, ge=0, le=6)
    start_time: time | None = None
    end_time: time | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    course: str | None = None
    batch: str | None = None
    status: str | None = Field(None, pattern="|".join(REVIEW_STATUSES))


class FacultyEdit(BaseModel):
    name: str | None = None
    department: str | None = None
    role: str | None = Field(None, pattern="|".join(ROLES))
    active: bool | None = None


def venue_out(v: Venue) -> dict:
    return {"id": v.id, "code": v.code, "name": v.name, "type": v.type, "capacity": v.capacity,
            "n_computers": v.n_computers, "byod": v.byod, "equipment": v.equipment, "software": v.software,
            "typical_use": v.typical_use, "in_charge": v.in_charge.name if v.in_charge else None}


def booking_out(b: Booking, user: Faculty | None = None) -> dict:
    e = b.event
    out = {
        "id": b.id, "event_id": e.id, "title": e.title, "category": e.category, "format": e.format,
        "expected_headcount": e.expected_headcount, "organiser": e.created_by.name,
        "venue": {"id": b.venue.id, "code": b.venue.code, "name": b.venue.name},
        "start": bk.local(b.start_ts).isoformat(), "end": bk.local(b.end_ts).isoformat(), "status": b.status,
        "hold_expires_at": bk.local(b.hold_expires_at).isoformat() if b.hold_expires_at else None,
        "soft_conflict_note": b.soft_conflict_note, "replaces_booking_id": b.replaces_booking_id,
    }
    if user is not None:
        out["is_owner"] = bk.is_owner(user, e)
        out["can_approve"] = b.status == "pending" and bk.can_manage(user, b.venue)
        out["can_change"] = b.status in ("pending", "confirmed") and (bk.is_owner(user, e) or user.role == "admin")
        out["can_cancel"] = b.status in bk.ACTIVE_BOOKING_STATUSES and (
            bk.is_owner(user, e) or bk.can_manage(user, b.venue))
    return out


# --- venues and availability ----------------------------------------------------------------------


@app.get("/me")
def me(user: Faculty = Depends(current_user)):
    return faculty_out(user)


@app.get("/venues")
def venues(db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    return [venue_out(v) for v in db.scalars(select(Venue).where(Venue.active).order_by(Venue.type, Venue.id))]


@app.get("/availability")
def availability(venue_id: int, start: datetime, end: datetime, db: Session = Depends(get_db),
                 user: Faculty = Depends(current_user)):
    clashes = bk.find_clashes(db, venue_id, start, end)
    return {"free": not clashes, "clashes": [asdict(c) for c in clashes],
            "soft_conflicts": [asdict(c) for c in bk.find_soft_conflicts(db, venue_id, start, end)]}


@app.get("/calendar")
def calendar(start: date, end: date, venue_id: int | None = None, db: Session = Depends(get_db),
             user: Faculty = Depends(current_user)):
    """Active bookings and classes between two dates, for the calendar view."""
    bookings = [booking_out(b, user) for b in bk.bookings_between(db, start, end, venue_id)]
    return {"bookings": bookings, "classes": bk.classes_between(db, start, end, venue_id)}


# --- bookings -------------------------------------------------------------------------------------


@app.post("/events", status_code=201)
def create_event(body: EventIn, db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    event = bk.request_booking(
        db, user, title=body.title, category=body.category, format=body.format,
        expected_headcount=body.expected_headcount, description=body.description, significance=body.significance,
        guest_profile=body.guest_profile, audience=body.audience, requirements=body.requirements,
        slots=[bk.Slot(s.venue_id, s.start, s.end) for s in body.slots],
        co_organisers=[(p.email, p.name) for p in body.co_organisers])
    return {"event_id": event.id, "bookings": [booking_out(b, user) for b in event.bookings]}


@app.patch("/events/{event_id}")
def edit_event(event_id: int, body: EventEdit, db: Session = Depends(get_db),
               user: Faculty = Depends(current_user)):
    event = bk.edit_event(db, user, event_id, **body.model_dump(exclude_unset=True))
    return {"event_id": event.id, "bookings": [booking_out(b, user) for b in event.bookings]}


@app.get("/bookings/mine")
def my_bookings(db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    return [booking_out(b, user) for b in bk.mine(db, user)]


@app.get("/approvals")
def approvals(db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    return [booking_out(b, user) for b in bk.awaiting(db, user)]


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: int, db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    b = db.get(Booking, booking_id)
    if b is None:
        raise HTTPException(404, "No such booking.")
    history = db.scalars(select(BookingHistory).where(BookingHistory.booking_id == b.id).order_by(BookingHistory.at))
    return {**booking_out(b, user), "parties": [{"email": p.email, "name": p.name, "role": p.role}
                                                for p in b.event.parties],
            "history": [{"action": h.action, "at": h.at.isoformat(), "reason": h.reason, "before": h.before,
                         "after": h.after} for h in history]}


@app.post("/bookings/{booking_id}/approve")
def approve(booking_id: int, db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    return booking_out(bk.approve(db, user, booking_id), user)


@app.post("/bookings/{booking_id}/reject")
def reject(booking_id: int, body: ReasonIn, db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    return booking_out(bk.reject(db, user, booking_id, body.reason or ""), user)


@app.post("/bookings/{booking_id}/cancel")
def cancel(booking_id: int, body: ReasonIn, db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    return booking_out(bk.cancel(db, user, booking_id, body.reason), user)


@app.post("/bookings/{booking_id}/change")
def change(booking_id: int, body: ChangeIn, db: Session = Depends(get_db), user: Faculty = Depends(current_user)):
    return booking_out(bk.request_change(db, user, booking_id, venue_id=body.venue_id, start=body.start,
                                         end=body.end), user)


@app.get("/notifications")
def notifications(limit: int = Query(30, le=100), db: Session = Depends(get_db),
                  user: Faculty = Depends(current_user)):
    rows = db.scalars(select(Notification).where(Notification.recipient == user.email,
                                                 Notification.channel == "in_app")
                      .order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit))
    return [{"id": n.id, "kind": n.kind, "booking_id": n.booking_id, "payload": n.payload,
             "at": n.created_at.isoformat()} for n in rows]


# --- admin: the faculty allowlist -----------------------------------------------------------------


@app.get("/admin/faculty")
def list_faculty(db: Session = Depends(get_db), admin: Faculty = Depends(require_admin)):
    return [{**faculty_out(f), "active": f.active} for f in db.scalars(select(Faculty).order_by(Faculty.name))]


@app.post("/admin/faculty", status_code=201)
def add_faculty(body: FacultyIn, db: Session = Depends(get_db), admin: Faculty = Depends(require_admin)):
    email = body.email.strip().lower()
    if db.scalar(select(Faculty).where(Faculty.email == email)):
        raise HTTPException(409, "That email is already on the list.")
    f = Faculty(email=email, name=body.name, department=body.department, role=body.role)
    db.add(f)
    db.flush()
    return {**faculty_out(f), "active": True}


@app.patch("/admin/faculty/{faculty_id}")
def edit_faculty(faculty_id: int, body: FacultyEdit, db: Session = Depends(get_db),
                 admin: Faculty = Depends(require_admin)):
    f = db.get(Faculty, faculty_id)
    if f is None:
        raise HTTPException(404, "No such person.")
    changes = body.model_dump(exclude_unset=True)
    if f.id == admin.id and (changes.get("active") is False or changes.get("role", "admin") != "admin"):
        raise HTTPException(409, "You can't remove your own admin access.")
    for k, v in changes.items():
        setattr(f, k, v)
    db.flush()
    return {**faculty_out(f), "active": f.active}


# --- admin: timetables ----------------------------------------------------------------------------


def slot_out(s: RecurringSlot) -> dict:
    return {"id": s.id, "venue_id": s.venue_id, "venue": s.venue.code, "weekday": s.weekday,
            "start_time": s.start_time.strftime("%H:%M"), "end_time": s.end_time.strftime("%H:%M"),
            "valid_from": s.valid_from.isoformat(), "valid_to": s.valid_to.isoformat(), "course": s.course,
            "class_label": s.class_label, "batch": s.batch, "source": s.source_ref, "status": s.status}


@app.get("/admin/timetables")
def list_timetables(db: Session = Depends(get_db), admin: Faculty = Depends(require_admin)):
    docs = db.scalars(select(SourceDocument).where(SourceDocument.kind == "timetable").order_by(SourceDocument.name))
    out = []
    for d in docs:
        slots = list(db.scalars(select(RecurringSlot).where(
            RecurringSlot.source_ref == timetable.SOURCE_PREFIX + d.name)))
        out.append({"id": d.id, "name": d.name, "processed_at": d.processed_at.isoformat() if d.processed_at else None,
                    "problems": d.error, "class_label": slots[0].class_label if slots else None,
                    **{st: sum(s.status == st for s in slots) for st in REVIEW_STATUSES}})
    return out


@app.post("/admin/timetables", status_code=201)
def upload_timetable(file: UploadFile, db: Session = Depends(get_db), admin: Faculty = Depends(require_admin)):
    name = Path(file.filename or "").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(422, "Upload a timetable PDF.")
    folder = get_settings().data_dir / "uploads" / "timetables"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(file.file.read())
    return timetable.ingest_pdf(db, path, force=True)


@app.get("/admin/timetable-slots")
def list_slots(status: str = "pending_review", source: str | None = None, db: Session = Depends(get_db),
               admin: Faculty = Depends(require_admin)):
    q = select(RecurringSlot).where(RecurringSlot.status == status)
    if source:
        q = q.where(RecurringSlot.source_ref == timetable.SOURCE_PREFIX + source)
    return [slot_out(s) for s in db.scalars(q.order_by(RecurringSlot.source_ref, RecurringSlot.weekday,
                                                       RecurringSlot.start_time))]


@app.patch("/admin/timetable-slots/{slot_id}")
def edit_slot(slot_id: int, body: SlotEdit, db: Session = Depends(get_db), admin: Faculty = Depends(require_admin)):
    s = db.get(RecurringSlot, slot_id)
    if s is None:
        raise HTTPException(404, "No such slot.")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    if s.end_time <= s.start_time or s.valid_to < s.valid_from:
        raise HTTPException(422, "The end must be after the start.")
    db.flush()
    db.refresh(s)
    flagged = timetable.flag_class_clashes(db) if s.status == "active" else 0
    return {**slot_out(s), "flagged_bookings": flagged}


# The web UI. Mounted last so the API routes above take precedence.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
