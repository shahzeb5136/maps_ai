# Property Scanner API

Turns a street address into a property intelligence report: satellite and
Street View imagery, an AI condition assessment, a historical change-detection
timeline, and three costed renovation concepts rendered image-to-image.

Runs on Railway as a service the website calls. One scan costs **1 credit**
from the shared `users` wallet.

```
scanner/    the pipeline — Google Maps + Gemini + the PDF. No web, no database.
api/        FastAPI: Clerk auth, credits, job rows, signed asset URLs.
main.py     local CLI for a one-off scan.
```

## What a scan produces

Written to `$STORAGE_DIR/scans/<scan_id>/`:

| File | What it is |
|---|---|
| `satellite_topdown.jpg` | Overhead parcel view |
| `street_facade_front.jpg` + 3 flanks | Street View orbit aimed at the building |
| `timeline_YYYY_MM.jpg` | One frame per historical Street View capture |
| `reno_before.jpg` / `reno_after_N.jpg` | Before/after pairs, one per concept |
| `report.json` | The full structured payload |
| `report.pdf` | The downloadable report |

The API never serves these directly. It hands the browser HMAC-signed,
time-limited URLs, because `<img src>` and download links cannot send an
`Authorization` header.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Liveness. No auth. |
| `GET` | `/api/credits` | Wallet balance |
| `GET` | `/api/scans` | The caller's scans, newest first |
| `POST` | `/api/scans` | `{address}` → charges 1 credit, queues the scan |
| `GET` | `/api/scans/{id}` | One scan; `result` carries signed URLs once complete |
| `DELETE` | `/api/scans/{id}` | Removes the row and its files |
| `GET` | `/api/scans/{id}/files/{name}` | Signed artifact. No bearer token. |

Everything except `/health` and the signed file route needs
`Authorization: Bearer <clerk session jwt>`.

`POST /api/scans` returns **402** when the wallet is short and **409** when the
user already has a scan running.

## Credits

`users.credits` is owned by the Next.js app; this service only decrements it.

- The one-at-a-time check, the debit and the scan row are one transaction
  against a `SELECT ... FOR UPDATE` on the user's row. Concurrent submits
  serialise, and a failure anywhere rolls the charge back — a user can never be
  debited for a scan that was never queued.
- Any failure — bad address, no imagery, a model error, a timeout, a redeploy
  mid-scan — refunds automatically. The refund is guarded by a `refunded` flag
  on the scan row, so it can only ever happen once.
- The PDF is the one non-fatal stage: if ReportLab fails, the scan still
  completes and the web report still works. You just don't get a download.

---

# Railway setup

You need: this repo on GitHub, and the Postgres you already run for credits.

### 1. Push the repo

From this folder:

```bash
git init && git add . && git commit -m "Property scanner service"
```

Create an empty GitHub repo, then:

```bash
git remote add origin https://github.com/<you>/property-scanner.git && git branch -M main && git push -u origin main
```

`.gitignore` already excludes `.env` and `property_scans/`. **Confirm no keys
are in the diff before pushing** — the API keys used to be hardcoded in
`main.py`, and although they've been moved to environment variables, the old
values should be rotated in Google Cloud since they existed in plaintext.

### 2. Create the service

In your existing Railway project (the one with Postgres, `trading_agents` and
`all_stock_data_reports`) → **+ New** → **GitHub Repo** → pick the repo.

Railway sees `Dockerfile` and `railway.json` and builds automatically.

### 3. Attach a volume

**Right-click the service → Attach Volume**, mount path `/data`.

This is not optional. Without it every redeploy wipes the images and PDFs of
every scan ever run, and users lose reports they paid for.

### 4. Set the variables

