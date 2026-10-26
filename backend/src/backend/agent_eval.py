"""Check the agent against the request set in data/eval/requests.jsonl.

    uv run python -m backend.agent_eval          # recommendations only, no Groq needed
    uv run python -m backend.agent_eval --llm    # also how well Groq reads each request
    uv run python -m backend.agent_eval --mails  # how well Groq reads the event out of the mock mail threads

For each request the recommendation must be valid (every option suits the event, is free in every slot
and is of an expected venue type) and, where a seasonal conflict is planted, report it on the venue asked
for while offering a venue without one. The Phase 3 bar is 80% valid and every planted conflict reported.
With --llm the request text goes through the agent's understanding step and each field is compared with
the expected one; --mails does the same for the 9 mock mail threads that ask for a venue. Run `knowledge ingest`
and `knowledge patterns` first so the patterns exist.
"""

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import booking as bk
from backend.agent import Turn, understand_context
from backend.models import Venue
from backend.recommend import BookingRequest, recommend, suits

CASES = Path(__file__).resolve().parents[3] / "data" / "eval" / "requests.jsonl"
MAILS = Path(__file__).resolve().parents[3] / "data" / "mock" / "mails" / "mock_mails.json"
MAIL_PREFIX = "Use the details in this mail for my booking:\n\n"  # as the agent's use_mail step sends it
TODAY = date(2026, 10, 22)  # the requests are written relative to this day
FIELDS = ("format", "expected_headcount", "dates", "start_time", "end_time", "category", "preferred_venue")


def load(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def mail_cases(path: Path = MAILS) -> list[dict]:
    """The mock threads that ask for a venue, as cases: the thread's text and the expected fields."""
    out = []
    for t in json.loads(path.read_text())["threads"]:
        if e := t.get("expected_booking"):
            out.append({"id": t["id"], "text": MAIL_PREFIX + "\n\n---\n\n".join(m["body"] for m in t["messages"]),
                        "request": {"format": e["format"], "expected_headcount": e["headcount"], "dates": [e["date"]],
                                    "start_time": e["start"], "end_time": e["end"]}})
    return out


def check(db: Session, embedder, case: dict) -> dict:
    r, expect = BookingRequest(**case["request"]), case["expect"]
    out = recommend(db, embedder, r)
    venues = {v.code: v for v in db.scalars(select(Venue))}
    problems = []
    if expect.get("none_fit"):
        if out["options"]:
            problems.append(f"expected nothing to fit, got {[o['code'] for o in out['options']]}")
    elif not out["options"]:
        problems.append("no options")
    for o in out["options"]:
        v = venues[o["code"]]
        if why := suits(v, r):
            problems.append(f"{v.code} doesn't suit: {why}")
        if v.type not in expect.get("types", [v.type]):
            problems.append(f"{v.code} is a {v.type}")
        if any(bk.find_clashes(db, v.id, s, e) for s, e in r.slots()):
            problems.append(f"{v.code} is busy")
    soft = expect.get("soft")
    reported = None
    if soft:
        pref = out.get("preferred") or {}
        reported = any(c["category"] == soft["category"] for c in pref.get("soft_conflicts", []))
        if not reported:
            problems.append(f"{soft['category']} season on {soft['venue']} not reported")
        if not any(not o["soft_conflicts"] for o in out["options"]):
            problems.append("no option without a seasonal warning")
    return {"id": case["id"], "ok": not problems, "problems": problems, "soft_reported": reported,
            "options": [o["code"] for o in out["options"]]}


def _same(field: str, got, want) -> bool:
    if field == "dates":
        return sorted(str(d) for d in got) == sorted(want)
    if field in ("start_time", "end_time"):
        return got is not None and got.strftime("%H:%M") == want
    if isinstance(want, str):
        return str(got or "").lower() == want.lower()
    return got == want


def understanding(llm, db: Session, case: dict) -> dict:
    """Which expected fields the model got right from the request text alone."""
    venues = [{"code": v.code, "name": v.name, "type": v.type, "capacity": v.capacity}
              for v in db.scalars(select(Venue).where(Venue.active))]
    turn: Turn = llm.understand(understand_context({}, [], venues), [], case["text"])
    got = turn.request
    return {f: _same(f, getattr(got, f), case["request"][f]) for f in FIELDS if f in case["request"]}


def understanding_score(llm, db: Session, cases: list[dict]) -> dict:
    per_field: dict[str, list[bool]] = {}
    for c in cases:
        try:
            for f, ok in understanding(llm, db, c).items():
                per_field.setdefault(f, []).append(ok)
        except Exception as e:  # count a failed call as wrong on every field
            print(f"{c['id']}: {e}")
            for f in FIELDS:
                if f in c["request"]:
                    per_field.setdefault(f, []).append(False)
    return {f: round(sum(v) / len(v), 3) for f, v in per_field.items()}


def evaluate(db: Session, embedder, cases: list[dict], llm=None) -> dict:
    results = [check(db, embedder, c) for c in cases]
    soft = [r for r in results if r["soft_reported"] is not None]
    summary = {"requests": len(results), "valid": sum(r["ok"] for r in results),
               "soft_planted": len(soft), "soft_reported": sum(r["soft_reported"] for r in soft),
               "failures": [r for r in results if not r["ok"]]}
    summary["valid_rate"] = round(summary["valid"] / len(results), 3)
    if llm is not None:
        summary["understanding"] = understanding_score(llm, db, cases)
    return summary


def main() -> None:
    from backend.agent import GroqLLM
    from backend.db import SessionLocal
    from backend.embeddings import get_embedder

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--llm", action="store_true", help="also check the understanding step with Groq")
    p.add_argument("--mails", action="store_true", help="only check what Groq reads from the mock mail threads")
    p.add_argument("--cases", type=Path, default=CASES)
    args = p.parse_args()
    bk._now = lambda: datetime.combine(TODAY, datetime.min.time(), bk._tz()).replace(hour=9)
    with SessionLocal() as db:
        if args.mails:
            print(json.dumps({"mails": understanding_score(GroqLLM(), db, mail_cases())}, indent=2))
            return
        out = evaluate(db, get_embedder(), load(args.cases), GroqLLM() if args.llm else None)
    for f in out.pop("failures"):
        print(f"FAIL {f['id']}: {'; '.join(f['problems'])}")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
