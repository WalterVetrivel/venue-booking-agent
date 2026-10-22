"""Booking rules: availability, and the request / approve / change / cancel lifecycle.

Every function runs inside the caller's transaction and writes the booking change, its
`booking_history` row and its outbox `notification` rows together, so a change is never saved
without its notices. Hard clashes (another booking, a class) refuse the request; soft conflicts
(seasonal patterns, declared reservations) only add a warning. See docs/PLAN.md, "Booking lifecycle".
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    BookingHistory,
    Event,
    EventParty,
    ExpectedReservation,
    Faculty,
    Notification,
    RecurringSlot,
    UsagePattern,
    Venue,
)

MINOR_FIELDS = ("title", "description", "expected_headcount", "audience", "requirements", "guest_profile")
NOTIFY_CHANNELS = ("email", "in_app")  # calendar invites arrive in Phase 4
SEASON_MARGIN_WEEKS = 1  # warn a week either side of a seasonal pattern; past dates drift a little


class BookingError(Exception):
    """A request the rules refuse. `code` is stable for the API and the agent; `message` is for people."""

    def __init__(self, code: str, message: str, details: list | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or []


@dataclass
class Slot:
    venue_id: int
    start: datetime
    end: datetime


@dataclass
class Clash:
    kind: str  # "booking" or "class"
    detail: str
    start: str
    end: str


@dataclass
class SoftConflict:
    category: str
    source: str  # "pattern" (seen in past years) or "declared" (added by an admin)
    confidence: float
    note: str
    evidence: list = field(default_factory=list)


@cache
def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def local(dt: datetime) -> datetime:
    """A timestamp in campus time. Naive values are taken to be campus time already."""
    return dt.replace(tzinfo=_tz()) if dt.tzinfo is None else dt.astimezone(_tz())


def _now() -> datetime:
    return datetime.now(_tz())


# --- availability -------------------------------------------------------------------------------


def find_clashes(db: Session, venue_id: int, start: datetime, end: datetime,
                 ignore_booking_ids: tuple[int, ...] = ()) -> list[Clash]:
    """Hard clashes: active bookings and active timetable classes overlapping the slot."""
    start, end = local(start), local(end)
    clashes = []
    bookings = db.scalars(select(Booking).where(
        Booking.venue_id == venue_id, Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        Booking.start_ts < end, Booking.end_ts > start, Booking.id.not_in(ignore_booking_ids),
    ).order_by(Booking.start_ts))
    for b in bookings:
        clashes.append(Clash("booking", f"{b.event.title} ({b.status})", local(b.start_ts).isoformat(),
                             local(b.end_ts).isoformat()))
    day = start.date()
    classes = db.scalars(select(RecurringSlot).where(
        RecurringSlot.venue_id == venue_id, RecurringSlot.status == "active",
        RecurringSlot.weekday == day.weekday(), RecurringSlot.valid_from <= day, RecurringSlot.valid_to >= day,
        RecurringSlot.start_time < end.time(), RecurringSlot.end_time > start.time(),
    ).order_by(RecurringSlot.start_time))
    for c in classes:
        label = " ".join(x for x in (c.class_label, c.batch) if x)
        clashes.append(Clash("class", f"{c.course} for {label}",
                             datetime.combine(day, c.start_time, _tz()).isoformat(),
                             datetime.combine(day, c.end_time, _tz()).isoformat()))
    return clashes


def _week_in(week: int, start: int, end: int) -> bool:
    return start <= week <= end if start <= end else week >= start or week <= end  # windows can wrap the year


def find_soft_conflicts(db: Session, venue_id: int, start: datetime, end: datetime) -> list[SoftConflict]:
    """Seasonal patterns and declared reservations for the slot's day. These warn, never block."""
    day = local(start).date()
    week = day.isocalendar().week
    conflicts = []
    for p in db.scalars(select(UsagePattern).where(UsagePattern.venue_id == venue_id)):
        start, end = p.week_start - SEASON_MARGIN_WEEKS, p.week_end + SEASON_MARGIN_WEEKS
        if _week_in(week, (start - 1) % 52 + 1, (end - 1) % 52 + 1) and (not p.weekdays or day.weekday() in p.weekdays):
            conflicts.append(SoftConflict(
                p.category, "pattern", p.confidence,
                f"Usually used for {p.category} around this time ({p.years_seen} of the last {p.years_observed} years).",
                p.evidence or []))
    for r in db.scalars(select(ExpectedReservation).where(
            ExpectedReservation.venue_id == venue_id, ExpectedReservation.start_date <= day,
            ExpectedReservation.end_date >= day)):
        who = f" by {r.owner_team}" if r.owner_team else ""
        conflicts.append(SoftConflict(
            r.category, "declared", 1.0,
            f"Expected to be needed for {r.category}{who} from {r.start_date:%d %b} to {r.end_date:%d %b}."
            + (f" {r.note}" if r.note else "")))
    return conflicts


