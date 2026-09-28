"""Shared fixtures. Everything here is offline — no network, no credentials."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from anzorlist.config import Settings
from anzorlist.extract.client import FetchResult, SiteClient
from anzorlist.extract.parser import parse_product
from anzorlist.marketplaces import resolve
from anzorlist.models.product import Product

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_SKUS = ("R985", "E1154", "S220", "E711")


def load_fetch(sku: str) -> FetchResult:
    raw = (FIXTURES / f"{sku}.html").read_bytes()
    html, encoding = SiteClient._decode(raw)
    return FetchResult(
        sku=sku,
        url=f"https://www.anzorjewelrycorp.com/Scripts/prodview.asp?SKU={sku}",
        html=html,
        raw_bytes=raw,
        content_hash=hashlib.sha256(raw).hexdigest(),
        encoding=encoding,
        from_cache=True,
        cache_path=FIXTURES / f"{sku}.html",
    )


@pytest.fixture(scope="session")
def products() -> dict[str, Product]:
    """All four fixtures, parsed once."""
    return {sku: parse_product(load_fetch(sku)) for sku in FIXTURE_SKUS}


@pytest.fixture
def ring(products: dict[str, Product]) -> Product:
    """R985 — a ring with a full ring-size variation axis."""
    return products["R985"]


@pytest.fixture
def earrings(products: dict[str, Product]) -> Product:
    """E1154 — earrings, no sizing axis, two-tone metal."""
    return products["E1154"]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolated from the developer's real .env and real data directory."""
    return Settings(
        _env_file=None,
        ANZOR_DATA_DIR=tmp_path / "data",
        ANZOR_STATE_DB=tmp_path / "data" / "test.sqlite",
        ANZOR_WORKBOOK=tmp_path / "Product Listing.xlsx",
        ANZOR_IMAGES_DIR=tmp_path / "images",
    )  # type: ignore[call-arg]


@pytest.fixture
def us():
    return resolve("US")
