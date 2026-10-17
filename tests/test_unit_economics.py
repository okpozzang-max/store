"""Unit economics tests (PRD 12 unit tests #2, #3, #5)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.analysis.unit_economics import calculate_costs
from src.models.economics import CostInputs
from src.utils.currency import UnsupportedCurrencyError


def test_shipping_and_fees_included(converter):
    inputs = CostInputs(
        foreign_price=100.0,
        currency="USD",
        exchange_rate=1380.0,
        foreign_shipping_cost=10.0,
        international_shipping_krw=20000.0,
        domestic_shipping_krw=3000.0,
        payment_fee_pct=0.03,
        platform_fee_pct=0.10,
        customs_tax_krw=5000.0,
        other_costs_krw=1000.0,
        domestic_sale_price_krw=250000.0,
    )
    result = calculate_costs(inputs, converter, target_margin_pct=20.0)

    # product cost = 100 * 1380 = 138000; foreign shipping = 10 * 1380 = 13800
    assert result.product_cost_krw == pytest.approx(138000.0)
    assert result.breakdown["foreign_shipping_krw"] == pytest.approx(13800.0)
    # card fee applies to the overseas payment only: (138000 + 13800) * 0.03 = 4554
    assert result.breakdown["payment_fee_krw"] == pytest.approx(4554.0)
    # before sale fees = 151800 + 4554 + 20000 + 5000 + 1000 + 3000 (domestic) = 185354
    assert result.cost_before_sale_fees_krw == pytest.approx(185354.0)
    # + platform fee 250000 * 0.10 = 25000 -> total 210354
    assert result.total_cost_krw == pytest.approx(210354.0)
    assert result.estimated_profit_krw == pytest.approx(39646.0)
    # required sale price for 20% margin = 185354 / (1 - 0.10 - 0.20)
    assert result.required_sale_price_krw == pytest.approx(round(185354 / 0.7), abs=1)


def test_fx_buffer_and_return_allowance(converter):
    inputs = CostInputs(
        foreign_price=100.0, currency="USD", exchange_rate=1380.0,
        fx_buffer_pct=0.03, return_allowance_pct=0.05, domestic_sale_price_krw=200000.0,
    )
    r = calculate_costs(inputs, converter)
    assert r.breakdown["fx_buffer_krw"] == pytest.approx(138000 * 0.03)
    assert r.breakdown["return_allowance_krw"] == pytest.approx(10000.0)
    assert r.total_cost_krw == pytest.approx(138000 + 4140 + 10000)


def test_required_price_unreachable_when_fees_exceed_100pct():
    from src.analysis.unit_economics import required_sale_price

    assert required_sale_price(1000, 0.5, 0.2, 40.0) is None


def test_margin_calculation(converter):
    inputs = CostInputs(
        foreign_price=100.0,
        currency="USD",
        exchange_rate=1380.0,
        domestic_sale_price_krw=200000.0,
    )
    result = calculate_costs(inputs, converter)
    # total = product cost 138000; profit = 62000; margin = 31%
    assert result.estimated_margin_pct == pytest.approx(31.0)


def test_negative_margin_handled(converter):
    inputs = CostInputs(
        foreign_price=200.0,
        currency="USD",
        exchange_rate=1380.0,
        domestic_sale_price_krw=100000.0,
    )
    result = calculate_costs(inputs, converter)
    assert result.estimated_profit_krw < 0
    assert result.estimated_margin_pct < 0


def test_zero_price_rejected():
    with pytest.raises(ValidationError):
        CostInputs(
            foreign_price=0.0,
            currency="USD",
            exchange_rate=1380.0,
            domestic_sale_price_krw=100000.0,
        )


def test_negative_price_rejected():
    with pytest.raises(ValidationError):
        CostInputs(
            foreign_price=-10.0,
            currency="USD",
            exchange_rate=1380.0,
            domestic_sale_price_krw=100000.0,
        )


def test_unsupported_currency_via_converter(converter):
    inputs = CostInputs(
        foreign_price=100.0,
        currency="XYZ",
        exchange_rate=1.0,
        domestic_sale_price_krw=100000.0,
    )
    with pytest.raises(UnsupportedCurrencyError):
        calculate_costs(inputs, converter)
