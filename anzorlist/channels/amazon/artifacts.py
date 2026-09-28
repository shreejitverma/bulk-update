"""Built-listing artifacts on disk: the payloads an operator reviews and then submits.

``anzorlist build`` writes one JSON file per listing; ``amazon validate`` and ``amazon submit``
read them back. The file is the artifact of record, so it must round-trip the *whole*
:class:`~anzorlist.models.listing.BuiltListing` - offer, issues, source SKU, parentage - and not
just the request body. A loader that dropped the issues would send a listing the local checks had
already blocked; one that dropped the source SKU could not honour ``amazon submit R985``.

Layout::

    data/build/<MARKETPLACE>/<SOURCE_SKU>/<LISTING_SKU>.json

One directory per source SKU per marketplace is what makes rebuilds exact. Rebuilding a SKU
replaces its directory wholesale, so a ring size that disappeared from the website does not leave
a stale child payload behind to be submitted later.

Operators may hand-edit ``attributes`` after review. The payload hash is therefore recomputed on
load rather than trusted from the file, so an edited payload is never mistaken for the one Amazon
already accepted.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError

from anzorlist.models.listing import BuiltListing

log = structlog.get_logger(__name__)

ARTIFACT_VERSION = 2


class ArtifactError(RuntimeError):
    """A build artifact could not be read. Names the file and the fix."""


@dataclass
class LoadedArtifacts:
    listings: list[BuiltListing] = field(default_factory=list)
    legacy_files: list[Path] = field(default_factory=list)


def family_dir(build_dir: Path, marketplace_code: str, source_sku: str) -> Path:
    return build_dir / marketplace_code / source_sku


def clear_family(build_dir: Path, marketplace_code: str, source_sku: str) -> None:
    """Remove every artifact of one source SKU in one marketplace."""
    target = family_dir(build_dir, marketplace_code, source_sku)
    if target.exists():
        shutil.rmtree(target)


def write_listings(build_dir: Path, listings: Iterable[BuiltListing]) -> list[Path]:
    """Replace the artifacts of every (marketplace, source SKU) present in ``listings``."""
    grouped: dict[tuple[str, str], list[BuiltListing]] = {}
    for listing in listings:
        grouped.setdefault((listing.marketplace_code, listing.source_sku), []).append(listing)

    written: list[Path] = []
    for (market, source_sku), group in grouped.items():
        target = family_dir(build_dir, market, source_sku)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for listing in group:
            path = target / f"{_safe_name(listing.sku)}.json"
            document: dict[str, Any] = {"artifactVersion": ARTIFACT_VERSION}
            document.update(listing.model_dump(mode="json"))
            path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
            written.append(path)
    return written


def load_listings(build_dir: Path) -> LoadedArtifacts:
    """Read every artifact under ``build_dir``. Raises on a corrupt file rather than skipping it.

    Skipping would make a submission silently smaller than the build the operator reviewed.
    Files in the pre-v2 flat layout (``data/build/US/R985-7.json``) are reported, not loaded:
    they carry no issues or offer, so sending them would bypass the local checks.
    """
    result = LoadedArtifacts()
    if not build_dir.exists():
        return result
    for market_dir in sorted(p for p in build_dir.iterdir() if p.is_dir()):
        result.legacy_files.extend(sorted(market_dir.glob("*.json")))
        for path in sorted(market_dir.glob("*/*.json")):
            result.listings.append(_read(path))
    return result


def _read(path: Path) -> BuiltListing:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ArtifactError(
            f"{path}: not readable JSON ({exc}). Rebuild with `anzorlist build`."
        ) from exc
    if not isinstance(data, dict) or data.pop("artifactVersion", None) != ARTIFACT_VERSION:
        raise ArtifactError(
            f"{path}: not a v{ARTIFACT_VERSION} build artifact. Rebuild with `anzorlist build`."
        )
    try:
        listing = BuiltListing.model_validate(data)
    except ValidationError as exc:
        raise ArtifactError(f"{path}: invalid build artifact: {exc}") from exc

    recomputed = listing.compute_payload_hash()
    if recomputed != listing.payload_hash:
        log.warning("artifacts.edited", path=str(path), sku=listing.sku)
        listing.payload_hash = recomputed
    return listing


def _safe_name(sku: str) -> str:
    """A filesystem-safe file stem. Seller SKUs may contain '/', which must not nest paths."""
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in sku)
