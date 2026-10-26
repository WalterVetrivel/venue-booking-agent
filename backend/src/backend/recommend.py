"""Venue recommendation: hard filter, soft-conflict check, ranking by similar past events.

Everything here is plain code over the database; the LLM only writes the explanation afterwards.
See docs/PLAN.md, "Venue recommendation from past events".
"""

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import booking as bk
from backend.embeddings import Embedder
from backend.models import FORMATS, PastEvent, Venue

HEADROOM = 1.1  # seats needed per expected participant
LAB_TYPES = ("computer_lab", "coe_lab")
VIP_GUESTS = ("government", "industry")


class BookingRequest(BaseModel):
    """What a faculty wants to book. The agent fills it in over the conversation."""

    title: str | None = None
    category: str | None = None
    format: str | None = Field(None, pattern="|".join(FORMATS))
    expected_headcount: int | None = Field(None, gt=0)
    dates: list[date] = Field(default_factory=list, description="Each day of the event")
    start_time: time | None = None
    end_time: time | None = None
    significance: int | None = Field(None, ge=1, le=3)
    guest_profile: str | None = None
    audience: str | None = None
    topics: list[str] = Field(default_factory=list)
    software: list[str] = Field(default_factory=list, description="Software needed on lab computers")
    equipment: list[str] = Field(default_factory=list)
    preferred_venue: str | None = None

    def missing(self) -> list[str]:
        """Fields the agent must ask for before it can look anything up."""
        need = {"expected_headcount": self.expected_headcount, "dates": self.dates, "start_time": self.start_time,
                "end_time": self.end_time, "format": self.format}
        return [k for k, v in need.items() if not v]

    def slots(self) -> list[tuple[datetime, datetime]]:
        tz = bk._tz()
        return [(datetime.combine(d, self.start_time, tz), datetime.combine(d, self.end_time, tz))
                for d in sorted(self.dates)]

    def describe(self) -> str:
        return " | ".join(x for x in (self.title, self.category, self.format, self.guest_profile, self.audience,
                                      ", ".join(self.topics)) if x)


@dataclass
class Similar:
    past_event_id: int
    title: str
    venue: str
    date: str
    headcount: int | None
    similarity: float
    source: str | None


@dataclass
class Option:
    venue_id: int
    code: str
    name: str
    capacity: int
    score: float
    reasons: list[str] = field(default_factory=list)
    soft_conflicts: list[dict] = field(default_factory=list)
    similar_events: list[dict] = field(default_factory=list)


def _seats_needed(r: BookingRequest) -> int:
    return math.ceil(r.expected_headcount * HEADROOM)


def suits(v: Venue, r: BookingRequest) -> str | None:
    """Why a venue can't host this event at all, or None if it can."""
    if v.capacity < _seats_needed(r):
        return f"seats {v.capacity}, fewer than {r.expected_headcount} people plus headroom"
    if r.format == "hands-on":
        if v.type not in LAB_TYPES:
            return "not a lab"
        if not v.byod and v.n_computers < r.expected_headcount:
            return f"has {v.n_computers} computers for {r.expected_headcount} people"
    lower_sw = {s.lower() for s in v.software}
    missing = [s for s in r.software if not any(s.lower() in x for x in lower_sw)]
    if missing and not v.byod:
        return f"doesn't have {', '.join(missing)}"
    lower_eq = {e.lower() for e in v.equipment}
    missing = [e for e in r.equipment if e.lower() not in lower_eq]
    if missing:
        return f"doesn't have {', '.join(missing)}"
    return None


def find_free_venues(db: Session, r: BookingRequest) -> tuple[list[Venue], dict[str, str]]:
    """Venues that suit the event and are free in every slot, and why each other venue was left out."""
    free, excluded = [], {}
    for v in db.scalars(select(Venue).where(Venue.active).order_by(Venue.id)):
        reason = suits(v, r)
        if reason is None:
            for start, end in r.slots():
                clashes = bk.find_clashes(db, v.id, start, end)
                if clashes:
                    reason = f"busy on {start:%d %b}: {clashes[0].detail}"
                    break
        if reason:
            excluded[v.code] = reason
        else:
            free.append(v)
    return free, excluded


