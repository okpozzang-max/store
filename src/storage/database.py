"""SQLite database schema and connection management (PRD 6).

Seven tables: data_sources, demand_signals, products, supplier_offers,
domestic_offers, opportunities, collection_runs. Prices are preserved per
observation (new rows, never overwritten). Foreign keys are enabled.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    country         TEXT,
    access_method   TEXT,
    terms_url       TEXT,
    license_notes   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_success_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS demand_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword       TEXT NOT NULL,
    category      TEXT,
    country       TEXT,
    source_id     INTEGER REFERENCES data_sources(id),
    observed_at   TEXT,
    period_start  TEXT,
    period_end    TEXT,
    demand_index  REAL NOT NULL,
    growth_pct    REAL,
    signal_type   TEXT NOT NULL DEFAULT 'search_interest',
    confidence    TEXT NOT NULL DEFAULT 'unverified',
    raw_reference TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    brand             TEXT,
    product_name      TEXT NOT NULL,
    model_number      TEXT,
    gtin              TEXT,
    category          TEXT,
    variant           TEXT,
    country_of_origin TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS supplier_offers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    source_id           INTEGER REFERENCES data_sources(id),
    supplier_name       TEXT NOT NULL,
    supplier_country    TEXT,
    product_url         TEXT,
    foreign_price       REAL NOT NULL,
    currency            TEXT NOT NULL,
    availability_status TEXT NOT NULL DEFAULT 'unknown',
    shipping_cost       REAL NOT NULL DEFAULT 0,
    shipping_currency   TEXT,
    price_checked_at    TEXT,
    match_confidence    REAL NOT NULL DEFAULT 0,
    verification_status TEXT NOT NULL DEFAULT 'unverified'
);

CREATE TABLE IF NOT EXISTS domestic_offers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id),
    seller_name       TEXT NOT NULL,
    product_url       TEXT,
    price_krw         REAL NOT NULL,
    shipping_cost_krw REAL NOT NULL DEFAULT 0,
    product_condition TEXT,
    match_confidence  REAL NOT NULL DEFAULT 0,
    price_checked_at  TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id           INTEGER NOT NULL REFERENCES products(id),
    supplier_offer_id    INTEGER REFERENCES supplier_offers(id),
    domestic_offer_id    INTEGER REFERENCES domestic_offers(id),
    demand_score         REAL NOT NULL,
    growth_score         REAL NOT NULL,
    price_score          REAL NOT NULL,
    margin_score         REAL NOT NULL,
    competition_score    REAL NOT NULL,
    total_score          REAL NOT NULL,
    estimated_profit_krw REAL,
    estimated_margin_pct REAL,
    risk_flags           TEXT,
    confidence           TEXT NOT NULL DEFAULT 'unverified',
    review_status        TEXT NOT NULL DEFAULT 'new',
    calculated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         INTEGER REFERENCES data_sources(id),
    started_at        TEXT,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'running',
    records_collected INTEGER NOT NULL DEFAULT 0,
    records_failed    INTEGER NOT NULL DEFAULT 0,
    error_summary     TEXT
);

CREATE INDEX IF NOT EXISTS idx_demand_keyword ON demand_signals(keyword);
CREATE INDEX IF NOT EXISTS idx_supplier_product ON supplier_offers(product_id);
CREATE INDEX IF NOT EXISTS idx_domestic_product ON domestic_offers(product_id);
CREATE INDEX IF NOT EXISTS idx_opportunity_product ON opportunities(product_id);
CREATE INDEX IF NOT EXISTS idx_products_model ON products(model_number);
CREATE INDEX IF NOT EXISTS idx_products_gtin ON products(gtin);

-- Phase 2: keyword candidates and demand <-> product links.
CREATE TABLE IF NOT EXISTS product_keywords (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id         INTEGER NOT NULL REFERENCES products(id),
    keyword            TEXT NOT NULL,
    normalized_keyword TEXT NOT NULL,
    origin             TEXT NOT NULL DEFAULT 'generated',
    match_method       TEXT,
    match_confidence   REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'candidate',
    reason             TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (product_id, normalized_keyword)
);
CREATE INDEX IF NOT EXISTS idx_product_keywords_norm ON product_keywords(normalized_keyword);
CREATE INDEX IF NOT EXISTS idx_product_keywords_status ON product_keywords(status);

-- Phase 3: supplier-level attributes for trust assessment (PRD 9.4).
CREATE TABLE IF NOT EXISTS suppliers (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL UNIQUE,
    country               TEXT,
    supplier_type         TEXT NOT NULL DEFAULT 'unknown',
    website               TEXT,
    trust_level           TEXT NOT NULL DEFAULT 'normal',
    ships_to_kr           INTEGER,
    return_policy         TEXT,
    purchase_restrictions TEXT,
    authenticity_notes    TEXT,
    notes                 TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after Phase 1. Applied with ALTER TABLE on existing databases.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("opportunities", "score_basis", "TEXT"),
    # Phase 3: supplier offer evidence
    ("supplier_offers", "supplier_type", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("supplier_offers", "listing_title", "TEXT"),
    ("supplier_offers", "external_id", "TEXT"),
    ("supplier_offers", "item_condition", "TEXT"),
    ("supplier_offers", "shipping_cost_known", "INTEGER NOT NULL DEFAULT 1"),
    ("supplier_offers", "ships_to_kr", "INTEGER"),
    ("supplier_offers", "seller_rating", "REAL"),
    ("supplier_offers", "seller_rating_count", "INTEGER"),
    ("supplier_offers", "match_method", "TEXT"),
    ("supplier_offers", "match_reason", "TEXT"),
    ("supplier_offers", "search_query", "TEXT"),
    # Phase 3: domestic offer evidence
    ("domestic_offers", "verification_status", "TEXT NOT NULL DEFAULT 'unverified'"),
    ("domestic_offers", "source_id", "INTEGER"),
    ("domestic_offers", "listing_title", "TEXT"),
    ("domestic_offers", "external_id", "TEXT"),
    ("domestic_offers", "shipping_cost_known", "INTEGER NOT NULL DEFAULT 1"),
    ("domestic_offers", "match_method", "TEXT"),
    ("domestic_offers", "match_reason", "TEXT"),
    # Expert review: cost & decision inputs
    ("products", "weight_kg", "REAL"),
    ("products", "my_sale_price_krw", "REAL"),
    ("supplier_offers", "lead_time_days", "INTEGER"),
]

POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS idx_supplier_offers_url ON supplier_offers(product_id, supplier_name, product_url);
CREATE INDEX IF NOT EXISTS idx_domestic_offers_url ON domestic_offers(product_id, seller_name, product_url);
"""


class Database:
    """Thin wrapper around a SQLite connection with schema management."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            # check_same_thread=False: Streamlit reruns the script on different
            # threads. The app is single-user/local, so one shared connection
            # is acceptable.
            self._conn = sqlite3.connect(
                self.db_path,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON;")
        return self._conn

    def init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        self._apply_migrations(conn)
        conn.executescript(POST_MIGRATION_SQL)
        conn.commit()

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the initial schema (idempotent)."""
        for table, column, decl in MIGRATIONS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        self.connect()
        self.init_schema()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
