import os
import sys
from typing import Any, Dict, Optional

# Permet d'importer "app.*" quand on lance ce script directement
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from sqlmodel import Session, select  # type: ignore

from app.db.session import engine
from app.models.product import Product
from app.services.wix_client import WixClient
from app.services.catalog_normalizer import normalize_product, normalize_variant


BATCH_SIZE = 25


def _is_variant_record(prod: Product) -> bool:
    opts = prod.options if isinstance(prod.options, dict) else {}
    return bool(opts.get("wix_variant_id"))


def _safe_options(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _find_existing_parent(db: Session, wix_id: str) -> Optional[Product]:
    with db.no_autoflush:
        stmt = select(Product).where(Product.wix_id == wix_id)
        rows = db.exec(stmt).all()

    for row in rows:
        if not _is_variant_record(row):
            return row

    return None


def _find_existing_variant(
    db: Session,
    sku: Optional[str],
    wix_variant_id: Optional[str],
) -> Optional[Product]:
    if sku:
        with db.no_autoflush:
            stmt = select(Product).where(Product.sku == sku)
            found = db.exec(stmt).first()
        if found:
            return found

    if wix_variant_id:
        with db.no_autoflush:
            stmt = select(Product)
            rows = db.exec(stmt).all()

        for row in rows:
            opts = row.options if isinstance(row.options, dict) else {}
            if opts.get("wix_variant_id") == wix_variant_id:
                return row

    return None


def _upsert_product(db: Session, existing: Optional[Product], data: Dict[str, Any]) -> None:
    clean_data = dict(data)

    if "options" in clean_data:
        clean_data["options"] = _safe_options(clean_data["options"])

    clean_data.pop("_track_quantity", None)
    clean_data.pop("_quantity", None)

    if existing:
        for field, value in clean_data.items():
            setattr(existing, field, value)
    else:
        db.add(Product(**clean_data))


def main() -> None:
    client = WixClient()
    version, raw_products = client.query_products(limit=100)

    synced_parents = 0
    synced_variants = 0
    skipped_variants = 0
    processed_since_commit = 0

    with Session(engine) as db:
        for wp in raw_products:
            parent_data = normalize_product(wp, version)
            parent_wix_id = parent_data.get("wix_id")

            if not parent_wix_id:
                continue

            existing_parent = _find_existing_parent(db, str(parent_wix_id))
            _upsert_product(db, existing_parent, parent_data)
            synced_parents += 1
            processed_since_commit += 1

            try:
                variants = client.query_variants_v1(product_id=str(parent_wix_id), limit=100)
            except Exception as e:
                print(f"[WARN] Impossible de récupérer les variantes pour {parent_wix_id}: {e}")
                variants = []

            for variant in variants:
                variant_data = normalize_variant(wp, variant)
                if not variant_data:
                    skipped_variants += 1
                    continue

                variant_options = _safe_options(variant_data.get("options"))
                wix_variant_id = variant_options.get("wix_variant_id")
                sku = variant_data.get("sku")

                existing_variant = _find_existing_variant(
                    db=db,
                    sku=str(sku).strip() if sku else None,
                    wix_variant_id=str(wix_variant_id).strip() if wix_variant_id else None,
                )

                _upsert_product(db, existing_variant, variant_data)
                synced_variants += 1
                processed_since_commit += 1

                if processed_since_commit >= BATCH_SIZE:
                    db.commit()
                    processed_since_commit = 0

        if processed_since_commit > 0:
            db.commit()

    print(
        f"[SYNC] Version catalogue: {version} | "
        f"Parents synchronisés: {synced_parents} | "
        f"Variantes synchronisées: {synced_variants} | "
        f"Variantes ignorées: {skipped_variants}"
    )


if __name__ == "__main__":
    main()
