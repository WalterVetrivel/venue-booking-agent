"""Read class timetable PDFs into weekly lab slots (`recurring_slot`).

    uv run python -m backend.timetable data/mock/timetables

Only cells that name a venue matter, e.g. "DBMS (B1, CC1) / DS (B2, CC2)" or "OOP LAB (CC1)". A cell
the parser is sure of becomes an active slot that blocks bookings. A cell it can't place (an unknown
venue, a time it can't read) becomes a `pending_review` slot for the admin to fix or reject, and
doesn't block anything until then. Confirmed bookings that a new timetable now clashes with are
flagged to their organiser and in-charge, never cancelled.
"""

import argparse
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path

import pdfplumber
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend import booking as bk
from backend.models import ACTIVE_BOOKING_STATUSES, Booking, RecurringSlot, SourceDocument, Venue

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5}
DEPARTMENTS = {
    "computer science and engineering": "CSE", "information technology": "IT",
    "artificial intelligence and data science": "AI&DS", "computer science and business systems": "CSBS",
    "electronics and communication engineering": "ECE", "electrical and electronics engineering": "EEE",
    "mechanical engineering": "MECH", "master of business administration": "MBA",
}
SOURCE_PREFIX = "timetable:"
CELL = re.compile(r"^(?P<subject>.+?)(?:\s+LAB)?\s*\((?P<inner>[^()]*)\)$", re.I)
TIME_RANGE = re.compile(r"(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})")


@dataclass
class ParsedSlot:
    weekday: int
    start: time
    end: time
    subject: str
    course: str
    batch: str | None
    venue: str
    problem: str | None = None  # set when the slot needs a person to check it


@dataclass
class ParsedTimetable:
    department: str | None
    year: str | None
    section: str | None
    valid_from: date | None
    valid_to: date | None
    slots: list[ParsedSlot] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def class_label(self) -> str:
        return " ".join(x for x in (self.department, self.year, self.section) if x)


def _clock(h: str, m: str) -> time:
    hour = int(h)
    return time(hour + 12 if hour < 8 else hour, int(m))  # "01.25" is 1:25 pm; classes run 9 to 5


def _flat(cell: str | None) -> str:
    return re.sub(r"\s*\n\s*", "", cell or "").strip()


