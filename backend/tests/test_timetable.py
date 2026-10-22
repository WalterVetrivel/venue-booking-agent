import csv
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from backend import booking as bk
from backend import seed, timetable
from backend.config import get_settings
from backend.models import Faculty, Notification, RecurringSlot, Venue

FOLDER = get_settings().data_dir / "mock" / "timetables"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
IST = ZoneInfo("Asia/Kolkata")


def _venue_codes():
    with open(get_settings().data_dir / "mock" / "venues.csv", newline="") as f:
        return {r["code"] for r in csv.DictReader(f)}


def test_parser_matches_the_answer_key():
    with open(FOLDER / "lab_slots_truth.csv", newline="") as f:
        truth = {(r["file"], r["weekday"], r["start"], r["end"], r["subject"], r["subject_name"],
                  None if r["batch"] == "all" else r["batch"], r["venue"], f"{r['dept']} {r['year']} {r['section']}",
                  r["valid_from"], r["valid_to"]) for r in csv.DictReader(f)}
    codes, got = _venue_codes(), set()
    for path in sorted(FOLDER.glob("*.pdf")):
        tt = timetable.parse_pdf(path, codes)
        assert tt.problems == [], path.name
        for s in tt.slots:
            assert s.problem is None
            got.add((path.name, WEEKDAYS[s.weekday], f"{s.start:%H:%M}", f"{s.end:%H:%M}", s.subject, s.course,
                     s.batch, s.venue, tt.class_label, str(tt.valid_from), str(tt.valid_to)))
    assert got == truth


def test_cells_it_cant_place_go_to_review():
    tt = timetable.ParsedTimetable(None, None, None, None, None)
    s = timetable._parse_part("DBMS (B1, CC99)", 0, time(9), time(10), {}, {"CC1"}, tt)
    assert s.problem and "CC99" in s.problem
    assert timetable._parse_part("OOP (T)", 0, time(9), time(10), {}, {"CC1"}, tt) is None
    s = timetable._parse_part("OOP LAB (CC1)", 0, time(9), time(10), {"OOP": "Java"}, {"CC1"}, tt)
    assert (s.venue, s.batch, s.course, s.problem) == ("CC1", None, "Java", None)


def test_ingest_replaces_mock_slots_and_skips_unchanged_files(db):
    seed.run(db, admin_email="admin@example.com")
    results = timetable.ingest_folder(db, FOLDER)
    assert sum(r["active"] for r in results) == 124
    count = select(func.count()).select_from(RecurringSlot)
    assert db.scalar(count) == 124
    assert db.scalar(count.where(RecurringSlot.source_ref.startswith("mock-truth:"))) == 0
    again = timetable.ingest_folder(db, FOLDER)
    assert all(r.get("skipped") for r in again) and db.scalar(count) == 124


def test_new_class_flags_a_confirmed_booking(db):
    seed.run(db, admin_email="admin@example.com")
    now = datetime(2026, 10, 22, 9, 0, tzinfo=IST)
    cc1 = db.scalar(select(Venue).where(Venue.code == "CC1"))
    alice = db.scalar(select(Faculty).where(Faculty.role == "faculty").order_by(Faculty.id))
    sat = datetime(2026, 11, 7, tzinfo=IST)
    event = bk.request_booking(db, alice, title="Workshop", category="workshop", format="hands-on",
                               expected_headcount=30, now=now,
                               slots=[bk.Slot(cc1.id, sat.replace(hour=10), sat.replace(hour=12))])
    bk.approve(db, cc1.in_charge, event.bookings[0].id)
    db.add(RecurringSlot(venue_id=cc1.id, weekday=5, start_time=time(9), end_time=time(11),
                         valid_from=sat.date(), valid_to=sat.date(), course="Extra lab", class_label="CSE II A",
                         source_ref="timetable:test.pdf", status="active"))
    db.flush()
    assert timetable.flag_class_clashes(db, now) == 1
    assert timetable.flag_class_clashes(db, now) == 0  # only once
    b = event.bookings[0]
    assert b.status == "confirmed" and "Extra lab" in b.soft_conflict_note
    kinds = {n.recipient for n in db.scalars(select(Notification).where(Notification.kind == "timetable_clash"))}
    assert kinds == {alice.email, cc1.in_charge.email}
