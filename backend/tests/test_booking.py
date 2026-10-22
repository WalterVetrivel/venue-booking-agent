from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from backend import booking as bk
from backend import seed
from backend.booking import BookingError, Slot
from backend.models import BookingHistory, ExpectedReservation, Faculty, Notification, Venue

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=IST)
MONDAY = datetime(2026, 11, 2, tzinfo=IST)  # CC1 has a DBMS lab 09:00-10:40 on Mondays
SATURDAY = datetime(2026, 11, 7, tzinfo=IST)  # no classes in the labs


def at(day: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m)


@pytest.fixture
def w(db):
    seed.run(db, admin_email="admin@example.com")
    venues = {v.code: v for v in db.scalars(select(Venue))}
    faculty = list(db.scalars(select(Faculty).where(Faculty.role == "faculty").order_by(Faculty.id)))
    return SimpleNamespace(
        db=db, v=venues, alice=faculty[0], bob=faculty[1],
        admin=db.scalar(select(Faculty).where(Faculty.role == "admin")),
        cc1_ic=venues["CC1"].in_charge,
    )


def request(w, code="CC1", start="11:00", end="13:00", day=SATURDAY, who=None, headcount=40, **kw):
    return bk.request_booking(w.db, who or w.alice, title="Python workshop", category="workshop",
                              format="hands-on", expected_headcount=headcount,
                              slots=[Slot(w.v[code].id, at(day, start), at(day, end))], now=NOW, **kw)


def notices(w, kind):
    return {n.recipient for n in w.db.scalars(select(Notification).where(Notification.kind == kind))}


def test_request_is_pending_and_notifies(w):
    event = request(w, co_organisers=[("co@example.com", "Co")])
    (b,) = event.bookings
    assert b.status == "pending"
    assert b.hold_expires_at == NOW + timedelta(hours=48)
    assert notices(w, "request_received") == {w.cc1_ic.email}
    assert notices(w, "request_submitted") == {w.alice.email, "co@example.com"}
    assert w.db.scalar(select(BookingHistory.action).where(BookingHistory.booking_id == b.id)) == "requested"


def test_class_clash_is_refused(w):
    with pytest.raises(BookingError) as e:
        request(w, start="10:00", end="12:00", day=MONDAY)
    assert e.value.code == "clash"
    assert e.value.details[0]["kind"] == "class"


def test_booking_clash_is_refused(w):
    request(w)
    with pytest.raises(BookingError) as e:
        request(w, start="12:00", end="14:00", who=w.bob)
    assert e.value.code == "clash" and e.value.details[0]["kind"] == "booking"


def test_slot_rules(w):
    for kwargs, code in [
        (dict(headcount=100), "over_capacity"),
        (dict(start="13:00", end="11:00"), "bad_time"),
        (dict(day=NOW - timedelta(days=3)), "in_past"),
    ]:
        with pytest.raises(BookingError) as e:
            request(w, **kwargs)
        assert e.value.code == code


def test_soft_conflict_warns_but_books(w):
    w.db.add(ExpectedReservation(venue_id=w.v["CC1"].id, category="admissions", start_date=SATURDAY.date(),
                                 end_date=SATURDAY.date(), owner_team="Admissions office"))
    w.db.flush()
    (b,) = request(w).bookings
    assert b.status == "pending" and "admissions" in b.soft_conflict_note


def test_only_the_in_charge_approves(w):
    (b,) = request(w).bookings
    with pytest.raises(BookingError) as e:
        bk.approve(w.db, w.bob, b.id)
    assert e.value.code == "forbidden"
    bk.approve(w.db, w.cc1_ic, b.id)
    assert b.status == "confirmed" and b.approved_by_id == w.cc1_ic.id
    assert w.alice.email in notices(w, "approved")


def test_reject_needs_a_reason_and_frees_the_slot(w):
    (b,) = request(w).bookings
    with pytest.raises(BookingError):
        bk.reject(w.db, w.cc1_ic, b.id, " ")
    bk.reject(w.db, w.cc1_ic, b.id, "Lab maintenance")
    assert b.status == "rejected"
    request(w, who=w.bob)  # the slot is free again


