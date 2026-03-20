from typing import Any, Dict, Optional


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    v = str(value).strip()
    return v or None


def _first_non_empty_str(*values: Any) -> Optional[str]:
    for v in values:
        s = _clean_str(v)
        if s:
            return s
    return None


def _normalize_options_payload(value: Any) -> Dict[str, Any]:
    """
    Normalise productOptions Wix pour qu'ils soient toujours stockés
    sous forme de dict compatible avec le champ Product.options.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"productOptions": value}
    return {}
    
    
def normalize_variant(parent: Dict[str, Any], variant: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    1 variant Wix = 1 Product Luxura
    Règle SKU:
      - si Wix fournit un SKU de variante non vide -> on le prend
      - sinon fallback stable: "<wix_product_id>:<wix_variant_id>"
    """
    wix_product_id = parent.get("id") or parent.get("_id")
    wix_variant_id = variant.get("id") or variant.get("_id") or variant.get("variantId")

    if not wix_product_id or not wix_variant_id:
        return None

    wix_product_id_s = str(wix_product_id).strip()
    wix_variant_id_s = str(wix_variant_id).strip()

    sku_field = variant.get("sku")  # peut être str OU dict OU vide

    # --- SKU variante (Wix peut le mettre à plusieurs endroits) ---
    sku = _first_non_empty_str(
        sku_field if isinstance(sku_field, str) else None,
        (variant.get("variant") or {}).get("sku"),  # ✅ le cas vu dans ton debug
        (sku_field or {}).get("value") if isinstance(sku_field, dict) else None,
        variant.get("stockKeepingUnit"),
        (variant.get("skuData") or {}).get("sku"),
        variant.get("vendorSku"),
        variant.get("itemNumber"),
    )

    if not sku:
        sku = f"{wix_product_id_s}:{wix_variant_id_s}"

    # --- choices/options ---
    choices = variant.get("choices") or variant.get("options") or {}
    if not isinstance(choices, dict):
        choices = {}

    # --- name ---
    base_name = _clean_str(parent.get("name")) or "Sans nom"
    suffix = " ".join(str(v) for v in choices.values() if v)
    name = f"{base_name} — {suffix}".strip(" —") if suffix else base_name

    # --- inventory (souvent pas fiable dans variants v1, mais on garde) ---
    inv = variant.get("inventory") or {}
    if not isinstance(inv, dict):
        inv = {}

    track = bool(inv.get("trackQuantity", False))

    qty_raw = inv.get("quantity", 0)
    try:
        qty = int(qty_raw or 0)
    except Exception:
        qty = 0

    in_stock = inv.get("inStock")
    if isinstance(in_stock, bool):
        is_in_stock = in_stock
    else:
        is_in_stock = qty > 0

    # --- price ---
    price_parent = (parent.get("priceData") or {}).get("price") or 0
    price_variant = (variant.get("priceData") or {}).get("price", price_parent) or 0
    try:
        price = float(price_variant or 0)
    except Exception:
        price = 0.0

    handle = _clean_str(parent.get("slug") or parent.get("handle") or parent.get("urlPart"))

    return {
        "wix_id": wix_product_id_s,
        "sku": sku,
        "name": name,
        "description": parent.get("description") or None,
        "price": price,
        "handle": handle,
        "is_in_stock": bool(is_in_stock),
        "quantity": int(qty),
        "options": {
            "wix_variant_id": wix_variant_id_s,
            "choices": choices,
        },
        "_track_quantity": track,
        "_quantity": qty,
    }


def normalize_product(wix_product: Dict[str, Any], version: str) -> Dict[str, Any]:
    """
    Transforme un produit Wix (parent) en dict compatible avec Product.
    NOTE: sku vide -> None pour éviter de casser un UNIQUE sur sku.
    """
    if version == "CATALOG_V1":
        inventory = (wix_product.get("inventory") or {}) or {}
        if not isinstance(inventory, dict):
            inventory = {}

        price_data = (wix_product.get("priceData") or {}) or {}
        if not isinstance(price_data, dict):
            price_data = {}

        sku = _clean_str(wix_product.get("sku"))

        try:
            price = float(price_data.get("price") or 0)
        except Exception:
            price = 0.0

        qty_raw = inventory.get("quantity") or 0
        try:
            qty = int(qty_raw or 0)
        except Exception:
            qty = 0

        return {
            "wix_id": wix_product.get("id") or wix_product.get("_id"),
            "sku": sku,
            "name": wix_product.get("name"),
            "price": price,
            "description": wix_product.get("description"),
            "handle": wix_product.get("slug"),
            "is_in_stock": bool(inventory.get("inStock", True)),
            "quantity": qty,
            "options": wix_product.get("productOptions") or {},
        }

    raise ValueError(f"Version de catalogue inconnue: {version}")
   
