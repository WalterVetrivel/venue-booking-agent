from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import booking, seed
from backend.app import app
from backend.config import REPO_ROOT, get_settings
from backend.db import get_db
from backend.models import Faculty, Venue

SAT = "2026-11-07"
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.fixture
def api(db, monkeypatch):
    seed.run(db, admin_email="admin@example.com")
    monkeypatch.setattr(get_settings(), "dev_login", True)
    monkeypatch.setattr(booking, "_now", lambda: NOW)  # the mock timetables run to the end of Nov 2026
    app.dependency_overrides[get_db] = lambda: db
    yield db
    app.dependency_overrides.clear()


def client(email: str) -> TestClient:
    c = TestClient(app)  # not used as a context manager, so the background jobs don't start
    assert c.post("/auth/dev-login", json={"email": email}).status_code == 200
    return c


@pytest.fixture
def people(api):
    cc1 = api.scalar(select(Venue).where(Venue.code == "CC1"))
    faculty = list(api.scalars(select(Faculty).where(Faculty.role == "faculty").order_by(Faculty.id)))
    return {"cc1": cc1.id, "cc2": api.scalar(select(Venue.id).where(Venue.code == "CC2")),
            "alice": faculty[0].email, "bob": faculty[1].email, "in_charge": cc1.in_charge.email,
            "admin": "admin@example.com"}


def event_body(venue_id, start="11:00", end="13:00", day=SAT):
    return {"title": "Python workshop", "category": "workshop", "format": "hands-on", "expected_headcount": 40,
            "slots": [{"venue_id": venue_id, "start": f"{day}T{start}:00+05:30", "end": f"{day}T{end}:00+05:30"}]}


def test_sign_in_needs_the_allowlist(api):
    c = TestClient(app)
    assert c.get("/me").status_code == 401
    assert c.post("/auth/dev-login", json={"email": "stranger@example.com"}).status_code == 403


def test_dev_login_is_off_by_default(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "dev_login", False)
    assert TestClient(app).post("/auth/dev-login", json={"email": "admin@example.com"}).status_code == 404


def test_book_approve_and_cancel(people):
    alice, ic = client(people["alice"]), client(people["in_charge"])
    r = alice.post("/events", json=event_body(people["cc1"]))
    assert r.status_code == 201, r.text
    booking = r.json()["bookings"][0]
    assert booking["status"] == "pending" and booking["can_cancel"] and not booking["can_approve"]

    inbox = ic.get("/approvals").json()
    assert [b["id"] for b in inbox] == [booking["id"]] and inbox[0]["can_approve"]
    assert ic.post(f"/bookings/{booking['id']}/approve").json()["status"] == "confirmed"
    assert any(n["kind"] == "approved" for n in alice.get("/notifications").json())

    detail = alice.get(f"/bookings/{booking['id']}").json()
    assert [h["action"] for h in detail["history"]] == ["requested", "approved"]
    assert alice.post(f"/bookings/{booking['id']}/cancel", json={}).json()["status"] == "cancelled"


def test_class_clash_returns_409(people):
    r = client(people["alice"]).post("/events", json=event_body(people["cc1"], "09:30", "10:30", "2026-11-02"))
    assert r.status_code == 409
    assert r.json()["code"] == "clash" and r.json()["details"][0]["kind"] == "class"


def test_availability_and_calendar(people):
    alice = client(people["alice"])
    r = alice.get("/availability", params={"venue_id": people["cc1"], "start": "2026-11-02T09:00:00+05:30",
                                           "end": "2026-11-02T10:00:00+05:30"})
    assert r.json()["free"] is False
    alice.post("/events", json=event_body(people["cc1"]))
    cal = alice.get("/calendar", params={"start": "2026-11-02", "end": "2026-11-09",
                                         "venue_id": people["cc1"]}).json()
    assert len(cal["bookings"]) == 1 and len(cal["classes"]) > 0


def test_change_request_via_api(people):
    alice, ic = client(people["alice"]), client(people["in_charge"])
    b = alice.post("/events", json=event_body(people["cc1"])).json()["bookings"][0]
    ic.post(f"/bookings/{b['id']}/approve")
    change = alice.post(f"/bookings/{b['id']}/change", json={"venue_id": people["cc2"]})
    assert change.status_code == 200 and change.json()["replaces_booking_id"] == b["id"]
    assert client(people["bob"]).post(f"/bookings/{b['id']}/cancel", json={}).status_code == 403


