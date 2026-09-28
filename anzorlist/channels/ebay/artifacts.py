"""eBay build artifacts: ``data/ebay/build/<SKU>.json``, one listing page per website SKU.

Kept apart from Amazon's ``data/build/`` tree so neither loader can mistake the other's files.
As with Amazon, a rebuild replaces the file and the payload hash is recomputed on load, so an
artifact edited after review is resubmitted rather than skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from anzorlist.channels.amazon.artifacts import ArtifactError
from anzorlist.channels.ebay.models import EbayListing

ARTIFACT_VERSION = 1


def build_dir(data_dir: Path) -> Path:
    return data_dir / "ebay" / "build"


def clear(data_dir: Path, source_sku: str) -> None:
    (build_dir(data_dir) / f"{source_sku}.json").unlink(missing_ok=True)


def write(data_dir: Path, listing: EbayListing) -> Path:
    target = build_dir(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{listing.source_sku}.json"
    document = {"artifactVersion": ARTIFACT_VERSION, **listing.model_dump(mode="json")}
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return path


def load_all(data_dir: Path) -> list[EbayListing]:
    listings: list[EbayListing] = []
    folder = build_dir(data_dir)
    if not folder.exists():
        return listings
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ArtifactError(f"{path}: not readable JSON ({exc}); rebuild it") from exc
        if not isinstance(data, dict) or data.pop("artifactVersion", None) != ARTIFACT_VERSION:
            raise ArtifactError(f"{path}: not a v{ARTIFACT_VERSION} eBay artifact; rebuild it")
        try:
            listing = EbayListing.model_validate(data)
        except ValidationError as exc:
            raise ArtifactError(f"{path}: invalid eBay artifact: {exc}") from exc
        listing.payload_hash = listing.compute_payload_hash()
        listings.append(listing)
    return listings
