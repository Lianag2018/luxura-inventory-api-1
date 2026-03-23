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


FAKE_VARIANT_ID = "00000000-0000-0000-0000-000000000000"


def _safe_options(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _get_variant_id_from_product(prod: Product) -> Optional[str]:
    opts = prod.options if isinstance(prod.options, dict) else {}
    raw = opts.get("wix_variant_id")
    if not raw:
        return None
    value = str(raw).strip()
    return value or None


def _is_fake_variant_id(value: Optional[str]) -> bool:
    if not value:
        return True
    return value.strip() == FAKE_VARIANT_ID


def _is_variant_record(prod: Product) -> bool:
    variant_id = _get_variant_id_from_product(prod)
    return bool(variant_id and not _is_fake_variant_id(variant_id))


def _find_product_by_sku(db: Session, sku: Optional[str]) -> Optional[Product]:
    if not sku:
        return None
    with db.no_autoflush:
        stmt = select(Product).where(Product.sku == sku)
        return db.exec(stmt).first()


def _find_existing_parent(
    db: Session,
    wix_id: Optional[str],
    sku: Optional[str],
) -> Optional[Product]:
    """
    Parent:
    1. wix_id
    2. sku fallback
    """
    if wix_id:
        with db.no_autoflush:
            stmt = select(Product).where(Product.wix_id == wix_id)
            rows = db.exec(stmt).all()

        for row in rows:
            if not _is_variant_record(row):
                return row

    if sku:
        with db.no_autoflush:
            stmt = select(Product).where(Product.sku == sku)
            rows = db.exec(stmt).all()

        for row in rows:
            if not _is_variant_record(row):
                return row

    return None


def _find_existing_variant(
    db: Session,
    wix_variant_id: Optional[str],
    sku: Optional[str],
) -> Optional[Product]:
    """
    Variante:
    1. wix_variant_id (PRIMARY)
    2. sku (fallback)
    """
    if wix_variant_id and not _is_fake_variant_id(wix_variant_id):
        with db.no_autoflush:
            rows = db.exec(select(Product)).all()

        for row in rows:
            row_variant_id = _get_variant_id_from_product(row)
            if row_variant_id == wix_variant_id:
                return row

    if sku:
        with db.no_autoflush:
            stmt = select(Product).where(Product.sku == sku)
            found = db.exec(stmt).first()
            if found:
                return found

    return None


def _safe_insert_sku(
    db: Session,
    desired_sku: Optional[str],
    wix_id: Optional[str],
    wix_variant_id: Optional[str],
) -> str:
    """
    Garantit un SKU insérable sans collision.
    Si le SKU désiré existe déjà sur une autre ligne, on retombe
    sur un SKU stable dérivé de wix_id + wix_variant_id.
    """
    fallback = f"{(wix_id or '').strip()}:{(wix_variant_id or '').strip()}".strip(":")

    desired = (desired_sku or "").strip()
    if not desired:
        return fallback

    existing = _find_product_by_sku(db, desired)
    if not existing:
        return desired

    existing_variant_id = _get_variant_id_from_product(existing)
    if existing_variant_id and wix_variant_id and existing_variant_id == wix_variant_id:
        return desired

    return fallback or desired


def _apply_locked_sku(existing: Product, incoming_sku: Optional[str]) -> None:
    """
    LOCK SKU:
    - ne jamais écraser existing.sku
    - sauf si existing.sku est vide / None
    """
    clean_incoming = (incoming_sku or "").strip() or None
    current = (existing.sku or "").strip() or None

    if current is None and clean_incoming is not None:
        existing.sku = clean_incoming


def _update_existing_product(existing: Product, data: Dict[str, Any]) -> None:
    clean_data = dict(data)
    clean_data["options"] = _safe_options(clean_data.get("options"))
    clean_data.pop("_track_quantity", None)
    clean_data.pop("_quantity", None)

    for field, value in clean_data.items():
        if field == "sku":
            _apply_locked_sku(existing, value)
            continue
        setattr(existing, field, value)


def _create_new_product(
    db: Session,
    data: Dict[str, Any],
    *,
    is_variant: bool,
) -> Product:
    clean_data = dict(data)
    clean_data["options"] = _safe_options(clean_data.get("options"))
    clean_data.pop("_track_quantity", None)
    clean_data.pop("_quantity", None)

    if is_variant:
        variant_opts = clean_data.get("options") or {}
        wix_variant_id = variant_opts.get("wix_variant_id")
        wix_id = clean_data.get("wix_id")
        clean_data["sku"] = _safe_insert_sku(
            db=db,
            desired_sku=clean_data.get("sku"),
            wix_id=str(wix_id).strip() if wix_id else None,
            wix_variant_id=str(wix_variant_id).strip() if wix_variant_id else None,
        )

    prod = Product(**clean_data)
    db.add(prod)
    return prod


def _upsert_product(
    db: Session,
    existing: Optional[Product],
    data: Dict[str, Any],
    *,
    is_variant: bool,
) -> Product:
    if existing:
        _update_existing_product(existing, data)
        return existing

    return _create_new_product(db, data, is_variant=is_variant)


def main() -> None:
    client = WixClient()
    version, raw_products = client.query_products(limit=100)

    synced_parents = 0
    synced_variants = 0
    skipped_variants = 0
    errors = 0

    with Session(engine) as db:
        for wp in raw_products:
            # -------------------------
            # Parent
            # -------------------------
            try:
                parent_data = normalize_product(wp, version)
                parent_wix_id = parent_data.get("wix_id")
                parent_sku = parent_data.get("sku")

                if not parent_wix_id and not parent_sku:
                    continue

                with db.no_autoflush:
                    existing_parent = _find_existing_parent(
                        db=db,
                        wix_id=str(parent_wix_id).strip() if parent_wix_id else None,
                        sku=str(parent_sku).strip() if parent_sku else None,
                    )

                _upsert_product(db, existing_parent, parent_data, is_variant=False)
                db.commit()
                synced_parents += 1

            except Exception as e:
                db.rollback()
                errors += 1
                print(f"[WARN] Conflit parent {wp.get('id') or wp.get('_id')}: {e}")
                continue

            # -------------------------
            # Variantes
            # -------------------------
            try:
                variants = (
                    client.query_variants_v1(product_id=str(parent_wix_id), limit=100)
                    if parent_wix_id
                    else []
                )
            except Exception as e:
                errors += 1
                print(f"[WARN] Impossible de récupérer les variantes pour {parent_wix_id}: {e}")
                continue

            for variant in variants:
                try:
                    variant_data = normalize_variant(wp, variant)
                    if not variant_data:
                        skipped_variants += 1
                        continue

                    variant_options = _safe_options(variant_data.get("options"))
                    wix_variant_id = variant_options.get("wix_variant_id")
                    wix_variant_id = str(wix_variant_id).strip() if wix_variant_id else None

                    # IGNORER LES FAUX VARIANTS
                    if _is_fake_variant_id(wix_variant_id):
                        skipped_variants += 1
                        continue

                    sku = variant_data.get("sku")
                    clean_sku = str(sku).strip() if sku else None

                    with db.no_autoflush:
                        existing_variant = _find_existing_variant(
                            db=db,
                            wix_variant_id=wix_variant_id,
                            sku=clean_sku,
                        )

                    _upsert_product(db, existing_variant, variant_data, is_variant=True)
                    db.commit()
                    synced_variants += 1

                except Exception as e:
                    db.rollback()
                    errors += 1
                    print(
                        f"[WARN] Conflit variante "
                        f"{variant.get('sku') or variant.get('id') or variant.get('_id') or variant.get('variantId')}: {e}"
                    )

    print(
        f"[SYNC] Version catalogue: {version} | "
        f"Parents synchronisés: {synced_parents} | "
        f"Variantes synchronisées: {synced_variants} | "
        f"Variantes ignorées: {skipped_variants} | "
        f"Erreurs: {errors}"
    )


if __name__ == "__main__":
    main()
