"""Database schema. See docs/PLAN.md, "Data model" and "Booking lifecycle".

Availability is decided here, never by the LLM: the exclusion constraint on `booking`
makes overlapping active bookings for the same venue impossible.
"""

from datetime import date, datetime, time

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384  # bge-small-en-v1.5

ROLES = ("faculty", "in_charge", "admin")
VENUE_TYPES = ("computer_lab", "seminar_hall", "coe_lab")
FORMATS = ("talk", "hands-on", "mixed")
ACTIVE_BOOKING_STATUSES = ("hold", "pending", "confirmed")
BOOKING_STATUSES = ACTIVE_BOOKING_STATUSES + ("rejected", "expired", "cancelled")
HISTORY_ACTIONS = ("requested", "approved", "rejected", "edited", "change_requested", "cancelled", "expired", "flagged")
PARTY_ROLES = ("organiser", "co_organiser", "contact", "in_charge", "admin")
CHANNELS = ("email", "calendar", "in_app")
REVIEW_STATUSES = ("active", "pending_review", "rejected")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Faculty(Timestamps, Base):
    """The allowlist. Only these accounts can sign in; the admin manages it."""

    __tablename__ = "faculty"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="role_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default="faculty")
    gmail_linked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Venue(Timestamps, Base):
    __tablename__ = "venue"
    __table_args__ = (
        CheckConstraint(_in("type", VENUE_TYPES), name="type_valid"),
        CheckConstraint("capacity > 0", name="capacity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    building: Mapped[str | None] = mapped_column(Text)
    capacity: Mapped[int] = mapped_column(Integer)
    n_computers: Mapped[int] = mapped_column(Integer, server_default="0")
    byod: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    equipment: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    software: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    typical_use: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    description: Mapped[str | None] = mapped_column(Text)
    in_charge_id: Mapped[int | None] = mapped_column(ForeignKey("faculty.id"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    in_charge: Mapped[Faculty | None] = relationship()


class RecurringSlot(Timestamps, Base):
    """A weekly class from a timetable. Rows pending review do not block bookings."""

    __tablename__ = "recurring_slot"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_valid"),
        CheckConstraint("end_time > start_time", name="time_order"),
        CheckConstraint("valid_to >= valid_from", name="date_order"),
        CheckConstraint(_in("status", REVIEW_STATUSES), name="status_valid"),
        Index("ix_recurring_slot_venue_weekday", "venue_id", "weekday"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venue.id"))
    weekday: Mapped[int] = mapped_column(SmallInteger)  # 0 = Monday
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)
    course: Mapped[str | None] = mapped_column(Text)
    class_label: Mapped[str | None] = mapped_column(Text)  # e.g. "CSE III D"
    batch: Mapped[str | None] = mapped_column(Text)  # e.g. "B1"
    source_ref: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    confidence: Mapped[float | None] = mapped_column(Float)

    venue: Mapped[Venue] = relationship()


class Event(Timestamps, Base):
    """What a faculty is booking for. One event can have bookings on several days or venues."""

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(_in("format", FORMATS), name="format_valid"),
        CheckConstraint("significance BETWEEN 1 AND 3", name="significance_valid"),
        CheckConstraint("expected_headcount > 0", name="headcount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(Text)
    expected_headcount: Mapped[int] = mapped_column(Integer)
    significance: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    guest_profile: Mapped[str | None] = mapped_column(Text)
    organiser_dept: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    description: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))

    created_by: Mapped[Faculty] = relationship()
    bookings: Mapped[list["Booking"]] = relationship(back_populates="event")
    parties: Mapped[list["EventParty"]] = relationship(back_populates="event")


class Booking(Timestamps, Base):
    """One venue for one continuous time range. Multi-day or multi-venue events have several rows."""

    __tablename__ = "booking"
    __table_args__ = (
        CheckConstraint(_in("status", BOOKING_STATUSES), name="status_valid"),
        CheckConstraint("end_ts > start_ts", name="time_order"),
        ExcludeConstraint(
            ("venue_id", "="),
            (text("tstzrange(start_ts, end_ts, '[)')"), "&&"),
            name="no_overlapping_active_bookings",
            using="gist",
            where=text(_in("status", ACTIVE_BOOKING_STATUSES)),
        ),
        Index("ix_booking_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"))
    venue_id: Mapped[int] = mapped_column(ForeignKey("venue.id"))
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="hold")
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("faculty.id"))
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    soft_conflict_note: Mapped[str | None] = mapped_column(Text)
    replaces_booking_id: Mapped[int | None] = mapped_column(ForeignKey("booking.id"))
    calendar_event_id: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default="1")

    event: Mapped[Event] = relationship(back_populates="bookings")
    venue: Mapped[Venue] = relationship()


