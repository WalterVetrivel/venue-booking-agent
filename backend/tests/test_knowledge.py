import csv
import hashlib
import math
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend import booking as bk
from backend import documents, extract, knowledge, patterns, seed
from backend.config import REPO_ROOT
from backend.models import DocumentChunk, Faculty, PastEvent, SourceDocument, UsagePattern, Venue

MOCK = REPO_ROOT / "data" / "mock"
DIM = 384


class FakeEmbedder:
    """Bag-of-words hashed into a vector: no model download, and similar texts still land close together."""

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        return [x / n for x in v]

    def passages(self, texts):
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)


def answer_key_extractor():
    """Stands in for Groq: answers from the mock answer key, found by the circular or report number."""
    with open(MOCK / "ground_truth" / "events.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    by_circular = {r["circular_no"]: r for r in rows}
    by_report = {r["event_id"]: r for r in rows}

    def run(text, venues):
        assert "A Block SH" in venues  # the venue list reaches the model
        m = re.search(r"Circular No\.?\s*(\S+)", text) or re.search(r"Report No\.?\s*(EV\d+)", text)
        r = (by_circular.get(m[1]) or by_report.get(m[1])) if m else None
        if r is None:
            return extract.Extracted(is_event=False)
        is_report = "EVENT REPORT" in text
        return extract.Extracted(
            is_event=True, title=r["title"], category=r["category"], format=r["format"],
            start_date=r["start_date"], end_date=r["end_date"], start_time=r["start_time"], end_time=r["end_time"],
            venue_codes=r["venues"].split(";"), expected_headcount=int(r["expected_headcount"]),
            actual_headcount=int(r["actual_headcount"]) if is_report else None, significance=int(r["significance"]),
            guest_profile=r["guest_profile"], organiser_dept=r["organiser"], audience=r["audience"],
            topics=r["topics"].split(";"), confidence=0.9)

    return run


@pytest.fixture
def factory(db):
    seed.run(db, admin_email="admin@example.com", with_past_events=True)
    db.flush()  # so later flushes from this session can't re-add rows another session deleted
    return sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint", expire_on_commit=False)


@pytest.fixture(scope="module")
def mock_texts():
    return {p.name: documents.pdf_text(p) for p in (MOCK / "drive" / "circulars").glob("*ADM*")}


def test_chunks_overlap_and_cover_the_text():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(40))
    pieces = documents.chunk(text, size=1000, overlap=200)
    assert len(pieces) > 3 and all(len(p) <= 1100 for p in pieces)
    assert pieces[0].splitlines()[-1] in pieces[1]  # overlap carries the last lines forward
    assert "line 39" in pieces[-1]


def test_doc_date_and_title():
    path = next((MOCK / "drive" / "circulars").glob("CIRCULAR NO.001 (ADM 2025-26)*"))
    assert documents.doc_date(documents.pdf_text(path)) == date(2026, 3, 4)
    assert documents.doc_title(path).endswith("ADMISSION COUNSELLING AND SPOT REGISTRATION 2026 27")


def test_ingest_extracts_merges_and_skips_unchanged(factory, db):
    folder = MOCK / "drive"
    emb, ex = FakeEmbedder(), answer_key_extractor()
    circ = next((folder / "circulars").glob("CIRCULAR NO.001 (MBA 2023-24)*"))
    report = next((folder / "reports").glob("REPORT EV0032 *"))
    assert knowledge.ingest_file(circ, "circular", emb, ex, session_factory=factory).events == 1
    assert knowledge.ingest_file(report, "report", emb, ex, session_factory=factory).events == 1
    rows = list(db.scalars(select(PastEvent).where(PastEvent.source_type != "ground_truth")))
    assert len(rows) == 1  # the report was merged into the circular's event
    (e,) = rows
    assert (e.venue.code, e.start_date, e.expected_headcount, e.actual_headcount) == ("MBA SH", date(2023, 7, 1), 140, 150)
    assert e.status == "active" and e.embedding is not None and "; " in e.source_ref
    assert knowledge.ingest_file(circ, "circular", emb, ex, session_factory=factory).status == "unchanged"
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) >= 2


