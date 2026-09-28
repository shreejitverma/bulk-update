"""End to end: workbook -> build -> etsy submit, against a fake Etsy."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fake_etsy import FakeEtsy
from test_amazon_upload_e2e import _white_jpeg, build_ok, env, run  # noqa: F401 - env is a fixture

from anzorlist import cli
from anzorlist.channels.etsy.client import EtsyClient
from anzorlist.config import settings as get_settings
from anzorlist.media.pipeline import MediaPipeline, ProcessedImage


@pytest.fixture
def etsy(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> FakeEtsy:  # noqa: F811
    for key, value in {
        "ETSY_API_KEY": "keystring",
        "ETSY_SHARED_SECRET": "secret",
        "ETSY_REFRESH_TOKEN": "etsy-refresh-0",
        "ETSY_SHOP_ID": "4242",
        "ETSY_SHIPPING_PROFILE_ID": "77",
        "ETSY_RETURN_POLICY_ID": "88",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    fake = FakeEtsy()
    monkeypatch.setattr(cli, "_make_etsy_client", lambda s: EtsyClient(s, transport=fake.transport))
    return fake


def test_build_writes_etsy_listing_with_files(env: dict[str, Any]) -> None:  # noqa: F811
    build_ok()
    ring = json.loads((env["data"] / "etsy" / "build" / "R985.json").read_text())
    assert ring["variation_property"] == "Ring size" and len(ring["variations"]) == 29
    assert len(ring["title"]) <= 140 and len(ring["tags"]) <= 13
    assert all(len(t) <= 20 for t in ring["tags"])
    assert ring["image_files"] and ring["image_files"][0].endswith(".jpg")


def test_submit_creates_uploads_sets_sizes_and_activates(
    env: dict[str, Any],  # noqa: F811
    etsy: FakeEtsy,
) -> None:
    build_ok()
    result = run("etsy", "submit", "--confirm", input="y\n")
    assert result.exit_code == 0, result.output
    assert len(etsy.listings) == 2
    assert all(listing["state"] == "active" for listing in etsy.listings.values())
    ring_id = next(i for i, x in etsy.listings.items() if x["taxonomy_id"] == 1250)
    products = etsy.inventory[ring_id]["products"]
    assert len(products) == 29 and etsy.inventory[ring_id]["price_on_property"] == [513]
    prices = {p["sku"]: p["offerings"][0]["price"] for p in products}
    assert prices["R985-7"] < prices["R985-12"]
    assert etsy.api_keys == {"keystring:secret"}
    # The rotated refresh token was persisted and is what the next run must use.
    saved = json.loads((env["data"] / "etsy" / "token.json").read_text())["refresh_token"]
    assert saved == etsy.valid_refresh


def test_rerun_after_a_failure_updates_the_same_listing(
    env: dict[str, Any],  # noqa: F811
    etsy: FakeEtsy,
) -> None:
    build_ok()
    etsy.fail_inventory_once = True
    first = run("etsy", "submit", "--confirm", "E1154", input="y\n")
    assert first.exit_code == 1
    assert len(etsy.listings) == 1 and etsy.uploads == 1
    get_settings.cache_clear()  # a new process: the refresh token must come from disk
    second = run("etsy", "submit", "--confirm", "E1154", input="y\n")
    assert second.exit_code == 0, second.output
    assert len(etsy.listings) == 1  # updated, not duplicated
    assert etsy.uploads == 1  # the same photo is not uploaded twice
    assert next(iter(etsy.listings.values()))["state"] == "active"


def test_publishing_is_gated(
    env: dict[str, Any],  # noqa: F811
    etsy: FakeEtsy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_ok()
    assert run("etsy", "submit").exit_code == 2
    monkeypatch.setenv("ANZOR_ALLOW_LIVE", "false")
    get_settings.cache_clear()
    assert run("etsy", "submit", "--confirm").exit_code == 2
    assert etsy.listings == {}


def test_a_replaced_photo_replaces_the_listing_image(
    env: dict[str, Any],  # noqa: F811
    etsy: FakeEtsy,
) -> None:
    photos = env["root"] / "images" / "E1154"
    (photos / "02.jpg").write_bytes(_white_jpeg(1300))
    build_ok()
    assert run("etsy", "submit", "--confirm", "E1154", input="y\n").exit_code == 0
    [listing_id] = etsy.listings
    assert len(etsy.images[listing_id]) == 2

    (photos / "01.jpg").write_bytes(_white_jpeg(1400))
    build_ok()
    new = json.loads((env["data"] / "etsy" / "build" / "E1154.json").read_text())["image_files"]
    result = run("etsy", "submit", "--confirm", "E1154", input="y\n")
    assert result.exit_code == 0, result.output
    live = etsy.images[listing_id].values()
    assert sorted(name for name, _ in live) == sorted(path.rsplit("/", 1)[1] for path in new)
    assert {ctype for _, ctype in live} == {"image/jpeg"}
    assert etsy.uploads == 3  # only the new main photo; the unchanged one stays


def test_etsy_listings_are_not_counted_as_amazon_listings(
    env: dict[str, Any],  # noqa: F811
    etsy: FakeEtsy,
) -> None:
    build_ok()
    assert run("etsy", "submit", "--confirm", "E1154", input="y\n").exit_code == 0
    result = run("amazon", "status")
    assert "0 SKU(s) believed to exist on Amazon" in result.output


def test_a_failed_image_hosting_upload_does_not_block_etsy(
    env: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Failing:
        def upload(self, image: ProcessedImage) -> str:
            raise RuntimeError("R2 is down")

    monkeypatch.setattr(MediaPipeline, "_get_uploader", lambda self: Failing())
    run("build", "--no-copy")
    listing = json.loads((env["data"] / "etsy" / "build" / "E1154.json").read_text())
    assert listing["image_files"] and listing["issues"] == []
