"""Pytest fixtures and path setup."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.database import Database  # noqa: E402
from src.utils.currency import CurrencyConverter  # noqa: E402


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.connect()
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def converter() -> CurrencyConverter:
    conv = CurrencyConverter()
    conv.set_rate("USD", 1380.0, provider="test")
    conv.set_rate("EUR", 1490.0, provider="test")
    conv.set_rate("JPY", 9.1, provider="test")
    conv.set_rate("GBP", 1750.0, provider="test")
    return conv


@pytest.fixture
def scoring_config() -> dict:
    from config.settings import load_scoring_config

    return load_scoring_config()
