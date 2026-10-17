"""Application settings.

Loads configuration from environment variables (and an optional ``.env`` file)
plus the YAML scoring configuration. Secrets are read from the environment only
and are never hardcoded or persisted to the database in plaintext.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Load .env if python-dotenv is available. It is optional at runtime so the app
# still works when only real environment variables are set.
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


# Project root is two levels up from this file (config/settings.py -> project).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
EXPORTS_DIR = PROJECT_ROOT / "exports"
SCORING_CONFIG_PATH = CONFIG_DIR / "scoring_config.yaml"
BRAND_ALIASES_PATH = CONFIG_DIR / "brand_aliases.yaml"
NAVER_CATEGORIES_PATH = CONFIG_DIR / "naver_categories.yaml"


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from the environment."""

    database_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("DATABASE_PATH", str(DATA_DIR / "overseas_demand_radar.db"))
        )
    )
    base_currency: str = field(default_factory=lambda: os.getenv("BASE_CURRENCY", "KRW"))
    target_margin_pct: float = field(
        default_factory=lambda: _get_float("TARGET_MARGIN_PCT", 20.0)
    )
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Optional external integrations (unused in Phase 1).
    naver_client_id: str = field(default_factory=lambda: os.getenv("NAVER_CLIENT_ID", ""))
    naver_client_secret: str = field(
        default_factory=lambda: os.getenv("NAVER_CLIENT_SECRET", "")
    )
    ollama_base_url: str = field(
        default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", ""))
    # Upper bound on Naver API calls per collection run (daily quota is 1,000).
    naver_max_calls_per_run: int = field(
        default_factory=lambda: int(_get_float("NAVER_MAX_CALLS_PER_RUN", 50))
    )
    # Anchor keyword included in every search-trend request so indices from
    # different requests share one scale (anchor's recent level == 50).
    naver_anchor_keyword: str = field(
        default_factory=lambda: os.getenv("NAVER_ANCHOR_KEYWORD", "해외직구").strip() or "해외직구"
    )
    # Optional separate NAVER API HUB Application for Shopping Insight. When
    # blank, the NAVER_CLIENT_ID / NAVER_CLIENT_SECRET pair is used instead.
    naver_shopping_client_id_raw: str = field(
        default_factory=lambda: os.getenv("NAVER_SHOPPING_CLIENT_ID", "").strip()
    )
    naver_shopping_client_secret_raw: str = field(
        default_factory=lambda: os.getenv("NAVER_SHOPPING_CLIENT_SECRET", "").strip()
    )

    # Phase 3: overseas supplier search APIs (all optional).
    ebay_client_id: str = field(default_factory=lambda: os.getenv("EBAY_CLIENT_ID", "").strip())
    ebay_client_secret: str = field(default_factory=lambda: os.getenv("EBAY_CLIENT_SECRET", "").strip())
    ebay_marketplace_id: str = field(
        default_factory=lambda: os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US").strip() or "EBAY_US"
    )
    ebay_delivery_country: str = field(
        default_factory=lambda: os.getenv("EBAY_DELIVERY_COUNTRY", "KR").strip()
    )
    rakuten_application_id: str = field(
        default_factory=lambda: os.getenv("RAKUTEN_APPLICATION_ID", "").strip()
    )
    rakuten_access_key: str = field(default_factory=lambda: os.getenv("RAKUTEN_ACCESS_KEY", "").strip())
    supplier_max_calls_per_run: int = field(
        default_factory=lambda: int(_get_float("SUPPLIER_MAX_CALLS_PER_RUN", 40))
    )
    supplier_max_queries_per_product: int = field(
        default_factory=lambda: int(_get_float("SUPPLIER_MAX_QUERIES_PER_PRODUCT", 3))
    )

    @property
    def naver_configured(self) -> bool:
        """True when both Naver credentials are present (values never exposed)."""
        return bool(self.naver_client_id.strip() and self.naver_client_secret.strip())

    @property
    def naver_shopping_has_own_keys(self) -> bool:
        """True when a dedicated Shopping Insight key pair is set (both parts)."""
        return bool(self.naver_shopping_client_id_raw and self.naver_shopping_client_secret_raw)

    @property
    def naver_shopping_client_id(self) -> str:
        return self.naver_shopping_client_id_raw if self.naver_shopping_has_own_keys else self.naver_client_id.strip()

    @property
    def naver_shopping_client_secret(self) -> str:
        return (
            self.naver_shopping_client_secret_raw
            if self.naver_shopping_has_own_keys
            else self.naver_client_secret.strip()
        )

    @property
    def naver_shopping_configured(self) -> bool:
        """Shopping Insight credentials available (dedicated pair, else the shared pair)."""
        return bool(self.naver_shopping_client_id and self.naver_shopping_client_secret)

    def ensure_directories(self) -> None:
        """Create data/exports directories if they do not yet exist."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


def load_scoring_config(path: Path | None = None) -> dict[str, Any]:
    """Load the YAML scoring configuration.

    Validates that scoring weights sum to 1.0 (within a small tolerance).
    """
    cfg_path = path or SCORING_CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    weights = config.get("weights", {})
    total = sum(float(v) for v in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"Scoring weights must sum to 1.0, got {total:.4f} ({weights})"
        )
    return config


# Singleton-style accessor.
settings = Settings()
