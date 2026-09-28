"""Runtime configuration, loaded from the environment / ``.env``.

Two rules govern this module:

1. **Secrets never get defaults.** A missing credential must surface as an explicit, readable
   error at the moment it is needed, not as a silent fallback that produces a confusing 403.
2. **Safety settings fail closed.** ``allow_live`` defaults to ``False``. Publishing to a live
   marketplace requires flipping it *and* passing ``--confirm`` at the call site; neither alone
   is sufficient.

Credentials are per-region, not per-marketplace: one self-authorization covers every marketplace
in a region, so NA (US/CA/MX) and EU (UK/DE/FR/...) each need their own refresh token and their
own seller ID. See :mod:`anzorlist.marketplaces`.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from anzorlist.marketplaces import Region


class MissingCredential(RuntimeError):
    """A required credential is absent. Carries the exact env var to set."""

    def __init__(self, var: str, why: str) -> None:
        super().__init__(
            f"Missing credential {var}. {why}\n"
            f"Set it in .env (see .env.example), or run `anzorlist doctor` for a full checklist."
        )
        self.var = var


class Settings(BaseSettings):
    """All tunables in one place. Instantiate via :func:`settings`, not directly."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Paths ----
    data_dir: Path = Field(default=Path("data"), alias="ANZOR_DATA_DIR")
    workbook_path: Path = Field(default=Path("Product Listing.xlsx"), alias="ANZOR_WORKBOOK")
    state_db: Path = Field(default=Path("data/anzorlist.sqlite"), alias="ANZOR_STATE_DB")
    # Operator-supplied product photos, one folder per SKU (images/R985/01.jpg, 02.jpg, ...).
    # When a SKU's folder exists, its photos replace the website's, which are often too small.
    images_dir: Path = Field(default=Path("images"), alias="ANZOR_IMAGES_DIR")

    # ---- Site extraction ----
    base_url: str = Field(default="https://www.anzorjewelrycorp.com", alias="ANZOR_BASE_URL")
    user_agent: str = Field(
        default="Mozilla/5.0 (compatible; anzorlist/0.1; +ops@anzorjewelrycorp.com)",
        alias="ANZOR_USER_AGENT",
    )
    req_per_sec: float = Field(default=1.0, alias="ANZOR_REQ_PER_SEC")

    # ---- Brand / seller identity (goes into every listing) ----
    brand_name: str = Field(default="Anzor Jewelry", alias="ANZOR_BRAND_NAME")
    manufacturer: str = Field(default="Anzor Jewelry Corp", alias="ANZOR_MANUFACTURER")
    country_of_origin: str = Field(default="US", alias="ANZOR_COUNTRY_OF_ORIGIN")
    is_brand_registered: bool = Field(default=False, alias="ANZOR_BRAND_REGISTERED")

    # ---- Copy generation ----
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    copy_model: str = Field(default="claude-sonnet-5", alias="ANZOR_COPY_MODEL")
    copy_model_escalation: str = Field(default="claude-opus-5", alias="ANZOR_COPY_MODEL_ESCALATION")

    # ---- Pricing ----
    markup_amazon: Decimal = Field(default=Decimal("0.20"), alias="PRICE_MARKUP_AMAZON")
    markup_ebay: Decimal = Field(default=Decimal("0.15"), alias="PRICE_MARKUP_EBAY")
    markup_etsy: Decimal = Field(default=Decimal("0.09"), alias="PRICE_MARKUP_ETSY")
    price_floor: Decimal = Field(default=Decimal("10.00"), alias="PRICE_FLOOR")

    # ---- Image hosting (Cloudflare R2, S3-compatible) ----
    r2_account_id: str | None = Field(default=None, alias="R2_ACCOUNT_ID")
    r2_access_key_id: SecretStr | None = Field(default=None, alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: SecretStr | None = Field(default=None, alias="R2_SECRET_ACCESS_KEY")
    r2_bucket: str | None = Field(default=None, alias="R2_BUCKET")
    r2_public_base_url: str | None = Field(default=None, alias="R2_PUBLIC_BASE_URL")

    # ---- Amazon SP-API ----
    lwa_client_id: SecretStr | None = Field(default=None, alias="SPAPI_LWA_CLIENT_ID")
    lwa_client_secret: SecretStr | None = Field(default=None, alias="SPAPI_LWA_CLIENT_SECRET")

    refresh_token_na: SecretStr | None = Field(default=None, alias="SPAPI_REFRESH_TOKEN_NA")
    refresh_token_eu: SecretStr | None = Field(default=None, alias="SPAPI_REFRESH_TOKEN_EU")
    refresh_token_fe: SecretStr | None = Field(default=None, alias="SPAPI_REFRESH_TOKEN_FE")

    seller_id_na: str | None = Field(default=None, alias="SPAPI_SELLER_ID_NA")
    seller_id_eu: str | None = Field(default=None, alias="SPAPI_SELLER_ID_EU")
    seller_id_fe: str | None = Field(default=None, alias="SPAPI_SELLER_ID_FE")

    marketplaces: str = Field(default="US", alias="ANZOR_MARKETPLACES")
    # Which channels `anzorlist build` produces listings for: amazon, ebay, etsy.
    channels: str = Field(default="amazon,ebay,etsy", alias="ANZOR_CHANNELS")
    use_sandbox: bool = Field(default=False, alias="SPAPI_SANDBOX")

    # ---- eBay Sell APIs (Inventory + Account + Taxonomy) ----
    ebay_env: str = Field(default="SANDBOX", alias="EBAY_ENV")  # SANDBOX or PRODUCTION
    ebay_client_id: SecretStr | None = Field(default=None, alias="EBAY_CLIENT_ID")
    ebay_client_secret: SecretStr | None = Field(default=None, alias="EBAY_CLIENT_SECRET")
    ebay_refresh_token: SecretStr | None = Field(default=None, alias="EBAY_REFRESH_TOKEN")
    ebay_marketplace_id: str = Field(default="EBAY_US", alias="EBAY_MARKETPLACE_ID")
    ebay_fulfillment_policy_id: str | None = Field(default=None, alias="EBAY_FULFILLMENT_POLICY_ID")
    ebay_payment_policy_id: str | None = Field(default=None, alias="EBAY_PAYMENT_POLICY_ID")
    ebay_return_policy_id: str | None = Field(default=None, alias="EBAY_RETURN_POLICY_ID")
    ebay_merchant_location_key: str | None = Field(default=None, alias="EBAY_MERCHANT_LOCATION_KEY")

    # ---- Etsy Open API v3 ----
    etsy_api_key: SecretStr | None = Field(default=None, alias="ETSY_API_KEY")  # the keystring
    etsy_shared_secret: SecretStr | None = Field(default=None, alias="ETSY_SHARED_SECRET")
    etsy_refresh_token: SecretStr | None = Field(default=None, alias="ETSY_REFRESH_TOKEN")
    etsy_shop_id: str | None = Field(default=None, alias="ETSY_SHOP_ID")
    etsy_shipping_profile_id: str | None = Field(default=None, alias="ETSY_SHIPPING_PROFILE_ID")
    etsy_return_policy_id: str | None = Field(default=None, alias="ETSY_RETURN_POLICY_ID")
    etsy_readiness_state_id: str | None = Field(default=None, alias="ETSY_READINESS_STATE_ID")
    etsy_when_made: str = Field(default="made_to_order", alias="ETSY_WHEN_MADE")
    # Who made the items, as Etsy's who_made. Only "i_did" earns the "handmade jewelry" tag.
    etsy_who_made: Literal["i_did", "someone_else", "collective"] = Field(
        default="i_did", alias="ETSY_WHO_MADE"
    )

    # ---- Safety ----
    allow_live: bool = Field(default=False, alias="ANZOR_ALLOW_LIVE")
    default_quantity: int = Field(default=1, alias="ANZOR_DEFAULT_QUANTITY")
    handling_time_days: int = Field(default=3, alias="ANZOR_HANDLING_TIME_DAYS")

    @field_validator("markup_amazon", "markup_ebay", "markup_etsy")
    @classmethod
    def _sane_markup(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") <= v < Decimal("1")):
            raise ValueError(
                f"markup must be a fraction in [0, 1) — got {v}. "
                "0.20 means 'absorb a 20% fee', not '20x'."
            )
        return v

    # ---- Per-region credential accessors (raise, never return a silent None) ----

    def refresh_token(self, region: Region) -> str:
        token = {
            Region.NA: self.refresh_token_na,
            Region.EU: self.refresh_token_eu,
            Region.FE: self.refresh_token_fe,
        }[region]
        if token is None:
            raise MissingCredential(
                f"SPAPI_REFRESH_TOKEN_{region.value.upper()}",
                f"Each SP-API region needs its own self-authorization. "
                f"You have not authorized the {region.value.upper()} region yet.",
            )
        return token.get_secret_value()

    def seller_id(self, region: Region) -> str:
        sid = {
            Region.NA: self.seller_id_na,
            Region.EU: self.seller_id_eu,
            Region.FE: self.seller_id_fe,
        }[region]
        if sid is None:
            raise MissingCredential(
                f"SPAPI_SELLER_ID_{region.value.upper()}",
                "This is your Merchant Token, found in Seller Central under "
                "Settings > Account Info > Merchant Token.",
            )
        return sid

    def client_credentials(self) -> tuple[str, str]:
        if self.lwa_client_id is None:
            raise MissingCredential(
                "SPAPI_LWA_CLIENT_ID", "Created when you register an SP-API app in Seller Central."
            )
        if self.lwa_client_secret is None:
            raise MissingCredential(
                "SPAPI_LWA_CLIENT_SECRET", "Shown once when the SP-API app is created."
            )
        return self.lwa_client_id.get_secret_value(), self.lwa_client_secret.get_secret_value()

    def ebay_credentials(self) -> tuple[str, str, str]:
        """(client id, client secret, refresh token) for the eBay Sell APIs."""
        for value, var, why in (
            (self.ebay_client_id, "EBAY_CLIENT_ID", "The App ID from your eBay developer keyset."),
            (self.ebay_client_secret, "EBAY_CLIENT_SECRET", "The Cert ID from the same keyset."),
            (
                self.ebay_refresh_token,
                "EBAY_REFRESH_TOKEN",
                "Issued when you grant your app access to the seller account (user token).",
            ),
        ):
            if value is None:
                raise MissingCredential(var, why)
        assert self.ebay_client_id and self.ebay_client_secret and self.ebay_refresh_token
        return (
            self.ebay_client_id.get_secret_value(),
            self.ebay_client_secret.get_secret_value(),
            self.ebay_refresh_token.get_secret_value(),
        )

    def ebay_listing_policies(self) -> dict[str, str]:
        """The three business policies and the inventory location every eBay offer needs."""
        required = {
            "EBAY_FULFILLMENT_POLICY_ID": self.ebay_fulfillment_policy_id,
            "EBAY_PAYMENT_POLICY_ID": self.ebay_payment_policy_id,
            "EBAY_RETURN_POLICY_ID": self.ebay_return_policy_id,
            "EBAY_MERCHANT_LOCATION_KEY": self.ebay_merchant_location_key,
        }
        for var, value in required.items():
            if not value:
                raise MissingCredential(
                    var, "Run `anzorlist ebay setup` to list your business policies and locations."
                )
        return {k: str(v) for k, v in required.items()}

    def etsy_settings(self) -> dict[str, str]:
        """Every value an Etsy listing needs, or MissingCredential naming the first gap."""
        values = {
            "ETSY_API_KEY": self.etsy_api_key.get_secret_value() if self.etsy_api_key else None,
            "ETSY_REFRESH_TOKEN": (
                self.etsy_refresh_token.get_secret_value() if self.etsy_refresh_token else None
            ),
            "ETSY_SHOP_ID": self.etsy_shop_id,
            "ETSY_SHIPPING_PROFILE_ID": self.etsy_shipping_profile_id,
            "ETSY_RETURN_POLICY_ID": self.etsy_return_policy_id,
        }
        for var, value in values.items():
            if not value:
                raise MissingCredential(var, "See docs/RUNBOOK.md, section Etsy.")
        return {k: str(v) for k, v in values.items()}

    def channel_set(self) -> set[str]:
        return {c.strip().lower() for c in self.channels.split(",") if c.strip()}

    def has_spapi_credentials(self) -> bool:
        """True when a live call could at least be attempted. Used to pick offline mode."""
        return self.lwa_client_id is not None and self.lwa_client_secret is not None

    # ---- Derived paths ----

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def parsed_dir(self) -> Path:
        return self.data_dir / "parsed"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def schema_cache_dir(self) -> Path:
        return self.data_dir / "schemas"

    @property
    def build_dir(self) -> Path:
        """Rendered listing payloads, one JSON per SKU per marketplace. Auditable artifacts."""
        return self.data_dir / "build"


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
