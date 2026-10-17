"""Unit economics: landed cost, profitability and required sale price (PRD 8).

Cost structure (all KRW):
    overseas_payment = item + seller shipping            (paid abroad by card)
    + card_fee       = overseas_payment * overseas_card_fee_pct
    + fx_buffer      = overseas_payment * fx_buffer_pct
    + international shipping (forwarder) + customs/VAT + packaging + other
    + domestic shipping
    = cost_before_sale_fees
    + platform_fee   = sale_price * platform_fee_pct     (deducted from the sale)
    + return_allowance = sale_price * return_allowance_pct
    = total_cost;  profit = sale_price - total_cost

Required sale price for a target margin m:
    P = cost_before_sale_fees / (1 - platform_fee_pct - return_allowance_pct - m)

The CurrencyConverter rejects unsupported currencies instead of converting
them with an arbitrary rate.
"""
from __future__ import annotations

from typing import Optional

from ..models.economics import CostInputs, CostResult
from ..utils.currency import CurrencyConverter


def required_sale_price(
    cost_before_sale_fees: float, platform_fee_pct: float, return_allowance_pct: float,
    target_margin_pct: float,
) -> Optional[float]:
    """Sale price at which net margin equals the target; None if unreachable."""
    denom = 1.0 - platform_fee_pct - return_allowance_pct - target_margin_pct / 100.0
    if denom <= 0:
        return None
    return cost_before_sale_fees / denom


def calculate_costs(
    inputs: CostInputs,
    converter: CurrencyConverter | None = None,
    target_margin_pct: Optional[float] = None,
) -> CostResult:
    if converter is not None:
        rate = converter.get_rate(inputs.currency).rate_to_krw
        product_cost = converter.to_krw(inputs.foreign_price, inputs.currency)
        foreign_shipping_krw = converter.to_krw(inputs.foreign_shipping_cost, inputs.currency)
    else:
        rate = inputs.exchange_rate
        product_cost = inputs.foreign_price * rate
        foreign_shipping_krw = inputs.foreign_shipping_cost * rate

    overseas_payment = product_cost + foreign_shipping_krw
    payment_fee = overseas_payment * inputs.payment_fee_pct
    fx_buffer = overseas_payment * inputs.fx_buffer_pct

    cost_before_sale_fees = (
        overseas_payment
        + payment_fee
        + fx_buffer
        + inputs.international_shipping_krw
        + inputs.customs_tax_krw
        + inputs.other_costs_krw
        + inputs.domestic_shipping_krw
    )
    sale = inputs.domestic_sale_price_krw
    platform_fee = sale * inputs.platform_fee_pct
    return_allowance = sale * inputs.return_allowance_pct
    total_cost = cost_before_sale_fees + platform_fee + return_allowance

    profit = sale - total_cost
    margin_pct = profit / sale * 100.0
    required = (
        required_sale_price(cost_before_sale_fees, inputs.platform_fee_pct,
                            inputs.return_allowance_pct, target_margin_pct)
        if target_margin_pct is not None else None
    )

    breakdown = {
        "product_cost_krw": round(product_cost, 2),
        "foreign_shipping_krw": round(foreign_shipping_krw, 2),
        "payment_fee_krw": round(payment_fee, 2),
        "fx_buffer_krw": round(fx_buffer, 2),
        "international_shipping_krw": round(inputs.international_shipping_krw, 2),
        "customs_tax_krw": round(inputs.customs_tax_krw, 2),
        "other_costs_krw": round(inputs.other_costs_krw, 2),
        "domestic_shipping_krw": round(inputs.domestic_shipping_krw, 2),
        "platform_fee_krw": round(platform_fee, 2),
        "return_allowance_krw": round(return_allowance, 2),
        "exchange_rate_krw_per_unit": rate,
        "domestic_sale_price_krw": round(sale, 2),
    }
    return CostResult(
        product_cost_krw=round(product_cost, 2),
        cost_before_sale_fees_krw=round(cost_before_sale_fees, 2),
        total_cost_krw=round(total_cost, 2),
        estimated_profit_krw=round(profit, 2),
        estimated_margin_pct=round(margin_pct, 2),
        required_sale_price_krw=round(required, 0) if required is not None else None,
        breakdown=breakdown,
    )
