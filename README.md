# PrestaShop Supplier Product Importer

A **local** web app (localhost only) that imports supplier product data from
Excel/CSV files into PrestaShop through the legacy **Webservice API**.

- **Backend:** Python + FastAPI, `openpyxl`/`pandas` for spreadsheets, `httpx` for API calls
- **Frontend:** single-page vanilla JS + Tailwind (CDN, no build step), served by FastAPI
- **Storage:** SQLite for saved mapping profiles and import history
- **Config:** `.env` for shop URL + API key (never committed)

> Status: scaffold + working end-to-end connection test, live schema fetching,
> the header-matching engine, XML payload builder, validator and a dry-run
> importer. The mapper's synonym dictionary is tuned further once a real
> supplier sheet is provided.

## Quick start

```bash
# 1. Create a virtualenv and install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure the shop connection
cp .env.example .env
# edit .env: set PRESTASHOP_URL and PRESTASHOP_API_KEY

# 3. Run (localhost only)
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 4. Open http://127.0.0.1:8000
```

You can also enter the URL + key directly in the **Settings** tab and click
**Test connection** without editing `.env`.

## Run with Docker (recommended for a clean, isolated setup)

No local Python needed — just Docker.

### One-line download + install

```bash
git clone https://github.com/sh1njure/10yearsexperience.git && cd 10yearsexperience && docker compose up --build -d
```

Then open **http://127.0.0.1:8000**. Stop it with `docker compose down` from the
repo folder.

### Or step by step

```bash
# from the repo root
docker compose up --build

# then open
http://127.0.0.1:8000
```

### Connecting to a PrestaShop on your own machine

If your shop runs on the **host** (e.g. `http://localhost:8080`), do **not**
use `localhost` in the Shop base URL — inside the container `localhost` is the
container itself. Use:

```
http://host.docker.internal:8080
```

The compose file already maps `host.docker.internal` to the host gateway so
this works on Linux as well as Docker Desktop.

Notes:

- The port is published to **`127.0.0.1` only**, so the tool stays reachable
  from your machine and is not exposed to your network — matching the
  localhost-only security model.
- Import **history and saved mapping profiles** persist in a named Docker
  volume (`importer-data`), so they survive `docker compose down`/`up`.
- Credentials: either create a `.env` (see `.env.example`) — it is picked up
  automatically and is optional — or just type the shop URL + key into the
  **Settings** tab at runtime.
- Stop with `docker compose down`. To wipe the stored history/profiles too,
  add `-v`.

Without compose, the equivalent plain Docker commands are:

```bash
docker build -t prestashop-supplier-importer .
docker run --rm -p 127.0.0.1:8000:8000 \
  -v importer-data:/app/appdata \
  --env-file .env \
  prestashop-supplier-importer
```

## Generating a Webservice key in PrestaShop

1. Back office → **Advanced Parameters → Webservice**.
2. Set **Enable PrestaShop's webservice** = *Yes*, save.
3. Click **Add new webservice key** → **Generate** a key.
4. Under **Permissions**, grant at least `GET` on everything you want to read
   (products, categories, stock_availables, combinations, manufacturers) and
   `GET/POST/PUT` on `products`, `stock_availables`, `combinations`, `images`
   for writing.
5. Save. Paste the generated key into the app's Settings tab (or `.env`).

Auth is HTTP Basic: **the API key is the username, the password is empty.**

## How it works (the flow)

1. **Settings** — shop URL + key. *Test connection* hits `GET /api/` and lists
   the resources your key can access.
2. **Upload** — drop an `.xlsx`/`.csv`, preview the first 10 rows, pick the
   sheet and confirm which row is the header.
3. **Mapping** — fetches the **live** field list via
   `GET {shop}/api/products?schema=blank` (and categories, stock_availables,
   combinations, manufacturers) and auto-matches your headers using
   exact → normalized → synonym → fuzzy (rapidfuzz) matching, each shown with a
   green/amber/red confidence badge and an override dropdown. Save/load mapping
   profiles per supplier.
4. **Import scope + mode** — choose what to push (products, prices, stock,
   categories, images, combinations) and a mode: create-only / update-only /
   upsert (match by `reference`).
5. **Validation** — required fields mapped, prices numeric, references unique,
   categories exist. Hard errors block the import.
6. **Dry run (default)** — builds every XML payload and shows exactly what would
   be sent, without sending it.
7. **Import** — row by row with a live progress bar (SSE), bounded concurrency
   (default 2), retry-with-backoff on 5xx, continue-on-error.
8. **Results + history** — per-row success/failure with the PrestaShop error
   message, downloadable as CSV. Every run is logged to SQLite.

## PrestaShop API rules respected

- Always fetch `?schema=blank` and fill the skeleton — XML is **never**
  hand-built (see `app/xml_builder.py`).
- Read-only fields (`id`, `manufacturer_name`, product `quantity`,
  `position_in_category`, …) are stripped before POST/PUT.
- Multilingual fields (`name`, `description`, `link_rewrite`) are wrapped as
  `<language id="1">value</language>`; the language id is configurable.
- `link_rewrite` is slugified so the product does not silently fail validation.
- Updates GET the full existing resource, change only mapped fields, then PUT
  the whole thing back (partial PUTs wipe fields).
- Quantity is set via `stock_availables`, never on the product.

## Project layout

```
app/
  api_client.py    # PrestaShop Webservice client (auth, connection test, schema)
  excel_parser.py  # .xlsx/.csv reading, preview, header-row selection
  mapper.py        # header → field matching cascade
  validator.py     # pre-import checks
  importer.py      # row-by-row import orchestration (dry-run, retry, concurrency)
  xml_builder.py   # fills the ?schema=blank skeleton, strips read-only, i18n
  db.py            # SQLite: mapping profiles + import history
  config.py        # .env-backed settings
  routers/         # FastAPI endpoints (settings, upload, mapping, import)
  main.py          # app entrypoint, serves the SPA
data/
  synonyms.json    # editable synonym dictionary (no code change to extend)
static/            # index.html + app.js (Tailwind CDN)
tests/             # pytest: header matching + XML builder (fixtures, no live API)
```

## Tests

```bash
pip install -r requirements.txt
pytest -q
```

Tests cover the header-matching logic and the XML builder using fixture files —
no live API calls.

## Security notes

- Runs on `127.0.0.1` by design. Do not expose it to a network.
- `.env` holds the API key and is git-ignored — never commit real credentials.