def test_minor_edit_via_api(people):
    alice = client(people["alice"])
    event_id = alice.post("/events", json=event_body(people["cc1"])).json()["event_id"]
    assert alice.patch(f"/events/{event_id}", json={"title": "Python bootcamp"}).status_code == 200
    r = alice.patch(f"/events/{event_id}", json={"expected_headcount": 500})
    assert r.status_code == 422 and r.json()["code"] == "over_capacity"


def test_admin_manages_the_allowlist(people):
    admin, alice = client(people["admin"]), client(people["alice"])
    assert alice.get("/admin/faculty").status_code == 403
    r = admin.post("/admin/faculty", json={"email": "New.Person@example.com", "name": "New Person"})
    assert r.status_code == 201 and r.json()["email"] == "new.person@example.com"
    assert admin.post("/admin/faculty", json={"email": "new.person@example.com", "name": "X"}).status_code == 409
    client("new.person@example.com")
    assert admin.patch(f"/admin/faculty/{r.json()['id']}", json={"active": False}).json()["active"] is False
    assert TestClient(app).post("/auth/dev-login", json={"email": "new.person@example.com"}).status_code == 403
    me = admin.get("/me").json()
    assert admin.patch(f"/admin/faculty/{me['id']}", json={"role": "faculty"}).status_code == 409


def test_ui_is_served(api):
    r = TestClient(app).get("/")
    assert r.status_code == 200 and "KIOT Venue Booking" in r.text


def test_admin_uploads_and_reviews_timetables(people, api, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    admin = client(people["admin"])
    assert client(people["alice"]).get("/admin/timetables").status_code == 403
    pdf = REPO_ROOT / "data" / "mock" / "timetables" / "TT_CSE_II_YR_A.pdf"
    r = admin.post("/admin/timetables", files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")})
    assert r.status_code == 201 and r.json()["active"] == 5 and r.json()["class"] == "CSE II A"
    assert [d["name"] for d in admin.get("/admin/timetables").json()] == [pdf.name]
    assert admin.get("/admin/timetable-slots").json() == []  # nothing to review

    slot = admin.get("/admin/timetable-slots", params={"status": "active", "source": pdf.name}).json()[0]
    r = admin.patch(f"/admin/timetable-slots/{slot['id']}", json={"status": "pending_review"})
    assert r.json()["status"] == "pending_review"
    assert [s["id"] for s in admin.get("/admin/timetable-slots").json()] == [slot["id"]]
    assert admin.patch(f"/admin/timetable-slots/{slot['id']}", json={"end_time": "08:00"}).status_code == 422
    assert admin.post("/admin/timetables", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 422


def test_admin_knowledge_pages(people, api):
    from datetime import date as d

    from backend import patterns
    from backend.models import PastEvent

    admin = client(people["admin"])
    assert client(people["alice"]).get("/admin/patterns").status_code == 403
    api.add(PastEvent(title="Unclear event", category="workshop", start_date=d(2025, 1, 6), end_date=d(2025, 1, 6),
                      source_type="circular", status="pending_review"))
    seed.run(api, admin_email="admin@example.com", with_past_events=True)
    api.flush()
    assert admin.post("/admin/patterns/refresh").json()["patterns"] > 0
    pats = admin.get("/admin/patterns").json()
    assert any(p["category"] == "admissions" and p["venue"] == "A Block SH" for p in pats)

    (pending,) = admin.get("/admin/past-events").json()
    assert admin.patch(f"/admin/past-events/{pending['id']}", json={"status": "active"}).status_code == 422
    r = admin.patch(f"/admin/past-events/{pending['id']}", json={"status": "active", "venue_id": people["cc1"]})
    assert r.json()["status"] == "active" and r.json()["venue"] == "CC1"
    google = admin.get("/admin/google").json()
    assert {"linked", "drive", "mail", "calendar", "notify_mode", "outbox"} <= set(google)
    assert google["notify_mode"] in ("admin", "live", "off") and set(google["outbox"]) == {"queued", "sent", "failed"}
