"""Currency conversion tests (PRD 12 unit tests #1, #5)."""
from __future__ import annotations

import pytest

from src.utils.currency import (
    CurrencyConverter,
    InvalidRateError,
    UnsupportedCurrencyError,
)


def test_krw_identity():
    conv = CurrencyConverter()
    assert conv.to_krw(1000, "KRW") == 1000
    assert conv.is_supported("KRW")


def test_usd_conversion(converter):
    assert converter.to_krw(10, "USD") == pytest.approx(13800.0)


def test_case_insensitive_currency(converter):
    assert converter.to_krw(1, "usd") == converter.to_krw(1, "USD")


def test_unsupported_currency_raises(converter):
    with pytest.raises(UnsupportedCurrencyError):
        converter.to_krw(100, "XYZ")


def test_invalid_rate_rejected():
    conv = CurrencyConverter()
    with pytest.raises(InvalidRateError):
        conv.set_rate("USD", 0.0)
    with pytest.raises(InvalidRateError):
        conv.set_rate("USD", -5.0)
    for invalid_rate in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(InvalidRateError):
            conv.set_rate("USD", invalid_rate)


def test_nan_amount_rejected(converter):
    with pytest.raises(ValueError):
        converter.to_krw(float("nan"), "USD")
    for invalid_amount in (float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            converter.to_krw(invalid_amount, "USD")
    with pytest.raises(ValueError, match="converted amount must be finite"):
        converter.to_krw(1e308, "USD")


def test_csv_loader_skips_non_finite_rates(tmp_path):
    from src.utils.currency import load_converter_from_csv

    rates = tmp_path / "rates.csv"
    rates.write_text(
        "currency,rate_to_krw\nUSD,nan\nEUR,inf\nGBP,-inf\nJPY,10\n",
        encoding="utf-8",
    )
    converter = load_converter_from_csv(rates)

    assert not converter.is_supported("USD")
    assert not converter.is_supported("EUR")
    assert not converter.is_supported("GBP")
    assert converter.to_krw(1, "JPY") == 10
