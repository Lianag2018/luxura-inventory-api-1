import os
import sys

# Permet d'importer "app.*" quand on lance ce script directement
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from sqlmodel import Session, select  # type: ignore

from app.db.session import engine
from app.models.product import Product
from app.services.wix_client import WixClient
from app.services.catalog_normalizer import normalize_product


def main():
    client = WixClient()
    version, raw_products = client.query_products(limit=100)

    with Session(engine) as db:
        synced = 0

        for wp in raw_products:
            data = normalize_product(wp, version)
            wix_id = data.get("wix_id")
            if not wix_id:
                continue

            stmt = select(Product).where(Product.wix_id == wix_id)
            existing = db.exec(stmt).first()

            if existing:
                for field, value in data.items():
                    if field == "options" and not isinstance(value, dict):
                        value = {}
                    setattr(existing, field, value)
            else:
                if "options" in data and not isinstance(data["options"], dict):
                    data["options"] = {}
                db.add(Product(**data))

            synced += 1

        db.commit()

    print(f"[SYNC] Version catalogue: {version} – Produits synchronisés: {synced}")


if __name__ == "__main__":
    main()
