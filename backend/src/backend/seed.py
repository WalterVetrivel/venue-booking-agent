"""Load the mock dataset into the database. Safe to run repeatedly.

    uv run python -m backend.seed                    # faculty, venues, timetable lab slots
    uv run python -m backend.seed --with-past-events # also past events from ground truth

Timetable slots and past events are loaded from the mock ground-truth CSVs so the rest of the
app can be built before the PDF and circular extractors exist (Phase 1-2). Those rows are tagged
so a later run of the real extractors can replace them.
"""

import argparse
import csv
from datetime import date, time
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.db import SessionLocal
from backend.models import Faculty, PastEvent, RecurringSlot, Venue

WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
MOCK_SLOT_SOURCE = "mock-truth:"
ADMIN_PLACEHOLDER = "ADMIN_EMAIL"


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(";") if v.strip()]


def _time(value: str) -> time:
    return time.fromisoformat(value)


def seed_faculty(db: Session, mock_dir: Path, admin_email: str | None) -> int:
    rows = _read(mock_dir / "faculty.csv")
    for r in rows:
        email = r["email"]
        if email.startswith(ADMIN_PLACEHOLDER):
            if not admin_email:
                raise SystemExit("Set ADMIN_EMAIL in .env so the admin account can be created.")
            email = admin_email
        stmt = insert(Faculty).values(email=email.lower(), name=r["name"], department=r["department"] or None,
                                      role=r["role"])
        db.execute(stmt.on_conflict_do_update(index_elements=["email"],
                                              set_={"name": stmt.excluded.name, "department": stmt.excluded.department,
                                                    "role": stmt.excluded.role}))
    return len(rows)


def seed_venues(db: Session, mock_dir: Path) -> int:
    rows = _read(mock_dir / "venues.csv")
    in_charges = {f.email: f.id for f in db.scalars(select(Faculty))}
    for r in rows:
        values = dict(
            code=r["code"], name=r["name"], type=r["type"], building=r["building"] or None,
            capacity=int(r["capacity"]), n_computers=int(r["n_computers"] or 0), byod=r["byod"] == "True",
            equipment=_list(r["equipment"]), software=_list(r["software"]), typical_use=_list(r["typical_use"]),
            aliases=_list(r["aliases"]), in_charge_id=in_charges.get(r["in_charge_email"].lower()),
        )
        stmt = insert(Venue).values(**values)
        db.execute(stmt.on_conflict_do_update(index_elements=["code"],
                                              set_={k: stmt.excluded[k] for k in values if k != "code"}))
    return len(rows)


def seed_recurring_slots(db: Session, mock_dir: Path) -> int:
    rows = _read(mock_dir / "timetables" / "lab_slots_truth.csv")
    venues = {v.code: v.id for v in db.scalars(select(Venue))}
    db.execute(delete(RecurringSlot).where(RecurringSlot.source_ref.startswith(MOCK_SLOT_SOURCE)))
    db.add_all(
        RecurringSlot(
            venue_id=venues[r["venue"]], weekday=WEEKDAYS[r["weekday"]], start_time=_time(r["start"]),
            end_time=_time(r["end"]), valid_from=date.fromisoformat(r["valid_from"]),
            valid_to=date.fromisoformat(r["valid_to"]), course=r["subject_name"],
            class_label=f"{r['dept']} {r['year']} {r['section']}", batch=None if r["batch"] == "all" else r["batch"],
            source_ref=MOCK_SLOT_SOURCE + r["file"], status="active", confidence=1.0,
        )
        for r in rows
    )
    return len(rows)


def seed_past_events(db: Session, mock_dir: Path) -> int:
    rows = _read(mock_dir / "ground_truth" / "events.csv")
    venues = {v.code: v.id for v in db.scalars(select(Venue))}
    db.execute(delete(PastEvent).where(PastEvent.source_type == "ground_truth"))
    n = 0
    for r in rows:
        for code in _list(r["venues"]):  # one row per venue used
            db.add(PastEvent(
                title=r["title"], category=r["category"], format=r["format"],
                expected_headcount=int(r["expected_headcount"]), actual_headcount=int(r["actual_headcount"]),
                significance=int(r["significance"]), guest_profile=r["guest_profile"], organiser_dept=r["organiser"],
                audience=r["audience"], topics=_list(r["topics"]), venue_id=venues[code],
                start_date=date.fromisoformat(r["start_date"]), end_date=date.fromisoformat(r["end_date"]),
                start_time=_time(r["start_time"]), end_time=_time(r["end_time"]),
                source_type="ground_truth", source_ref=r["circular_no"], status="active", confidence=1.0,
            ))
            n += 1
    return n


def embed_past_events(db: Session, embedder) -> int:
    """Fill in the similarity vector for past events that don't have one yet."""
    from backend.extract import event_text

    events = list(db.scalars(select(PastEvent).where(PastEvent.embedding.is_(None))))
    for e, vec in zip(events, embedder.passages([event_text(e) for e in events]) if events else []):
        e.embedding = vec
    return len(events)


def run(db: Session, with_past_events: bool = False, mock_dir: Path | None = None,
        admin_email: str | None = None, embedder=None) -> dict[str, int]:
    settings = get_settings()
    mock_dir = mock_dir or settings.data_dir / "mock"
    counts = {
        "faculty": seed_faculty(db, mock_dir, admin_email or settings.admin_email),
    }
    db.flush()
    counts["venues"] = seed_venues(db, mock_dir)
    db.flush()
    counts["recurring_slots"] = seed_recurring_slots(db, mock_dir)
    if with_past_events:
        counts["past_events"] = seed_past_events(db, mock_dir)
        if embedder is not None:
            db.flush()
            embed_past_events(db, embedder)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-past-events", action="store_true",
                        help="also load past events from data/mock/ground_truth/events.csv")
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        embedder = None
        if args.with_past_events:
            from backend.embeddings import get_embedder

            embedder = get_embedder()  # downloads the model on first use
        counts = run(db, with_past_events=args.with_past_events, embedder=embedder)
    print(", ".join(f"{k}: {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