def _soft_note(conflicts: list[SoftConflict]) -> str | None:
    return " ".join(c.note for c in conflicts) or None


def _validate_slot(db: Session, slot: Slot, headcount: int, now: datetime) -> Venue:
    venue = db.get(Venue, slot.venue_id)
    if venue is None or not venue.active:
        raise BookingError("unknown_venue", f"There is no bookable venue with id {slot.venue_id}.")
    start, end = local(slot.start), local(slot.end)
    if end <= start:
        raise BookingError("bad_time", "The end time must be after the start time.")
    if start.date() != end.date():
        raise BookingError("bad_time", "A booking covers one day; book each day of a multi-day event separately.")
    if start < now:
        raise BookingError("in_past", "That time has already passed.")
    if headcount > venue.capacity:
        raise BookingError("over_capacity", f"{venue.name} seats {venue.capacity}, fewer than the {headcount} expected.")
    return venue


def _check_free(db: Session, slot: Slot, ignore: tuple[int, ...] = ()) -> None:
    clashes = find_clashes(db, slot.venue_id, slot.start, slot.end, ignore)
    if clashes:
        raise BookingError("clash", "The venue is not free at that time.", [asdict(c) for c in clashes])


def _flush_guarded(db: Session) -> None:
    """Flush, turning a lost race on the exclusion constraint into a normal clash error."""
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as e:
        if "no_overlapping_active_bookings" in str(e.orig):
            raise BookingError("clash", "Someone else has just booked the venue at that time.") from e
        raise


# --- history and notifications --------------------------------------------------------------------


def snapshot(b: Booking) -> dict:
    return {"booking_id": b.id, "venue": b.venue.code if b.venue else b.venue_id,
            "start": local(b.start_ts).isoformat(), "end": local(b.end_ts).isoformat(), "status": b.status}


def _record(db: Session, b: Booking, actor: Faculty | None, action: str, before: dict | None = None,
            reason: str | None = None) -> None:
    db.add(BookingHistory(booking_id=b.id, actor_id=actor.id if actor else None, action=action,
                          before=before, after=snapshot(b), reason=reason))


def _notify(db: Session, b: Booking, kind: str, recipients: set[str], **payload) -> None:
    payload = {"title": b.event.title, "booking": snapshot(b), **payload}
    for email in sorted(recipients):
        for channel in NOTIFY_CHANNELS:
            db.add(Notification(event_id=b.event_id, booking_id=b.id, recipient=email, channel=channel,
                                kind=kind, payload=payload))


def _everyone(event: Event, *extra: str | None) -> set[str]:
    return {p.email for p in event.parties} | {e for e in extra if e}


def _in_charge_email(venue: Venue) -> str | None:
    return venue.in_charge.email if venue.in_charge else None


def _add_party(event: Event, email: str | None, name: str | None, role: str) -> None:
    if email and not any(p.email == email and p.role == role for p in event.parties):
        event.parties.append(EventParty(email=email.lower(), name=name, role=role))


# --- permissions ----------------------------------------------------------------------------------


def is_owner(actor: Faculty, event: Event) -> bool:
    return event.created_by_id == actor.id


def can_manage(actor: Faculty, venue: Venue) -> bool:
    return actor.role == "admin" or venue.in_charge_id == actor.id


def _get(db: Session, booking_id: int) -> Booking:
    b = db.get(Booking, booking_id, with_for_update=True)
    if b is None:
        raise BookingError("not_found", f"There is no booking {booking_id}.")
    return b


# --- lifecycle --------------------------------------------------------------------------------------