Service → **Variables** → **Raw Editor**:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
MAPS_API_KEY=<your Google Maps key>
GEMINI_API_KEY=<your Gemini key>
CLERK_ISSUER=https://clerk.your-domain.com
SIGNING_SECRET=<openssl rand -hex 32>
STORAGE_DIR=/data
ALLOWED_ORIGINS=https://your-website.com,http://localhost:3000
SCAN_CREDIT_COST=1
MAX_CONCURRENT_SCANS=2
```

Notes:

- **`DATABASE_URL`** — type the `${{Postgres.DATABASE_URL}}` reference rather
  than pasting a URL, so it follows the plugin if credentials rotate. It must
  be the *same* Postgres the website uses, or the wallet won't match.
- **`CLERK_ISSUER`** — Clerk dashboard → **Configure → API keys → Frontend API
  URL**. It's the `iss` claim on the tokens the site sends, e.g.
  `https://clerk.yourdomain.com` in production or
  `https://xxx-yy-00.clerk.accounts.dev` on a dev instance. Get this wrong and
  every request 401s.
- **`SIGNING_SECRET`** — any long random string. Changing it later invalidates
  every outstanding image and PDF link (they regenerate on the next page load,
  so it's survivable, just don't rotate it casually).
- **`ALLOWED_ORIGINS`** — your website's origins, comma-separated, no trailing
  slashes. A missing origin here shows up in the browser as a CORS error, not
  as a server error.
- `PUBLIC_BASE_URL` is derived from `RAILWAY_PUBLIC_DOMAIN` automatically —
  only set it if you put the service behind a custom domain.

### 5. Generate a domain

Service → **Settings → Networking → Generate Domain**. Copy the URL.

### 6. Point the website at it

In the Next.js app's environment (Vercel or wherever it's hosted):

```
NEXT_PUBLIC_PROPERTY_API_URL=https://your-scanner.up.railway.app
```

No trailing slash — paths are appended directly.

### 7. Check it

```bash
curl https://your-scanner.up.railway.app/health
```

Expect `{"status":"ok","service":"property-scanner","credit_cost":1,"storage_writable":true}`.

If `storage_writable` is `false`, the volume isn't mounted at `/data`.

Then sign in to the dashboard, open **Property Scanner**, and run one address.
Watch the Railway deploy logs — the scan logs each stage as it goes.

---

## Google Cloud

The Maps key needs these three APIs enabled on its project, or the scan fails
at the first fetch:

- Geocoding API
- Maps Static API
- Street View Static API

Restrict the key to those three APIs. It's used server-side only, so an IP
restriction is possible too, though Railway egress IPs are not stable.

## Cost per scan

Roughly, at current pricing: ~5 static-map/Street-View fetches, up to 4 more
for the timeline, 3 Gemini text calls, and 3 image generations. The image
generations dominate. Set `SCANNER_ENABLE_RENOVATION=false` to cut a scan to
the assessment only, or `SCANNER_ENABLE_TEMPORAL=false` to drop the historical
diff.

## Local development

```bash
pip install -r requirements.txt && cp .env.example .env
```

Fill in `.env`, then either run one scan:

```bash
python main.py "Maze Tower, Dubai UAE"
```

or serve the API:

```bash
uvicorn api.main:app --reload --port 8002
```

`DATABASE_URL` has to point at a real Postgres for the API — the credits table
lives there. Pointing it at the Railway Postgres public URL works fine for
development.

## Scaling notes

The billing path is already replica-safe (it serialises on a row lock). The
**worker** is not — the service assumes one instance:

- In-flight scans live in the process, tracked by an `asyncio.Semaphore`.
- On boot it marks every `queued`/`running` row as failed and refunds them,
  on the assumption that no other worker owns them. A second replica booting
  would cancel the first one's live scans.

To run replicas you'd need a worker-lease column on `property_scans` and a
claim query (`UPDATE ... WHERE status='queued' ... RETURNING` with
`SKIP LOCKED`) instead of the in-process semaphore. Until scan volume demands
it, one instance with `MAX_CONCURRENT_SCANS` is simpler and cheaper.