def test_doubtful_extraction_goes_to_review(factory, db):
    path = next((MOCK / "drive" / "circulars").glob("*.pdf"))
    unsure = lambda text, venues: extract.Extracted(is_event=True, title="Something", start_date=date(2025, 1, 6),
                                                    venue_text="Main hall", confidence=0.9)
    r = knowledge.ingest_file(path, "circular", FakeEmbedder(), unsure, session_factory=factory)
    assert (r.events, r.review) == (1, 1)
    e = db.scalar(select(PastEvent).where(PastEvent.source_type == "circular"))
    assert e.status == "pending_review" and e.venue_id is None


def test_failed_extraction_is_recorded(factory, db):
    path = next((MOCK / "drive" / "circulars").glob("*.pdf"))

    def broken(text, venues):
        raise RuntimeError("rate limited")

    r = knowledge.ingest_file(path, "circular", FakeEmbedder(), broken, session_factory=factory)
    assert r.status == "failed"
    assert "rate limited" in db.scalar(select(SourceDocument.error).where(SourceDocument.name == path.name))


def test_full_mock_drive_finds_the_planted_patterns(factory, db):
    """Phase 2 exit check: the admissions pattern for A Block SH is found from the mock circulars."""
    results = knowledge.ingest_folder(MOCK / "drive", FakeEmbedder(), answer_key_extractor(), session_factory=factory)
    assert not [r for r in results if r.status == "failed"]
    assert db.scalar(select(func.count()).select_from(PastEvent).where(PastEvent.source_type == "ground_truth")) == 0
    found = {(p.category, p.venue.code): p for p in patterns.compute(db, date(2026, 10, 22))}
    admissions = found[("admissions", "A Block SH")]
    assert (admissions.years_seen, admissions.years_observed) == (3, 3)
    assert 10 <= admissions.week_start <= admissions.week_end <= 12
    assert ("inauguration", "E Block SH") in found and ("fdp", "D Block SH") in found
    assert all(("exam", f"CC{i}") in found for i in range(1, 7))
    assert found[("placement_training", "COE1")].weekdays == [5]
    assert ("guest_lecture", "A Block SH") not in found  # one-off talks don't make a season

    hits = documents.search(db, "admission counselling spot registration", FakeEmbedder(), k=3)
    assert hits and "ADMISSION" in hits[0].document


def test_a_seasonal_pattern_warns_a_booking(factory, db):
    with factory.begin() as s:
        patterns.compute(s, date(2026, 10, 22))  # from the seeded answer-key events
    hall = db.scalar(select(Venue).where(Venue.code == "A Block SH"))
    assert db.scalar(select(UsagePattern).where(UsagePattern.venue_id == hall.id))
    ist = ZoneInfo("Asia/Kolkata")
    start = datetime(2027, 3, 8, 10, tzinfo=ist)  # week 10, inside the admissions window
    conflicts = bk.find_soft_conflicts(db, hall.id, start, start.replace(hour=12))
    assert [c.category for c in conflicts] == ["admissions"] and "3 of the last 3 years" in conflicts[0].note
    alice = db.scalar(select(Faculty).where(Faculty.role == "faculty"))
    event = bk.request_booking(db, alice, title="Talk", category="guest_lecture", format="talk",
                               expected_headcount=60, slots=[bk.Slot(hall.id, start, start.replace(hour=12))],
                               now=datetime(2026, 10, 22, tzinfo=ist))
    assert event.bookings[0].status == "pending" and "admissions" in event.bookings[0].soft_conflict_note
    june = datetime(2027, 6, 7, 10, tzinfo=ist)
    assert bk.find_soft_conflicts(db, hall.id, june, june.replace(hour=12)) == []
