# Property Scanner API

Turns an address and a handful of photographs into a property inspection: every
defect located on the photo it was seen in, merged into one register, scored
by building system, read out in a narrated walkthrough, and answerable in
plain English afterwards. Alongside it, the original survey: satellite and
Street View imagery, a historical change timeline, and renovation concepts
rendered image-to-image.

Everything the report says is read from the pictures, except the roof
measurements, which come from Google's Solar API. The scan sees photographs
and nothing else — no comparable sales, no local labour rates — so it reports
condition, scope, urgency and who to call, and quotes no prices, valuations or
returns.

Runs on Railway as a service the website calls. One scan costs **1 credit**
from the shared `users` wallet; questions and studio renders on a finished
scan are included up to a per-scan allowance.

```
scanner/    the pipeline — Google Maps + Gemini + the PDF. No web, no database.
api/        FastAPI: Clerk auth, credits, job rows, signed asset URLs.
tests/      pytest: fake Gemini, fake Maps, optional real Postgres.
main.py     local CLI for a one-off scan.
```

## What a scan does

Most scans carry five or six photos; the ceiling is 50. Everything below works
with none at all — the satellite plate and the front Street View plate are
inspected exactly like photographs.

| Stage | What happens | Model calls |
|---|---|---|
| Locate | Geocode, Street View metadata. An address the map cannot find still scans if photos came with it. | — |
| Imagery | Satellite + 4 Street View angles, **in parallel with** the Solar API: roof planes, pitch, orientation, area, a ranked panel layout. | — |
| Pinpoint inspection | Every image inspected **on its own call**, in parallel (`SCANNER_VISION_CONCURRENCY`). Each returns located findings (box, system, severity, urgency, trade, confidence), equipment with data-plate text, the space it shows, photo quality. Feeds the live view. Historical Street View diffing runs in the background meanwhile. | 1 per image |
| Synthesis | Thumbnails of every image + every finding → duplicates merged into issues, photos grouped into rooms, coverage gaps named. **In parallel with** the exterior survey. | 2 |
| Concepts | Exterior renovation concepts + renders ‖ interior makeovers in the chosen style, with visible defects shown fixed ‖ the listing kit. | ~1 + 3 + 3 images |
| Report | Health score, equipment ages, annotated evidence, PDF. | — |

Two things fail a scan, and both refund: nothing to look at, and nothing
assessable at the end. Every other stage is an enrichment that costs the report
a section when it fails, never the credit.

### Nothing the model says is the score

`scanner/scoring.py` computes every number. Each merged issue removes a fixed
share of its system's 100 points by severity (critical 40%, major 22%, minor
8%, cosmetic 3%, compounding, discounted for low confidence); systems combine
by fixed weights; a system no photo showed is **not assessed**, never assumed
fine; any open critical issue caps the overall at 69. Below half coverage the
score is flagged provisional — normal for a five-photo scan — and the report's
coverage section lists the exact shots that would complete it.

Equipment ages come from data-plate years (or a decoded serial, flagged as
such) against published service-life ranges.

### The model proposes, the code disposes

`synthesis.merge()` validates everything the synthesis call returns: unknown
evidence IDs are dropped, every finding lands in exactly one issue (or the
explicit `dismissed` list), every photo in exactly one room. If the call fails
outright, the report degrades to one issue per finding and rooms by label.

## What a scan produces

Written to `$STORAGE_DIR/scans/<scan_id>/`:

| File | What it is |
|---|---|
| `owner_photo_N.jpg` + `thumb_…` | Photos the user attached, re-encoded on upload |
| `satellite_topdown.jpg`, `street_*.jpg` + `thumb_…` | Google plates |
| `timeline_YYYY_MM.jpg` | One frame per historical Street View capture |
| `reno_before.jpg` / `reno_after_N.jpg` | Exterior concept pairs |
| `makeover_N.jpg` | Interior makeovers made during the scan |
| `studio_<id>.jpg` | On-demand studio renders |
| `annot_*.jpg`, `crop_I01.jpg`, `satellite_roof.jpg` | Pre-drawn evidence for the PDF |
| `report.json` / `report.pdf` | The payload and the downloadable report |

The API never serves these directly. It hands the browser HMAC-signed,
time-limited URLs, because `<img src>` and download links cannot send an
`Authorization` header.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Liveness + config self-check. No auth. |
| `GET` | `/api/limits` | Photo limit, goals, styles, allowances. No auth. |
| `GET` | `/api/credits` | Wallet balance |
| `GET` | `/api/scans` | The caller's scans, newest first |
| `POST` | `/api/scans` | multipart `address` + optional `images[]`, `goal`, `style` → charges 1 credit, queues the scan |
| `GET` | `/api/scans/{id}` | One scan; `live` while running (per-photo feed), `result` + `allowances` once complete |
| `DELETE` | `/api/scans/{id}` | Removes the row, its questions and renders, and its files |
| `POST` | `/api/scans/{id}/ask` | `{question}` → answer with cited photos and boxes |
| `GET` | `/api/scans/{id}/questions` | The scan's Q&A history |
| `POST` | `/api/scans/{id}/renders` | `{source, mode, style?, instruction?}` → a studio render |
| `GET` | `/api/scans/{id}/renders` | The scan's studio renders |
| `GET` | `/api/scans/{id}/files/{name}` | Signed artifact. No bearer token. |