class EventParty(Base):
    """Everyone notified about an event's bookings."""

    __tablename__ = "event_party"
    __table_args__ = (
        CheckConstraint(_in("role", PARTY_ROLES), name="role_valid"),
        UniqueConstraint("event_id", "email", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)

    event: Mapped[Event] = relationship(back_populates="parties")


class BookingHistory(Base):
    """Audit trail. Written in the same transaction as every booking change."""

    __tablename__ = "booking_history"
    __table_args__ = (CheckConstraint(_in("action", HISTORY_ACTIONS), name="action_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("booking.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("faculty.id"))  # null = system
    action: Mapped[str] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    """Outbox. Rows are written with the change that causes them and sent by a worker."""

    __tablename__ = "notification"
    __table_args__ = (
        CheckConstraint(_in("channel", CHANNELS), name="channel_valid"),
        CheckConstraint(_in("status", ("queued", "sent", "failed")), name="status_valid"),
        Index("ix_notification_queued", "status", "created_at", postgresql_where=text("status = 'queued'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"))
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("booking.id"))
    recipient: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # e.g. request_created, approved, change_requested
    payload: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceDocument(Timestamps, Base):
    """A file ingested from Drive (or a sample). md5 lets sync skip unchanged files."""

    __tablename__ = "source_document"
    __table_args__ = (CheckConstraint(_in("kind", ("timetable", "circular", "report", "other")), name="kind_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    drive_file_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    md5: Mapped[str | None] = mapped_column(Text)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class PastEvent(Timestamps, Base):
    """A past use of a venue: extracted from circulars and reports, or from a confirmed booking."""

    __tablename__ = "past_event"
    __table_args__ = (
        CheckConstraint(_in("status", REVIEW_STATUSES), name="status_valid"),
        CheckConstraint(_in("source_type", ("circular", "report", "booking", "ground_truth")), name="source_valid"),
        Index("ix_past_event_venue_date", "venue_id", "start_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str | None] = mapped_column(Text)
    expected_headcount: Mapped[int | None] = mapped_column(Integer)
    actual_headcount: Mapped[int | None] = mapped_column(Integer)
    significance: Mapped[int | None] = mapped_column(SmallInteger)
    guest_profile: Mapped[str | None] = mapped_column(Text)
    organiser_dept: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    description: Mapped[str | None] = mapped_column(Text)
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venue.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    source_type: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_document.id"))
    status: Mapped[str] = mapped_column(Text, server_default="active")
    confidence: Mapped[float | None] = mapped_column(Float)
    embedding = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    venue: Mapped[Venue | None] = relationship()


class UsagePattern(Base):
    """A seasonal soft conflict, derived nightly from past_event. Warns, never blocks."""

    __tablename__ = "usage_pattern"
    __table_args__ = (
        CheckConstraint("week_start BETWEEN 1 AND 53 AND week_end BETWEEN 1 AND 53", name="weeks_valid"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venue.id"), index=True)
    category: Mapped[str] = mapped_column(Text)
    week_start: Mapped[int] = mapped_column(SmallInteger)
    week_end: Mapped[int] = mapped_column(SmallInteger)
    weekdays: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger))  # e.g. [5] for Saturdays only
    years_seen: Mapped[int] = mapped_column(SmallInteger)
    years_observed: Mapped[int] = mapped_column(SmallInteger)
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[list] = mapped_column(JSONB, server_default="[]")  # past_event ids and source refs
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    venue: Mapped[Venue] = relationship()


class ExpectedReservation(Timestamps, Base):
    """Declared by an admin, e.g. "admissions will need A Block SH in March". Warns, never blocks."""

    __tablename__ = "expected_reservation"
    __table_args__ = (CheckConstraint("end_date >= start_date", name="date_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venue.id"), index=True)
    category: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    owner_team: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("faculty.id"))

    venue: Mapped[Venue] = relationship()


class DocumentChunk(Base):
    """The RAG index: embedded chunks plus a full-text column for hybrid search."""

    __tablename__ = "document_chunk"
    __table_args__ = (
        UniqueConstraint("source_document_id", "chunk_index"),
        Index("ix_document_chunk_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_document_chunk_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_document_id: Mapped[int] = mapped_column(ForeignKey("source_document.id", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('english', text)", persisted=True))
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venue.id"))
    doc_date: Mapped[date | None] = mapped_column(Date)
    category: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, server_default="{}")


class ChatSession(Timestamps, Base):
    """One conversation with the agent: the transcript and the booking draft it is building."""

    __tablename__ = "chat_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id", ondelete="CASCADE"), index=True)
    messages: Mapped[list] = mapped_column(JSONB, server_default="[]")  # [{role, content, data?}]
    state: Mapped[dict] = mapped_column(JSONB, server_default="{}")  # draft request, shown options, pending action
