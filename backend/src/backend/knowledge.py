"""The knowledge pipeline: read documents, index them for search, extract past events, find patterns.

    uv run python -m backend.knowledge ingest ../data/mock/drive   # local folder with circulars/, reports/
    uv run python -m backend.knowledge ingest --no-extract ...     # index for search only, no Groq calls
    uv run python -m backend.knowledge drive                       # sync the Drive folder, then ingest it
    uv run python -m backend.knowledge patterns                    # recompute seasonal patterns
    uv run python -m backend.knowledge search "admission counselling venue"
    uv run python -m backend.knowledge evaluate                    # extraction accuracy on the mock data

Each document is committed on its own, so a run stopped by a rate limit resumes where it left off:
unchanged documents are skipped by checksum.
"""

import argparse
import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import delete, select

from backend import documents, extract, patterns
from backend.config import get_settings
from backend.db import SessionLocal
from backend.embeddings import Embedder, get_embedder
from backend.models import PastEvent, SourceDocument

KIND_BY_FOLDER = {"circulars": "circular", "reports": "report"}
REF = re.compile(r"(?:Circular|Report) No\.?\s*(\S+)", re.I)


@dataclass
class Result:
    file: str
    status: str  # indexed, unchanged, failed
    chunks: int = 0
    events: int = 0
    review: int = 0
    error: str | None = None


def ingest_file(path: Path, kind: str, embedder: Embedder, extractor: extract.Extractor | None,
                drive_file_id: str | None = None, modified_at: datetime | None = None,
                force: bool = False, session_factory=SessionLocal) -> Result:
    with session_factory.begin() as db:
        doc, changed = documents.upsert_document(db, path, kind, drive_file_id, modified_at)
        if not changed and not force:
            return Result(path.name, "unchanged")
        doc_id = doc.id
    try:
        text = documents.pdf_text(path)
        x = extractor(text, _venues(session_factory)) if extractor and text else None
        with session_factory.begin() as db:
            doc = db.get(SourceDocument, doc_id)
            n = documents.index_document(db, doc, text, documents.doc_title(path), embedder,
                                         category=x.category if x else None)
            saved = extract.save_extraction(db, doc, x, embedder, _ref(text)) if x else []
            return Result(path.name, "indexed", n, len(saved), sum(e.status == "pending_review" for e in saved))
    except Exception as e:
        with session_factory.begin() as db:
            db.get(SourceDocument, doc_id).error = f"{type(e).__name__}: {e}"[:2000]
        return Result(path.name, "failed", error=str(e))


def _venues(session_factory) -> str:
    with session_factory() as db:
        return extract.venue_list(db)


def _ref(text: str) -> str | None:
    m = REF.search(text)
    return m[1] if m else None


def drop_answer_key_events(session_factory=SessionLocal) -> None:
    """Remove past events the seed loaded from the mock answer key, once real extraction takes over."""
    with session_factory.begin() as db:
        db.execute(delete(PastEvent).where(PastEvent.source_type == "ground_truth"))


def ingest_folder(folder: Path, embedder: Embedder, extractor: extract.Extractor | None, force: bool = False,
                  session_factory=SessionLocal) -> list[Result]:
    if extractor:
        drop_answer_key_events(session_factory)
    results = []
    for sub, kind in KIND_BY_FOLDER.items():
        for path in sorted((folder / sub).glob("*.pdf")):
            r = ingest_file(path, kind, embedder, extractor, force=force, session_factory=session_factory)
            results.append(r)
            print(f"{r.status:9} {r.file}" + (f": {r.chunks} chunks, {r.events} events" if r.status == "indexed" else "")
                  + (f", {r.review} to review" if r.review else "") + (f" ({r.error})" if r.error else ""), flush=True)
    return results


def ingest_drive(embedder: Embedder, extractor: extract.Extractor | None, force: bool = False) -> list[Result]:
    from backend import drive, timetable

    if extractor:
        drop_answer_key_events()
    results = []
    for f in drive.sync():
        if f.kind == "timetable":
            with SessionLocal.begin() as db:
                timetable.ingest_pdf(db, f.path, force)
            continue
        results.append(ingest_file(f.path, f.kind, embedder, extractor, f.file_id, f.modified_at, force))
    return results


def evaluate(mock_dir: Path) -> dict:
    """Compare extracted events with the mock answer key: share of events found, and field accuracy."""
    with open(mock_dir / "ground_truth" / "events.csv", newline="") as f:
        truth = list(csv.DictReader(f))
    stats = {"events": 0, "found": 0, "venue": 0, "date": 0, "category": 0, "time": 0}
    with SessionLocal() as db:
        for t in truth:
            names = [n for n in (t["circular_file"], t["report_file"]) if n]
            rows = list(db.scalars(select(PastEvent).join(SourceDocument, PastEvent.source_document_id == SourceDocument.id)
                                   .where(SourceDocument.name.in_(names))))
            stats["events"] += 1
            if not rows:
                continue
            stats["found"] += 1
            codes = {e.venue.code for e in rows if e.venue}
            stats["venue"] += codes == set(t["venues"].split(";"))
            stats["date"] += any(e.start_date == date.fromisoformat(t["start_date"]) for e in rows)
            stats["category"] += any(e.category == t["category"] for e in rows)
            stats["time"] += any(e.start_time and e.start_time.strftime("%H:%M") == t["start_time"] for e in rows)
    n = stats["found"] or 1
    return {"events": stats["events"], "found": stats["found"],
            **{k: round(stats[k] / n, 3) for k in ("venue", "date", "category", "time")}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Index documents, extract past events, find patterns.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("folder", type=Path)
    p.add_argument("--no-extract", action="store_true")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("drive")
    p.add_argument("--no-extract", action="store_true")
    p.add_argument("--force", action="store_true")
    sub.add_parser("patterns")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    sub.add_parser("evaluate")
    args = parser.parse_args()

    if args.cmd in ("ingest", "drive"):
        extractor = None if args.no_extract else extract.groq_extractor()
        if args.cmd == "ingest":
            results = ingest_folder(args.folder, get_embedder(), extractor, args.force)
        else:
            results = ingest_drive(get_embedder(), extractor, args.force)
        failed = [r for r in results if r.status == "failed"]
        print(f"{len(results)} files, {sum(r.status == 'indexed' for r in results)} indexed, "
              f"{sum(r.events for r in results)} events, {len(failed)} failed")
    if args.cmd in ("ingest", "drive", "patterns"):
        with SessionLocal.begin() as db:
            found = patterns.compute(db)
            for pt in sorted(found, key=lambda x: (x.category, x.venue_id)):
                print(f"pattern: {pt.category} in {pt.venue.code}, weeks {pt.week_start}-{pt.week_end}, "
                      f"{pt.years_seen} of {pt.years_observed} years")
    elif args.cmd == "search":
        with SessionLocal() as db:
            for h in documents.search(db, args.query, get_embedder(), args.k):
                print(f"{h.score:.4f} {h.document}\n    {h.text[:300]!r}")
    elif args.cmd == "evaluate":
        print(evaluate(get_settings().data_dir / "mock"))


if __name__ == "__main__":
    main()
