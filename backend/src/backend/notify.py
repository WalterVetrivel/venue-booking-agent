"""The notification worker: sends the email and calendar rows of the outbox.

Every booking change writes `notification` rows in the same transaction (booking._notify). This worker
sends them from the admin's linked Google account:

- email: one mail per row, saying what happened and what changed, with a link to the app;
- calendar: one Google Calendar event per confirmed booking with everyone involved as attendees. A
  change moves the same event and a cancellation deletes it, with sendUpdates=all so Google sends the
  updated invites itself. Pending requests get no event.

NOTIFY_MODE decides who receives anything: `admin` (the default) sends every mail and invite to
ADMIN_EMAIL only, noting who it was meant for, which is safe with the mock faculty; `live` sends to the
real people; `off` sends nothing. In-app notices don't depend on this worker.
"""

from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import booking as bk
from backend.config import get_settings
from backend.models import Booking, Notification

MAX_ATTEMPTS = 5
BATCH = 50

SUBJECT = {
    "request_received": "New venue request: {title}", "request_submitted": "Venue request sent: {title}",
    "approved": "Confirmed: {title}", "rejected": "Not approved: {title}",
    "change_requested": "Change requested: {title}", "change_approved": "Change confirmed: {title}",
    "change_rejected": "Change not approved: {title}", "request_changed": "Request updated: {title}",
    "cancelled": "Cancelled: {title}", "event_edited": "Details updated: {title}", "expired": "Request expired: {title}",
    "approval_reminder": "Reminder, waiting for your approval: {title}", "timetable_clash": "Timetable clash: {title}",
}
LEAD = {
    "request_received": "A faculty member has asked for a venue you approve. Please approve or reject it.",
    "request_submitted": "The request has been sent to the venue in-charge for approval.",
    "approved": "The booking is confirmed.",
    "rejected": "The venue in-charge did not approve the request.",
    "change_requested": "A change has been requested. The current booking stays until the change is approved.",
    "change_approved": "The change is confirmed.",
    "change_rejected": "The change was not approved, so the original booking stands.",
    "request_changed": "The pending request has been changed.",
    "cancelled": "The booking has been cancelled.",
    "event_edited": "The event details have been updated.",
    "expired": "The request was not approved in time and has been released.",
    "approval_reminder": "This request is still waiting for your approval and will be released if not approved.",
    "timetable_clash": "A new timetable puts a class in the venue at this time. Please sort out a change.",
}
LINK = {"request_received": "approvals", "approval_reminder": "approvals"}


class Sender(Protocol):
    def send_email(self, to: str, subject: str, body: str) -> None: ...

    def upsert_event(self, event_id: str | None, *, summary: str, location: str, start: datetime, end: datetime,
                     attendees: list[str], description: str) -> str: ...

    def delete_event(self, event_id: str) -> None: ...


def _slot(s: dict) -> str:
    start, end = datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])
    return f"{s['venue']}, {start:%a %d %b %Y}, {start:%H:%M} to {end:%H:%M}"


def compose(n: Notification) -> tuple[str, str]:
    p, s = n.payload, get_settings()
    subject = SUBJECT.get(n.kind, "Venue booking update: {title}").format(title=p.get("title", "your event"))
    lines = [LEAD.get(n.kind, "There is an update on a booking."), "", f"Event: {p.get('title', '')}"]
    if p.get("booking"):
        lines.append(f"Booking: {_slot(p['booking'])}")
    if p.get("previous"):
        lines.append(f"Was: {_slot(p['previous'])}")
    if p.get("changes"):
        lines.append("Changed: " + ", ".join(f"{k.replace('_', ' ')} to {v}" for k, v in p["changes"].items()))
    if p.get("reason"):
        lines.append(f"Reason: {p['reason']}")
    if p.get("soft_conflict"):
        lines.append(f"Seasonal note: {p['soft_conflict']}")
    if p.get("classes"):
        lines.append("Classes: " + "; ".join(c.get("detail", "") for c in p["classes"]))
    lines += ["", f"Open the app: {s.app_url}/#{LINK.get(n.kind, 'mine')}"]
    return subject, "\n".join(lines)


def _route(recipients: list[str]) -> tuple[list[str], str]:
    """Who actually gets it under NOTIFY_MODE, and a note naming the intended people when that differs."""
    s = get_settings()
    if s.notify_mode == "live" or not s.admin_email:
        return recipients, ""
    return [s.admin_email], f"[Test mode: meant for {', '.join(recipients)}]\n\n"


def _sent(n: Notification) -> None:
    n.status, n.sent_at, n.last_error = "sent", bk._now(), None


