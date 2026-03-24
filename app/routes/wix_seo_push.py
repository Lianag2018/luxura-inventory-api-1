import os
import re
import html
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db.session import get_session
from app.models.product import Product

router = APIRouter(prefix="/wix", tags=["wix-seo-push"])

WIX_API_BASE = "https://www.wixapis.com"
DEFAULT_PUBLIC_BASE = "https://luxura-inventory-api.onrender.com"

WIX_PUSH_SECRET_ENV = "WIX_PUSH_SECRET"

COLOR_MAP: Dict[str, Dict[str, str]] = {
    "1": {"luxe": "Onyx Noir", "sku": "ONYX-NOIR"},
    "1B": {"luxe": "Noir Soie", "sku": "NOIR-SOIE"},
    "2": {"luxe": "Espresso Intense", "sku": "ESPRESSO-INTENSE"},
    "3": {"luxe": "Châtaigne Douce", "sku": "CHATAIGNE-DOUCE"},
    "6": {"luxe": "Caramel Doré", "sku": "CARAMEL-DORE"},
    "6/24": {"luxe": "Golden Hour", "sku": "GOLDEN-HOUR"},
    "6/6T24": {"luxe": "Caramel Soleil", "sku": "CARAMEL-SOLEIL"},
    "18/22": {"luxe": "Champagne Doré", "sku": "CHAMPAGNE-DORE"},
    "60A": {"luxe": "Platine Pur", "sku": "PLATINE-PUR"},
    "HPS": {"luxe": "Cendré Étoilé", "sku": "CENDRE-ETOILE"},
    "CB": {"luxe": "Miel Sauvage Ombré", "sku": "MIEL-SAUVAGE-OMBRE"},
    "DB": {"luxe": "Nuit Mystère", "sku": "NUIT-MYSTERE"},
    "DC": {"luxe": "Chocolat Profond", "sku": "CHOCOLAT-PROFOND"},
    "PHA": {"luxe": "Cendré Céleste", "sku": "CENDRE-CELESTE"},
    "613/18A": {"luxe": "Diamant Glacé", "sku": "DIAMANT-GLACE"},
    "CACAO": {"luxe": "Cacao Velours", "sku": "CACAO-VELOURS"},
    "CINNAMON": {"luxe": "Cannelle Épicée", "sku": "CANNELLE-EPICEE"},
}

TYPE_META = {
    "halo": {"label": "Halo", "series": "Everly", "prefix": "H"},
    "genius": {"label": "Genius", "series": "Vivian", "prefix": "G"},
    "tape": {"label": "Tape", "series": "Aurora", "prefix": "T"},
    "i-tip": {"label": "I-Tip", "series": "Eleanor", "prefix": "I"},
}

FAKE_VARIANT_ID = "00000000-0000-0000-0000-000000000000"


class PushRequest(BaseModel):
    product_ids: Optional[List[int]] = None
    category: Optional[str] = None
    limit: int = Field(default=20, ge=1, le=500)
    confirm: bool = False
    secret: Optional[str] = None
    include_info_sections: bool = True


# -------------------------
# Auth / token
# -------------------------
def _get_instance_id() -> str:
    instance_id = (os.getenv("WIX_INSTANCE_ID") or "").strip()
    if not instance_id:
        raise HTTPException(500, "Missing env: WIX_INSTANCE_ID")
    return instance_id


def _get_public_base_url() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE).strip().rstrip("/")


def _require_secret(secret: Optional[str]) -> None:
    expected = (os.getenv(WIX_PUSH_SECRET_ENV) or "").strip()
    if not expected:
        raise HTTPException(500, f"Missing env: {WIX_PUSH_SECRET_ENV}")
    if (secret or "").strip() != expected:
        raise HTTPException(403, "Invalid secret")


