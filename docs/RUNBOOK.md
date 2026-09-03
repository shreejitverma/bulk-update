# Runbook: getting from "Seller Central only" to live listings

You have a Seller Central account but no SP-API developer application, and no UPCs. This is the
path from there to listings created by `anzorlist`. Steps 1–3 are approvals you request from
Amazon and wait on; step 4 onward is configuration.

**Everything up to `anzorlist build` works today, with none of this done.** Do that first — a
built payload in `data/build/` is the thing you review, and it costs nothing to produce.

---

## Step 0 — what works right now, before any of this

```bash
uv sync
cp .env.example .env          # edit ANZOR_BRAND_NAME and ANZOR_MANUFACTURER

anzorlist doctor              # shows exactly what is and isn't configured
anzorlist workbook init       # generates Product Listing.xlsx
# ... fill in the SKU column ...
anzorlist workbook validate
anzorlist build --no-media    # no image hosting configured yet
```

That produces complete Amazon payloads in `data/build/US/*.json`. Read one. Everything after
this runbook is about getting permission to send them.

---

## Step 1 — Professional selling plan

**Required. Individual plans cannot list in gated categories and have no SP-API access.**

Seller Central → **Settings → Account Info → Manage → Your Services**. Confirm you are on
**Professional** ($39.99/month). If you are on Individual, upgrade — nothing below works
otherwise.

While you are there, copy your **Merchant Token** (Settings → Account Info → Merchant Token,
format `A1XXXXXXXXXXXX`). That is `SPAPI_SELLER_ID_NA` in `.env`.

---

## Step 2 — Fine Jewelry category approval

**Required, and the longest pole. Start this first.**

Fine jewelry is gated. Without approval, every listing you submit is rejected — after the work
of building it.

Seller Central → **Catalog → Add a Product → search for a comparable jewelry ASIN**. If you see
"Apply to sell" rather than a listing form, you are not yet approved. Follow the application.

Amazon typically asks for:

| Requirement | What to have ready |
|---|---|
| Professional selling plan | Step 1 |
| Invoices | Purchase invoices from suppliers, dated within the last 90 days |
| Images | Professional photos on pure white, ≥1000px on the longest side, per SKU |
| Quality assurance | A written QA/refurbishment policy for jewelry you manufacture |
| Order defect rate | Below 1% if you have selling history |

As a manufacturer, the invoice requirement is the one that trips people up — Amazon wants proof
of supply chain. Raw materials invoices (gold, stones) generally satisfy it; ask the reviewer if
your first submission is rejected rather than resubmitting the same documents.

**Expect 3–10 business days.** Do steps 3 and 4 in parallel.

---

## Step 3 — GTIN exemption

**Required for you, because you manufacture your own pieces and have no UPCs.**

Do **not** buy barcodes from a reseller. Amazon validates GTINs against the GS1 registry, and a
resold or invented barcode is grounds for account suspension. The exemption is the correct and
supported route.