def request_booking(db: Session, actor: Faculty, *, title: str, category: str, format: str,
                    expected_headcount: int, slots: list[Slot], description: str | None = None,
                    significance: int = 1, guest_profile: str | None = None, audience: str | None = None,
                    requirements: list[str] | None = None, co_organisers: list[tuple[str, str | None]] = (),
                    now: datetime | None = None) -> Event:
    """Create an event with one pending booking per slot, each waiting for its venue's in-charge."""
    now = now or _now()
    if not slots:
        raise BookingError("no_slots", "Pick at least one venue and time.")
    venues = [_validate_slot(db, s, expected_headcount, now) for s in slots]
    for s in slots:
        _check_free(db, s)

    event = Event(title=title, category=category, format=format, expected_headcount=expected_headcount,
                  significance=significance, guest_profile=guest_profile, organiser_dept=actor.department,
                  audience=audience, requirements=requirements or [], description=description, created_by=actor)
    db.add(event)
    _add_party(event, actor.email, actor.name, "organiser")
    for email, name in co_organisers:
        _add_party(event, email, name, "co_organiser")
    for v in venues:
        _add_party(event, _in_charge_email(v), v.in_charge.name if v.in_charge else None, "in_charge")

    bookings = []
    for s, v in zip(slots, venues):
        b = Booking(event=event, venue=v, start_ts=local(s.start), end_ts=local(s.end), status="pending",
                    requested_by_id=actor.id, hold_expires_at=now + timedelta(hours=get_settings().hold_hours),
                    soft_conflict_note=_soft_note(find_soft_conflicts(db, v.id, s.start, s.end)))
        db.add(b)
        bookings.append(b)
    _flush_guarded(db)

    for b in bookings:
        _record(db, b, actor, "requested")
        _notify(db, b, "request_received", {_in_charge_email(b.venue)} - {None},
                soft_conflict=b.soft_conflict_note)
        _notify(db, b, "request_submitted", _everyone(event) - {_in_charge_email(b.venue)},
                soft_conflict=b.soft_conflict_note)
    return event


def approve(db: Session, actor: Faculty, booking_id: int) -> Booking:
    """Confirm a pending booking. For a change request, the original is released in the same step."""
    b = _get(db, booking_id)
    if not can_manage(actor, b.venue):
        raise BookingError("forbidden", "Only this venue's in-charge or an admin can approve.")
    if b.status != "pending":
        raise BookingError("bad_state", f"This booking is {b.status}, not waiting for approval.")
    classes = [c for c in find_clashes(db, b.venue_id, b.start_ts, b.end_ts, (b.id,)) if c.kind == "class"]
    if classes:  # the timetable may have changed since the request
        raise BookingError("clash", "A class now uses the venue at that time.", [asdict(c) for c in classes])

    before = snapshot(b)
    original = db.get(Booking, b.replaces_booking_id, with_for_update=True) if b.replaces_booking_id else None
    if original is not None and original.status in ACTIVE_BOOKING_STATUSES:
        old = snapshot(original)
        original.status = "cancelled"
        _flush_guarded(db)
        _record(db, original, actor, "cancelled", old, reason=f"Replaced by booking {b.id}")
    b.status, b.approved_by_id, b.hold_expires_at = "confirmed", actor.id, None
    b.version += 1
    _flush_guarded(db)
    _record(db, b, actor, "approved", before)
    kind = "change_approved" if original is not None else "approved"
    old_in_charge = _in_charge_email(original.venue) if original is not None else None
    _notify(db, b, kind, _everyone(b.event, old_in_charge) - {actor.email},
            previous=snapshot(original) if original is not None else None)
    return b


def reject(db: Session, actor: Faculty, booking_id: int, reason: str) -> Booking:
    """Turn down a pending booking. For a change request, the original booking stands."""
    b = _get(db, booking_id)
    if not can_manage(actor, b.venue):
        raise BookingError("forbidden", "Only this venue's in-charge or an admin can reject.")
    if b.status != "pending":
        raise BookingError("bad_state", f"This booking is {b.status}, not waiting for approval.")
    if not reason or not reason.strip():
        raise BookingError("reason_required", "Give a reason so the organiser knows what to change.")
    before = snapshot(b)
    b.status, b.hold_expires_at = "rejected", None
    db.flush()
    _record(db, b, actor, "rejected", before, reason)
    kind = "change_rejected" if b.replaces_booking_id else "rejected"
    _notify(db, b, kind, _everyone(b.event) - {actor.email}, reason=reason)
    return b


def cancel(db: Session, actor: Faculty, booking_id: int, reason: str | None = None) -> Booking:
    """Release a booking. The organiser can cancel their own; an in-charge or admin must give a reason."""
    b = _get(db, booking_id)
    owner = is_owner(actor, b.event)
    if not owner and not can_manage(actor, b.venue):
        raise BookingError("forbidden", "Only the organiser, the venue's in-charge or an admin can cancel.")
    if not owner and not (reason and reason.strip()):
        raise BookingError("reason_required", "Give a reason so the organiser knows why.")
    if b.status not in ACTIVE_BOOKING_STATUSES:
        raise BookingError("bad_state", f"This booking is already {b.status}.")
    before = snapshot(b)
    b.status, b.hold_expires_at = "cancelled", None
    for change in _pending_changes(db, b.id):  # a change to a cancelled booking means nothing now
        old = snapshot(change)
        change.status = "cancelled"
        _record(db, change, actor, "cancelled", old, reason=f"Booking {b.id} was cancelled")
    db.flush()
    _record(db, b, actor, "cancelled", before, reason)
    _notify(db, b, "cancelled", _everyone(b.event) - {actor.email}, reason=reason)
    return b


