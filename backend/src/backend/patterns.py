"""Seasonal usage patterns (soft conflicts), recomputed nightly from `past_event`.

A venue is "usually used for X around week W" when events of category X used it within the same few
weeks in at least 2 of the last 3 years. Such patterns only ever warn; see docs/PLAN.md.
"""

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.models import PastEvent, UsagePattern

YEARS = 3  # look back this many years
MIN_YEARS = 2  # seen in at least this many of them
GAP_WEEKS = 4  # events this close in the year belong to the same season (e.g. training every few Saturdays)
# Institution-wide uses that recur on the calendar. One-off talks and workshops land in a hall in a similar
# month by chance; counting them would bury the real warnings in noise.
SEASONAL_CATEGORIES = ("admissions", "exam", "inauguration", "ceremony", "address", "fdp", "placement_training",
                       "training", "hackathon", "cultural")


def _days(e: PastEvent) -> list[date]:
    end = e.end_date or e.start_date
    return [e.start_date + timedelta(days=i) for i in range((end - e.start_date).days + 1)]


def compute(db: Session, today: date | None = None) -> list[UsagePattern]:
    """Rebuild usage_pattern from active past events in the last YEARS years."""
    today = today or date.today()
    since = today - timedelta(days=365 * YEARS)

    def ago(d: date) -> int:  # 0 for the last 12 months, 1 for the 12 before, ...
        return (today - d).days // 365

    events = list(db.scalars(select(PastEvent).where(
        PastEvent.status == "active", PastEvent.venue_id.is_not(None), PastEvent.category.in_(SEASONAL_CATEGORIES),
        PastEvent.start_date > since, PastEvent.start_date <= today)))
    observed = len({ago(e.start_date) for e in events})

    by_key: dict[tuple[int, str], list[PastEvent]] = defaultdict(list)
    for e in events:
        by_key[(e.venue_id, e.category)].append(e)

    db.execute(delete(UsagePattern))
    patterns = []
    for (venue_id, category), group in by_key.items():
        days = sorted(((d.isocalendar().week, d, e) for e in group for d in _days(e)), key=lambda t: (t[0], t[1]))
        season: list[tuple[int, date, PastEvent]] = []
        for item in days + [None]:  # a sentinel closes the last season
            if item is not None and (not season or item[0] - season[-1][0] <= GAP_WEEKS):
                season.append(item)
                continue
            years = {ago(d) for _, d, _ in season if d <= today}
            if len(years) >= MIN_YEARS:
                weekdays = sorted({d.weekday() for _, d, _ in season})
                seen_events = list({e.id: e for _, _, e in season}.values())
                p = UsagePattern(
                    venue_id=venue_id, category=category, week_start=season[0][0], week_end=season[-1][0],
                    weekdays=weekdays if len(weekdays) <= 2 else None, years_seen=len(years),
                    years_observed=observed, confidence=round(len(years) / observed, 2),
                    evidence=[{"past_event_id": e.id, "title": e.title, "date": e.start_date.isoformat(),
                               "source": e.source_ref} for e in sorted(seen_events, key=lambda e: e.start_date)][-10:])
                db.add(p)
                patterns.append(p)
            season = [item] if item is not None else []
    db.flush()
    return patterns
