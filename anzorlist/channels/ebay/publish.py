"""Create eBay listings through the Inventory API, in bulk, idempotently.

A listing goes live in two phases, and only the second is visible to buyers:

1. **Stage** - write the inventory items (``bulkCreateOrReplaceInventoryItem``, 25 per call), the
   group for a size family (``createOrReplaceInventoryItemGroup``), and one offer per item
   (``bulkCreateOffer`` for new ones, ``updateOffer`` for existing ones). Staged offers are
   unpublished: nothing appears on eBay.
2. **Publish** - ``publishOffer`` for a single item, ``publishOfferByInventoryItemGroup`` for a
   family. eBay runs its full listing validation here, and returns the listing ID.

Every step is an upsert keyed by seller SKU, so re-running a partially failed batch converges
instead of duplicating: an offer that already exists for a SKU is found and updated, never
created twice (eBay rejects the duplicate with error 25002 anyway).

Publishing requires the same two independent gates as Amazon: ``confirm=True`` from the caller and
``ANZOR_ALLOW_LIVE`` in settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import structlog

from anzorlist.channels.ebay.client import EbayClient, EbayError
from anzorlist.channels.ebay.models import EbayListing
from anzorlist.config import Settings
from anzorlist.models.listing import IssueSeverity, ListingIssue, ListingStatus, SubmissionOutcome

log = structlog.get_logger(__name__)

INVENTORY = "/sell/inventory/v1"
BULK_LIMIT = 25  # eBay's cap per bulk request


class EbayLiveWriteBlocked(RuntimeError):
    def __init__(self, what: str, *, confirm: bool, allow_live: bool) -> None:
        missing = []
        if not confirm:
            missing.append("the call did not pass confirm=True (CLI: --confirm)")
        if not allow_live:
            missing.append("ANZOR_ALLOW_LIVE is not set to true in .env")
        super().__init__(f"Refusing to publish {what} on eBay: " + "; ".join(missing) + ".")


@dataclass
class StageResult:
    offer_ids: dict[str, str] = field(default_factory=dict)  # sku -> offerId
    issues: list[ListingIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.blocking for i in self.issues)


class EbayPublisher:
    def __init__(self, client: EbayClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    # ------------------------------------------------------------------ stage

    def stage(self, listing: EbayListing) -> StageResult:
        """Write items, group, and offers. Creates nothing a buyer can see."""
        result = StageResult()
        policies = self._settings.ebay_listing_policies()

        # Items, 25 per call. The bulk call answers 207 with a status per SKU.
        for start in range(0, len(listing.items), BULK_LIMIT):
            chunk = listing.items[start : start + BULK_LIMIT]
            requests = [
                {
                    **item.inventory_item_body(listing.description),
                    "sku": item.sku,
                    "locale": "en_US",
                }
                for item in chunk
            ]
            _, body = self._client.request(
                "POST",
                f"{INVENTORY}/bulk_create_or_replace_inventory_item",
                json_body={"requests": requests},
            )
            result.issues.extend(_bulk_issues(body, "inventory item"))
        if not result.ok:
            return result

        if listing.is_group:
            assert listing.group_key is not None
            self._client.request(
                "PUT",
                f"{INVENTORY}/inventory_item_group/{quote(listing.group_key, safe='')}",
                json_body=listing.group_body(),
            )

        # Offers: update the ones that exist, bulk-create the rest.
        new_items = []
        for item in listing.items:
            existing = self._find_offer(item.sku, listing.marketplace_id)
            body = listing.offer_body(item, policies)
            if existing is None:
                new_items.append((item, body))
                continue
            self._client.request("PUT", f"{INVENTORY}/offer/{existing}", json_body=body)
            result.offer_ids[item.sku] = existing
        for start in range(0, len(new_items), BULK_LIMIT):
            chunk_bodies = [b for _, b in new_items[start : start + BULK_LIMIT]]
            _, body = self._client.request(
                "POST", f"{INVENTORY}/bulk_create_offer", json_body={"requests": chunk_bodies}
            )
            result.issues.extend(_bulk_issues(body, "offer"))
            for response in (body or {}).get("responses", []) or []:
                if isinstance(response, dict) and response.get("offerId"):
                    result.offer_ids[str(response["sku"])] = str(response["offerId"])
        missing = [i.sku for i in listing.items if i.sku not in result.offer_ids]
        if missing and result.ok:
            result.issues.append(
                ListingIssue(
                    code="OfferMissing",
                    message=f"eBay returned no offer id for {', '.join(missing[:5])}",
                    source="ebay",
                )
            )
        return result

    def _find_offer(self, sku: str, marketplace_id: str) -> str | None:
        status, body = self._client.request(
            "GET",
            f"{INVENTORY}/offer",
            params={"sku": sku, "marketplace_id": marketplace_id},
            ok_statuses=(404,),
        )
        if status == 404 or not isinstance(body, dict):
            return None
        offers = [o for o in body.get("offers", []) or [] if isinstance(o, dict)]
        matching = [o for o in offers if o.get("marketplaceId") in (None, marketplace_id)]
        return str(matching[0]["offerId"]) if matching else None

    # ------------------------------------------------------------------ publish

    def submit(self, listing: EbayListing, *, confirm: bool = False) -> SubmissionOutcome:
        """Stage, then publish. Always returns an outcome; never raises on an eBay rejection."""
        if not (confirm and self._settings.allow_live):
            raise EbayLiveWriteBlocked(
                listing.source_sku, confirm=confirm, allow_live=self._settings.allow_live
            )
        try:
            staged = self.stage(listing)
            if not staged.ok:
                return self._outcome(listing, ListingStatus.REJECTED, staged.issues)
            if listing.is_group:
                _, body = self._client.request(
                    "POST",
                    f"{INVENTORY}/offer/publish_by_inventory_item_group",
                    json_body={
                        "inventoryItemGroupKey": listing.group_key,
                        "marketplaceId": listing.marketplace_id,
                    },
                )
            else:
                offer_id = staged.offer_ids[listing.items[0].sku]
                _, body = self._client.request("POST", f"{INVENTORY}/offer/{offer_id}/publish")
        except EbayError as exc:
            issues = [
                ListingIssue(
                    code=str(e.get("errorId", "EbayError")),
                    message=str(e.get("longMessage") or e.get("message", "")),
                    source="ebay",
                )
                for e in exc.errors
            ] or [ListingIssue(code=f"HTTP{exc.status}", message=str(exc), source="ebay")]
            return self._outcome(listing, ListingStatus.REJECTED, issues)

        listing_id = str((body or {}).get("listingId", ""))
        warnings = [
            ListingIssue(
                code=str(w.get("errorId", "EbayWarning")),
                message=str(w.get("message", "")),
                severity=IssueSeverity.WARNING,
                source="ebay",
            )
            for w in (body or {}).get("warnings", []) or []
            if isinstance(w, dict)
        ]
        if not listing_id:
            return self._outcome(
                listing,
                ListingStatus.ERROR,
                [ListingIssue(code="NoListingId", message="publish returned no listing id")],
            )
        log.warning("ebay.published", sku=listing.source_sku, listing_id=listing_id)
        return self._outcome(listing, ListingStatus.SUBMITTED, warnings, listing_id)

    @staticmethod
    def _outcome(
        listing: EbayListing,
        status: ListingStatus,
        issues: list[ListingIssue],
        listing_id: str | None = None,
    ) -> SubmissionOutcome:
        return SubmissionOutcome(
            sku=listing.source_sku,
            marketplace_id=listing.marketplace_id,
            marketplace_code=listing.marketplace_id,
            mode="SUBMIT",
            status=status,
            submission_id=listing_id,
            issues=issues,
            submitted_at=datetime.now(timezone.utc),
            payload_hash=listing.payload_hash,
        )


def _bulk_issues(body: Any, what: str) -> list[ListingIssue]:
    """Per-SKU failures from a bulk response. A 207 can hide failures inside a success status."""
    issues: list[ListingIssue] = []
    for response in (body or {}).get("responses", []) or []:
        if not isinstance(response, dict):
            continue
        status = int(response.get("statusCode", 0) or 0)
        for err in response.get("errors", []) or []:
            issues.append(
                ListingIssue(
                    code=str(err.get("errorId", "EbayError")),
                    message=f"{what} {response.get('sku')}: "
                    f"{err.get('longMessage') or err.get('message', '')}",
                    source="ebay",
                )
            )
        if status >= 300 and not response.get("errors"):
            issues.append(
                ListingIssue(
                    code=f"HTTP{status}",
                    message=f"{what} {response.get('sku')} failed with {status}",
                    source="ebay",
                )
            )
    return issues