def _fetch_access_token(instance_id: str) -> str:
    base = _get_public_base_url()
    try:
        token_res = requests.post(
            f"{base}/wix/token",
            params={"instance_id": instance_id},
            timeout=30,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"Token fetch network error: {e}")

    if not token_res.ok:
        raise HTTPException(502, f"Token fetch failed: {token_res.status_code} {token_res.text}")

    try:
        data = token_res.json()
    except ValueError:
        raise HTTPException(502, f"Token fetch invalid JSON: {token_res.text[:500]}")

    access_token = (data.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(502, "No access_token returned by /wix/token")

    return access_token


def _headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# -------------------------
# Local product helpers
# -------------------------
def _safe_options(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _get_variant_id(prod: Product) -> Optional[str]:
    opts = _safe_options(prod.options)
    raw = opts.get("wix_variant_id")
    if not raw:
        return None
    val = str(raw).strip()
    if not val or val == FAKE_VARIANT_ID:
        return None
    return val


def _is_variant(prod: Product) -> bool:
    return bool(_get_variant_id(prod))


def _first(items: List[Any]) -> Any:
    return items[0] if items else None


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = value.replace("é", "e").replace("è", "e").replace("ê", "e")
    value = value.replace("à", "a").replace("â", "a")
    value = value.replace("î", "i").replace("ï", "i")
    value = value.replace("ô", "o")
    value = value.replace("ù", "u").replace("û", "u")
    value = value.replace("ç", "c")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _infer_type_and_series(prod: Product) -> Tuple[str, str, str]:
    hay = " ".join([
        prod.name or "",
        prod.handle or "",
        " ".join(_safe_options(prod.options).get("categories", []) or []),
    ]).lower()

    if "halo" in hay:
        meta = TYPE_META["halo"]
    elif "genius" in hay:
        meta = TYPE_META["genius"]
    elif "i-tip" in hay or "itip" in hay or "i tip" in hay:
        meta = TYPE_META["i-tip"]
    elif "bande adh" in hay or "tape" in hay or "aurora" in hay:
        meta = TYPE_META["tape"]
    else:
        meta = TYPE_META["genius"]

    return meta["label"], meta["series"], meta["prefix"]


def _extract_color_code(prod: Product) -> Optional[str]:
    text = " ".join([
        prod.name or "",
        prod.sku or "",
        prod.handle or "",
    ])

    # d'abord dans le nom #CODE
    m = re.search(r"#\s*([A-Za-z0-9/]+)", text)
    if m:
        return m.group(1).strip().upper()

    # ensuite dans un ancien SKU type H16120#1
    if prod.sku:
        m2 = re.search(r"#([A-Za-z0-9/]+)$", prod.sku.strip(), flags=re.IGNORECASE)
        if m2:
            return m2.group(1).strip().upper()

    return None


def _extract_length_weight_from_variant(prod: Product) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Lit la longueur/poids depuis les choices locales DB.
    Ex:
    '16" 120 grammes' -> ('16', '120', '16" 120 grammes')
    """
    opts = _safe_options(prod.options)
    choices = _safe_options(opts.get("choices"))

    raw = (
        choices.get("Longeur")
        or choices.get("Longueur")
        or choices.get("longeur")
        or choices.get("longueur")
        or ""
    )
    raw = str(raw).strip()

    if not raw:
        name = prod.name or ""
        m_name = re.search(r'(\d{2})["\'″]?\s*(\d{2,3})\s*gram', name, flags=re.IGNORECASE)
        if m_name:
            length = m_name.group(1)
            weight = m_name.group(2)
            return length, weight, f'{length}" {weight} grammes'
        return None, None, None

    m = re.search(r'(\d{2})["\'″]?\s*(\d{2,3})\s*gram', raw, flags=re.IGNORECASE)
    if not m:
        return None, None, raw

    length = m.group(1)
    weight = m.group(2)
    return length, weight, raw
    

def _color_meta(code: Optional[str]) -> Dict[str, str]:
    if not code:
        return {"luxe": "Couleur Signature", "sku": "COULEUR-SIGNATURE"}
    return COLOR_MAP.get(code.upper(), {
        "luxe": code.upper(),
        "sku": _slugify(code.upper()).upper().replace("-", "-"),
    })


def _build_product_name(prod: Product) -> str:
    product_type, series, _prefix = _infer_type_and_series(prod)
    color_code = _extract_color_code(prod) or "?"
    color = _color_meta(color_code)
    return f"{product_type} {series} {color['luxe']} #{color_code}"


def _build_variant_sku(prod: Product) -> Optional[str]:
    product_type, _series, prefix = _infer_type_and_series(prod)
    _ = product_type  # silence
    color_code = _extract_color_code(prod)
    if not color_code:
        return None

    length, weight, _raw = _extract_length_weight_from_variant(prod)
    if not length or not weight:
        return None

    color = _color_meta(color_code)
    return f"{prefix}-{length}-{weight}-{color_code.upper()}-{color['sku']}"


def _extract_length_weight_from_choice_value(raw: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Lit la longueur/poids depuis la vraie variante Wix.
    Ex:
    '16" 120 grammes' -> ('16', '120', '16" 120 grammes')
    '20" 140 grammes' -> ('20', '140', '20" 140 grammes')
    """
    text = str(raw or "").strip()
    if not text:
        return None, None, None

    m = re.search(r'(\d{2})["\'″]?\s*(\d{2,3})\s*gram', text, flags=re.IGNORECASE)
    if not m:
        return None, None, text

    return m.group(1), m.group(2), text


def _get_wix_variant_choice_value(wix_variant: Dict[str, Any]) -> Optional[str]:
    choices = wix_variant.get("choices") or wix_variant.get("options") or {}
    if not isinstance(choices, dict):
        choices = {}

    raw = (
        choices.get("Longeur")
        or choices.get("Longueur")
        or choices.get("longeur")
        or choices.get("longueur")
        or ""
    )
    raw = str(raw).strip()
    return raw or None


def _build_variant_sku_from_wix_variant(
    parent: Product,
    wix_variant: Dict[str, Any],
) -> Optional[str]:
    """
    Construit le SKU depuis la VRAIE variante Wix.
    Ex:
    H-16-120-1B-NOIR-SOIE
    H-20-140-1B-NOIR-SOIE
    """
    _product_type, _series, prefix = _infer_type_and_series(parent)

    color_code = _extract_color_code(parent)
    if not color_code:
        return None

    raw_choice = _get_wix_variant_choice_value(wix_variant)
    length, weight, _raw = _extract_length_weight_from_choice_value(raw_choice)
    if not length or not weight:
        return None

    color = _color_meta(color_code)
    clean_code = color_code.upper().replace("/", "-")
    sku_name = color.get("sku", clean_code)

    return f"{prefix}-{length}-{weight}-{clean_code}-{sku_name}"


def _prepare_variant_updates_from_wix(
    parent: Product,
    wix_variants: List[Dict[str, Any]],
    current_variant_skus: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Prépare les mises à jour variantes à partir des vraies variantes Wix.
    """
    updates: List[Dict[str, Any]] = []
    seen_target_skus = set()

    for wix_variant in wix_variants:
        variant_id = str(
            wix_variant.get("id")
            or wix_variant.get("_id")
            or wix_variant.get("variantId")
            or ""
        ).strip()

        if not variant_id:
            continue

        raw_choice = _get_wix_variant_choice_value(wix_variant)
        target_sku = _build_variant_sku_from_wix_variant(parent, wix_variant)
        current_sku = ""
        if current_variant_skus:
            current_sku = current_variant_skus.get(variant_id, "")

        if not target_sku:
            updates.append({
                "variant_id": variant_id,
                "choice": raw_choice,
                "current_sku": current_sku,
                "target_sku": None,
                "status": "skipped_missing_parts",
            })
            continue

        if target_sku in seen_target_skus:
            updates.append({
                "variant_id": variant_id,
                "choice": raw_choice,
                "current_sku": current_sku,
                "target_sku": target_sku,
                "status": "skipped_duplicate_target_in_batch",
            })
            continue

        seen_target_skus.add(target_sku)

        updates.append({
            "variant_id": variant_id,
            "choice": raw_choice,
            "current_sku": current_sku,
            "target_sku": target_sku,
            "status": "planned",
        })

    return updates
    
    
def _html_escape(v: str) -> str:
    return html.escape(v or "", quote=True)


def _build_description_html(prod: Product) -> str:
    product_type, series, _prefix = _infer_type_and_series(prod)
    color_code = _extract_color_code(prod) or ""
    color = _color_meta(color_code)
    luxe_name = color.get("luxe", color_code)

    return f"""
Extensions {product_type} - Volume instantané sans engagement par Luxura.


🎯 CONCEPT UNIQUE:
• Fil invisible ajustable qui repose sur votre tête  
• Aucune fixation permanente - 100% réversible  
• Application en moins de 2 minutes  
• Retrait instantané sans aide professionnelle  


💎 QUALITÉ PREMIUM:
• 100% cheveux humains vierges Remy  
• Cuticules intactes pour un mouvement naturel  
• Série {series} - Collection professionnelle Luxura  
• Teinte: {luxe_name} #{color_code}  


✨ AVANTAGES UNIQUES:
• Zéro dommage aux cheveux naturels  
• Parfait pour usage quotidien ou occasionnel  
• Idéal pour cheveux fins ou fragiles  
• Durée de vie: 12 mois et plus avec bon entretien  


📍 APPLICATION:
Auto-application - Aucune aide requise  


📍 DISPONIBLE AU QUÉBEC:
Extensions capillaires Québec  
Extensions cheveux Montréal  
Extensions capillaires Laval  
Extensions cheveux Lévis  
Extensions capillaires Trois-Rivières  
Extensions cheveux Beauce  
Extensions capillaires Sainte-Marie  


Luxura Distribution - Extensions professionnelles haut de gamme.
"""


def _build_info_sections(prod: Product) -> List[Dict[str, str]]:
    product_type, series, _prefix = _infer_type_and_series(prod)
    label = f"{product_type} {series}".strip()

    # Formats possibles à partir des variantes locales du même wix_id
    # sera surchargé plus tard si on a la liste complète
    return [
        {
            "key": "description",
            "title": "Description",
            "plainDescription": (
                f"Installation rapide et facile. Extensions {label} confortables et discrètes. "
                f"Cheveux 100% naturels Remy à cuticules alignées."
            ),
        },
        {
            "key": "a-propos",
            "title": "À propos",
            "plainDescription": (
                "Extensions Luxura 100% cheveux naturels Remy. Durée de vie 9 à 12 mois "
                "(semi-permanent) ou jusqu'à 2 ans pour certaines pièces prêt-à-porter selon l'entretien."
            ),
        },
        {
            "key": "recommandation",
            "title": "Recommandation",
            "plainDescription": "1 paquet pour volume naturel, 2 paquets pour volume maximum.",
        },
        {
            "key": "format",
            "title": "Format",
            "plainDescription": "16 pouces (120g) | 20 pouces (140g)",
        },
        {
            "key": "entretien",
            "title": "Entretien",
            "plainDescription": (
                "Shampooing doux sans sulfates. Lavage 1x/semaine. Masque hydratant 1x sur 2. "
                "Démêler avec un peigne à grosses dents en commençant par les pointes. "
                "Séchage à l'air libre ou chaleur modérée."
            ),
        },
        {
            "key": "precommande",
            "title": "Précommande",
            "plainDescription": "Pré-commandes acceptées. Notification à l'arrivée du stock.",
        },
    ]


def _build_info_sections_with_formats(parent: Product, variants: List[Product]) -> List[Dict[str, str]]:
    sections = _build_info_sections(parent)
    formats = []

    for v in variants:
        length, weight, _raw = _extract_length_weight_from_variant(v)
        if length and weight:
            formats.append(f'{length} pouces ({weight}g)')

    formats = sorted(set(formats))
    if formats:
        for s in sections:
            if s["key"] == "format":
                s["plainDescription"] = " | ".join(formats)

    return sections


def _filter_local_products(
    db: Session,
    product_ids: Optional[List[int]],
    category: Optional[str],
    limit: int,
) -> List[Product]:
    rows = db.exec(select(Product)).all()

    if product_ids:
        wanted = set(product_ids)
        rows = [r for r in rows if r.id in wanted]

    if category:
        cat = category.strip().lower()
        filtered: List[Product] = []
        for r in rows:
            product_type, _series, _prefix = _infer_type_and_series(r)
            if product_type.lower() == cat:
                filtered.append(r)
        rows = filtered

    parents = [r for r in rows if r.wix_id and not _is_variant(r)]
    parents = parents[:limit]
    return parents


def _collect_family(db: Session, parent: Product) -> Tuple[Product, List[Product]]:
    rows = db.exec(select(Product).where(Product.wix_id == parent.wix_id)).all()
    parent_row = _first([r for r in rows if not _is_variant(r)]) or parent
    variants = [r for r in rows if _is_variant(r)]
    return parent_row, variants


# -------------------------
# Wix API helpers
# -------------------------
def _wix_v1_get_product(wix_id: str, access_token: str) -> Dict[str, Any]:
    r = requests.get(
        f"{WIX_API_BASE}/stores/v1/products/{wix_id}",
        headers=_headers(access_token),
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(502, f"Wix get product failed: {r.status_code} {r.text}")
    return r.json()


def _wix_v1_patch_product_basic(
    wix_id: str,
    access_token: str,
    *,
    name: str,
    description: str,
) -> Dict[str, Any]:
    payload = {
        "name": name,
        "description": description,
    }
    r = requests.patch(
        f"{WIX_API_BASE}/stores/v1/products/{wix_id}",
        headers=_headers(access_token),
        json=payload,
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(502, f"Wix patch product failed: {r.status_code} {r.text}")
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


def _wix_v1_query_variants(wix_id: str, access_token: str) -> List[Dict[str, Any]]:
    r = requests.post(
        f"{WIX_API_BASE}/stores/v1/products/{wix_id}/variants/query",
        headers=_headers(access_token),
        json={"query": {"paging": {"limit": 100}}},
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(502, f"Wix query variants failed: {r.status_code} {r.text}")

    data = r.json() or {}
    items = data.get("variants") or data.get("items") or []
    if not isinstance(items, list):
        return []
    return items


def _wix_v1_patch_variants(
    wix_id: str,
    access_token: str,
    updates: List[Dict[str, str]],
) -> Dict[str, Any]:
    """
    Deux payloads tentés par prudence.
    """
    payload_candidates = [
        {"variants": [{"id": u["id"], "sku": u["sku"]} for u in updates]},
        {"variants": [{"id": u["id"], "variant": {"sku": u["sku"]}} for u in updates]},
    ]

    last_error = None
    for payload in payload_candidates:
        r = requests.patch(
            f"{WIX_API_BASE}/stores/v1/products/{wix_id}/variants",
            headers=_headers(access_token),
            json=payload,
            timeout=30,
        )
        if r.ok:
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}

        last_error = f"{r.status_code} {r.text}"

    raise HTTPException(502, f"Wix patch variants failed: {last_error}")


def _wix_v3_get_product(wix_id: str, access_token: str) -> Dict[str, Any]:
    r = requests.get(
        f"{WIX_API_BASE}/stores/v3/products/{wix_id}",
        headers=_headers(access_token),
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(502, f"Wix v3 get product failed: {r.status_code} {r.text}")
    return r.json()


def _wix_v3_get_or_create_info_section(
    access_token: str,
    *,
    unique_name: str,
    title: str,
    plain_description: str,
) -> Dict[str, Any]:
    payload = {
        "uniqueName": unique_name,
        "title": title,
        "plainDescription": plain_description,
    }
    r = requests.post(
        f"{WIX_API_BASE}/stores/v3/info-sections/get-or-create",
        headers=_headers(access_token),
        json=payload,
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(502, f"Wix get-or-create info section failed: {r.status_code} {r.text}")
    return r.json()


def _extract_v3_product(data: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(data.get("product"), dict):
        return data["product"]
    return data


def _wix_v3_patch_product_info_sections(
    wix_id: str,
    access_token: str,
    info_sections: List[Dict[str, Any]],
) -> Dict[str, Any]:
    data = _wix_v3_get_product(wix_id, access_token)
    product = _extract_v3_product(data)
    revision = product.get("revision")
    if revision is None:
        raise HTTPException(502, "Wix v3 product missing revision")

    current_sections = product.get("infoSections") or []
    current_ids = {
        s.get("id")
        for s in current_sections
        if isinstance(s, dict) and s.get("id")
    }

    merged = list(current_sections)
    for section in info_sections:
        if section.get("id") not in current_ids:
            merged.append(section)

    payload_candidates = [
        {"revision": revision, "infoSections": merged},
        {"product": {"revision": revision, "infoSections": merged}},
    ]

    last_error = None
    for payload in payload_candidates:
        r = requests.patch(
            f"{WIX_API_BASE}/stores/v3/products/{wix_id}",
            headers=_headers(access_token),
            json=payload,
            timeout=30,
        )
        if r.ok:
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}
        last_error = f"{r.status_code} {r.text}"

    raise HTTPException(502, f"Wix v3 patch info sections failed: {last_error}")


# -------------------------
# Planning / preview
# -------------------------
def _wix_variant_sku_map(wix_variants: List[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for v in wix_variants:
        vid = str(v.get("id") or v.get("_id") or v.get("variantId") or "").strip()
        sku = str(v.get("sku") or (v.get("variant") or {}).get("sku") or "").strip()
        if vid:
            out[vid] = sku
    return out


def _build_plan_for_parent(
    parent: Product,
    variants: List[Product],
    wix_variants: Optional[List[Dict[str, Any]]] = None,
    current_variant_skus: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    target_name = _build_product_name(parent)
    target_desc = _build_description_html(parent)
    target_sections = _build_info_sections_with_formats(parent, variants)

    variant_plans: List[Dict[str, Any]] = []

    if wix_variants:
        variant_plans = _prepare_variant_updates_from_wix(
            parent=parent,
            wix_variants=wix_variants,
            current_variant_skus=current_variant_skus,
        )
    else:
        # fallback preview local si jamais Wix n'est pas interrogé
        seen_target_skus = set()
        for v in variants:
            variant_id = _get_variant_id(v)
            if not variant_id:
                continue

            length, weight, raw_choice = _extract_length_weight_from_variant(v)
            target_sku = _build_variant_sku(v)

            if not target_sku:
                variant_plans.append({
                    "db_id": v.id,
                    "variant_id": variant_id,
                    "choice": raw_choice,
                    "current_sku": v.sku,
                    "target_sku": None,
                    "status": "skipped_missing_parts",
                })
                continue

            if target_sku in seen_target_skus:
                variant_plans.append({
                    "db_id": v.id,
                    "variant_id": variant_id,
                    "choice": raw_choice,
                    "current_sku": v.sku,
                    "target_sku": target_sku,
                    "status": "skipped_duplicate_target_in_batch",
                })
                continue

            seen_target_skus.add(target_sku)
            variant_plans.append({
                "db_id": v.id,
                "variant_id": variant_id,
                "choice": raw_choice,
                "current_sku": v.sku,
                "target_sku": target_sku,
                "status": "planned",
            })

    return {
        "db_parent_id": parent.id,
        "wix_id": parent.wix_id,
        "current_name": parent.name,
        "target_name": target_name,
        "current_description": parent.description,
        "target_description": target_desc,
        "info_sections": target_sections,
        "variants": variant_plans,
    }


def _rollback_snapshot(
    wix_id: str,
    current_product: Dict[str, Any],
    current_variants: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "wix_id": wix_id,
        "product": current_product,
        "variants": current_variants,
    }


# -------------------------
# Endpoints
# -------------------------
@router.post("/seo/push_preview")
def push_preview(req: PushRequest, db: Session = Depends(get_session)) -> Dict[str, Any]:
    parents = _filter_local_products(
        db=db,
        product_ids=req.product_ids,
        category=req.category,
        limit=req.limit,
    )

    instance_id = _get_instance_id()
    access_token = _fetch_access_token(instance_id)

    changes = []
    for parent in parents:
        parent_row, variants = _collect_family(db, parent)
        wix_id = (parent_row.wix_id or "").strip()

        wix_variants = _wix_v1_query_variants(wix_id, access_token) if wix_id else []
        current_variant_skus = _wix_variant_sku_map(wix_variants)

        changes.append(
            _build_plan_for_parent(
                parent=parent_row,
                variants=variants,
                wix_variants=wix_variants,
                current_variant_skus=current_variant_skus,
            )
        )

    return {
        "ok": True,
        "mode": "preview",
        "count": len(changes),
        "changes": changes,
    }
    

@router.post("/seo/push_apply")
def push_apply(req: PushRequest, db: Session = Depends(get_session)) -> Dict[str, Any]:
    if not req.confirm:
        raise HTTPException(400, "confirm=true requis")
    _require_secret(req.secret)

    parents = _filter_local_products(
        db=db,
        product_ids=req.product_ids,
        category=req.category,
        limit=req.limit,
    )

    instance_id = _get_instance_id()
    access_token = _fetch_access_token(instance_id)

    results = []
    success = 0
    skipped = 0
    errors = 0

    for parent in parents:
        parent_row, variants = _collect_family(db, parent)
        wix_id = (parent_row.wix_id or "").strip()

        try:
            current_product_data = _wix_v1_get_product(wix_id, access_token)
            current_variants = _wix_v1_query_variants(wix_id, access_token)
            rollback = _rollback_snapshot(wix_id, current_product_data, current_variants)

            current_variant_skus = _wix_variant_sku_map(current_variants)
            target_skus_in_wix = {sku for sku in current_variant_skus.values() if sku}

            plan = _build_plan_for_parent(
                parent=parent_row,
                variants=variants,
                wix_variants=current_variants,
                current_variant_skus=current_variant_skus,
            )
            variant_updates = []
            variant_errors = []

            for vp in plan["variants"]:
                if vp["status"] != "planned":
                    skipped += 1
                    continue

                target_sku = vp.get("target_sku")
                variant_id = vp.get("variant_id")
                current_sku = vp.get("current_sku") or current_variant_skus.get(variant_id, "")
                
                if not target_sku:
                    skipped += 1
                    continue

                # si le target sku existe déjà sur une autre variante Wix de ce produit, on skip
                if target_sku in target_skus_in_wix and current_sku != target_sku:
                    variant_errors.append({
                        "variant_id": variant_id,
                        "current_sku": current_sku,
                        "target_sku": target_sku,
                        "error": "target_sku_already_exists_on_another_variant",
                    })
                    errors += 1
                    continue

                if current_sku == target_sku:
                    skipped += 1
                    continue

                variant_updates.append({
                    "id": variant_id,
                    "sku": target_sku,
                })

            product_resp = _wix_v1_patch_product_basic(
                wix_id=wix_id,
                access_token=access_token,
                name=plan["target_name"],
                description=plan["target_description"],
            )

            variants_resp = None
            if variant_updates:
                variants_resp = _wix_v1_patch_variants(
                    wix_id=wix_id,
                    access_token=access_token,
                    updates=variant_updates,
                )

            info_sections_resp = None
            info_section_errors = []
            if req.include_info_sections:
                try:
                    created_sections = []
                    for section in plan["info_sections"]:
                        unique_name = f"luxura-{_slugify(plan['target_name'])}-{section['key']}"
                        sec = _wix_v3_get_or_create_info_section(
                            access_token=access_token,
                            unique_name=unique_name,
                            title=section["title"],
                            plain_description=section["plainDescription"],
                        )
                        section_obj = sec.get("infoSection") if isinstance(sec, dict) else None
                        if not isinstance(section_obj, dict):
                            section_obj = sec if isinstance(sec, dict) else {}
                        if section_obj:
                            created_sections.append({
                                "id": section_obj.get("id"),
                                "uniqueName": section_obj.get("uniqueName") or unique_name,
                                "title": section_obj.get("title") or section["title"],
                            })

                    if created_sections:
                        info_sections_resp = _wix_v3_patch_product_info_sections(
                            wix_id=wix_id,
                            access_token=access_token,
                            info_sections=created_sections,
                        )
                except Exception as e:
                    info_section_errors.append(str(e))
                    errors += 1

            success += 1
            results.append({
                "wix_id": wix_id,
                "db_parent_id": plan["db_parent_id"],
                "name_updated_to": plan["target_name"],
                "variant_updates": variant_updates,
                "variant_errors": variant_errors,
                "info_section_errors": info_section_errors,
                "rollback": rollback,
                "responses": {
                    "product": product_resp,
                    "variants": variants_resp,
                    "info_sections": info_sections_resp,
                },
            })

        except Exception as e:
            errors += 1
            results.append({
                "wix_id": wix_id,
                "db_parent_id": plan["db_parent_id"],
                "error": str(e),
                "rollback": None,
            })

    return {
        "ok": True,
        "mode": "apply",
        "success": success,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }
