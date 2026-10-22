# Initial setup before implementation

Written for a Windows laptop with no GPU (PowerShell commands). Steps 1-6 take roughly an evening; nothing here costs money.

## 1. Install the tools

| Tool | Why | Install (PowerShell) | Check |
| --- | --- | --- | --- |
| Git | Version control | `winget install Git.Git` | `git --version` |
| Python 3.12 + uv | Backend, agent, MCP servers, ingestion | `winget install Python.Python.3.12` then `winget install astral-sh.uv` | `python --version`, `uv --version` |
| Node.js 22 LTS | Frontend (Next.js) | `winget install OpenJS.NodeJS.LTS` | `node -v` |
| Docker Desktop | Runs Postgres + pgvector locally | `winget install Docker.DockerDesktop` (enable the WSL 2 backend when asked, then restart) | `docker version` |
| VS Code (optional) | Editor | `winget install Microsoft.VisualStudioCode` | |

Open a new terminal after installing so the PATH updates.

## 2. Create the project skeleton

Inside `D:\projects\venue_booking_agent`:

```powershell
git init
mkdir backend, frontend, mcp_servers, data\samples, data\mock, secrets, docs
```

Create `.gitignore` with at least:

```
.env
secrets/
*.token.json
__pycache__/
.venv/
node_modules/
pgdata/
```

Move `PLAN.md` and `SETUP.md` into `docs\` if you like.

## 3. Start Postgres with pgvector

Create `docker-compose.yml` in the project root:

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: venue
      POSTGRES_PASSWORD: venue_dev_pw
      POSTGRES_DB: venue_booking
    ports:
      - "5432:5432"
    volumes:
      - ./pgdata:/var/lib/postgresql/data
```

Then:

```powershell
docker compose up -d
docker compose exec db psql -U venue -d venue_booking -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS btree_gist;"
```

`btree_gist` is needed for the no-double-booking exclusion constraint; `vector` is pgvector. Both commands should print `CREATE EXTENSION`.

## 4. Get a Groq API key

1. Sign up at https://console.groq.com and create an API key.
2. Note which models your account lists (one large model with tool calling for the agent, one small fast model for extraction).
3. Quick test:

```powershell
$env:GROQ_API_KEY = "gsk_..."
curl.exe https://api.groq.com/openai/v1/models -H "Authorization: Bearer $env:GROQ_API_KEY"
```

You should get a JSON list of models.

## 5. Set up Google access (free, no billing)

In https://console.cloud.google.com with your personal account:

1. **Create a project**, e.g. `venue-booking-agent`.
2. **Enable APIs** (APIs & Services → Library): Google Drive API, Gmail API, Google Calendar API.
3. **Configure the OAuth consent screen** (Google Auth Platform → Branding / Audience):
    - User type: External. App name: Venue Booking Agent. Support email: yours.
    - Publishing status: leave in **Testing**.
    - **Test users:** add your own address and every account you'll test with (see step 6).
4. **Add scopes** (Data Access): `openid`, `email`, `profile`, `.../auth/drive.readonly`, `.../auth/gmail.readonly`, `.../auth/gmail.send`, `.../auth/calendar.events`.
5. **Create an OAuth client** (Clients → Create client):
    - Type: **Web application**.
    - Authorised JavaScript origin: `http://localhost:3000`
    - Authorised redirect URI: `http://localhost:8000/auth/google/callback`
    - Download the JSON and save it as `secrets\google_oauth_client.json`.

While the app is in Testing mode, sign-ins expire after about 7 days and only listed test users can sign in. That's fine for the prototype.

## 6. Prepare Google Drive and test accounts

1. In your Drive, create a folder `Venue Booking Agent Data` with subfolders `circulars`, `reports`, `timetables`.
2. Open the folder and copy its ID from the URL (the part after `/folders/`).
3. Create one or two extra Gmail accounts to act as a test faculty member and a test venue in-charge, and add them as test users in step 5.3. Your own account can be the admin and the sender of notifications.

## 7. Create the `.env` file

In the project root:

```
DATABASE_URL=postgresql+psycopg://venue:venue_dev_pw@localhost:5432/venue_booking
GROQ_API_KEY=gsk_...
GROQ_AGENT_MODEL=<large model id from the Groq console>
GROQ_EXTRACT_MODEL=<small model id from the Groq console>
GOOGLE_OAUTH_CLIENT_FILE=secrets/google_oauth_client.json
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/auth/google/callback
DRIVE_ROOT_FOLDER_ID=<folder id from step 6>
ADMIN_EMAIL=<your gmail address>
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

## 8. Set up the Python environment

```powershell
cd backend
uv init --python 3.12
uv add fastapi "uvicorn[standard]" sqlalchemy "psycopg[binary]" alembic pgvector pydantic-settings python-dotenv `
       langgraph langchain-groq langchain-mcp-adapters mcp `
       google-api-python-client google-auth-oauthlib `
       pdfplumber fastembed apscheduler httpx
uv run python -c "import fastapi, langgraph, pdfplumber, fastembed; print('ok')"
```

The first `fastembed` use downloads the small embedding model (about 130 MB); it runs fine on CPU.

## 9. Hand over the inputs

- Put the sample timetable PDF in `data\samples\`. The mock timetables, venues, circulars and reports are generated from it.
- Answer the open questions in the plan: who maintains the faculty allowlist, how many venues and blocks the mock campus should have, and whether there is a cut-off for changes and cancellations.

## Checklist

- [ ] Tools installed and checked
- [ ] Project skeleton and `.gitignore`
- [ ] Postgres running with `vector` and `btree_gist`
- [ ] Groq key works
- [ ] Google project, APIs, consent screen, test users, OAuth client JSON
- [ ] Drive folder and its ID
- [ ] `.env` filled in
- [ ] Python environment installs cleanly
- [ ] Sample timetable in `data\samples\`

Once these are done, implementation starts with Phase 0: database schema and migrations, the venue list, and the mock data.
