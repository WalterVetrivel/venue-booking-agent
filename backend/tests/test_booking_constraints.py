from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from backend import seed
from backend.models import Booking, Event, Faculty, Venue

IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 11, 10, 10, 0, tzinfo=IST)


@pytest.fixture
def setup(db):
    seed.run(db, admin_email="admin@example.com")
    faculty = db.query(Faculty).filter(Faculty.role == "faculty").first()
    venue = db.query(Venue).filter(Venue.code == "CC1").one()
    event = Event(title="Guest lecture", category="guest_lecture", format="talk", expected_headcount=50,
                  created_by_id=faculty.id)
    db.add(event)
    db.flush()
    return db, event, venue, faculty


def _book(db, event, venue, faculty, start, hours=2, status="pending"):
    b = Booking(event_id=event.id, venue_id=venue.id, start_ts=start, end_ts=start + timedelta(hours=hours),
                status=status, requested_by_id=faculty.id)
    db.add(b)
    db.flush()
    return b


@pytest.mark.parametrize("status", ["hold", "pending", "confirmed"])
def test_overlapping_active_booking_is_refused(setup, status):
    db, event, venue, faculty = setup
    _book(db, event, venue, faculty, START, status=status)
    with pytest.raises(IntegrityError, match="no_overlapping_active_bookings"):
        with db.begin_nested():
            _book(db, event, venue, faculty, START + timedelta(hours=1))


@pytest.mark.parametrize("status", ["cancelled", "rejected", "expired"])
def test_inactive_booking_does_not_block(setup, status):
    db, event, venue, faculty = setup
    _book(db, event, venue, faculty, START, status=status)
    _book(db, event, venue, faculty, START + timedelta(hours=1))


def test_back_to_back_bookings_are_allowed(setup):
    db, event, venue, faculty = setup
    _book(db, event, venue, faculty, START)
    _book(db, event, venue, faculty, START + timedelta(hours=2))


def test_same_time_other_venue_is_allowed(setup):
    db, event, venue, faculty = setup
    other = db.query(Venue).filter(Venue.code == "CC2").one()
    _book(db, event, venue, faculty, START)
    _book(db, event, other, faculty, START)


def test_end_must_follow_start(setup):
    db, event, venue, faculty = setup
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            _book(db, event, venue, faculty, START, hours=0)
