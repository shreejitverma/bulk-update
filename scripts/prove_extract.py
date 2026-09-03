"""Offline proof: parse each committed fixture into a Product and dump JSON + a summary.

No network. Builds a FetchResult straight from tests/fixtures/{SKU}.html so the parser is
exercised in isolation against saved snapshots (the same way CI will).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anzorlist.extract.client import SiteClient  # noqa: E402
from anzorlist.extract.parser import FetchResult, parse_product  # noqa: E402

FIXTURES = Path("tests/fixtures")
SKUS = ["R985", "E1154", "S220", "E711"]


def load(sku: str) -> FetchResult:
    raw = (FIXTURES / f"{sku}.html").read_bytes()
    html, enc = SiteClient._decode(raw)
    return FetchResult(
        sku=sku,
        url=f"https://www.anzorjewelrycorp.com/Scripts/prodview.asp?SKU={sku}",
        html=html,
        raw_bytes=raw,
        content_hash=hashlib.sha256(raw).hexdigest(),
        encoding=enc,
        from_cache=True,
        cache_path=FIXTURES / f"{sku}.html",
    )


def main() -> None:
    out_dir = Path("data/parsed")
    out_dir.mkdir(parents=True, exist_ok=True)
    for sku in SKUS:
        p = parse_product(load(sku))
        (out_dir / f"{sku}.json").write_text(p.model_dump_json(indent=2))
        sizes = p.size_options
        size_axes = sorted({v.axis for v in sizes})
        deltas = [f"{v.label}:+{v.price_delta.amount}" for v in sizes[:2]]
        print(f"\n===== {sku} =====")
        print(f"  family        : {p.family.value} (verified={p.family_verified})")
        print(f"  internal_id   : {p.internal_product_id}")
        print(f"  title         : {p.marketing_title_raw[:80]!r}")
        print(f"  short_name    : {p.short_name!r}")
        print(f"  metal         : {p.attributes.metal_purity} {p.attributes.metal_color} "
              f"{p.attributes.metal_type}  weight={p.attributes.gross_weight_g}g")
        print("  gemstones     : "
              + "; ".join(f"{g.type} carat={g.carat_weight} treat={g.treatment} "
                          f"genuine={g.genuine} origin={g.origin}" for g in p.attributes.gemstones))
        print(f"  price         : list={p.pricing.list_price and p.pricing.list_price.amount} "
              f"our={p.pricing.our_price and p.pricing.our_price.amount} "
              f"save={p.pricing.you_save and p.pricing.you_save.amount} "
              f"({p.pricing.you_save_percent}%)")
        print(f"  stock/ship    : in_stock={p.in_stock} free_ship={p.free_shipping}")
        print(f"  specs         : {[r.label for r in p.specs]}")
        print(f"  size_options  : n={len(sizes)} axes={size_axes} e.g. {deltas}")
        appraisals = [(a.name, str(a.price_delta.amount)) for a in p.appraisal_options]
        print(f"  appraisals    : {appraisals}")
        imgs = [m for m in p.media if m.kind == 'image']
        vids = [m for m in p.media if m.kind == 'video']
        main_img = imgs[0].source_url if imgs else None
        print(f"  media         : {len(imgs)} images, {len(vids)} video  main={main_img}")
        print(f"  breadcrumbs   : {p.breadcrumbs}")
        print(f"  warnings      : {[(w.kind, w.field) for w in p.warnings]}")
        print(f"  provenance    : {len(p.provenance)} keys tracked")


if __name__ == "__main__":
    main()
