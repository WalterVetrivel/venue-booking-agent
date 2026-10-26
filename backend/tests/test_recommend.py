from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from backend import booking as bk
from backend import patterns, seed
from backend.models import Faculty, Venue
from backend.recommend import BookingRequest, find_free_venues, recommend
from conftest import FakeEmbedder

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 10, 22)


@pytest.fixture
def w(db, monkeypatch):
    monkeypatch.setattr(bk, "_now", lambda: datetime(2026, 10, 22, 9, tzinfo=IST))
    seed.run(db, admin_email="admin@example.com", with_past_events=True, embedder=FakeEmbedder())
    db.flush()
    patterns.compute(db, TODAY)
    return db


def req(**kw):
    base = dict(title="Guest lecture on cloud computing", category="guest_lecture", format="talk",
                expected_headcount=80, dates=[date(2026, 11, 7)], start_time=time(10), end_time=time(12))
    return BookingRequest(**{**base, **kw})


def test_missing_fields():
    assert set(BookingRequest(title="x").missing()) == {"expected_headcount", "dates", "start_time", "end_time", "format"}
    assert req().missing() == []


def test_hard_filter(w):
    free, excluded = find_free_venues(w, req(format="hands-on", expected_headcount=50))
    codes = {v.code for v in free}
    assert codes <= {"CC1", "CC2"}  # the only labs with 55+ seats and 50+ computers
    assert "not a lab" in excluded["D Block SH"]
    busy, excluded = find_free_venues(w, req(format="hands-on", expected_headcount=25, dates=[date(2026, 11, 2)],
                                             start_time=time(9), end_time=time(10)))
    assert "busy" in excluded["CC1"] and "Database Management" in excluded["CC1"]
    _, excluded = find_free_venues(w, req(format="hands-on", expected_headcount=20, software=["MATLAB"]))
    assert all("MATLAB" in r or "computers" in r or "lab" in r or "seats" in r for r in excluded.values())


def test_talk_goes_to_a_hall_ranked_by_history(w):
    out = recommend(w, FakeEmbedder(), req())
    codes = [o["code"] for o in out["options"]]
    assert len(codes) == 3 and all(c.endswith("SH") for c in codes)
    assert all(o["reasons"] for o in out["options"])


def test_admissions_season_is_flagged_and_a_clean_alternative_offered(w):
    out = recommend(w, FakeEmbedder(), req(dates=[date(2027, 3, 10)], preferred_venue="A Block SH"))
    assert out["preferred"]["soft_conflicts"][0]["category"] == "admissions"
    assert all(o["code"] != "A Block SH" or o["soft_conflicts"] for o in out["options"])
    assert any(not o["soft_conflicts"] for o in out["options"])
    ash = next((o for o in out["options"] if o["code"] == "A Block SH"), None)
    if ash:
        assert ash["score"] < max(o["score"] for o in out["options"] if not o["soft_conflicts"])


def test_nothing_free_suggests_other_dates(w):
    alice = w.scalar(select(Faculty).where(Faculty.role == "faculty"))
    halls = [v for v in w.scalars(select(Venue)) if v.capacity >= 330]  # only E Block SH seats 300 people + headroom
    slot = (datetime(2026, 11, 7, 10, tzinfo=IST), datetime(2026, 11, 7, 12, tzinfo=IST))
    for v in halls:
        bk.request_booking(w, alice, title="Busy", category="meeting", format="talk", expected_headcount=10,
                           slots=[bk.Slot(v.id, *slot)])
    out = recommend(w, FakeEmbedder(), req(expected_headcount=300))
    assert out["options"] == [] and out["nearest_free_dates"]
    assert all(d["date"] != "2026-11-07" for d in out["nearest_free_dates"])
