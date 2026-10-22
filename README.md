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
uv run python -m backend.timetable ../data/mock/timetables   # optional: read the timetable PDFs instead
```

## Run the app

```
cd backend
uv run uvicorn backend.app:app --reload
```

Open http://localhost:8000 for the web app; the API docs are at http://localhost:8000/docs. To try it without Google Sign-In, set `DEV_LOGIN=true` in `.env`
and sign in with `POST /auth/dev-login` using an email on the faculty list. Pending requests expire after 48 hours
(the in-charge is reminded at 24); a background job checks every 10 minutes.

## Circulars, reports and seasonal patterns

```
cd backend
uv run python -m backend.knowledge ingest ../data/mock/drive   # index for search and extract past events (Groq)
uv run python -m backend.knowledge ingest --no-extract ../data/mock/drive   # search index only, no Groq calls
uv run python -m backend.knowledge evaluate                    # extraction accuracy against the mock answer key
uv run python -m backend.knowledge search "admission counselling venue"
uv run python -m backend.knowledge patterns                    # recompute seasonal patterns
```

Running extraction replaces the past events the seed loaded from the answer key. Each file is committed
separately and unchanged files are skipped, so a run stopped by Groq's rate limits can simply be started again.
The embedding model (about 130 MB) downloads on first use.

To read from Google Drive instead, set `DRIVE_ROOT_FOLDER_ID`, sign in as the admin, open Knowledge and click
"Link Google Drive". The folder needs `circulars`, `reports` and `timetables` subfolders. It is then read every
night at 2 am, or on demand with `uv run python -m backend.knowledge drive`.

## Tests

The tests migrate a separate `venue_test` database from scratch and roll back after each test.

```
docker compose exec db createdb -U venue venue_test   # once
cd backend
uv run pytest
```

Set `TEST_DATABASE_URL` to point them at another database.
