"""Currency conversion utilities (PRD 8.3).

An exchange rate table maps a currency code to KRW. Each rate records its
provider, the timestamp it was fetched, and the basis. Unsupported currencies
raise rather than being silently converted with an arbitrary rate.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


class UnsupportedCurrencyError(ValueError):
    """Raised when a conversion is requested for a currency with no rate."""


class InvalidRateError(ValueError):
    """Raised when a rate is non-positive or otherwise invalid."""


@dataclass(frozen=True)
class ExchangeRate:
    """A single currency -> KRW rate with provenance."""

    currency: str
    rate_to_krw: float  # 1 unit of `currency` == rate_to_krw KRW
    provider: str = "manual"
    fetched_at: Optional[datetime] = None
    basis: str = "spot"

    def __post_init__(self) -> None:
        if not math.isfinite(self.rate_to_krw) or self.rate_to_krw <= 0:
            raise InvalidRateError(
                f"rate_to_krw for {self.currency} must be finite and > 0, got {self.rate_to_krw}"
            )


class CurrencyConverter:
    """Converts foreign amounts into KRW using a registered rate table.

    KRW is always supported with an identity rate of 1.0.
    """

    BASE = "KRW"

    def __init__(self, rates: Optional[dict[str, ExchangeRate]] = None) -> None:
        self._rates: dict[str, ExchangeRate] = {}
        # KRW identity.
        self._rates[self.BASE] = ExchangeRate(self.BASE, 1.0, provider="identity")
        if rates:
            for rate in rates.values():
                self.register(rate)

    def register(self, rate: ExchangeRate) -> None:
        """Register or replace a rate for a currency (case-insensitive code)."""
        self._rates[rate.currency.upper()] = rate

    def set_rate(
        self,
        currency: str,
        rate_to_krw: float,
        provider: str = "manual",
        fetched_at: Optional[datetime] = None,
        basis: str = "spot",
    ) -> None:
        self.register(
            ExchangeRate(currency.upper(), rate_to_krw, provider, fetched_at, basis)
        )

    def is_supported(self, currency: str) -> bool:
        return currency.upper() in self._rates

    def get_rate(self, currency: str) -> ExchangeRate:
        code = currency.upper()
        if code not in self._rates:
            raise UnsupportedCurrencyError(
                f"No exchange rate registered for currency '{currency}'. "
                "Refusing to convert with an arbitrary rate."
            )
        return self._rates[code]

    def to_krw(self, amount: float, currency: str) -> float:
        """Convert ``amount`` in ``currency`` to KRW.

        Raises UnsupportedCurrencyError for unknown currencies and
        InvalidRateError-style ValueError for a non-finite amount.
        """
        if not math.isfinite(amount):
            raise ValueError("amount must be finite")
        rate = self.get_rate(currency)
        converted = amount * rate.rate_to_krw
        if not math.isfinite(converted):
            raise ValueError("converted amount must be finite")
        return converted


def load_converter_from_csv(path: str | Path) -> CurrencyConverter:
    """Build a CurrencyConverter from a rates CSV.

    Expected columns: currency, rate_to_krw, provider, fetched_at, basis.
    Rows with invalid/non-positive rates are skipped (they are not silently
    used with an arbitrary rate).
    """
    converter = CurrencyConverter()
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                fetched = row.get("fetched_at")
                converter.set_rate(
                    currency=(row.get("currency") or "").strip(),
                    rate_to_krw=float(row["rate_to_krw"]),
                    provider=(row.get("provider") or "csv").strip(),
                    fetched_at=datetime.fromisoformat(fetched) if fetched else None,
                    basis=(row.get("basis") or "spot").strip(),
                )
            except (KeyError, ValueError, InvalidRateError):
                continue
    return converter
