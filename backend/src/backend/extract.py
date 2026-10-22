"""Turn a circular or event report into `past_event` rows with an LLM on Groq.

The model reads the document and fills a fixed schema; code then validates it, maps venues to known
codes, and merges a report with the circular for the same event. Anything doubtful (no venue, no
date, low confidence) is saved as `pending_review` so it never feeds recommendations unchecked.
"""

from datetime import date, time
from typing import Callable, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.embeddings import Embedder
from backend.models import PastEvent, SourceDocument, Venue

CATEGORIES = ("guest_lecture", "workshop", "seminar", "fdp", "training", "placement_training", "hackathon",
              "contest", "exam", "admissions", "address", "inauguration", "ceremony", "value_added_course",
              "meeting", "cultural", "other")
MIN_CONFIDENCE = 0.6


class Extracted(BaseModel):
    """What one circular or report says about an event. Leave a field null when the document doesn't say."""

    is_event: bool = Field(description="False if the document is not about an event held in a venue")
    title: str | None = Field(None, description="Name of the event or programme")
    category: Literal[CATEGORIES] | None = None  # type: ignore[valid-type]
    format: Literal["talk", "hands-on", "mixed"] | None = Field(
        None, description="hands-on if participants use computers or equipment; talk for lectures and addresses")
    start_date: date | None = None
    end_date: date | None = None
    start_time: str | None = Field(None, description="24-hour HH:MM")
    end_time: str | None = Field(None, description="24-hour HH:MM")
    venue_codes: list[str] = Field(default_factory=list, description="Codes from the venue list; empty if unsure")
    venue_text: str | None = Field(None, description="The venue exactly as written in the document")
    expected_headcount: int | None = Field(None, description="Expected or registered participants")
    actual_headcount: int | None = Field(None, description="Participants who attended (reports only)")
    significance: int | None = Field(
        None, ge=1, le=3, description="1 routine, 2 department-level, 3 institution-level or VIP guest")
    guest_profile: Literal["none", "internal", "external_academic", "industry", "government"] | None = None
    organiser_dept: str | None = Field(None, description="Short department code, e.g. CSE, MBA, Admissions")
    audience: str | None = None
    topics: list[str] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0, le=1, description="How sure you are of the dates and venue")


Extractor = Callable[[str, str], Extracted]  # (document text, venue list) -> extraction

PROMPT = """You extract facts about campus events from college circulars and event reports.
The text was pulled from a PDF, so table cells may be split across lines; reassemble them.
Dates are written DD.MM.YYYY. Times may be "02.10 PM" or "09.00 AM to 17:00"; return 24-hour HH:MM.
Map the venue to codes from this list, using names and aliases; use several codes if the event used several venues:
{venues}

Document:
\"\"\"
{text}
\"\"\""""


def venue_list(db: Session) -> str:
    return "\n".join(f"- {v.code}: {v.name}; also called {', '.join(v.aliases) or '-'}"
                     for v in db.scalars(select(Venue).where(Venue.active).order_by(Venue.id)))


def groq_extractor() -> Extractor:
    """Extract with the small model, retrying once with the large model if the answer doesn't validate."""
    from langchain_groq import ChatGroq  # imported here so tests and other commands don't need it

    s = get_settings()
    if not (s.groq_api_key and s.groq_extract_model):
        raise SystemExit("Set GROQ_API_KEY and GROQ_EXTRACT_MODEL in .env to extract events.")
    models = [m for m in (s.groq_extract_model, s.groq_agent_model) if m]
    chains = [ChatGroq(model=m, api_key=s.groq_api_key, temperature=0, max_retries=6)
              .with_structured_output(Extracted) for m in models]

    def run(text_: str, venues: str) -> Extracted:
        prompt = PROMPT.format(venues=venues, text=text_[:12000])
        error = None
        for chain in chains:
            try:
                return chain.invoke(prompt)
            except Exception as e:  # a malformed answer from the small model: try the large one
                error = e
        raise error

    return run


def _time(value: str | None) -> time | None:
    try:
        return time.fromisoformat(value) if value else None
    except ValueError:
        return None


def event_text(e: PastEvent) -> str:
    """What gets embedded for similarity search: the parts of an event that predict its venue."""
    return " | ".join(x for x in (e.title, e.category, e.format, e.guest_profile, e.audience,
                                  ", ".join(e.topics or []), e.description) if x)


def save_extraction(db: Session, doc: SourceDocument, x: Extracted, embedder: Embedder,
                    source_ref: str | None = None) -> list[PastEvent]:
    """Store an extraction as one past_event per venue, merging with rows from the same event's other document."""
    db.execute(delete(PastEvent).where(PastEvent.source_document_id == doc.id))  # this document, read again
    if not x.is_event or not x.title or not x.start_date:
        return []
    venues = {v.code: v for v in db.scalars(select(Venue))}
    codes = [c for c in dict.fromkeys(x.venue_codes) if c in venues]
    doubtful = x.confidence < MIN_CONFIDENCE or not codes
    source_type = "report" if doc.kind == "report" else "circular"
    saved = []
    for code in codes or [None]:
        venue_id = venues[code].id if code else None
        existing = db.scalar(select(PastEvent).where(
            PastEvent.venue_id == venue_id, PastEvent.start_date == x.start_date, PastEvent.category == x.category,
            PastEvent.source_type.in_(("circular", "report")), PastEvent.source_document_id != doc.id)) \
            if venue_id else None
        e = existing
        if e is None:
            e = PastEvent(source_type=source_type, source_document_id=doc.id, status="active")
            db.add(e)
        fields = dict(
            title=x.title, category=x.category, format=x.format, expected_headcount=x.expected_headcount,
            actual_headcount=x.actual_headcount, significance=x.significance, guest_profile=x.guest_profile,
            organiser_dept=x.organiser_dept, audience=x.audience, topics=x.topics, venue_id=venue_id,
            start_date=x.start_date, end_date=x.end_date or x.start_date, start_time=_time(x.start_time),
            end_time=_time(x.end_time))
        for k, v in fields.items():
            if v not in (None, []) or getattr(e, k, None) is None:  # a merge fills gaps, never blanks a field
                setattr(e, k, v)
        refs = [r for r in (e.source_ref, source_ref) if r]
        e.source_ref = "; ".join(dict.fromkeys(refs)) or None
        e.confidence = max(e.confidence or 0, x.confidence)
        if existing is None:  # an event another document already confirmed stays active
            e.status = "pending_review" if doubtful else "active"
        saved.append(e)
    db.flush()
    vectors = embedder.passages([event_text(e) for e in saved])
    for e, v in zip(saved, vectors):
        e.embedding = v
    return saved
