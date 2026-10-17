"""Storage, duplicate detection and pipeline integration tests.

Covers PRD 12 unit test #7 (duplicate model-number detection) and the
integration tests (CSV import -> DB -> analysis -> export; one source failing
does not lose other data).
"""
from __future__ import annotations

from pathlib import Path

from config.settings import load_scoring_config
from src.analysis.scoring import Scorer
from src.models.product import Product
from src.models.supplier import DomesticOffer, SupplierOffer
from src.services.pipeline import AnalysisPipeline
from src.storage.repositories import (
    DomesticOfferRepository,
    ProductRepository,
    SupplierOfferRepository,
)

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"


def test_duplicate_model_number_detection(db):
    repo = ProductRepository(db.connect())
    repo.add(Product(product_name="Widget A", model_number="M-1"))
    repo.add(Product(product_name="Widget A dup", model_number="M-1"))
    matches = repo.find_by_model_number("M-1")
    assert len(matches) == 2  # both detectable as same model number


def test_supplier_offer_append_only(db):
    conn = db.connect()
    prepo = ProductRepository(conn)
    srepo = SupplierOfferRepository(conn)
    pid = prepo.add(Product(product_name="P", model_number="X1"))
    srepo.add(SupplierOffer(product_id=pid, supplier_name="S", foreign_price=10, currency="USD"))
    srepo.add(SupplierOffer(product_id=pid, supplier_name="S", foreign_price=12, currency="USD"))
    offers = srepo.list_by_product(pid)
    assert len(offers) == 2  # price change creates a new row, not an overwrite


def test_pipeline_sample_import_and_analyze(db, converter):
    scorer = Scorer(load_scoring_config())
    pipeline = AnalysisPipeline(db, scorer, converter)
    summary = pipeline.run_from_directory(SAMPLE_DIR)

    assert summary.products_imported == 5
    assert summary.supplier_offers_imported == 5
    assert summary.domestic_offers_imported == 5
    assert summary.opportunities_created == 5

    opps = pipeline.opportunities.list_all()
    assert len(opps) == 5
    # Every opportunity has a score and a confidence (basis provided).
    for o in opps:
        assert 0.0 <= o.total_score <= 100.0
        assert o.confidence is not None


def test_pipeline_export_roundtrip(db, converter, tmp_path):
    import csv

    scorer = Scorer(load_scoring_config())
    pipeline = AnalysisPipeline(db, scorer, converter)
    pipeline.run_from_directory(SAMPLE_DIR)

    out = tmp_path / "opportunities.csv"
    n = pipeline.export_opportunities_csv(out)
    assert n == 5
    assert out.is_file()

    with open(out, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 5
    assert "total_score" in rows[0]


def test_one_missing_file_does_not_abort(db, converter, tmp_path):
    """Only products.csv present: products still import, no crash."""
    import shutil

    shutil.copy(SAMPLE_DIR / "products.csv", tmp_path / "products.csv")
    scorer = Scorer(load_scoring_config())
    pipeline = AnalysisPipeline(db, scorer, converter)
    summary = pipeline.run_from_directory(tmp_path)

    assert summary.products_imported == 5
    assert summary.supplier_offers_imported == 0
    assert summary.domestic_offers_imported == 0
    # No supplier/domestic offers => no opportunities, but run completes.
    assert summary.opportunities_created == 0


def test_deterministic_results(db, converter):
    """Same input => consistent scores (PRD 12 acceptance)."""
    scorer = Scorer(load_scoring_config())
    p1 = AnalysisPipeline(db, scorer, converter)
    p1.run_from_directory(SAMPLE_DIR)
    scores_a = sorted(o.total_score for o in p1.opportunities.list_all())

    # Fresh DB, same inputs.
    from src.storage.database import Database

    db2 = Database(":memory:")
    db2.connect()
    db2.init_schema()
    p2 = AnalysisPipeline(db2, scorer, converter)
    p2.run_from_directory(SAMPLE_DIR)
    scores_b = sorted(o.total_score for o in p2.opportunities.list_all())
    db2.close()

    assert scores_a == scores_b