Studio modes: `restyle`, `stage` (furnish an empty room), `declutter`,
`repair` (the inspection's findings shown fixed), `twilight`, `custom`.

`POST /api/scans` returns **402** when the wallet is short, **409** when the
user already has a scan running, and **422** when an attachment is rejected.
Ask and renders return **429** once the scan's allowance is used, and a failed
call does not count against it.

## Owner-supplied photos

Up to `MAX_PHOTOS` (default 50) images can ride along with the address — JPEG,
PNG, WebP or HEIC. The website downscales them in the browser before upload,
so fifty photos is a few tens of megabytes, not hundreds; HEIC, which most
browsers cannot decode, is sent as-is and decoded here by `pillow-heif`.

Nothing is stored as uploaded. Every attachment is decoded, bounded, and
re-encoded to a JPEG under a name we chose (`owner_photo_N.jpg`), which handles
attacker-controlled filenames, polyglot files, decompression bombs and EXIF —
including the GPS coordinates phones habitually embed — in one step. EXIF
*orientation* is applied before the metadata is dropped, so portrait photos are
not analysed sideways. Decoding runs off the event loop.

Validation happens **before** the credit is charged, so a rejected photo costs
nothing.

Owner photos are treated as the most current view of the building: they lead
wherever they disagree with a possibly-stale Street View capture, and the
exterior concepts are rendered from an owner photograph of the exterior when
there is one. Interior photos never reach the exterior stages.

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
CLERK_ISSUER=https://clerk.your-domain.com,https://your-instance-00.clerk.accounts.dev
SIGNING_SECRET=<openssl rand -hex 32>
STORAGE_DIR=/data
ALLOWED_ORIGINS=https://your-website.com,http://localhost:3000
SCAN_CREDIT_COST=1
MAX_CONCURRENT_SCANS=2
MAX_PHOTOS=50
MAX_UPLOAD_MB=12
```

`MAX_UPLOAD_IMAGES` is retired — it is ignored, so a deployment that set it to
6 when six was the product gets the new limit without a variable change. Use
`MAX_PHOTOS` to set a different one.

Notes:

- **`DATABASE_URL`** — type the `${{Postgres.DATABASE_URL}}` reference rather
  than pasting a URL, so it follows the plugin if credentials rotate. It must
  be the *same* Postgres the website uses, or the wallet won't match.
- **`CLERK_ISSUER`** — Clerk dashboard → **Configure → API keys → Frontend API
  URL**. It's the `iss` claim on the tokens the site sends. **Comma-separated,
  and you almost certainly need two of them:** the deployed site authenticates
  against your production Clerk instance (`https://clerk.yourdomain.com`, from
  a `pk_live_` key) while `npm run dev` uses a development instance
  (`https://xxx-yy-00.clerk.accounts.dev`, from `pk_test_`). They sign with
  different keys, so listing only one makes the other 401 on every request with
  "Invalid session token". `/health` lists every configured issuer and whether
  its key set actually fetches.
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

And one more for the roof measurements and solar layout:

- **Solar API**

Without it the scan still completes — the stage logs a 403 once and the report
simply has no roof-measurement section.

Restrict the key to those four APIs. It's used server-side only, so an IP
restriction is possible too, though Railway egress IPs are not stable.

## Cost per scan

Roughly: ~5 static-map/Street-View fetches, up to 4 more for the timeline, one
Solar API call, one vision call per image (a typical six-photo scan makes
eight), ~5 text calls, and up to 6 image generations (3 exterior concepts, 3
interior makeovers). The image generations dominate. Questions and studio
renders afterwards are capped by `ASK_LIMIT_PER_SCAN` (40) and
`RENDER_LIMIT_PER_SCAN` (12).

Switches: `SCANNER_ENABLE_RENOVATION`, `SCANNER_ENABLE_MAKEOVERS`
(`SCANNER_MAKEOVER_COUNT`), `SCANNER_ENABLE_LISTING`, `SCANNER_ENABLE_SOLAR`,
`SCANNER_ENABLE_TEMPORAL`.

## Local development

```bash
pip install -r requirements.txt && cp .env.example .env
```

Fill in `.env`, then either run one scan:

```bash
python main.py "Maze Tower, Dubai UAE" --photo kitchen.jpg --photo boiler.jpg --goal buying
```

or serve the API:

```bash
uvicorn api.main:app --reload --port 8002
```

`DATABASE_URL` has to point at a real Postgres for the API — the credits table
lives there. Pointing it at the Railway Postgres public URL works fine for
development.

### Tests

```bash
pip install -r requirements-dev.txt
pytest                                                     # pipeline, scoring, PDF
TEST_DATABASE_URL=postgresql://postgres@localhost/scanner_test pytest   # + the API
```

The suite runs the whole pipeline against a fake Gemini client and fake Google
fetches (`tests/fakes.py`), so it needs no keys and no network. The API tests
need a disposable Postgres database; they create their own tables.

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
