"""Create and update Etsy listings.

Etsy has no SKU-keyed upsert, so the numeric ``listing_id`` for each website SKU is recorded in
``data/etsy/listings.json`` the moment Etsy returns it, together with the digests of the images
already uploaded. A rerun after any failure therefore updates the same listing instead of
creating a second one, and never uploads the same photo twice.

Order of operations: create (as a draft) or update the listing, upload new images, set the
inventory (sizes with their own prices), then activate. Activation is the step that makes it
visible and incurs Etsy's listing fee, and it needs both safety gates.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from anzorlist.channels.etsy.client import EtsyClient, EtsyError
from anzorlist.channels.etsy.models import EtsyListing
from anzorlist.config import Settings
from anzorlist.models.listing import ListingIssue, ListingStatus, SubmissionOutcome

log = structlog.get_logger(__name__)

# Family -> Etsy seller-taxonomy node name under "Jewelry". The numeric ids are looked up in
# Etsy's own taxonomy at run time rather than hard-coded, because Etsy renumbers them.
FAMILY_TAXONOMY = {
    "Ring": "Rings",
    "Earrings": "Earrings",
    "Pendant": "Necklaces",
    "Necklace": "Necklaces",
    "Bracelet": "Bracelets",
    "Jewelry Set": "Jewelry Sets",
}


class EtsyLiveWriteBlocked(RuntimeError):
    pass


class EtsyState:
    """listing_id and uploaded image digests per website SKU, persisted after every change."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "etsy" / "listings.json"
        self._data: dict[str, dict[str, Any]] = (
            json.loads(self.path.read_text()) if self.path.exists() else {}
        )

    def get(self, sku: str) -> dict[str, Any]:
        return self._data.setdefault(sku, {"listing_id": None, "images": []})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)


class EtsyPublisher:
    def __init__(self, client: EtsyClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._state = EtsyState(settings.data_dir)
        self._taxonomy: dict[str, int] | None = None

    def taxonomy_id(self, family: str) -> int:
        if self._taxonomy is None:
            body = self._client.request("GET", "/v3/application/seller-taxonomy/nodes", auth=False)
            jewelry = next(
                (n for n in (body or {}).get("results", []) if n.get("name") == "Jewelry"), None
            )
            if jewelry is None:
                raise EtsyError(0, "GET", "seller-taxonomy", "no top-level 'Jewelry' node")
            self._taxonomy = {c["name"]: int(c["id"]) for c in jewelry.get("children", [])}
        name = FAMILY_TAXONOMY[family]
        if name not in self._taxonomy:
            raise EtsyError(0, "GET", "seller-taxonomy", f"no 'Jewelry > {name}' node")
        return self._taxonomy[name]

    def submit(self, listing: EtsyListing, *, confirm: bool = False) -> SubmissionOutcome:
        if not (confirm and self._settings.allow_live):
            raise EtsyLiveWriteBlocked(
                f"Refusing to publish {listing.source_sku} on Etsy: needs --confirm and "
                f"ANZOR_ALLOW_LIVE=true"
            )
        cfg = self._settings.etsy_settings()
        shop = f"/v3/application/shops/{self._client.shop_id}"
        state = self._state.get(listing.source_sku)
        try:
            fields = {
                **listing.listing_fields(),
                "taxonomy_id": self.taxonomy_id(listing.family),
                "shipping_profile_id": int(cfg["ETSY_SHIPPING_PROFILE_ID"]),
                "return_policy_id": int(cfg["ETSY_RETURN_POLICY_ID"]),
                "who_made": "i_did",
                "when_made": self._settings.etsy_when_made,
                "is_supply": False,
                "type": "physical",
            }
            if self._settings.etsy_readiness_state_id:
                fields["readiness_state_id"] = int(self._settings.etsy_readiness_state_id)
            if state["listing_id"] is None:
                created = self._client.request(
                    "POST",
                    f"{shop}/listings",
                    json_body={
                        **fields,
                        "quantity": listing.quantity,
                        "price": float(listing.price),
                    },
                )
                state["listing_id"] = int(created["listing_id"])
                self._state.save()  # before anything else can fail
            else:
                self._client.request(
                    "PATCH", f"{shop}/listings/{state['listing_id']}", json_body=fields
                )
            listing_id = state["listing_id"]

            for rank, path in enumerate(listing.image_files, start=1):
                data = Path(path).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if digest in state["images"]:
                    continue
                self._client.request(
                    "POST",
                    f"{shop}/listings/{listing_id}/images",
                    files={"image": (Path(path).name, data, "image/jpeg")},
                    data={"rank": str(rank)},
                )
                state["images"].append(digest)
                self._state.save()

            self._client.request(
                "PUT",
                f"/v3/application/listings/{listing_id}/inventory",
                json_body=listing.inventory_body(),
            )
            self._client.request(
                "PATCH", f"{shop}/listings/{listing_id}", json_body={"state": "active"}
            )
        except (EtsyError, OSError) as exc:
            return _outcome(
                listing,
                ListingStatus.REJECTED if isinstance(exc, EtsyError) else ListingStatus.ERROR,
                [ListingIssue(code="EtsyError", message=str(exc), source="etsy")],
                str(state["listing_id"]) if state["listing_id"] else None,
            )
        log.warning("etsy.published", sku=listing.source_sku, listing_id=listing_id)
        return _outcome(listing, ListingStatus.SUBMITTED, [], str(listing_id))


def _outcome(
    listing: EtsyListing,
    status: ListingStatus,
    issues: list[ListingIssue],
    listing_id: str | None,
) -> SubmissionOutcome:
    return SubmissionOutcome(
        sku=listing.source_sku,
        marketplace_id=listing.marketplace_id,
        marketplace_code="ETSY",
        mode="SUBMIT",
        status=status,
        submission_id=listing_id,
        issues=issues,
        submitted_at=datetime.now(timezone.utc),
        payload_hash=listing.payload_hash,
    )
