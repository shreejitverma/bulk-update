"""Product Type Definitions API — and the offline validator it makes possible.

Amazon publishes a JSON Schema for every (product type, marketplace) pair describing exactly
which attributes exist, which are required, and what shape their values take. That schema is the
difference between guessing and knowing.

The workflow is:

1. ``searchDefinitionsProductTypes`` — find the right product type for a jewelry item.
2. ``getDefinitionsProductType`` — get a *presigned link* to the schema (the schema itself is not
   inline; the response carries a short-lived S3 URL).
3. Download and cache the schema on disk.
4. Validate every generated payload against it locally, before submitting anything.

Step 4 is why this module matters most. A listing that fails Amazon's schema comes back as an
opaque rejection minutes later, one SKU at a time, against a rate-limited endpoint. The same
failure is found locally in milliseconds, for the whole catalog at once, with a message pointing
at the exact attribute — and it works with no credentials at all once a schema is cached, which
is precisely the situation until SP-API registration completes.

The presigned link expires quickly, so the schema is written to disk immediately on fetch and
re-read from cache thereafter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from anzorlist.channels.amazon.client import SpApiClient
from anzorlist.marketplaces import Marketplace

log = structlog.get_logger(__name__)

DEFINITIONS_BASE = "/definitions/2020-09-01/productTypes"

# Requirement sets Amazon recognises. LISTING is the full item (product data + offer); the
# split variants exist for sellers who own only one half of a shared listing.
Requirements = str  # "LISTING" | "LISTING_PRODUCT_ONLY" | "LISTING_OFFER_ONLY"


@dataclass
class ProductTypeSchema:
    """A cached Amazon product-type schema plus the metadata needed to reason about it."""

    product_type: str
    marketplace_id: str
    requirements: str
    schema: dict[str, Any]
    display_name: str = ""
    version: str = ""
    path: Path | None = None

    @property
    def required_attributes(self) -> list[str]:
        req = self.schema.get("required", [])
        return sorted(str(r) for r in req)

    @property
    def known_attributes(self) -> list[str]:
        return sorted(self.schema.get("properties", {}))

    def validator(self) -> Draft202012Validator:
        return Draft202012Validator(self.schema)


@dataclass
class AttributeIssue:
    """A local schema violation, phrased the way the operator needs to hear it."""

    attribute: str
    message: str
    path: str
    severity: str = "ERROR"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.path or self.attribute}: {self.message}"


class DefinitionsClient:
    """Fetches and caches product-type schemas. Falls back to cache when offline."""

    def __init__(self, client: SpApiClient | None, cache_dir: Path) -> None:
        self._client = client
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._memo: dict[tuple[str, str, str], ProductTypeSchema] = {}

    # -- discovery --

    def search_product_types(
        self, marketplace: Marketplace, *, keywords: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """List product types available in a marketplace, optionally filtered by keyword.

        Amazon revises this vocabulary, and it is marketplace-specific — the same jewelry item can
        be ``RING`` in one marketplace and unavailable in another. Never hardcode the answer.
        """
        if self._client is None:
            raise RuntimeError(
                "Product-type discovery needs SP-API credentials. "
                "Until then the defaults in anzorlist.ingest.schema.JEWELRY_PRODUCT_TYPES are used."
            )
        params: dict[str, Any] = {"marketplaceIds": marketplace.marketplace_id}
        if keywords:
            params["keywords"] = ",".join(keywords)
        payload = self._client.get(
            DEFINITIONS_BASE, operation="searchDefinitionsProductTypes", params=params
        )
        types = payload.get("productTypes", []) if isinstance(payload, dict) else []
        log.info("definitions.searched", marketplace=marketplace.code, count=len(types),
                 keywords=keywords)
        return list(types)

    # -- schema fetch + cache --

    def cache_path(self, product_type: str, marketplace: Marketplace, requirements: str) -> Path:
        return self._cache_dir / f"{marketplace.code}_{product_type}_{requirements}.json"

    def get_schema(
        self,
        product_type: str,
        marketplace: Marketplace,
        *,
        requirements: str = "LISTING",
        refresh: bool = False,
    ) -> ProductTypeSchema:
        """Return the schema, preferring the on-disk cache.

        With ``refresh=False`` and a cached copy present this makes no network call at all, which
        is what lets the whole build+validate pipeline run without credentials.
        """
        key = (product_type, marketplace.marketplace_id, requirements)
        if not refresh and key in self._memo:
            return self._memo[key]

        path = self.cache_path(product_type, marketplace, requirements)
        if not refresh and path.exists():
            data = json.loads(path.read_text())
            pts = ProductTypeSchema(
                product_type=product_type,
                marketplace_id=marketplace.marketplace_id,
                requirements=requirements,
                schema=data["schema"],
                display_name=data.get("displayName", ""),
                version=data.get("version", ""),
                path=path,
            )
            self._memo[key] = pts
            return pts

        if self._client is None:
            raise SchemaUnavailable(product_type, marketplace, path)

        pts = self._fetch_schema(product_type, marketplace, requirements)
        path.write_text(json.dumps(
            {"schema": pts.schema, "displayName": pts.display_name, "version": pts.version,
             "productType": product_type, "marketplaceId": marketplace.marketplace_id,
             "requirements": requirements},
            indent=2,
        ))
        pts.path = path
        self._memo[key] = pts
        log.info("definitions.schema_cached", product_type=product_type,
                 marketplace=marketplace.code, path=str(path),
                 required=len(pts.required_attributes))
        return pts

    def _fetch_schema(
        self, product_type: str, marketplace: Marketplace, requirements: str
    ) -> ProductTypeSchema:
        assert self._client is not None
        payload = self._client.get(
            f"{DEFINITIONS_BASE}/{product_type}",
            operation="getDefinitionsProductType",
            params={
                "marketplaceIds": marketplace.marketplace_id,
                "requirements": requirements,
                "locale": marketplace.locale,
            },
        )
        link = (payload or {}).get("schema", {}).get("link", {})
        resource = link.get("resource")
        if not resource:
            raise RuntimeError(
                f"getDefinitionsProductType returned no schema link for {product_type} in "
                f"{marketplace.code}. Response keys: {sorted(payload or {})}"
            )
        # Presigned S3 URL, valid for minutes. Fetch it now, with no auth header — adding the
        # SP-API bearer token to a presigned request makes S3 reject it.
        with httpx.Client(timeout=60.0) as raw:
            resp = raw.get(resource)
            resp.raise_for_status()
            schema = resp.json()

        return ProductTypeSchema(
            product_type=product_type,
            marketplace_id=marketplace.marketplace_id,
            requirements=requirements,
            schema=schema,
            display_name=str(payload.get("displayName", "")),
            version=str(payload.get("productTypeVersion", {}).get("version", "")),
        )


class SchemaUnavailable(RuntimeError):
    """No cached schema and no credentials to fetch one. Explains both ways forward."""

    def __init__(self, product_type: str, marketplace: Marketplace, path: Path) -> None:
        super().__init__(
            f"No cached Amazon schema for {product_type} in {marketplace.code} and no SP-API "
            f"credentials to download one.\n"
            f"  Expected cache file: {path}\n"
            f"  Once credentials are set: anzorlist amazon sync-schemas\n"
            f"  Until then, payloads are built and checked against the built-in structural rules, "
            f"but not against Amazon's authoritative schema."
        )
        self.product_type = product_type
        self.path = path


def validate_attributes(
    attributes: dict[str, Any], schema: ProductTypeSchema, *, max_issues: int = 50
) -> list[AttributeIssue]:
    """Validate a listing's attributes against Amazon's schema. Returns every issue found.

    Errors are rewritten into operator language: ``jsonschema``'s native message for a missing
    required property names the property inside a sentence about the parent object, which is
    unreadable when the parent is a 300-attribute listing.
    """
    issues: list[AttributeIssue] = []
    validator = schema.validator()
    for err in sorted(validator.iter_errors(attributes), key=lambda e: list(e.absolute_path)):
        issues.append(_translate(err))
        if len(issues) >= max_issues:
            issues.append(AttributeIssue(
                attribute="", path="",
                message=f"... more issues suppressed after {max_issues}; fix these first.",
                severity="INFO",
            ))
            break
    return issues


def _translate(err: JsonSchemaError) -> AttributeIssue:
    path_parts = [str(p) for p in err.absolute_path]
    path = ".".join(path_parts) if path_parts else "(root)"
    attribute = path_parts[0] if path_parts else ""

    if err.validator == "required":
        # err.message is "'brand' is a required property"
        missing = str(err.message).split("'")[1] if "'" in str(err.message) else str(err.message)
        return AttributeIssue(
            attribute=missing,
            path=missing if not path_parts else f"{path}.{missing}",
            message=f"required attribute {missing!r} is missing",
        )
    if err.validator == "enum":
        allowed = err.validator_value if isinstance(err.validator_value, list) else []
        shown = ", ".join(map(str, allowed[:12])) + (" ..." if len(allowed) > 12 else "")
        return AttributeIssue(attribute=attribute, path=path,
                              message=f"value not allowed here; Amazon accepts: {shown}")
    if err.validator == "maxLength":
        return AttributeIssue(attribute=attribute, path=path,
                              message=f"too long — Amazon's limit is {err.validator_value} "
                                      f"characters")
    if err.validator == "type":
        return AttributeIssue(attribute=attribute, path=path,
                              message=f"wrong type — expected {err.validator_value}")
    return AttributeIssue(attribute=attribute, path=path, message=str(err.message))