def _failed(n: Notification, error: Exception) -> None:
    n.attempts += 1
    n.last_error = f"{type(error).__name__}: {error}"[:500]
    if n.attempts >= MAX_ATTEMPTS:
        n.status = "failed"


def _queued(db: Session, channel: str) -> list[Notification]:
    return list(db.scalars(select(Notification).where(Notification.status == "queued", Notification.channel == channel)
                           .order_by(Notification.id).limit(BATCH).with_for_update(skip_locked=True)))


def send_emails(db: Session, sender: Sender) -> int:
    count = 0
    for n in _queued(db, "email"):
        subject, body = compose(n)
        to, note = _route([n.recipient])
        try:
            for addr in to:
                sender.send_email(addr, subject, note + body)
            _sent(n)
            count += 1
        except Exception as e:  # retried on the next run, up to MAX_ATTEMPTS
            _failed(n, e)
    return count


def sync_calendar(db: Session, sender: Sender) -> int:
    """Bring each touched booking's calendar event in line with the booking, then mark its rows sent."""
    rows = _queued(db, "calendar")
    by_booking: dict[int | None, list[Notification]] = {}
    for n in rows:
        by_booking.setdefault(n.booking_id, []).append(n)
    count = 0
    for booking_id, notes in by_booking.items():
        b = db.get(Booking, booking_id) if booking_id else None
        try:
            if b is not None:
                _sync_event(db, b, sender)
            for n in notes:
                _sent(n)
            count += 1
        except Exception as e:
            for n in notes:
                _failed(n, e)
    return count


def _sync_event(db: Session, b: Booking, sender: Sender) -> None:
    original = db.get(Booking, b.replaces_booking_id) if b.replaces_booking_id else None
    if b.status == "confirmed":
        event_id = b.calendar_event_id or (original.calendar_event_id if original is not None else None)
        people = sorted(bk._everyone(b.event, bk._in_charge_email(b.venue)) - {None})
        attendees, note = _route(people)
        description = (f"{note}Venue booking for {b.event.title}.\nOrganiser: {b.event.created_by.name}\n"
                       f"Approved in the KIOT venue booking app: {get_settings().app_url}/#mine")
        if b.soft_conflict_note:
            description += f"\nSeasonal note: {b.soft_conflict_note}"
        b.calendar_event_id = sender.upsert_event(
            event_id, summary=b.event.title, location=b.venue.name, start=bk.local(b.start_ts),
            end=bk.local(b.end_ts), attendees=attendees, description=description)
        if original is not None and original.calendar_event_id == b.calendar_event_id:
            original.calendar_event_id = None  # the event now belongs to the new booking
    elif b.status in ("cancelled", "rejected", "expired") and b.calendar_event_id:
        sender.delete_event(b.calendar_event_id)
        b.calendar_event_id = None


def deliver(db: Session, sender: Sender) -> dict:
    if get_settings().notify_mode == "off":
        return {"emails": 0, "calendar": 0}
    return {"emails": send_emails(db, sender), "calendar": sync_calendar(db, sender)}


class GoogleSender:
    """Gmail and Calendar through the admin's linked account."""

    def __init__(self):
        from googleapiclient.discovery import build

        from backend import google_auth

        self.mail = build("gmail", "v1", credentials=google_auth.system_credentials(google_auth.GMAIL_SEND),
                          cache_discovery=False)
        self.cal = build("calendar", "v3", credentials=google_auth.system_credentials(google_auth.CALENDAR),
                         cache_discovery=False)
        self.calendar_id = get_settings().notify_calendar_id

    def send_email(self, to: str, subject: str, body: str) -> None:
        import base64
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["To"], msg["Subject"] = to, subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.mail.users().messages().send(userId="me", body={"raw": raw}).execute()

    def upsert_event(self, event_id, *, summary, location, start, end, attendees, description) -> str:
        tz = get_settings().timezone
        body = {"summary": summary, "location": location, "description": description,
                "start": {"dateTime": start.isoformat(), "timeZone": tz},
                "end": {"dateTime": end.isoformat(), "timeZone": tz},
                "attendees": [{"email": a} for a in attendees]}
        events = self.cal.events()
        if event_id:
            return events.patch(calendarId=self.calendar_id, eventId=event_id, body=body,
                                sendUpdates="all").execute()["id"]
        return events.insert(calendarId=self.calendar_id, body=body, sendUpdates="all").execute()["id"]

    def delete_event(self, event_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self.cal.events().delete(calendarId=self.calendar_id, eventId=event_id, sendUpdates="all").execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):  # already gone
                raise