def test_change_request_keeps_original_until_approved(w):
    (b,) = request(w).bookings
    bk.approve(w.db, w.cc1_ic, b.id)
    change = bk.request_change(w.db, w.alice, b.id, venue_id=w.v["CC2"].id, now=NOW)
    assert (b.status, change.status, change.replaces_booking_id) == ("confirmed", "pending", b.id)
    assert w.v["CC2"].in_charge.email in notices(w, "change_requested")
    bk.approve(w.db, w.v["CC2"].in_charge, change.id)
    assert (b.status, change.status) == ("cancelled", "confirmed")
    assert w.alice.email in notices(w, "change_approved")


def test_rejected_change_leaves_original(w):
    (b,) = request(w).bookings
    bk.approve(w.db, w.cc1_ic, b.id)
    change = bk.request_change(w.db, w.alice, b.id, start=at(SATURDAY, "14:00"), end=at(SATURDAY, "16:00"), now=NOW)
    bk.reject(w.db, w.cc1_ic, change.id, "Busy then")
    assert (b.status, change.status) == ("confirmed", "rejected")


def test_change_overlapping_itself_is_explained(w):
    (b,) = request(w).bookings
    bk.approve(w.db, w.cc1_ic, b.id)
    with pytest.raises(BookingError) as e:
        bk.request_change(w.db, w.alice, b.id, end=at(SATURDAY, "14:00"), now=NOW)
    assert e.value.code == "overlaps_current"


def test_pending_booking_changes_in_place(w):
    (b,) = request(w).bookings
    bk.request_change(w.db, w.alice, b.id, start=at(SATURDAY, "11:30"), end=at(SATURDAY, "13:30"), now=NOW)
    assert b.status == "pending" and bk.local(b.start_ts).hour == 11 and b.version == 2


def test_cancel_rules(w):
    (b,) = request(w).bookings
    with pytest.raises(BookingError):
        bk.cancel(w.db, w.bob, b.id)
    with pytest.raises(BookingError) as e:
        bk.cancel(w.db, w.cc1_ic, b.id)
    assert e.value.code == "reason_required"
    bk.cancel(w.db, w.alice, b.id)
    assert b.status == "cancelled"
    assert w.cc1_ic.email in notices(w, "cancelled")


def test_cancel_drops_pending_change(w):
    (b,) = request(w).bookings
    bk.approve(w.db, w.cc1_ic, b.id)
    change = bk.request_change(w.db, w.alice, b.id, venue_id=w.v["CC2"].id, now=NOW)
    bk.cancel(w.db, w.alice, b.id)
    assert change.status == "cancelled"


def test_minor_edit(w):
    event = request(w)
    bk.edit_event(w.db, w.alice, event.id, title="Python bootcamp", expected_headcount=55)
    assert event.title == "Python bootcamp"
    assert w.cc1_ic.email in notices(w, "event_edited")
    with pytest.raises(BookingError) as e:
        bk.edit_event(w.db, w.alice, event.id, expected_headcount=90)
    assert e.value.code == "over_capacity"
    with pytest.raises(BookingError) as e:
        bk.edit_event(w.db, w.alice, event.id, venue_id=1)
    assert e.value.code == "not_minor"


def test_holds_expire_with_a_reminder_first(w):
    (b,) = request(w).bookings
    assert bk.remind_in_charges(w.db, NOW + timedelta(hours=30)) == 1
    assert bk.remind_in_charges(w.db, NOW + timedelta(hours=31)) == 0  # only once
    assert bk.expire_holds(w.db, NOW + timedelta(hours=47)) == 0
    assert bk.expire_holds(w.db, NOW + timedelta(hours=49)) == 1
    assert b.status == "expired"
    request(w, who=w.bob)


def test_inbox_and_my_bookings(w):
    (b,) = request(w).bookings
    assert [x.id for x in bk.awaiting(w.db, w.cc1_ic)] == [b.id]
    assert [x.id for x in bk.awaiting(w.db, w.admin)] == [b.id]
    other = next(v.in_charge for v in w.v.values() if v.in_charge_id != w.cc1_ic.id)
    assert bk.awaiting(w.db, other) == []
    assert [x.id for x in bk.mine(w.db, w.alice)] == [b.id]
    assert bk.mine(w.db, w.bob) == []
