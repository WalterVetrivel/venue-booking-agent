from sqlalchemy import func, select

from backend import seed
from backend.models import Faculty, PastEvent, RecurringSlot, Venue

ADMIN = "admin@example.com"


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_seed_loads_mock_data(db):
    counts = seed.run(db, with_past_events=True, admin_email=ADMIN)
    assert counts["venues"] == _count(db, Venue) == 19
    assert counts["faculty"] == _count(db, Faculty) == 33
    assert counts["recurring_slots"] == _count(db, RecurringSlot) == 124
    assert counts["past_events"] == _count(db, PastEvent) > 0
    admin = db.scalar(select(Faculty).where(Faculty.email == ADMIN))
    assert admin is not None and admin.role == "admin"
    assert db.scalar(select(func.count()).select_from(Venue).where(Venue.in_charge_id.is_(None))) == 0


def test_seed_is_idempotent(db):
    seed.run(db, with_past_events=True, admin_email=ADMIN)
    first = {m: _count(db, m) for m in (Faculty, Venue, RecurringSlot, PastEvent)}
    seed.run(db, with_past_events=True, admin_email=ADMIN)
    assert {m: _count(db, m) for m in first} == first


def test_seed_needs_admin_email(db, monkeypatch):
    monkeypatch.setattr(seed.get_settings(), "admin_email", None)
    try:
        seed.run(db)
    except SystemExit as e:
        assert "ADMIN_EMAIL" in str(e)
    else:
        raise AssertionError("seed ran without an admin email")