1. Go to **[Apply for a GTIN exemption](https://sellercentral.amazon.com/productutility/gtinexemption)**
   (Catalog → Add a Product → "I'm adding a product not sold on Amazon" → "Apply for a GTIN
   exemption").
2. Select the **product category** (Jewelry) and enter your **brand name**. It must match
   `ANZOR_BRAND_NAME` in `.env` **exactly** — the exemption is granted per brand string.
3. Amazon asks for either:
   - **Brand Registry enrolment** (needs a registered or pending trademark), or
   - **Proof of brand ownership**: 2–10 images of the product showing your brand permanently
     affixed — for jewelry, the maker's mark or hallmark stamped on the piece, photographed
     legibly — plus a letter from the manufacturer. Since you *are* the manufacturer, that
     letter is written on your own letterhead.
4. Submit and wait. Usually 1–5 business days.

**Strongly consider Brand Registry** even though it takes longer. It makes the GTIN exemption
automatic, unlocks A+ Content (which materially lifts jewelry conversion), and gives you control
over your own detail pages. It needs a registered or pending trademark; a USPTO application in
"pending" status qualifies.

Once granted, set in `.env`:

```dotenv
ANZOR_BRAND_NAME=Anzor Jewelry     # exactly as approved
ANZOR_BRAND_REGISTERED=true        # only after Brand Registry enrolment completes
```

The system already emits `supplier_declared_has_product_identifier_exemption` on every listing
without a UPC. That flag *asserts* the exemption — it does not create one. Submitting it before
approval produces error `5461` ("You do not have permission to list without a product ID").

---

## Step 4 — Register as a developer and create the SP-API app

This is what produces the credentials `anzorlist` needs.

### 4a. Developer profile

Seller Central → **Settings → User Permissions → Third-party developer and apps → Developer
Central** → **Register as a developer**.

The form asks about data use. Answer for what this tool actually does — it is a private,
self-use integration:

| Question | Answer |
|---|---|
| Are you a public or private developer? | **Private** (self-use, not distributed) |
| Data you will access | Listings, Catalog, Product Type Definitions, Feeds |
| Do you access PII? | **No** — this tool never touches orders or customer data |
| Security controls | Credentials in a `.env` file excluded from version control, on a single operator workstation |
| Data retention | Product data only, retained locally; no customer data collected |

Answering "No" to PII matters: PII access triggers a much heavier security review, and this tool
genuinely does not need Orders or Reports roles. **Request only the roles below.**

Approval is typically 1–3 business days for a private developer.

### 4b. Create the application

Developer Central → **Add new app client**.

| Field | Value |
|---|---|
| App name | `anzorlist` |
| API type | **SP-API** |
| Roles | **Product Listing** and **Inventory and Order Tracking** — nothing else |
| App type | **Private / self-authorization** |
| OAuth Login URI | `https://localhost/anzorlist/login` (unused for self-auth, but required) |
| OAuth Redirect URI | `https://localhost/anzorlist/redirect` |

On save you get an **LWA client identifier** and **client secret**. The secret is shown once.

```dotenv
SPAPI_LWA_CLIENT_ID=amzn1.application-oa2-client.xxxxxxxx
SPAPI_LWA_CLIENT_SECRET=amzn1.oa2-cs.v1.xxxxxxxx
```

### 4c. Self-authorize

Developer Central → your app → **Authorize** → **Generate refresh token**.

This is the step people miss. The client id and secret alone grant nothing; the refresh token is
what ties the app to your seller account.

```dotenv
SPAPI_REFRESH_TOKEN_NA=Atzr|IwEBIxxxxxxxx
SPAPI_SELLER_ID_NA=A1XXXXXXXXXXXX          # the Merchant Token from step 1
```

> **The refresh token is per region, not per marketplace.** One NA token covers US, CA, and MX.
> The EU marketplaces (UK, DE, FR, IT, ES) need a **separate authorization** producing
> `SPAPI_REFRESH_TOKEN_EU` and `SPAPI_SELLER_ID_EU`. Get US working first.

### 4d. Verify

```bash
anzorlist doctor                 # every row should read OK
anzorlist amazon preflight       # confirms marketplace registration
anzorlist amazon sync-schemas    # caches Amazon's real JSON schemas
```

`sync-schemas` is worth running the moment credentials work. From then on, every `anzorlist
build` validates payloads against Amazon's authoritative rules offline, for the whole catalog,
with no further API calls.

---

## Step 5 — Image hosting

Amazon does not accept uploaded image bytes for listings. It fetches **URLs**, so the images
need a public home. Cloudflare R2 is configured here because its egress is free, and Amazon's
crawler re-fetches images repeatedly across marketplaces and over a listing's life.

1. Cloudflare dashboard → **R2** → create a bucket (e.g. `anzor-listing-images`).
2. Bucket → **Settings → Public access** → enable a public r2.dev URL, or connect a custom
   domain (a custom domain is better: r2.dev has rate limits Amazon's crawler can hit).
3. **Manage R2 API Tokens** → create a token with **Object Read & Write** on that bucket.

```dotenv
R2_ACCOUNT_ID=xxxxxxxxxxxxxxxx
R2_ACCESS_KEY_ID=xxxxxxxxxxxxxxxx
R2_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxx
R2_BUCKET=anzor-listing-images
R2_PUBLIC_BASE_URL=https://images.anzorjewelrycorp.com
```

Then `anzorlist build` will download, validate, and host every product image, and report any
that fail Amazon's requirements (under 1000px, non-white background, wrong format).

---

## Step 6 — First live listing

Do this with **one** SKU, not the catalog.

```bash
# 1. Stage a single SKU: set Include = Y for one row, N for the rest
anzorlist workbook validate

# 2. Build it
anzorlist build R985

# 3. Read the payload. This is the artifact under review.
cat "data/build/US/R985-PARENT.json"

# 4. Amazon's own validator. Creates nothing.
anzorlist amazon validate R985

# 5. Only when step 4 is clean:
export ANZOR_ALLOW_LIVE=true
anzorlist amazon submit R985 --confirm
```

Then wait 15–30 minutes and check the detail page in Seller Central. Amazon processes new
fine-jewelry listings asynchronously, so a successful submission is not yet a live page.

Once one SKU is correct end to end, set `Include = Y` on the rest and run `anzorlist build`
followed by `anzorlist amazon validate`. Do not skip validate on the batch — a category-level
problem shows up identically on every SKU, and finding it in a dry run costs nothing.

---

## Common errors

| Error | Meaning | Fix |
|---|---|---|
| `5461` | No permission to list without a product ID | GTIN exemption not approved for this brand + category (step 3) |
| `8541` / "not approved to list" | Category gating | Fine Jewelry approval pending (step 2) |
| `90220` | Required attribute missing | Run `anzorlist amazon sync-schemas`, rebuild — the local schema check will now name the attribute |
| `invalid_grant` from LWA | Refresh token revoked or from another app | Re-run self-authorization (step 4c) |
| `invalid_client` from LWA | Client id/secret mismatch | Re-copy from Developer Central; the secret is shown once |
| `403` on every call | App lacks the role, or wrong region | Confirm **Product Listing** role, and that the token matches the marketplace's region |
| `429` | Rate limited | The client already backs off; reduce concurrency if it persists |
| Listing accepted then suppressed | Image or content policy | Check the main image is on pure white ≥1000px, and re-read the description for shipping/contact text |

---

## Rollback

The ledger records every submission with its exact payload.

```bash
anzorlist amazon status                     # what is believed live
anzorlist amazon status R985                # full history for one SKU
anzorlist amazon delete R985 --confirm      # remove an offer
```

Deletion removes **your offer**. If the ASIN accumulated reviews or sales rank, re-listing does
not restore them — prefer setting quantity to zero over deleting, unless the listing itself is
wrong.

---

## What is deliberately not automated

- **Nothing auto-publishes.** `submit` requires `--confirm` *and* `ANZOR_ALLOW_LIVE=true`, and
  prompts interactively on top of that. Three gates, because a bad catalog push is expensive to
  undo.
- **No pricing automation.** Prices come from the website plus your configured markup, or from
  an explicit override. There is no repricer.
- **No inventory sync.** Quantity comes from the spreadsheet. Wiring it to real stock is a
  separate integration with its own failure modes.
