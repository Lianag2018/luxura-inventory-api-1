import os
import sys
from typing import Any, Dict, Optional
from datetime import datetime, timezone

# Permet d'importer "app.*" quand on lance ce script directement
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from sqlmodel import Session, select  # type: ignore
from sqlalchemy.exc import IntegrityError

from app.db.session import engine
from app.models.product import Product
from app.services.wix_client import WixClient
from app.services.catalog_normalizer import normalize_product, normalize_variant


BATCH_SIZE = 10  # Réduit pour éviter les conflits


def _is_variant_record(prod: Product) -> bool:
    opts = prod.options if isinstance(prod.options, dict) else {}
    return bool(opts.get("wix_variant_id"))


def _safe_options(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _find_existing_product(db: Session, wix_id: Optional[str], sku: Optional[str]) -> Optional[Product]:
    """
    Trouve un produit existant par wix_id OU par SKU (non-null uniquement)
    """
    if wix_id:
        stmt = select(Product).where(Product.wix_id == wix_id)
        row = db.exec(stmt).first()
        if row:
            return row

    # Ne chercher par SKU que si SKU est non-vide
    if sku and sku.strip():
        stmt = select(Product).where(Product.sku == sku.strip())
        row = db.exec(stmt).first()
        if row:
            return row

    return None


def _find_existing_variant(
    db: Session,
    wix_id: Optional[str],
    sku: Optional[str],
    wix_variant_id: Optional[str],
) -> Optional[Product]:
    """
    Trouve une variante existante par wix_id, SKU ou wix_variant_id
    """
    # 1. Par wix_id
    if wix_id:
        stmt = select(Product).where(Product.wix_id == wix_id)
        found = db.exec(stmt).first()
        if found:
            return found

    # 2. Par SKU (non-vide uniquement)
    if sku and sku.strip():
        stmt = select(Product).where(Product.sku == sku.strip())
        found = db.exec(stmt).first()
        if found:
            return found

    # 3. Par wix_variant_id dans options (plus lent mais fiable)
    if wix_variant_id:
        stmt = select(Product)
        rows = db.exec(stmt).all()
        for row in rows:
            opts = row.options if isinstance(row.options, dict) else {}
            if opts.get("wix_variant_id") == wix_variant_id:
                return row

    return None


def _upsert_product(db: Session, existing: Optional[Product], data: Dict[str, Any]) -> Optional[Product]:
    """
    Upsert sécurisé avec gestion des conflits
    """
    clean_data = dict(data)

    if "options" in clean_data:
        clean_data["options"] = _safe_options(clean_data["options"])

    clean_data.pop("_track_quantity", None)
    clean_data.pop("_quantity", None)

    # Nettoyer SKU
    sku = clean_data.get("sku")
    if sku:
        sku = sku.strip()
        clean_data["sku"] = sku if sku else None
    else:
        clean_data["sku"] = None

    # Update timestamps
    clean_data["updated_at"] = datetime.now(timezone.utc)

    if existing:
        # UPDATE
        for field, value in clean_data.items():
            if field != "created_at":  # Ne pas écraser created_at
                setattr(existing, field, value)
        return existing
    else:
        # INSERT - vérifier encore une fois avant
        wix_id = clean_data.get("wix_id")
        sku = clean_data.get("sku")
        
        # Double-check pour éviter les doublons
        if wix_id:
            stmt = select(Product).where(Product.wix_id == wix_id)
            found = db.exec(stmt).first()
            if found:
                for field, value in clean_data.items():
                    if field != "created_at":
                        setattr(found, field, value)
                return found
        
        if sku:
            stmt = select(Product).where(Product.sku == sku)
            found = db.exec(stmt).first()
            if found:
                for field, value in clean_data.items():
                    if field != "created_at":
                        setattr(found, field, value)
                return found

        # Vraiment nouveau - créer
        clean_data["created_at"] = datetime.now(timezone.utc)
        prod = Product(**clean_data)
        db.add(prod)
        return prod


def main() -> None:
    client = WixClient()
    version, raw_products = client.query_products(limit=100)

    synced_parents = 0
    synced_variants = 0
    skipped_variants = 0
    errors = 0

    with Session(engine) as db:
        for wp in raw_products:
            try:
                parent_data = normalize_product(wp, version)
                parent_wix_id = parent_data.get("wix_id")
                parent_sku = parent_data.get("sku")

                if not parent_wix_id and not parent_sku:
                    continue

                # Chercher parent existant
                existing_parent = _find_existing_product(
                    db,
                    str(parent_wix_id).strip() if parent_wix_id else None,
                    str(parent_sku).strip() if parent_sku else None,
                )

                _upsert_product(db, existing_parent, parent_data)
                synced_parents += 1

                # Commit après chaque parent pour éviter les conflits batch
                try:
                    db.commit()
                except IntegrityError as e:
                    db.rollback()
                    print(f"[WARN] Conflit parent {parent_wix_id}: {e}")
                    errors += 1
                    continue

                # Traiter les variantes
                try:
                    variants = (
                        client.query_variants_v1(
                            product_id=str(parent_wix_id),
                            limit=100,
                        )
                        if parent_wix_id
                        else []
                    )
                except Exception as e:
                    print(f"[WARN] Impossible de récupérer les variantes pour {parent_wix_id}: {e}")
                    variants = []

                for variant in variants:
                    try:
                        variant_data = normalize_variant(wp, variant)
                        if not variant_data:
                            skipped_variants += 1
                            continue

                        variant_options = _safe_options(variant_data.get("options"))
                        wix_variant_id = variant_options.get("wix_variant_id")
                        variant_wix_id = variant_data.get("wix_id")
                        sku = variant_data.get("sku")

                        existing_variant = _find_existing_variant(
                            db=db,
                            wix_id=str(variant_wix_id).strip() if variant_wix_id else None,
                            sku=str(sku).strip() if sku else None,
                            wix_variant_id=str(wix_variant_id).strip() if wix_variant_id else None,
                        )

                        _upsert_product(db, existing_variant, variant_data)
                        synced_variants += 1

                        # Commit après chaque variante
                        db.commit()

                    except IntegrityError as e:
                        db.rollback()
                        print(f"[WARN] Conflit variante {sku}: {e}")
                        errors += 1
                    except Exception as e:
                        db.rollback()
                        print(f"[ERROR] Variante {sku}: {e}")
                        errors += 1

            except Exception as e:
                db.rollback()
                print(f"[ERROR] Produit: {e}")
                errors += 1

    print(
        f"[SYNC] Version catalogue: {version} | "
        f"Parents synchronisés: {synced_parents} | "
        f"Variantes synchronisées: {synced_variants} | "
        f"Variantes ignorées: {skipped_variants} | "
        f"Erreurs: {errors}"
    )


if __name__ == "__main__":
    main()
```

 votre repo GitHub, puis Render va redéployer automatiquement!**
