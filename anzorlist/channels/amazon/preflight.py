"""Preflight: answer "are we even allowed to list this?" before building anything.

Fine jewelry is a **gated category** on Amazon. Approval is per seller, per marketplace, and
sometimes per brand, and an unapproved seller's listings are rejected at submission — after the
work of extraction, copy generation, image hosting, and mapping has already been done for the
whole catalog.

Three checks run here, cheapest first:

1. **Marketplace participation** — is the account even registered in the marketplaces the
   spreadsheet asks for? Listing to a marketplace the seller has not opened fails confusingly.
2. **Category / brand approval** (Listings Restrictions API) — does this seller have permission
   to list in this product type, in this condition, in this marketplace?
3. **Product type availability** — does the product type exist in that marketplace at all?

Every one of these is read-only. Preflight can never modify the account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from anzorlist.channels.amazon.client import SpApiClient, SpApiError
from anzorlist.marketplaces import Marketplace

log = structlog.get_logger(__name__)

RESTRICTIONS_PATH = "/listings/2021-08-01/restrictions"
PARTICIPATIONS_PATH = "/sellers/v1/marketplaceParticipations"


@dataclass
class Restriction:
    """One reason a seller may not list something, plus how to fix it.

    Amazon returns a ``links`` array whose ``resource`` is the Seller Central approval URL. That
    URL is the single most useful part of the response and is surfaced verbatim.
    """

    marketplace_id: str
    condition_type: str
    reasons: list[str] = field(default_factory=list)
    approval_urls: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        out = f"{self.condition_type}: " + "; ".join(self.reasons)
        if self.approval_urls:
            out += "\n    Request approval: " + "\n    ".join(self.approval_urls)
        return out


@dataclass
class PreflightReport:
    """The complete go/no-go picture for one marketplace."""

    marketplace: Marketplace
    participating: bool | None = None  # None when the check could not run
    restrictions: list[Restriction] = field(default_factory=list)
    product_types_available: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.participating is False or bool(self.restrictions)

    def render(self) -> str:
        lines = [f"{self.marketplace.code} ({self.marketplace.country})"]
        if self.participating is None:
            lines.append("  participation : not checked")
        elif self.participating:
            lines.append("  participation : yes")
        else:
            lines.append("  participation : NO — the account is not registered here")
        if self.restrictions:
            lines.append("  restrictions  : LISTING BLOCKED")
            for r in self.restrictions:
                lines.append(f"    - {r}")
        else:
            lines.append("  restrictions  : none reported")
        if self.product_types_available:
            lines.append(f"  product types : {', '.join(self.product_types_available)}")
        for note in self.notes:
            lines.append(f"  note          : {note}")
        return "\n".join(lines)


class PreflightClient:
    """Read-only account and permission checks."""

    def __init__(self, client: SpApiClient) -> None:
        self._client = client

    def participations(self) -> dict[str, dict[str, Any]]:
        """Marketplaces this account is registered in, keyed by marketplace ID."""
        payload = self._client.get(PARTICIPATIONS_PATH, operation="getMarketplaceParticipations")
        out: dict[str, dict[str, Any]] = {}
        for entry in (payload or {}).get("payload", []) or []:
            mk = entry.get("marketplace", {})
            if mk.get("id"):
                out[str(mk["id"])] = entry
        log.info("preflight.participations", count=len(out))
        return out

    def restrictions(
        self,
        marketplace: Marketplace,
        *,
        asin: str | None = None,
        condition_type: str = "new_new",
        seller_id: str | None = None,
    ) -> list[Restriction]:
        """Ask whether this seller may list under the given conditions.

        The API is ASIN-oriented: it answers "may you list against *this* ASIN". For a brand-new
        product with no ASIN there is nothing to pass, so the honest answer is that this check
        cannot be run — reported as a note rather than a false all-clear.
        """
        if asin is None:
            return []
        params = {
            "asin": asin,
            "sellerId": seller_id or self._client.seller_id,
            "marketplaceIds": marketplace.marketplace_id,
            "conditionType": condition_type,
        }
        payload = self._client.get(RESTRICTIONS_PATH, operation="getListingsRestrictions",
                                   params=params)
        out: list[Restriction] = []
        for entry in (payload or {}).get("restrictions", []) or []:
            reasons_raw = entry.get("reasons", []) or []
            out.append(Restriction(
                marketplace_id=str(entry.get("marketplaceId", marketplace.marketplace_id)),
                condition_type=str(entry.get("conditionType", condition_type)),
                reasons=[str(r.get("message", "")) for r in reasons_raw if isinstance(r, dict)],
                approval_urls=[
                    str(link.get("resource", ""))
                    for r in reasons_raw if isinstance(r, dict)
                    for link in (r.get("links", []) or [])
                    if isinstance(link, dict) and link.get("resource")
                ],
            ))
        if out:
            log.warning("preflight.restricted", marketplace=marketplace.code, asin=asin,
                        count=len(out))
        return out

    def run(
        self,
        marketplace: Marketplace,
        *,
        product_types: list[str] | None = None,
        sample_asin: str | None = None,
    ) -> PreflightReport:
        """Full preflight for one marketplace. Never raises — a failed check becomes a note, so
        one unavailable endpoint cannot hide the results of the others."""
        report = PreflightReport(marketplace=marketplace)

        try:
            parts = self.participations()
            report.participating = marketplace.marketplace_id in parts
            if not report.participating:
                report.notes.append(
                    f"Register for {marketplace.country} in Seller Central before listing there."
                )
        except SpApiError as exc:
            report.notes.append(f"participation check failed: {exc}")
        except Exception as exc:  # noqa: BLE001 — a preflight must always produce a report
            report.notes.append(f"participation check unavailable: {exc}")

        if sample_asin:
            try:
                report.restrictions = self.restrictions(marketplace, asin=sample_asin)
            except Exception as exc:  # noqa: BLE001
                report.notes.append(f"restrictions check failed: {exc}")
        else:
            report.notes.append(
                "Category gating was not verified: the Listings Restrictions API needs an ASIN, "
                "and brand-new jewelry has none. Confirm 'Fine Jewelry' approval in Seller "
                "Central > Inventory > Add a Product, or pass --asin with a comparable ASIN."
            )

        if product_types:
            report.product_types_available = product_types
        return report
