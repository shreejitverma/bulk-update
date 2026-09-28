# anzorlist

Turn products on [anzorjewelrycorp.com](https://www.anzorjewelrycorp.com) into Amazon, eBay and
Etsy listings — without letting anything reach the marketplace unreviewed.

You fill in SKUs. The system extracts each product from the website, writes compliant copy from
the extracted facts, prices it so Amazon's fee doesn't eat your margin, hosts the images, builds
the listing payload, and validates it against Amazon's own schema. Nothing is created on Amazon
until you explicitly say so, twice.

```
Product Listing.xlsx          you fill in the SKU column
        │
        ▼
   extract                    prodview.asp → Product (every value carries provenance)
        │
        ├── media             download → validate against Amazon's rules → host on R2
        ├── copy              Claude writes it; a validator checks every claim against the specs
        └── price             web price ÷ (1 − fee), so you net the web price
        │
        ▼
    map                       → Amazon attributes; ring sizes become a real variation family
    schema check              against Amazon's own JSON Schema, offline, no credentials
        │
   ─────┼───────────────────────────────────────────────────  network boundary
        ▼
  amazon validate             Amazon's VALIDATION_PREVIEW — creates nothing
  amazon submit --confirm     the only command that writes
```

## Quick start

```bash
uv sync
cp .env.example .env

anzorlist doctor              # what's configured, what's missing, and how to fix it
anzorlist workbook init       # generates Product Listing.xlsx
anzorlist catalog scan        # discovers every SKU in the store, appends them as Include = N
# → set Include = Y on what you want listed. Hover any header for what it does.
anzorlist workbook validate   # every error at once, with cell references
anzorlist build               # payloads land in data/build/<marketplace>/<SKU>/
```

`build` needs no Amazon credentials. That's deliberate — the payload on disk is the thing you
review, and producing it costs nothing.

## Why the pieces are the way they are

**Pricing is a gross-up, not a markup.** Amazon's fine-jewelry referral fee comes off the top. A
20% markup on a $1,000 piece nets $960 — a 4% loss, invisible on any single line and compounding
across the catalog. `web ÷ (1 − 0.20)` nets exactly $1,000. `PRICE_MARKUP_AMAZON` is read as
"the fee fraction to absorb", so the intuitive setting produces the correct arithmetic.

**Generated copy is never trusted.** Jewelry copy is a legal document — under the FTC Jewelry
Guides an unqualified "diamond" means a *natural* diamond. So every generated claim is checked
mechanically against the verbatim spec rows from the source page. A carat weight that isn't in
the specs fails. "Lab-grown sapphire" shortened to "sapphire" fails. "Gold plated" shortened to
"gold" fails. On failure the model is retried with the specific errors, then escalated to a
stronger model, then falls back to a spec-sheet rendering that cannot hallucinate because it
only restates extracted rows.

**Ring sizes are a variation family, not 29 listings.** One parent, one detail page, one review
count, one Buy Box — with each size's price delta from the site applied to its own child.

**The schema check runs offline.** Amazon publishes a JSON Schema per product type per
marketplace. Once cached, every payload is validated against Amazon's real rules in
milliseconds, for the whole catalog, with no API calls and no rate limit. This is the highest-
leverage check in the system and it's the one that works before your SP-API registration
completes.

**Three gates before anything goes live.** `--confirm` on the command, `ANZOR_ALLOW_LIVE=true`
in the environment, and an interactive prompt. They're independent on purpose: neither a stray
flag nor a stale config value can cause a write alone.

## Commands

| Command | Network | What it does |
|---|---|---|
| `doctor` | none | Configuration and credential check, with the fix for each gap |
| `workbook init` | none | Generate the workbook; existing rows are preserved |
| `workbook validate` | none | Validate every row, report every error with cell references |
| `catalog scan` | Anzor site | Walk the category listings and append every new SKU as `Include = N` |
| `catalog audit-images` | Anzor site | How much of the catalog clears Amazon's 1000px main-image bar |
| `build [SKUS...]` | Anzor site, Anthropic, R2 | Extract → copy → price → map → schema-check |
| `amazon preflight` | SP-API (read) | Marketplace registration and category gating |
| `amazon product-types` | SP-API (read) | The product types Amazon actually accepts for this account |
| `amazon sync-schemas` | SP-API (read) | Cache Amazon's JSON schemas for offline validation |
| `amazon validate [SKUS...]` | SP-API (dry run) | Amazon validates the payload and creates nothing |
| `amazon submit --confirm` | SP-API (**write**) | Creates listings, one previewed call per listing |
| `amazon submit --confirm --feed` | SP-API (**write**) | Bulk: parents per item, the rest in `JSON_LISTINGS_FEED` documents |
| `amazon feed-status [FEED_ID]` | SP-API (read) | Reconcile a bulk feed's per-listing results into the ledger; no id lists unreconciled feeds |
| `amazon status [SKU]` | none | Ledger state and submission history |
| `amazon delete --confirm` | SP-API (**write**) | Remove an offer |
| `ebay setup` | eBay (read) | List business policy ids and inventory locations for `.env` |
| `ebay submit --confirm [SKUS...]` | eBay (**write**) | Bulk-stage items and offers, then publish; idempotent on rerun |
| `etsy submit --confirm [SKUS...]` | Etsy (**write**) | Create or update, upload photos, set sizes, activate; idempotent on rerun |

Exit codes: `0` done, `1` something failed or was blocked, `2` refused by a safety gate or bad setup,
`3` accepted by Amazon but the result is not known yet (reconcile with `amazon feed-status`).

## The workbook

Only **SKU** is required. Every other column is an override that beats the extracted site value;
a blank cell always defers to the website and can never blank out a value. Columns are grouped —
Control, Offer, Copy, Attributes — and every header carries a hover note explaining what it does
and when to leave it alone.

The generated workbook includes four worked example rows (the SKUs with committed test fixtures),
a "How to use" sheet, a Reference sheet with every marketplace code, and an Upload Results tab
that gets filled in after a run.

## Layout

```
anzorlist/
  config.py            settings; secrets have no defaults, safety flags fail closed
  marketplaces.py      marketplace registry (region → endpoint, currency, locale)
  pricing.py           the gross-up
  pipeline.py          orchestration; stages 1–6 need no Amazon credentials
  cli.py               Typer CLI, grouped by safety boundary
  ingest/              workbook schema, template writer, validated reader, results writer
  extract/             site client (throttle, cache, encoding), provenance-tracking parser,
                       category walker that discovers the catalog's SKUs
  generate/            sanitize → generate → validate; escalation on validator failure
  media/               download, Amazon-requirement checks, R2 hosting, catalog-wide image audit
  channels/amazon/     auth, rate-limited transport, definitions, preflight, listings, feeds, mapper,
                       build artifacts on disk, submission planning
  channels/ebay/       OAuth client, mapper (80-char titles, size groups), bulk stage and publish
  channels/etsy/       OAuth client (rotating refresh token), mapper, create/update, photo sync
  store/               SQLite submission ledger
scripts/smoke_build.py fixture → Amazon payload, offline; run in CI on every push
docs/RUNBOOK.md        SP-API registration, GTIN exemption, first live listing; eBay and Etsy setup
```

## Tests

```bash
uv run pytest          # no network, no credentials
```

The suite runs entirely against four committed HTML fixtures. The safety-critical tests are the
FTC claim checks in `test_copy_guards.py` and the live-write gates in
`test_workbook_and_safety.py` — those assert the system *refuses* to act, which is the kind of
regression that fails silently.

`test_amazon_upload_e2e.py` drives the real CLI from `build` through `validate`, `submit`, and
`feed-status` against a fake Amazon (`tests/fake_amazon.py`) that speaks LWA, Listings Items,
Feeds, and presigned S3 in Amazon's documented shapes, so the whole upload path is exercised
before a live account exists.

`test_ebay_e2e.py` and `test_etsy_e2e.py` do the same for `ebay submit` and `etsy submit` against
`tests/fake_ebay.py` and `tests/fake_etsy.py`.

## Status

Working end to end offline. Amazon calls are implemented and gated but unexercised against a
live account — SP-API developer registration is pending. See `docs/RUNBOOK.md` for that path.

eBay is implemented (`channels/ebay/`): `build` writes `data/ebay/build/<SKU>.json` when
`ANZOR_CHANNELS` includes `ebay`, and `ebay submit` stages and publishes through the Inventory
API. Etsy is implemented (`channels/etsy/`): `build` writes `data/etsy/build/<SKU>.json`, and
`etsy submit` creates or updates each listing, uploads its photos as files (no image hosting
needed), sets sizes as inventory, and activates it. Setup for both is in `docs/RUNBOOK.md`.