def similar_events(db: Session, embedder: Embedder, r: BookingRequest, k: int = 10) -> list[Similar]:
    vec = embedder.query(r.describe())
    dist = PastEvent.embedding.cosine_distance(vec)
    rows = db.execute(select(PastEvent, dist).where(PastEvent.status == "active", PastEvent.venue_id.is_not(None),
                                                    PastEvent.embedding.is_not(None)).order_by(dist).limit(k))
    return [Similar(e.id, e.title, e.venue.code, e.start_date.isoformat(), e.actual_headcount or e.expected_headcount,
                    round(1 - d, 3), e.source_ref) for e, d in rows]


def rank(db: Session, r: BookingRequest, venues: list[Venue], similar: list[Similar]) -> list[Option]:
    total = sum(max(s.similarity, 0) for s in similar) or 1
    vip = (r.significance or 1) >= 3 or r.guest_profile in VIP_GUESTS
    options = []
    for v in venues:
        used = [s for s in similar if s.venue == v.code]
        usage = sum(max(s.similarity, 0) for s in used) / total
        fill = r.expected_headcount / v.capacity
        fit = 1.0 if vip else max(0.0, 1 - abs(0.75 - fill) / 0.75)  # about three-quarters full is ideal
        prior = 0.2 if (r.format == "hands-on") == (v.type in LAB_TYPES) else 0.0
        soft = [c for start, end in r.slots() for c in bk.find_soft_conflicts(db, v.id, start, end)]
        penalty = 0.6 * max((c.confidence for c in soft), default=0)
        score = round(0.5 * usage + 0.3 * fit + prior - penalty, 3)

        reasons = [f"Seats {v.capacity} for {r.expected_headcount} people"
                   + (" (a large hall suits an institution-level event)" if vip and fill < 0.5 else "")]
        if r.format == "hands-on":
            reasons.append("BYOD lab" if v.byod else f"{v.n_computers} computers")
        if used:
            reasons.append(f"{len(used)} of the {len(similar)} most similar past events "
                           f"{'was' if len(used) == 1 else 'were'} held here, e.g. {used[0].title} ({used[0].date})")
        unique_soft = list({c.note: c for c in soft}.values())
        options.append(Option(v.id, v.code, v.name, v.capacity, score, reasons,
                              [asdict(c) for c in unique_soft], [asdict(s) for s in used[:3]]))
    return sorted(options, key=lambda o: -o.score)


def nearest_free_dates(db: Session, r: BookingRequest, days: int = 7, limit: int = 3) -> list[dict]:
    """When nothing suitable is free, the closest other days at the same time where something is."""
    first = min(r.dates)
    found = []
    for delta in sorted(range(-days, days + 1), key=abs):
        d = first + timedelta(days=delta)
        if delta == 0 or d < bk._now().date() or d.weekday() == 6:
            continue
        alt = r.model_copy(update={"dates": [d]})
        free, _ = find_free_venues(db, alt)
        if free:
            found.append({"date": d.isoformat(), "venues": [v.code for v in free[:3]]})
        if len(found) == limit:
            break
    return found


def recommend(db: Session, embedder: Embedder, r: BookingRequest, top: int = 3) -> dict:
    """The full look-up: free venues, ranked with reasons and soft conflicts; or the nearest free dates."""
    free, excluded = find_free_venues(db, r)
    if r.format == "talk" and any(v.type not in LAB_TYPES for v in free):
        for v in [v for v in free if v.type in LAB_TYPES]:  # keep labs free for hands-on work
            excluded[v.code] = "a lab; a seminar hall is free for a talk"
            free.remove(v)
    similar = similar_events(db, embedder, r)
    options = rank(db, r, free, similar)
    best_clean = [o for o in options if not o.soft_conflicts]
    shown = options[:top]
    if shown and shown[0].soft_conflicts and best_clean and best_clean[0] not in shown:
        shown = shown[: top - 1] + best_clean[:1]  # always offer a clean alternative to a warned venue
    out = {"options": [asdict(o) for o in shown], "excluded": excluded,
           "similar_events": [asdict(s) for s in similar[:5]]}
    if r.preferred_venue:
        pref = next((o for o in options if r.preferred_venue.lower() in (o.code.lower(), o.name.lower())), None)
        out["preferred"] = asdict(pref) if pref else {"code": r.preferred_venue,
                                                      "unavailable": excluded.get(r.preferred_venue, "not found")}
    if not options:
        out["nearest_free_dates"] = nearest_free_dates(db, r)
    return out
