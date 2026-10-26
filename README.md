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
"Link Google account". The folder needs `circulars`, `reports` and `timetables` subfolders. It is then read every
night at 2 am, or on demand with `uv run python -m backend.knowledge drive`.

## Gmail and notices

**Notices.** The same "Link Google account" on the Knowledge page also lets the app send email notices and calendar
invites from that account: a mail to each person for every change, and one calendar event per confirmed booking,
with everyone involved invited. A change moves the event and a cancellation deletes it, so Google sends the updates.
`NOTIFY_MODE` in `.env` decides who receives them:
- `admin` (the default) sends everything to `ADMIN_EMAIL` only, marked with who it was meant for. This is safe
  while the faculty list has placeholder addresses.
- `live` sends to the real people.
- `off` sends nothing; the in-app notices still work.

Notices are sent every minute, retried up to 5 times, and the Knowledge page shows the outbox.

**Faculty Gmail.** On the Assistant page, "Link my Gmail" gives the assistant read-only access to your own mailbox
(it refuses any other account). Then say "the details are in my mail" or press "From my Gmail". It shows the
matching mails. Only the one you pick is read, and only its text goes to Groq; nothing from your mail is stored.
To try it with the mock threads, load them into your Gmail with `uv run python data/mock/mails/load_mock_mails.py`
(see the top of that script), then check how well Groq reads them:

```
cd backend
uv run python -m backend.agent_eval --mails
```

The gmail and drive tools are MCP servers too, at `/mcp/gmail/mcp` and `/mcp/drive/mcp`.

## The assistant (chat) and MCP servers

The Assistant page is the chat. It needs `GROQ_API_KEY` and `GROQ_AGENT_MODEL` in `.env`. It asks for whatever is
missing (headcount, dates, times, talk or hands-on), suggests free venues with reasons and seasonal warnings, and
only requests, changes or cancels a booking after you say yes to the exact action. Run `alembic upgrade head` once
for the conversation table.

Its tools are MCP servers, also served over streamable HTTP so you can try them from the MCP Inspector or
Claude Desktop: `/mcp/booking/mcp`, `/mcp/knowledge/mcp`, `/mcp/recommender/mcp`, `/mcp/gmail/mcp` and
`/mcp/drive/mcp`. Get a bearer token (valid for
an hour, acting as you) from http://localhost:8000/mcp/token while signed in. Over stdio:

```
cd backend
ACTING_EMAIL=you@example.com uv run python -m backend.mcp_servers booking   # or knowledge, recommender, gmail, drive
```

To check the recommendations and the model's reading of requests against the 32 requests in `data/eval`:

```
cd backend
uv run python -m backend.agent_eval          # valid shortlists and planted seasonal warnings, no Groq
uv run python -m backend.agent_eval --llm    # also how accurately Groq reads each request, field by field
```

## Tests

The tests migrate a separate `venue_test` database from scratch and roll back after each test.

```
docker compose exec db createdb -U venue venue_test   # once
cd backend
uv run pytest
```

Set `TEST_DATABASE_URL` to point them at another database.
