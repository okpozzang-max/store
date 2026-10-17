"""Repository layer: typed CRUD over the SQLite schema (PRD 5, 6).

Each repository maps between Pydantic models and SQLite rows. Supplier offers
and domestic offers are append-only for price observations (PRD 6.4): updating
a price inserts a new row instead of overwriting.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from ..models.demand import DataSource, DemandSignal
from ..models.economics import CollectionRun, Opportunity
from ..models.enums import (
    USER_DECIDED_STATUSES,
    USER_OFFER_DECISIONS,
    AvailabilityStatus,
    Confidence,
    KeywordLinkStatus,
    ReviewStatus,
    RunStatus,
    SignalType,
    SupplierType,
    TrustLevel,
    VerificationStatus,
)
from ..models.product import Product, ProductKeyword
from ..models.supplier import DomesticOffer, Supplier, SupplierOffer


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class _BaseRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn


class DataSourceRepository(_BaseRepo):
    def add(self, source: DataSource) -> int:
        cur = self.conn.execute(
            """INSERT INTO data_sources
               (name, source_type, country, access_method, terms_url,
                license_notes, enabled, last_success_at, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source.name,
                source.source_type,
                source.country,
                source.access_method,
                source.terms_url,
                source.license_notes,
                1 if source.enabled else 0,
                _iso(source.last_success_at),
                source.last_error,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get(self, source_id: int) -> Optional[DataSource]:
        row = self.conn.execute(
            "SELECT * FROM data_sources WHERE id = ?", (source_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def list_all(self) -> list[DataSource]:
        rows = self.conn.execute(
            "SELECT * FROM data_sources ORDER BY id"
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def get_by_name(self, name: str) -> Optional[DataSource]:
        row = self.conn.execute(
            "SELECT * FROM data_sources WHERE name = ? ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        return self._from_row(row) if row else None

    def ensure(self, source: DataSource) -> int:
        """Return the id of the source with this name, registering it if new.

        Existing user settings (enabled flag, notes) are left untouched.
        """
        existing = self.get_by_name(source.name)
        if existing is not None and existing.id is not None:
            return existing.id
        return self.add(source)

    def mark_success(self, source_id: int, at: Optional[datetime] = None) -> None:
        self.conn.execute(
            "UPDATE data_sources SET last_success_at = ?, last_error = NULL WHERE id = ?",
            (_iso(at or datetime.now()), source_id),
        )
        self.conn.commit()

    def mark_error(self, source_id: int, message: str) -> None:
        self.conn.execute(
            "UPDATE data_sources SET last_error = ? WHERE id = ?",
            (message[:500], source_id),
        )
        self.conn.commit()

    def set_enabled(self, source_id: int, enabled: bool) -> None:
        self.conn.execute(
            "UPDATE data_sources SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, source_id),
        )
        self.conn.commit()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DataSource:
        return DataSource(
            id=row["id"],
            name=row["name"],
            source_type=row["source_type"],
            country=row["country"],
            access_method=row["access_method"],
            terms_url=row["terms_url"],
            license_notes=row["license_notes"],
            enabled=bool(row["enabled"]),
            last_success_at=_parse_dt(row["last_success_at"]),
            last_error=row["last_error"],
            created_at=_parse_dt(row["created_at"]),
        )


class DemandSignalRepository(_BaseRepo):
    def add(self, signal: DemandSignal) -> int:
        cur = self.conn.execute(
            """INSERT INTO demand_signals
               (keyword, category, country, source_id, observed_at,
                period_start, period_end, demand_index, growth_pct,
                signal_type, confidence, raw_reference)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal.keyword,
                signal.category,
                signal.country,
                signal.source_id,
                _iso(signal.observed_at),
                _iso(signal.period_start),
                _iso(signal.period_end),
                signal.demand_index,
                signal.growth_pct,
                signal.signal_type.value,
                signal.confidence.value,
                signal.raw_reference,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def find_duplicate(self, signal: DemandSignal) -> Optional[int]:
        """Id of an existing observation with the same identity, if any.

        Identity = keyword + source + signal type + observation period, so a
        re-import of the same file does not duplicate rows.
        """
        row = self.conn.execute(
            """SELECT id FROM demand_signals
               WHERE keyword = ? AND source_id IS ? AND signal_type = ?
                 AND period_start IS ? AND period_end IS ?
               LIMIT 1""",
            (
                signal.keyword,
                signal.source_id,
                signal.signal_type.value,
                _iso(signal.period_start),
                _iso(signal.period_end),
            ),
        ).fetchone()
        return int(row["id"]) if row else None

    def add_if_new(self, signal: DemandSignal) -> tuple[int, bool]:
        existing = self.find_duplicate(signal)
        if existing is not None:
            return existing, False
        return self.add(signal), True

    def list_by_keyword(self, keyword: str) -> list[DemandSignal]:
        rows = self.conn.execute(
            "SELECT * FROM demand_signals WHERE keyword = ? ORDER BY observed_at",
            (keyword,),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def list_all(self) -> list[DemandSignal]:
        rows = self.conn.execute("SELECT * FROM demand_signals ORDER BY id").fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DemandSignal:
        return DemandSignal(
            id=row["id"],
            keyword=row["keyword"],
            category=row["category"],
            country=row["country"],
            source_id=row["source_id"],
            observed_at=_parse_dt(row["observed_at"]),
            period_start=_parse_dt(row["period_start"]),
            period_end=_parse_dt(row["period_end"]),
            demand_index=row["demand_index"],
            growth_pct=row["growth_pct"],
            signal_type=SignalType(row["signal_type"]),
            confidence=Confidence(row["confidence"]),
            raw_reference=row["raw_reference"],
        )


class ProductRepository(_BaseRepo):
    def add(self, product: Product) -> int:
        cur = self.conn.execute(
            """INSERT INTO products
               (brand, product_name, model_number, gtin, category, variant,
                country_of_origin, weight_kg, my_sale_price_krw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                product.brand,
                product.product_name,
                product.model_number,
                product.gtin,
                product.category,
                product.variant,
                product.country_of_origin,
                product.weight_kg,
                product.my_sale_price_krw,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get(self, product_id: int) -> Optional[Product]:
        row = self.conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def find_by_model_number(self, model_number: str) -> list[Product]:
        rows = self.conn.execute(
            "SELECT * FROM products WHERE model_number = ?", (model_number,)
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def find_by_gtin(self, gtin: str) -> list[Product]:
        rows = self.conn.execute(
            "SELECT * FROM products WHERE gtin = ?", (gtin,)
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def find_existing(self, product: Product) -> Optional[Product]:
        """Find the stored product this record refers to (GTIN > model > brand+name)."""
        if product.gtin:
            hits = self.find_by_gtin(product.gtin)
            if hits:
                return hits[0]
        if product.model_number:
            row = self.conn.execute(
                """SELECT * FROM products WHERE model_number = ?
                   AND (brand IS NULL OR ? IS NULL OR lower(brand) = lower(?))
                   ORDER BY id LIMIT 1""",
                (product.model_number, product.brand, product.brand),
            ).fetchone()
            if row:
                return self._from_row(row)
            return None
        row = self.conn.execute(
            """SELECT * FROM products WHERE lower(product_name) = lower(?)
               AND lower(COALESCE(brand, '')) = lower(COALESCE(?, ''))
               ORDER BY id LIMIT 1""",
            (product.product_name, product.brand),
        ).fetchone()
        return self._from_row(row) if row else None

    def upsert(self, product: Product) -> tuple[int, bool]:
        """Return (id, created). Re-importing the same product does not duplicate it;
        newly provided weight / own sale price values are applied to the stored product."""
        existing = self.find_existing(product)
        if existing is not None and existing.id is not None:
            updates = {
                k: getattr(product, k) for k in ("weight_kg", "my_sale_price_krw")
                if getattr(product, k) is not None and getattr(product, k) != getattr(existing, k)
            }
            if updates:
                self.update_fields(existing.id, **updates)
            return existing.id, False
        return self.add(product), True

    def update_fields(self, product_id: int, **fields: Optional[float]) -> None:
        allowed = {"weight_kg", "my_sale_price_krw"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        self.conn.execute(
            f"UPDATE products SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = datetime('now') WHERE id = ?",
            (*[fields[c] for c in cols], product_id),
        )
        self.conn.commit()

    def list_all(self) -> list[Product]:
        rows = self.conn.execute("SELECT * FROM products ORDER BY id").fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Product:
        return Product(
            id=row["id"],
            brand=row["brand"],
            product_name=row["product_name"],
            model_number=row["model_number"],
            gtin=row["gtin"],
            category=row["category"],
            variant=row["variant"],
            country_of_origin=row["country_of_origin"],
            weight_kg=row["weight_kg"] if "weight_kg" in row.keys() else None,
            my_sale_price_krw=row["my_sale_price_krw"] if "my_sale_price_krw" in row.keys() else None,
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )


def _b(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(bool(value))


def _rb(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


_SUPPLIER_OFFER_COLS = (
    "product_id", "source_id", "supplier_name", "supplier_country", "product_url",
    "foreign_price", "currency", "availability_status", "shipping_cost",
    "shipping_currency", "price_checked_at", "match_confidence", "verification_status",
    "supplier_type", "listing_title", "external_id", "item_condition",
    "shipping_cost_known", "ships_to_kr", "seller_rating", "seller_rating_count",
    "match_method", "match_reason", "search_query", "lead_time_days",
)


class SupplierOfferRepository(_BaseRepo):
    """Foreign offers. Append-only per price observation (PRD 6.4)."""

    @staticmethod
    def _values(o: SupplierOffer) -> tuple:
        return (
            o.product_id, o.source_id, o.supplier_name, o.supplier_country, o.product_url,
            o.foreign_price, o.currency, o.availability_status.value, o.shipping_cost,
            o.shipping_currency, _iso(o.price_checked_at), o.match_confidence,
            o.verification_status.value, o.supplier_type.value, o.listing_title,
            o.external_id, o.item_condition, int(o.shipping_cost_known), _b(o.ships_to_kr),
            o.seller_rating, o.seller_rating_count, o.match_method, o.match_reason,
            o.search_query, o.lead_time_days,
        )

    def add(self, offer: SupplierOffer) -> int:
        """Insert a new offer observation (never overwrites)."""
        cols = ", ".join(_SUPPLIER_OFFER_COLS)
        marks = ", ".join("?" for _ in _SUPPLIER_OFFER_COLS)
        cur = self.conn.execute(
            f"INSERT INTO supplier_offers ({cols}) VALUES ({marks})", self._values(offer)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_if_new(self, offer: SupplierOffer) -> tuple[int, bool]:
        """Append unless an identical observation (same check time) exists."""
        row = self.conn.execute(
            """SELECT id FROM supplier_offers
               WHERE product_id = ? AND supplier_name = ? AND product_url IS ?
                 AND foreign_price = ? AND currency = ? AND price_checked_at IS ?
               LIMIT 1""",
            (offer.product_id, offer.supplier_name, offer.product_url,
             offer.foreign_price, offer.currency, _iso(offer.price_checked_at)),
        ).fetchone()
        if row:
            return int(row["id"]), False
        return self.add(offer), True

    def latest_for_listing(
        self, product_id: int, supplier_name: str, product_url: Optional[str]
    ) -> Optional[SupplierOffer]:
        row = self.conn.execute(
            """SELECT * FROM supplier_offers
               WHERE product_id = ? AND supplier_name = ? AND product_url IS ?
               ORDER BY id DESC LIMIT 1""",
            (product_id, supplier_name, product_url),
        ).fetchone()
        return self._from_row(row) if row else None

    def record_observation(self, offer: SupplierOffer) -> tuple[int, str]:
        """Store a freshly collected observation.

        - The user's decision on this listing (verified / rejected) carries over.
        - Price / shipping / availability changed -> new row ('new' or 'changed').
        - Unchanged -> refresh ``price_checked_at`` of the latest row ('refreshed').
        """
        prev = self.latest_for_listing(offer.product_id, offer.supplier_name, offer.product_url)
        if prev is not None and prev.verification_status in USER_OFFER_DECISIONS:
            offer = offer.model_copy(update={"verification_status": prev.verification_status})
        if prev is not None and prev.id is not None:
            same = (
                prev.foreign_price == offer.foreign_price
                and prev.currency == offer.currency
                and prev.shipping_cost == offer.shipping_cost
                and prev.availability_status == offer.availability_status
            )
            if same:
                self.conn.execute(
                    """UPDATE supplier_offers SET price_checked_at = ?, match_confidence = ?,
                       match_method = ?, match_reason = ?, verification_status = ?
                       WHERE id = ?""",
                    (_iso(offer.price_checked_at), offer.match_confidence, offer.match_method,
                     offer.match_reason, offer.verification_status.value, prev.id),
                )
                self.conn.commit()
                return prev.id, "refreshed"
            return self.add(offer), "changed"
        return self.add(offer), "new"

    def set_status_for_listing(self, offer_id: int, status: VerificationStatus) -> None:
        """Apply a manual decision to every observation of the same listing."""
        o = self.get(offer_id)
        if o is None:
            return
        self.conn.execute(
            """UPDATE supplier_offers SET verification_status = ?
               WHERE product_id = ? AND supplier_name = ? AND product_url IS ?""",
            (status.value, o.product_id, o.supplier_name, o.product_url),
        )
        self.conn.commit()

    def get(self, offer_id: int) -> Optional[SupplierOffer]:
        row = self.conn.execute(
            "SELECT * FROM supplier_offers WHERE id = ?", (offer_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def list_by_product(self, product_id: int) -> list[SupplierOffer]:
        rows = self.conn.execute(
            "SELECT * FROM supplier_offers WHERE product_id = ? ORDER BY price_checked_at, id",
            (product_id,),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def list_all(self) -> list[SupplierOffer]:
        rows = self.conn.execute("SELECT * FROM supplier_offers ORDER BY id").fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SupplierOffer:
        keys = set(row.keys())

        def g(name: str, default: Any = None) -> Any:
            return row[name] if name in keys and row[name] is not None else default

        return SupplierOffer(
            id=row["id"],
            product_id=row["product_id"],
            source_id=row["source_id"],
            supplier_name=row["supplier_name"],
            supplier_country=row["supplier_country"],
            product_url=row["product_url"],
            foreign_price=row["foreign_price"],
            currency=row["currency"],
            availability_status=AvailabilityStatus(row["availability_status"]),
            shipping_cost=row["shipping_cost"],
            shipping_currency=row["shipping_currency"],
            price_checked_at=_parse_dt(row["price_checked_at"]),
            match_confidence=row["match_confidence"],
            verification_status=VerificationStatus(row["verification_status"]),
            supplier_type=SupplierType(g("supplier_type", "unknown")),
            listing_title=g("listing_title"),
            external_id=g("external_id"),
            item_condition=g("item_condition"),
            shipping_cost_known=bool(g("shipping_cost_known", 1)),
            ships_to_kr=_rb(g("ships_to_kr")),
            seller_rating=g("seller_rating"),
            seller_rating_count=g("seller_rating_count"),
            match_method=g("match_method"),
            match_reason=g("match_reason"),
            search_query=g("search_query"),
            lead_time_days=g("lead_time_days"),
        )


_DOMESTIC_OFFER_COLS = (
    "product_id", "seller_name", "product_url", "price_krw", "shipping_cost_krw",
    "product_condition", "match_confidence", "price_checked_at", "verification_status",
    "source_id", "listing_title", "external_id", "shipping_cost_known", "match_method",
    "match_reason",
)


class DomesticOfferRepository(_BaseRepo):
    @staticmethod
    def _values(o: DomesticOffer) -> tuple:
        return (
            o.product_id, o.seller_name, o.product_url, o.price_krw, o.shipping_cost_krw,
            o.product_condition, o.match_confidence, _iso(o.price_checked_at),
            o.verification_status.value, o.source_id, o.listing_title, o.external_id,
            int(o.shipping_cost_known), o.match_method, o.match_reason,
        )

    def add(self, offer: DomesticOffer) -> int:
        cols = ", ".join(_DOMESTIC_OFFER_COLS)
        marks = ", ".join("?" for _ in _DOMESTIC_OFFER_COLS)
        cur = self.conn.execute(
            f"INSERT INTO domestic_offers ({cols}) VALUES ({marks})", self._values(offer)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_if_new(self, offer: DomesticOffer) -> tuple[int, bool]:
        """Append a domestic price observation unless the identical one exists."""
        row = self.conn.execute(
            """SELECT id FROM domestic_offers
               WHERE product_id = ? AND seller_name = ? AND product_url IS ?
                 AND price_krw = ? AND shipping_cost_krw = ? AND price_checked_at IS ?
               LIMIT 1""",
            (offer.product_id, offer.seller_name, offer.product_url, offer.price_krw,
             offer.shipping_cost_krw, _iso(offer.price_checked_at)),
        ).fetchone()
        if row:
            return int(row["id"]), False
        return self.add(offer), True

    def latest_for_listing(
        self, product_id: int, seller_name: str, product_url: Optional[str]
    ) -> Optional[DomesticOffer]:
        row = self.conn.execute(
            """SELECT * FROM domestic_offers
               WHERE product_id = ? AND seller_name = ? AND product_url IS ?
               ORDER BY id DESC LIMIT 1""",
            (product_id, seller_name, product_url),
        ).fetchone()
        return self._from_row(row) if row else None

    def record_observation(self, offer: DomesticOffer) -> tuple[int, str]:
        """Same semantics as SupplierOfferRepository.record_observation."""
        prev = self.latest_for_listing(offer.product_id, offer.seller_name, offer.product_url)
        if prev is not None and prev.verification_status in USER_OFFER_DECISIONS:
            offer = offer.model_copy(update={"verification_status": prev.verification_status})
        if prev is not None and prev.id is not None:
            if prev.price_krw == offer.price_krw and prev.shipping_cost_krw == offer.shipping_cost_krw:
                self.conn.execute(
                    """UPDATE domestic_offers SET price_checked_at = ?, match_confidence = ?,
                       match_method = ?, match_reason = ?, verification_status = ?
                       WHERE id = ?""",
                    (_iso(offer.price_checked_at), offer.match_confidence, offer.match_method,
                     offer.match_reason, offer.verification_status.value, prev.id),
                )
                self.conn.commit()
                return prev.id, "refreshed"
            return self.add(offer), "changed"
        return self.add(offer), "new"

    def set_status_for_listing(self, offer_id: int, status: VerificationStatus) -> None:
        o = self.get(offer_id)
        if o is None:
            return
        self.conn.execute(
            """UPDATE domestic_offers SET verification_status = ?
               WHERE product_id = ? AND seller_name = ? AND product_url IS ?""",
            (status.value, o.product_id, o.seller_name, o.product_url),
        )
        self.conn.commit()

    def get(self, offer_id: int) -> Optional[DomesticOffer]:
        row = self.conn.execute(
            "SELECT * FROM domestic_offers WHERE id = ?", (offer_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def list_by_product(self, product_id: int) -> list[DomesticOffer]:
        rows = self.conn.execute(
            "SELECT * FROM domestic_offers WHERE product_id = ? ORDER BY price_checked_at, id",
            (product_id,),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def count_by_product(self, product_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM domestic_offers WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        return int(row["c"])

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DomesticOffer:
        keys = set(row.keys())

        def g(name: str, default: Any = None) -> Any:
            return row[name] if name in keys and row[name] is not None else default

        return DomesticOffer(
            id=row["id"],
            product_id=row["product_id"],
            seller_name=row["seller_name"],
            product_url=row["product_url"],
            price_krw=row["price_krw"],
            shipping_cost_krw=row["shipping_cost_krw"],
            product_condition=row["product_condition"],
            match_confidence=row["match_confidence"],
            price_checked_at=_parse_dt(row["price_checked_at"]),
            verification_status=VerificationStatus(g("verification_status", "unverified")),
            source_id=g("source_id"),
            listing_title=g("listing_title"),
            external_id=g("external_id"),
            shipping_cost_known=bool(g("shipping_cost_known", 1)),
            match_method=g("match_method"),
            match_reason=g("match_reason"),
        )


class SupplierRepository(_BaseRepo):
    """Supplier-level attributes (PRD 9.4). User edits are never overwritten
    by automatic registration."""

    def get_by_name(self, name: str) -> Optional[Supplier]:
        row = self.conn.execute("SELECT * FROM suppliers WHERE name = ?", (name,)).fetchone()
        return self._from_row(row) if row else None

    def ensure(
        self,
        name: str,
        country: Optional[str] = None,
        supplier_type: SupplierType = SupplierType.UNKNOWN,
        website: Optional[str] = None,
    ) -> Supplier:
        existing = self.get_by_name(name)
        if existing is not None:
            # Fill blanks only; never override user-set values.
            if existing.supplier_type == SupplierType.UNKNOWN and supplier_type != SupplierType.UNKNOWN:
                self.conn.execute(
                    "UPDATE suppliers SET supplier_type = ? WHERE id = ?",
                    (supplier_type.value, existing.id),
                )
            if not existing.country and country:
                self.conn.execute(
                    "UPDATE suppliers SET country = ? WHERE id = ?", (country, existing.id)
                )
            self.conn.commit()
            return self.get_by_name(name)  # type: ignore[return-value]
        self.conn.execute(
            "INSERT INTO suppliers (name, country, supplier_type, website) VALUES (?, ?, ?, ?)",
            (name, country, supplier_type.value, website),
        )
        self.conn.commit()
        return self.get_by_name(name)  # type: ignore[return-value]

    def update(self, supplier: Supplier) -> None:
        self.conn.execute(
            """UPDATE suppliers SET country = ?, supplier_type = ?, website = ?,
               trust_level = ?, ships_to_kr = ?, return_policy = ?,
               purchase_restrictions = ?, authenticity_notes = ?, notes = ?,
               updated_at = datetime('now') WHERE id = ?""",
            (supplier.country, supplier.supplier_type.value, supplier.website,
             supplier.trust_level.value, _b(supplier.ships_to_kr), supplier.return_policy,
             supplier.purchase_restrictions, supplier.authenticity_notes, supplier.notes,
             supplier.id),
        )
        self.conn.commit()

    def list_all(self) -> list[Supplier]:
        rows = self.conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Supplier:
        return Supplier(
            id=row["id"],
            name=row["name"],
            country=row["country"],
            supplier_type=SupplierType(row["supplier_type"]),
            website=row["website"],
            trust_level=TrustLevel(row["trust_level"]),
            ships_to_kr=_rb(row["ships_to_kr"]),
            return_policy=row["return_policy"],
            purchase_restrictions=row["purchase_restrictions"],
            authenticity_notes=row["authenticity_notes"],
            notes=row["notes"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )


class OpportunityRepository(_BaseRepo):
    """Opportunities are append-only per analysis run; the latest row per
    product is the current view, older rows are the scoring history."""

    def add(self, opp: Opportunity) -> int:
        cur = self.conn.execute(
            """INSERT INTO opportunities
               (product_id, supplier_offer_id, domestic_offer_id,
                demand_score, growth_score, price_score, margin_score,
                competition_score, total_score, estimated_profit_krw,
                estimated_margin_pct, risk_flags, confidence, review_status,
                score_basis)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opp.product_id,
                opp.supplier_offer_id,
                opp.domestic_offer_id,
                opp.demand_score,
                opp.growth_score,
                opp.price_score,
                opp.margin_score,
                opp.competition_score,
                opp.total_score,
                opp.estimated_profit_krw,
                opp.estimated_margin_pct,
                json.dumps(opp.risk_flags, ensure_ascii=False),
                opp.confidence.value,
                opp.review_status.value,
                json.dumps(opp.score_basis, ensure_ascii=False, default=str),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_all(self) -> list[Opportunity]:
        """All rows including history, highest score first."""
        rows = self.conn.execute(
            "SELECT * FROM opportunities ORDER BY total_score DESC, id"
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def list_latest(self) -> list[Opportunity]:
        """Most recent opportunity per product.

        Ordered for decision-making, not by raw score: evaluable and
        profitable candidates first, loss-making ones next, not-evaluable last;
        total score within each group (a high-demand loss-maker never tops the list).
        """
        rows = self.conn.execute(
            """SELECT * FROM opportunities
               WHERE id IN (SELECT MAX(id) FROM opportunities GROUP BY product_id)
               ORDER BY (estimated_margin_pct IS NULL), (estimated_margin_pct < 0),
                        total_score DESC, id"""
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def latest_for_product(self, product_id: int) -> Optional[Opportunity]:
        row = self.conn.execute(
            "SELECT * FROM opportunities WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        return self._from_row(row) if row else None

    def history_for_product(self, product_id: int) -> list[Opportunity]:
        rows = self.conn.execute(
            "SELECT * FROM opportunities WHERE product_id = ? ORDER BY id",
            (product_id,),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def update_review_status(self, opp_id: int, status: ReviewStatus) -> None:
        self.conn.execute(
            "UPDATE opportunities SET review_status = ? WHERE id = ?",
            (status.value, opp_id),
        )
        self.conn.commit()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Opportunity:
        keys = row.keys()
        basis_raw = row["score_basis"] if "score_basis" in keys else None
        return Opportunity(
            id=row["id"],
            product_id=row["product_id"],
            supplier_offer_id=row["supplier_offer_id"],
            domestic_offer_id=row["domestic_offer_id"],
            demand_score=row["demand_score"],
            growth_score=row["growth_score"],
            price_score=row["price_score"],
            margin_score=row["margin_score"],
            competition_score=row["competition_score"],
            total_score=row["total_score"],
            estimated_profit_krw=row["estimated_profit_krw"],
            estimated_margin_pct=row["estimated_margin_pct"],
            risk_flags=json.loads(row["risk_flags"]) if row["risk_flags"] else [],
            confidence=Confidence(row["confidence"]),
            review_status=ReviewStatus(row["review_status"]),
            calculated_at=_parse_dt(row["calculated_at"]),
            score_basis=json.loads(basis_raw) if basis_raw else {},
        )


_LINK_RANK = {
    KeywordLinkStatus.CANDIDATE: 0,
    KeywordLinkStatus.NEEDS_REVIEW: 1,
    KeywordLinkStatus.AUTO_LINKED: 2,
}


class ProductKeywordRepository(_BaseRepo):
    """Keyword candidates and keyword <-> product links (Phase 2)."""

    def get(self, keyword_id: int) -> Optional[ProductKeyword]:
        row = self.conn.execute(
            "SELECT * FROM product_keywords WHERE id = ?", (keyword_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def get_by_key(self, product_id: int, normalized_keyword: str) -> Optional[ProductKeyword]:
        row = self.conn.execute(
            "SELECT * FROM product_keywords WHERE product_id = ? AND normalized_keyword = ?",
            (product_id, normalized_keyword),
        ).fetchone()
        return self._from_row(row) if row else None

    def upsert(self, pk: ProductKeyword) -> tuple[int, bool]:
        """Insert or strengthen a keyword link. Returns (id, changed).

        - User decisions (confirmed / rejected) are never overwritten.
        - An existing link is only replaced by a stronger one
          (candidate < needs_review < auto_linked, then higher confidence).
        """
        existing = self.get_by_key(pk.product_id, pk.normalized_keyword)
        if existing is None:
            cur = self.conn.execute(
                """INSERT INTO product_keywords
                   (product_id, keyword, normalized_keyword, origin, match_method,
                    match_confidence, status, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pk.product_id,
                    pk.keyword,
                    pk.normalized_keyword,
                    pk.origin,
                    pk.match_method,
                    pk.match_confidence,
                    pk.status.value,
                    pk.reason,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid), True

        assert existing.id is not None
        if existing.status in USER_DECIDED_STATUSES:
            return existing.id, False
        new_rank, old_rank = _LINK_RANK[pk.status], _LINK_RANK[existing.status]
        stronger = new_rank > old_rank or (
            new_rank == old_rank and pk.match_confidence > existing.match_confidence
        )
        if not stronger:
            return existing.id, False
        self.conn.execute(
            """UPDATE product_keywords
               SET keyword = ?, origin = ?, match_method = ?, match_confidence = ?,
                   status = ?, reason = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (
                pk.keyword,
                pk.origin,
                pk.match_method,
                pk.match_confidence,
                pk.status.value,
                pk.reason,
                existing.id,
            ),
        )
        self.conn.commit()
        return existing.id, True

    def set_status(self, keyword_id: int, status: KeywordLinkStatus) -> None:
        """Record a manual decision (confirm / reject)."""
        self.conn.execute(
            "UPDATE product_keywords SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status.value, keyword_id),
        )
        self.conn.commit()

    def list_by_product(self, product_id: int) -> list[ProductKeyword]:
        rows = self.conn.execute(
            "SELECT * FROM product_keywords WHERE product_id = ? ORDER BY match_confidence DESC, id",
            (product_id,),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def list_by_status(self, status: KeywordLinkStatus) -> list[ProductKeyword]:
        rows = self.conn.execute(
            "SELECT * FROM product_keywords WHERE status = ? ORDER BY product_id, id",
            (status.value,),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def list_all(self) -> list[ProductKeyword]:
        rows = self.conn.execute(
            "SELECT * FROM product_keywords ORDER BY product_id, id"
        ).fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ProductKeyword:
        return ProductKeyword(
            id=row["id"],
            product_id=row["product_id"],
            keyword=row["keyword"],
            normalized_keyword=row["normalized_keyword"],
            origin=row["origin"],
            match_method=row["match_method"],
            match_confidence=row["match_confidence"],
            status=KeywordLinkStatus(row["status"]),
            reason=row["reason"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )


class CollectionRunRepository(_BaseRepo):
    def start(self, source_id: Optional[int] = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO collection_runs (source_id, started_at, status)
               VALUES (?, ?, ?)""",
            (source_id, datetime.now().isoformat(), RunStatus.RUNNING.value),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish(
        self,
        run_id: int,
        status: RunStatus,
        records_collected: int = 0,
        records_failed: int = 0,
        error_summary: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """UPDATE collection_runs
               SET finished_at = ?, status = ?, records_collected = ?,
                   records_failed = ?, error_summary = ?
               WHERE id = ?""",
            (
                datetime.now().isoformat(),
                status.value,
                records_collected,
                records_failed,
                error_summary,
                run_id,
            ),
        )
        self.conn.commit()

    def list_all(self) -> list[CollectionRun]:
        rows = self.conn.execute(
            "SELECT * FROM collection_runs ORDER BY id DESC"
        ).fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CollectionRun:
        return CollectionRun(
            id=row["id"],
            source_id=row["source_id"],
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            status=RunStatus(row["status"]),
            records_collected=row["records_collected"],
            records_failed=row["records_failed"],
            error_summary=row["error_summary"],
        )
