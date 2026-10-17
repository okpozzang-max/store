"""Collector base interface (PRD 4.4).

All collectors share a common contract: check support, collect, convert to the
common model format, record errors/throttling, and record provenance. A failure
in one collector must not abort the whole run (PRD 2.2 #12, 12-integration).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class CollectionResult(Generic[T]):
    """Outcome of a collection: successful records plus per-row errors."""

    source_name: str
    records: list[T] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    collected_at: datetime = field(default_factory=datetime.now)
    reference: Optional[str] = None  # e.g. file path / URL for provenance

    @property
    def records_collected(self) -> int:
        return len(self.records)

    @property
    def records_failed(self) -> int:
        return len(self.errors)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


class BaseCollector(abc.ABC, Generic[T]):
    """Abstract collector. Subclasses implement ``is_supported`` and ``collect``."""

    #: Human-readable source name recorded with provenance.
    source_name: str = "base"

    @abc.abstractmethod
    def is_supported(self, target: Any) -> bool:
        """Return True if this collector can handle ``target``."""

    @abc.abstractmethod
    def collect(self, target: Any) -> CollectionResult[T]:
        """Collect records from ``target`` and return a CollectionResult."""
