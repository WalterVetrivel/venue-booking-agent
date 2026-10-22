# Venue Booking Agent

A chat assistant that helps KIOT faculty find and book venues and labs. It checks timetables, past events and
seasonal patterns, recommends venues with reasons, and routes bookings to venue in-charges for approval.

- Plan: [docs/PLAN.md](docs/PLAN.md)
- One-time setup (Docker, Groq, Google): [docs/SETUP.md](docs/SETUP.md)
- Mock data: [data/mock/README.md](data/mock/README.md)

## Quickstart

```
cp .env.example .env            # then fill it in
docker compose up -d db         # Postgres 16 with pgvector
cd backend
uv sync
uv run alembic upgrade head     # create the schema
uv run python -m backend.seed --with-past-events   # faculty, venues, lab timetable slots, past events
```

## Tests

The tests migrate a separate `venue_test` database from scratch and roll back after each test.

```
docker compose exec db createdb -U venue venue_test   # once
cd backend
uv run pytest
```

Set `TEST_DATABASE_URL` to point them at another database.
