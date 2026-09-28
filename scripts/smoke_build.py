"""End-to-end smoke test: fixture HTML -> Amazon listing payload, with no network.

CI runs this alongside the unit suite. A green unit suite with a broken end-to-end path is the
failure mode that actually matters here — every stage can pass in isolation while the wiring
between them is wrong. This exercises the real route and asserts on the shape Amazon requires.

Exits non-zero on any failure so CI fails loudly.
"""

from __future__ import annotations

import hashlib
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anzorlist.channels.amazon.mapper import AmazonMapper  # noqa: E402
from anzorlist.config import Settings  # noqa: E402
from anzorlist.extract.client import FetchResult, SiteClient  # noqa: E402
from anzorlist.extract.parser import parse_product  # noqa: E402
from anzorlist.generate.copy import fallback_copy  # noqa: E402
from anzorlist.generate.validate import validate_copy  # noqa: E402
from anzorlist.ingest.row import ListingRow  # noqa: E402
from anzorlist.marketplaces import resolve  # noqa: E402
from anzorlist.pricing import price_for  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def load(sku: str) -> FetchResult:
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


def main() -> int:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    mapper = AmazonMapper(settings)
    us = resolve("US")

    for sku in sorted(p.stem for p in FIXTURES.glob("*.html")):
        product = parse_product(load(sku))
        row = ListingRow(sku=sku, variation_source="site")
        copy = fallback_copy(product, settings.brand_name)

        report = validate_copy(
            product=product,
            title=copy.title,
            bullets=copy.bullets,
            description=copy.description,
            search_terms=copy.search_terms,
        )
        check(
            report.ok,
            f"{sku}: fallback copy failed its own validator: {[str(i) for i in report.errors][:3]}",
        )

        web = product.pricing.our_price.amount if product.pricing.our_price else None
        check(web is not None, f"{sku}: no selling price extracted")
        quote = price_for(web, us, fee_fraction=Decimal("0.20"), floor=settings.price_floor)
        check(
            quote.price > (web or Decimal(0)),
            f"{sku}: gross-up did not raise the price above the web price",
        )

        listings = mapper.build(
            product=product,
            row=row,
            marketplace=us,
            copy=copy,
            quote=quote,
            image_urls=["https://cdn.test/main.jpg"],
        )
        check(bool(listings), f"{sku}: mapper produced no listings")

        for listing in listings:
            body = listing.body()
            check(
                set(body) >= {"productType", "requirements", "attributes"},
                f"{sku}/{listing.sku}: body is missing a required key",
            )
            for key, value in listing.attributes.items():
                check(
                    isinstance(value, list) and all(isinstance(v, dict) for v in value),
                    f"{sku}/{listing.sku}: attribute {key} is not a list of objects",
                )
                check(
                    all("marketplace_id" in v for v in value),
                    f"{sku}/{listing.sku}: attribute {key} is not marketplace-scoped",
                )
            if listing.is_parent:
                check(
                    listing.offer is None and "purchasable_offer" not in listing.attributes,
                    f"{sku}: variation parent must not carry an offer",
                )
            else:
                check(
                    "purchasable_offer" in listing.attributes,
                    f"{sku}/{listing.sku}: buyable listing has no purchasable_offer",
                )

        parents = [x for x in listings if x.is_parent]
        if parents:
            check(listings[0].is_parent, f"{sku}: parent must be first for Amazon ordering")
            children = [x for x in listings if x.parent_sku]
            check(len(children) >= 2, f"{sku}: a variation family needs at least 2 children")
            check(len({x.sku for x in children}) == len(children), f"{sku}: duplicate child SKUs")

        print(
            f"  ok  {sku:<8} {len(listings):>3} listing(s)  "
            f"{'family' if parents else 'standalone':<10} ${quote.price}"
        )

    if failures:
        print(f"\nFAILED ({len(failures)}):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\nsmoke build passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