def _pending_changes(db: Session, booking_id: int) -> list[Booking]:
    return list(db.scalars(select(Booking).where(Booking.replaces_booking_id == booking_id,
                                                 Booking.status == "pending").with_for_update()))


def request_change(db: Session, actor: Faculty, booking_id: int, *, venue_id: int | None = None,
                   start: datetime | None = None, end: datetime | None = None,
                   now: datetime | None = None) -> Booking:
    """Move a booking to another venue or time.

    A pending booking is changed in place and keeps waiting for approval. A confirmed booking stays
    confirmed while a new pending booking for the new slot waits for approval; approving it swaps
    the two, rejecting it leaves the original as it was.
    """
    now = now or _now()
    b = _get(db, booking_id)
    if not is_owner(actor, b.event) and actor.role != "admin":
        raise BookingError("forbidden", "Only the organiser can change this booking.")
    if b.status not in ("pending", "confirmed"):
        raise BookingError("bad_state", f"This booking is {b.status} and can't be changed.")
    slot = Slot(venue_id or b.venue_id, start or b.start_ts, end or b.end_ts)
    if (slot.venue_id, local(slot.start), local(slot.end)) == (b.venue_id, local(b.start_ts), local(b.end_ts)):
        raise BookingError("no_change", "That is the booking's current venue and time.")
    venue = _validate_slot(db, slot, b.event.expected_headcount, now)
    soft = _soft_note(find_soft_conflicts(db, slot.venue_id, slot.start, slot.end))
    hold_until = now + timedelta(hours=get_settings().hold_hours)
    old_in_charge = _in_charge_email(b.venue)
    _add_party(b.event, _in_charge_email(venue), venue.in_charge.name if venue.in_charge else None, "in_charge")

    if b.status == "pending":
        _check_free(db, slot, (b.id,))
        before = snapshot(b)
        b.venue, b.start_ts, b.end_ts = venue, local(slot.start), local(slot.end)
        b.hold_expires_at, b.soft_conflict_note = hold_until, soft
        b.version += 1
        _flush_guarded(db)
        _record(db, b, actor, "edited", before)
        _notify(db, b, "request_changed", _everyone(b.event, old_in_charge) - {actor.email},
                previous=before, soft_conflict=soft)
        return b

    for earlier in _pending_changes(db, b.id):  # a newer change request replaces an older one
        old = snapshot(earlier)
        earlier.status = "cancelled"
        _record(db, earlier, actor, "cancelled", old, reason="Replaced by a newer change request")
    db.flush()
    clashes = find_clashes(db, slot.venue_id, slot.start, slot.end)
    if any(c.kind == "booking" for c in clashes) and slot.venue_id == b.venue_id and \
            local(slot.start) < local(b.end_ts) and local(slot.end) > local(b.start_ts):
        raise BookingError(
            "overlaps_current",
            "The new time overlaps this booking's current time in the same venue. Pick a time that doesn't "
            "overlap, or cancel and book again.", [asdict(c) for c in clashes])
    if clashes:
        raise BookingError("clash", "The venue is not free at that time.", [asdict(c) for c in clashes])
    change = Booking(event=b.event, venue=venue, start_ts=local(slot.start), end_ts=local(slot.end),
                     status="pending", requested_by_id=actor.id, hold_expires_at=hold_until,
                     soft_conflict_note=soft, replaces_booking_id=b.id)
    db.add(change)
    _flush_guarded(db)
    _record(db, change, actor, "change_requested", snapshot(b))
    _notify(db, change, "change_requested", _everyone(b.event, old_in_charge) - {actor.email},
            previous=snapshot(b), soft_conflict=soft)
    return change


