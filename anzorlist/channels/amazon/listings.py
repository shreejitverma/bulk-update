"""Listings Items API (2021-08-01) — create, validate, inspect, and delete listings.

This is the modern per-SKU path and it is chosen over the Feeds API deliberately:

* **Synchronous validation.** A PUT returns ``ACCEPTED`` or ``INVALID`` with structured issues in
  the response. The Feeds API returns a feed ID you poll for minutes, then a gzipped report.
* **VALIDATION_PREVIEW.** ``mode=VALIDATION_PREVIEW`` runs Amazon's full validation and creates
  nothing. This is a real dry run against Amazon's own rules, not a local approximation.
* **Per-SKU idempotency.** PUT on a seller SKU is an upsert. Re-running a partially-failed batch
  is safe; the same is not true of a feed that half-applied.

The Feeds API remains available in :mod:`anzorlist.channels.amazon.feeds` for bulk backfills,
where its throughput beats 5 requests/second.

**Safety.** :meth:`ListingsClient.put` refuses to run in ``SUBMIT`` mode unless the caller passes
``confirm=True`` *and* settings enable live writes. The two gates are independent on purpose: a
config flag alone cannot cause a write, and neither can a stray CLI flag.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

import structlog

from anzorlist.channels.amazon.client import SpApiClient, SpApiError
from anzorlist.config import Settings
from anzorlist.marketplaces import Marketplace
from anzorlist.models.listing import (
    BuiltListing,
    IssueSeverity,
    ListingIssue,
    ListingStatus,
    SubmissionOutcome,
)

log = structlog.get_logger(__name__)

LISTINGS_BASE = "/listings/2021-08-01/items"
Mode = Literal["VALIDATION_PREVIEW", "SUBMIT"]


class LiveWriteBlocked(RuntimeError):
    """A live write was attempted without both gates open. Explains exactly what is missing."""

    def __init__(self, sku: str, *, confirm: bool, allow_live: bool) -> None:
        missing = []
        if not confirm:
            missing.append("the call did not pass confirm=True (CLI: --confirm)")
        if not allow_live:
            missing.append("ANZOR_ALLOW_LIVE is not set to true in .env")
        super().__init__(
            f"Refusing to create a live Amazon listing for {sku}: " + "; ".join(missing) + ".\n"
            "Both gates must be open. Run `anzorlist amazon validate` first — it exercises the "
            "identical payload through Amazon's validator and creates nothing."
        )


class ListingsClient:
    """Per-SKU listing operations against one marketplace's region."""

    def __init__(self, client: SpApiClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    # ------------------------------------------------------------------ write

    def put(
        self,
        listing: BuiltListing,
        marketplace: Marketplace,
        *,
        mode: Mode = "VALIDATION_PREVIEW",
        confirm: bool = False,
    ) -> SubmissionOutcome:
        """Create or replace a listing.

        ``mode="VALIDATION_PREVIEW"`` is the default and is always safe: Amazon validates and
        discards. ``mode="SUBMIT"`` writes, and is refused unless both safety gates are open.
        """
        if mode == "SUBMIT" and not (confirm and self._settings.allow_live):
            raise LiveWriteBlocked(listing.sku, confirm=confirm,
                                   allow_live=self._settings.allow_live)

        params: dict[str, Any] = {
            "marketplaceIds": marketplace.marketplace_id,
            "issueLocale": marketplace.locale,
        }
        if mode == "VALIDATION_PREVIEW":
            params["mode"] = "VALIDATION_PREVIEW"

        path = f"{LISTINGS_BASE}/{self._client.seller_id}/{_encode_sku(listing.sku)}"
        log.info("listings.put", sku=listing.sku, marketplace=marketplace.code, mode=mode,
                 product_type=listing.product_type, is_parent=listing.is_parent)

        try:
            resp = self._client.request(
                "PUT", path, operation="putListingsItem", params=params,
                json_body=listing.body(),
            )
        except SpApiError as exc:
            return SubmissionOutcome(
                sku=listing.sku,
                marketplace_id=marketplace.marketplace_id,
                marketplace_code=marketplace.code,
                mode=mode,
                status=ListingStatus.ERROR,
                request_id=exc.request_id,
                http_status=exc.status,
                issues=[
                    ListingIssue(code=str(e.get("code", "SpApiError")),
                                 message=str(e.get("message", "")),
                                 severity=IssueSeverity.ERROR, source="amazon")
                    for e in exc.errors
                ],
                submitted_at=datetime.now(timezone.utc),
                payload_hash=listing.payload_hash,
            )

        return _outcome_from_response(resp.json, listing, marketplace, mode, resp.request_id,
                                      resp.status)

    def patch(
        self,
        sku: str,
        marketplace: Marketplace,
        patches: list[dict[str, Any]],
        product_type: str,
        *,
        mode: Mode = "VALIDATION_PREVIEW",
        confirm: bool = False,
    ) -> SubmissionOutcome:
        """Partially update a listing (JSON-Patch style).

        Preferred over PUT for price and quantity changes: a PUT replaces the whole listing, so
        any attribute omitted from the payload is cleared. A PATCH that touches only
        ``purchasable_offer`` cannot accidentally wipe the product description.
        """
        if mode == "SUBMIT" and not (confirm and self._settings.allow_live):
            raise LiveWriteBlocked(sku, confirm=confirm, allow_live=self._settings.allow_live)

        params: dict[str, Any] = {
            "marketplaceIds": marketplace.marketplace_id,
            "issueLocale": marketplace.locale,
        }
        if mode == "VALIDATION_PREVIEW":
            params["mode"] = "VALIDATION_PREVIEW"

        path = f"{LISTINGS_BASE}/{self._client.seller_id}/{_encode_sku(sku)}"
        resp = self._client.request(
            "PATCH", path, operation="patchListingsItem", params=params,
            json_body={"productType": product_type, "patches": patches},
        )
        stub = BuiltListing(
            sku=sku, source_sku=sku, marketplace_id=marketplace.marketplace_id,
            marketplace_code=marketplace.code, product_type=product_type, attributes={},
        )
        return _outcome_from_response(resp.json, stub, marketplace, mode, resp.request_id,
                                      resp.status)

    def delete(
        self, sku: str, marketplace: Marketplace, *, confirm: bool = False
    ) -> SubmissionOutcome:
        """Delete a listing. Always a live write — there is no preview mode for deletion."""
        if not (confirm and self._settings.allow_live):
            raise LiveWriteBlocked(sku, confirm=confirm, allow_live=self._settings.allow_live)

        path = f"{LISTINGS_BASE}/{self._client.seller_id}/{_encode_sku(sku)}"
        resp = self._client.request(
            "DELETE", path, operation="deleteListingsItem",
            params={"marketplaceIds": marketplace.marketplace_id,
                    "issueLocale": marketplace.locale},
        )
        log.warning("listings.deleted", sku=sku, marketplace=marketplace.code)
        payload = resp.json if isinstance(resp.json, dict) else {}
        return SubmissionOutcome(
            sku=sku,
            marketplace_id=marketplace.marketplace_id,
            marketplace_code=marketplace.code,
            mode="SUBMIT",
            status=ListingStatus.PENDING,
            submission_id=payload.get("submissionId"),
            request_id=resp.request_id,
            http_status=resp.status,
            submitted_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------ read

    def get(
        self,
        sku: str,
        marketplace: Marketplace,
        *,
        included_data: tuple[str, ...] = ("summaries", "attributes", "issues", "offers",
                                          "fulfillmentAvailability"),
    ) -> dict[str, Any] | None:
        """Fetch a listing's current state. Returns ``None`` when the SKU does not exist,
        which is how the pipeline distinguishes "create" from "update"."""
        path = f"{LISTINGS_BASE}/{self._client.seller_id}/{_encode_sku(sku)}"
        try:
            return self._client.get(
                path, operation="getListingsItem",
                params={"marketplaceIds": marketplace.marketplace_id,
                        "issueLocale": marketplace.locale,
                        "includedData": ",".join(included_data)},
            )
        except SpApiError as exc:
            if exc.status == 404:
                return None
            raise

    def exists(self, sku: str, marketplace: Marketplace) -> bool:
        return self.get(sku, marketplace, included_data=("summaries",)) is not None


def _encode_sku(sku: str) -> str:
    """Percent-encode a seller SKU for use as a path segment.

    Seller SKUs may legitimately contain ``/``, ``#`` and spaces, all of which change the meaning
    of the URL if passed through raw.
    """
    from urllib.parse import quote

    return quote(sku, safe="")


def _outcome_from_response(
    payload: Any,
    listing: BuiltListing,
    marketplace: Marketplace,
    mode: Mode,
    request_id: str | None,
    http_status: int,
) -> SubmissionOutcome:
    """Translate a Listings Items response into a :class:`SubmissionOutcome`.

    Amazon's ``status`` is ``ACCEPTED`` or ``INVALID``. ``ACCEPTED`` in VALIDATION_PREVIEW mode
    means "this would work" — nothing was created. Warnings can accompany an acceptance and are
    kept, because a warning today is frequently a suppression next quarter.
    """
    data = payload if isinstance(payload, dict) else {}
    issues = [
        ListingIssue(
            code=str(i.get("code", "Unknown")),
            message=str(i.get("message", "")),
            severity=_severity(i.get("severity")),
            attribute_names=[str(a) for a in i.get("attributeNames", []) or []],
            source="amazon",
        )
        for i in data.get("issues", []) or []
        if isinstance(i, dict)
    ]
    amazon_status = str(data.get("status", "")).upper()
    has_error = any(i.blocking for i in issues)

    if amazon_status == "ACCEPTED" and not has_error:
        status = (ListingStatus.VALIDATED if mode == "VALIDATION_PREVIEW"
                  else ListingStatus.SUBMITTED)
    elif amazon_status == "INVALID" or has_error:
        status = (ListingStatus.VALIDATION_FAILED if mode == "VALIDATION_PREVIEW"
                  else ListingStatus.REJECTED)
    else:
        status = ListingStatus.PENDING

    outcome = SubmissionOutcome(
        sku=listing.sku,
        marketplace_id=marketplace.marketplace_id,
        marketplace_code=marketplace.code,
        mode=mode,
        status=status,
        submission_id=data.get("submissionId"),
        request_id=request_id,
        issues=issues,
        http_status=http_status,
        submitted_at=datetime.now(timezone.utc),
        payload_hash=listing.payload_hash,
    )
    log.info("listings.outcome", **{"sku": listing.sku, "marketplace": marketplace.code,
                                    "mode": mode, "status": status.value,
                                    "issues": len(issues)})
    return outcome


def _severity(raw: object) -> IssueSeverity:
    try:
        return IssueSeverity(str(raw).upper())
    except ValueError:
        return IssueSeverity.ERROR  # unknown severity is treated as blocking, never ignored
