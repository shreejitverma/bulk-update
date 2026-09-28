"""Etsy build artifacts: ``data/etsy/build/<SKU>.json``. Same contract as eBay's."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from anzorlist.channels.amazon.artifacts import ArtifactError
from anzorlist.channels.etsy.models import EtsyListing

ARTIFACT_VERSION = 1


def build_dir(data_dir: Path) -> Path:
    return data_dir / "etsy" / "build"


def clear(data_dir: Path, source_sku: str) -> None:
    (build_dir(data_dir) / f"{source_sku}.json").unlink(missing_ok=True)


def write(data_dir: Path, listing: EtsyListing) -> Path:
    target = build_dir(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{listing.source_sku}.json"
    document = {"artifactVersion": ARTIFACT_VERSION, **listing.model_dump(mode="json")}
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return path


def load_all(data_dir: Path, shop: dict[str, Any]) -> list[EtsyListing]:
    """Every built listing, hashed together with the shop fields it will be submitted with, so a
    changed shop setting makes the listing due again."""
    listings: list[EtsyListing] = []
    folder = build_dir(data_dir)
    if not folder.exists():
        return listings
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ArtifactError(f"{path}: not readable JSON ({exc}); rebuild it") from exc
        if not isinstance(data, dict) or data.pop("artifactVersion", None) != ARTIFACT_VERSION:
            raise ArtifactError(f"{path}: not a v{ARTIFACT_VERSION} Etsy artifact; rebuild it")
        try:
            listing = EtsyListing.model_validate(data)
        except ValidationError as exc:
            raise ArtifactError(f"{path}: invalid Etsy artifact: {exc}") from exc
        listing.payload_hash = listing.compute_payload_hash(shop)
        listings.append(listing)
    return listings
