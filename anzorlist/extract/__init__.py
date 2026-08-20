"""Site extraction: client (fetch + cache) and parser (HTML -> Product)."""

from anzorlist.extract.client import FetchResult, SiteClient
from anzorlist.extract.parser import parse_product

__all__ = ["FetchResult", "SiteClient", "parse_product"]