def edit_event(db: Session, actor: Faculty, event_id: int, **changes) -> Event:
    """Minor edits that need no re-approval: title, description, headcount within capacity, and so on."""
    event = db.get(Event, event_id, with_for_update=True)
    if event is None:
        raise BookingError("not_found", f"There is no event {event_id}.")
    if not is_owner(actor, event) and actor.role != "admin":
        raise BookingError("forbidden", "Only the organiser can edit this event.")
    unknown = set(changes) - set(MINOR_FIELDS)
    if unknown:
        raise BookingError("not_minor", f"Change {', '.join(sorted(unknown))} with a change request instead.")
    active = [b for b in event.bookings if b.status in ACTIVE_BOOKING_STATUSES]
    headcount = changes.get("expected_headcount")
    if headcount is not None:
        small = [b.venue.name for b in active if b.venue.capacity < headcount]
        if small:
            raise BookingError("over_capacity", f"{', '.join(small)} can't seat {headcount}. "
                               "Request a change to a bigger venue first.")
    before = {k: getattr(event, k) for k in changes}
    for k, v in changes.items():
        setattr(event, k, v)
    db.flush()
    for b in active:
        _record(db, b, actor, "edited", {"event": before})
        _notify(db, b, "event_edited", _everyone(event) - {actor.email}, changes=changes, previous=before)
    return event


# --- scheduled jobs -------------------------------------------------------------------------------


def expire_holds(db: Session, now: datetime | None = None) -> int:
    """Release pending bookings whose in-charge did not act in time."""
    now = now or _now()
    expired = list(db.scalars(select(Booking).where(Booking.status == "pending",
                                                    Booking.hold_expires_at < now).with_for_update()))
    for b in expired:
        before = snapshot(b)
        b.status = "expired"
        db.flush()
        _record(db, b, None, "expired", before, "Not approved in time")
        _notify(db, b, "expired", _everyone(b.event))
    return len(expired)


def remind_in_charges(db: Session, now: datetime | None = None) -> int:
    """Remind in-charges once about requests whose hold is running out."""
    now = now or _now()
    already = select(Notification.booking_id).where(Notification.kind == "approval_reminder")
    due = list(db.scalars(select(Booking).where(
        Booking.status == "pending",
        Booking.hold_expires_at < now + timedelta(hours=get_settings().reminder_hours),
        Booking.hold_expires_at >= now, Booking.id.not_in(already))))
    for b in due:
        if email := _in_charge_email(b.venue):
            _notify(db, b, "approval_reminder", {email}, expires_at=local(b.hold_expires_at).isoformat())
    return len(due)


def bookings_between(db: Session, start: date, end: date, venue_id: int | None = None,
                     include_inactive: bool = False) -> list[Booking]:
    """Bookings overlapping [start, end) in campus days, for calendars and lists."""
    lo = datetime.combine(start, time(), _tz())
    hi = datetime.combine(end, time(), _tz())
    q = select(Booking).where(Booking.start_ts < hi, Booking.end_ts > lo)
    if venue_id:
        q = q.where(Booking.venue_id == venue_id)
    if not include_inactive:
        q = q.where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
    return list(db.scalars(q.order_by(Booking.start_ts)))


def classes_between(db: Session, start: date, end: date, venue_id: int | None = None) -> list[dict]:
    """Timetable classes on each day in [start, end), for calendars."""
    q = select(RecurringSlot).where(RecurringSlot.status == "active", RecurringSlot.valid_from < end,
                                    RecurringSlot.valid_to >= start)
    if venue_id:
        q = q.where(RecurringSlot.venue_id == venue_id)
    slots = list(db.scalars(q))
    out = []
    day = start
    while day < end:
        for c in slots:
            if c.weekday == day.weekday() and c.valid_from <= day <= c.valid_to:
                out.append({"venue_id": c.venue_id, "course": c.course, "class_label": c.class_label,
                            "batch": c.batch, "start": datetime.combine(day, c.start_time, _tz()).isoformat(),
                            "end": datetime.combine(day, c.end_time, _tz()).isoformat()})
        day += timedelta(days=1)
    return sorted(out, key=lambda c: (c["start"], c["venue_id"]))


def awaiting(db: Session, actor: Faculty) -> list[Booking]:
    """The approvals inbox: pending bookings for venues this person looks after (all of them for an admin)."""
    q = select(Booking).join(Venue).where(Booking.status == "pending")
    if actor.role != "admin":
        q = q.where(Venue.in_charge_id == actor.id)
    return list(db.scalars(q.order_by(Booking.hold_expires_at)))


def mine(db: Session, actor: Faculty) -> list[Booking]:
    """Bookings for events this person organises or co-organises."""
    q = select(Booking).join(Event).where(or_(
        Event.created_by_id == actor.id,
        Event.id.in_(select(EventParty.event_id).where(EventParty.email == actor.email,
                                                       EventParty.role.in_(("co_organiser", "contact")))))
    )
    return list(db.scalars(q.order_by(Booking.start_ts.desc())))