def _semester_dates(text: str) -> tuple[date | None, date | None]:
    """Wef date from the header; the end is the semester's usual last day (odd: 30 Nov, even: 30 Apr)."""
    wef = re.search(r"Wef:\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    start = date(int(wef[3]), int(wef[2]), int(wef[1])) if wef else None
    term = re.search(r"\((\d{4})-(\d{2})\s+(ODD|EVEN)\)", text)
    if not term:
        return start, None
    first = int(term[1])
    return start, date(first, 11, 30) if term[3] == "ODD" else date(first + 1, 4, 30)


def _subject_names(tables: list[list[list]]) -> dict[str, str]:
    names = {}
    for table in tables:
        if table and table[0] and (table[0][1] or "").strip().lower() == "acronym":
            for row in table[1:]:
                if row[1] and row[2]:
                    names[_flat(row[1])] = re.sub(r"\s*\(.*?\)\s*$", "", " ".join(row[2].split()))
    return names


def parse_pdf(path: Path, venue_codes: set[str]) -> ParsedTimetable:
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        tables = page.extract_tables()

    dept = re.search(r"Department of ([A-Za-z &]+)", text)
    cls = re.search(r"Yr/Sem/Sec:\s*([IVX]+)\s*YR\s*/\s*[IVX]+\s*SEM\s*/\s*([A-Z])", text)
    valid_from, valid_to = _semester_dates(text)
    tt = ParsedTimetable(
        department=DEPARTMENTS.get(dept[1].strip().lower(), dept[1].strip()) if dept else None,
        year=cls[1] if cls else None, section=cls[2] if cls else None,
        valid_from=valid_from, valid_to=valid_to)
    if not cls or not dept:
        tt.problems.append("Couldn't read the department, year or section from the header.")
    if not valid_from or not valid_to:
        tt.problems.append("Couldn't read the semester dates from the header.")
    names = _subject_names(tables)

    grid = next((t for t in tables if any(r and _flat(r[0]).lower() == "day/time" for r in t)), None)
    if grid is None:
        tt.problems.append("Couldn't find the day-by-period grid.")
        return tt
    header = next(r for r in grid if r and _flat(r[0]).lower() == "day/time")
    periods = [TIME_RANGE.search(_flat(c)) for c in header]
    is_break = [any(r and _flat(r[i]).lower().startswith(("break", "lunch")) for r in grid if i < len(r))
                for i in range(len(header))]

    for row in grid:
        day = WEEKDAYS.get(_flat(row[0])[:3].lower()) if row and row[0] else None
        if day is None:
            continue
        for i, cell in enumerate(row[1:], start=1):
            content = " ".join((cell or "").split())
            if not content or "(" not in content or i >= len(periods) or periods[i] is None:
                continue
            last = i  # a cell spans the empty cells after it, up to the next break
            while last + 1 < len(row) and row[last + 1] is None and not is_break[last + 1] and periods[last + 1]:
                last += 1
            start = _clock(periods[i][1], periods[i][2])
            end = _clock(periods[last][3], periods[last][4])
            for part in content.split(" / "):
                if slot := _parse_part(part.strip(), day, start, end, names, venue_codes, tt):
                    tt.slots.append(slot)
    return tt


def _parse_part(part: str, day: int, start: time, end: time, names: dict[str, str], venue_codes: set[str],
                tt: ParsedTimetable) -> ParsedSlot | None:
    m = CELL.match(part)
    if not m:
        return None
    inner = [x.strip() for x in m["inner"].split(",")]
    if inner == ["T"]:  # a tutorial period, held in the class's own room
        return None
    batch, venue = (inner[0], inner[1]) if len(inner) == 2 else (None, inner[0])
    subject = m["subject"].strip()
    problem = None
    if venue not in venue_codes:
        problem = f"Unknown venue '{venue}' in cell '{part}'."
    elif batch is not None and not re.fullmatch(r"B\d+", batch):
        problem = f"Couldn't read the batch in cell '{part}'."
    return ParsedSlot(day, start, end, subject, names.get(subject, subject), batch, venue, problem)


def ingest_pdf(db: Session, path: Path, force: bool = False) -> dict:
    """Load one timetable, replacing slots from an earlier version of the same file."""
    data = path.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    doc = db.scalar(select(SourceDocument).where(SourceDocument.kind == "timetable", SourceDocument.name == path.name))
    if doc and doc.md5 == md5 and doc.processed_at and not force:
        return {"file": path.name, "skipped": True}
    if doc is None:
        doc = SourceDocument(name=path.name, kind="timetable")
        db.add(doc)
    venues = {v.code: v.id for v in db.scalars(select(Venue))}
    tt = parse_pdf(path, set(venues))

    source_ref = SOURCE_PREFIX + path.name
    db.execute(delete(RecurringSlot).where(RecurringSlot.source_ref == source_ref))
    header_ok = tt.valid_from and tt.valid_to
    active = review = 0
    for s in tt.slots:
        if s.problem and s.venue not in venues:
            tt.problems.append(s.problem)  # no venue to attach it to
            continue
        status = "pending_review" if s.problem or not header_ok else "active"
        db.add(RecurringSlot(
            venue_id=venues[s.venue], weekday=s.weekday, start_time=s.start, end_time=s.end,
            valid_from=tt.valid_from or date.today(), valid_to=tt.valid_to or tt.valid_from or date.today(),
            course=s.course, class_label=tt.class_label, batch=s.batch, source_ref=source_ref, status=status,
            confidence=0.5 if status == "pending_review" else 1.0))
        active += status == "active"
        review += status == "pending_review"
    doc.md5, doc.processed_at = md5, datetime.now(timezone.utc)
    doc.error = " ".join(tt.problems) or None
    db.flush()
    return {"file": path.name, "class": tt.class_label, "active": active, "pending_review": review,
            "problems": tt.problems, "flagged_bookings": flag_class_clashes(db)}


def ingest_folder(db: Session, folder: Path, force: bool = False) -> list[dict]:
    """Load every timetable PDF in a folder. Slots the seed loaded from the mock answer key are replaced."""
    db.execute(delete(RecurringSlot).where(RecurringSlot.source_ref.startswith("mock-truth:")))
    return [ingest_pdf(db, p, force) for p in sorted(folder.glob("*.pdf"))]


def flag_class_clashes(db: Session, now: datetime | None = None) -> int:
    """Tell organisers and in-charges about bookings that a class now overlaps. Each is flagged once."""
    flagged = 0
    upcoming = db.scalars(select(Booking).where(Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                                                Booking.end_ts > (now or datetime.now(timezone.utc))))
    for b in upcoming:
        classes = [c for c in bk.find_clashes(db, b.venue_id, b.start_ts, b.end_ts, (b.id,)) if c.kind == "class"]
        if not classes:
            continue
        detail = "; ".join(f"{c.detail} {c.start[11:16]}-{c.end[11:16]}" for c in classes)
        if b.soft_conflict_note and detail in b.soft_conflict_note:
            continue
        b.soft_conflict_note = " ".join(x for x in (b.soft_conflict_note, f"Timetable clash: {detail}.") if x)
        bk._record(db, b, None, "flagged", reason=f"A timetable now has a class here: {detail}")
        bk._notify(db, b, "timetable_clash", bk._everyone(b.event, bk._in_charge_email(b.venue)), classes=detail)
        flagged += 1
    return flagged


def main() -> None:
    from backend.db import SessionLocal

    parser = argparse.ArgumentParser(description="Load timetable PDFs into lab slots.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--force", action="store_true", help="re-read files even if they haven't changed")
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        for r in ingest_folder(db, args.folder, args.force):
            if r.get("skipped"):
                print(f"{r['file']}: unchanged")
            else:
                print(f"{r['file']}: {r['class']}, {r['active']} slots, {r['pending_review']} to review"
                      + "".join(f"\n  ! {p}" for p in r["problems"]))


if __name__ == "__main__":
    main()
