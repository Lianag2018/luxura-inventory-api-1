import os
import re
import html
import json
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

    print("TOKEN STATUS:", token_res.status_code)
    print("TOKEN RESPONSE:", token_res.text[:1000])

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
# Small utils
# -------------------------
def _safe_json_response(resp: requests.Response) -> Dict[str, Any]:
    try:
        return resp.json() if resp.text else {}
    except ValueError:
        return {"raw": resp.text}


def _extract_product_name(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    product = data.get("product")
    if isinstance(product, dict):
        return str(product.get("name") or "").strip()
    return str(data.get("name") or "").strip()


def _extract_product_description(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    product = data.get("product")
    if isinstance(product, dict):
        return str(product.get("description") or "").strip()
    return str(data.get("description") or "").strip()


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

    m = re.search(r"#\s*([A-Za-z0-9/]+)", text)
    if m:
        return m.group(1).strip().upper()

    if prod.sku:
        m2 = re.search(r"#([A-Za-z0-9/]+)$", prod.sku.strip(), flags=re.IGNORECASE)
        if m2:
            return m2.group(1).strip().upper()

    return None


def _extract_length_weight_from_variant(prod: Product) -> Tuple[Optional[str], Optional[str], Optional[str]]:
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
    _product_type, _series, prefix = _infer_type_and_series(prod)
    color_code = _extract_color_code(prod)
    if not color_code:
        return None

    length, weight, _raw = _extract_length_weight_from_variant(prod)
    if not length or not weight:
        return None

    color = _color_meta(color_code)
    clean_code = color_code.upper().replace("/", "-")
    return f"{prefix}-{length}-{weight}-{clean_code}-{color['sku']}"


def _extract_length_weight_from_choice_value(raw: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
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
""".strip()


def _build_info_sections(prod: Product) -> List[Dict[str, str]]:
    product_type, series, _prefix = _infer_type_and_series(prod)
    color_code = _extract_color_code(prod) or ""
    color = _color_meta(color_code)
    luxe_name = color.get("luxe", color_code)

    label = f"{product_type} {series}".strip()

    if product_type == "Halo":
        desc = (
            f"Installation rapide et facile. Les extensions {label} offrent un volume instantané "
            f"et un rendu naturel sans engagement permanent. Cheveux 100% naturels Remy à cuticules alignées."
        )
        about = (
            f"Les extensions {label} Luxura sont conçues pour les clientes qui veulent plus de volume "
            f"et de longueur en quelques minutes. Série {series}, qualité professionnelle, confort maximal "
            f"et teinte {luxe_name} #{color_code}."
        )
        recommendation = (
            "1 pièce pour un volume naturel, 2 pièces si vous recherchez un effet plus dense et plus glamour."
        )
        maintenance = (
            "Brosser délicatement avant et après usage. Laver avec un shampooing doux sans sulfates. "
            "Utiliser un masque hydratant sur les longueurs. Sécher à l'air libre ou à basse température."
        )

    elif product_type == "Genius":
        desc = (
            f"Les extensions {label} sont des trames invisibles haut de gamme conçues pour une finition "
            f"naturelle, légère et luxueuse. Cheveux 100% naturels Remy, parfaits pour les salons professionnels."
        )
        about = (
            f"La série {series} propose des extensions Genius souples, discrètes et confortables, "
            f"idéales pour une pose couture ou intégrée. Teinte {luxe_name} #{color_code}."
        )
        recommendation = (
            "1 paquet pour un ajout discret, 2 à 3 paquets pour une transformation complète et un volume riche."
        )
        maintenance = (
            "Laver avec des soins professionnels sans sulfates. Hydrater régulièrement les longueurs. "
            "Démêler des pointes vers la racine avec une brosse adaptée. Limiter la chaleur excessive."
        )

    elif product_type == "Tape":
        desc = (
            f"Les extensions {label} à bandes adhésives offrent une pose rapide, discrète et confortable. "
            f"Elles sont parfaites pour obtenir longueur et volume avec un résultat naturel."
        )
        about = (
            f"La série {series} Luxura utilise des cheveux 100% naturels Remy avec une finition premium. "
            f"La teinte {luxe_name} #{color_code} assure un rendu lumineux et élégant."
        )
        recommendation = (
            "1 paquet pour densifier légèrement, 2 paquets ou plus pour un résultat plus complet et homogène."
        )
        maintenance = (
            "Utiliser un shampooing doux sans huiles lourdes aux racines. Éviter les produits gras sur les bandes. "
            "Brosser délicatement et espacer les lavages pour prolonger la tenue."
        )

    elif product_type == "I-Tip":
        desc = (
            f"Les extensions {label} I-Tip permettent une pose mèche à mèche souple, naturelle et personnalisée. "
            f"Elles conviennent parfaitement aux poses professionnelles haut de gamme."
        )
        about = (
            f"La série {series} Luxura offre une finition raffinée avec cheveux 100% naturels Remy, "
            f"cuticules alignées et teinte {luxe_name} #{color_code}."
        )
        recommendation = (
            "Quantité recommandée selon le résultat souhaité : volume léger, correction ciblée ou transformation complète."
        )
        maintenance = (
            "Employer des soins doux sans sulfates. Hydrater les longueurs sans surcharger les points d'attache. "
            "Démêler soigneusement chaque jour et éviter les gestes brusques."
        )

    else:
        desc = (
            f"Extensions {label} de qualité professionnelle, conçues pour offrir longueur, volume et rendu naturel. "
            f"Cheveux 100% naturels Remy."
        )
        about = (
            f"Extensions Luxura Série {series}, finition premium, teinte {luxe_name} #{color_code}."
        )
        recommendation = "La quantité recommandée varie selon le volume désiré et le service recherché."
        maintenance = (
            "Utiliser des soins doux, hydrater les longueurs et démêler délicatement pour préserver la qualité des cheveux."
        )

    return [
        {
            "key": "description",
            "title": "Description",
            "plainDescription": desc,
        },
        {
            "key": "a-propos",
            "title": "À propos",
            "plainDescription": about,
        },
        {
            "key": "recommandation",
            "title": "Recommandation",
            "plainDescription": recommendation,
        },
        {
            "key": "format",
            "title": "Format",
            "plainDescription": "16 pouces (120g) | 20 pouces (140g)",
        },
        {
            "key": "entretien",
            "title": "Entretien",
            "plainDescription": maintenance,
        },
        {
            "key": "precommande",
            "title": "Précommande",
            "plainDescription": (
                "Pré-commandes acceptées. Contactez-nous pour connaître les délais et être avisée dès le retour en stock."
            ),
        },
        {
            "key": "seo-local",
            "title": "Disponibilité",
            "plainDescription": (
                "Disponible pour le Québec, Montréal, Lévis, Trois-Rivières, la Beauce et Sainte-Marie."
            ),
        },
    ]


def _build_info_sections_with_formats(parent: Product, variants: List[Product]) -> List[Dict[str, str]]:
    sections = _build_info_sections(parent)
    formats = []

    for v in variants:
        length, weight, _raw = _extract_length_weight_from_variant(v)
        if length and weight:
            formats.append(f"{length} pouces ({weight}g)")

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
# Wix API helpers - V1 / V3
# -------------------------
def _wix_v1_get_product(wix_id: str, access_token: str) -> Dict[str, Any]:
    r = requests.get(
        f"{WIX_API_BASE}/stores/v1/products/{wix_id}",
        headers=_headers(access_token),
        timeout=30,
    )

    print("GET PRODUCT STATUS:", r.status_code)
    print("GET PRODUCT RESPONSE:", r.text[:4000])

    if not r.ok:
        raise HTTPException(502, f"Wix get product failed: {r.status_code} {r.text}")

    return _safe_json_response(r)


def _wix_v1_verify_product(wix_id: str, access_token: str) -> Dict[str, Any]:
    r = requests.get(
        f"{WIX_API_BASE}/stores/v1/products/{wix_id}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30,
    )

    print("VERIFY STATUS:", r.status_code)
    print("VERIFY RESPONSE:", r.text[:4000])

    if not r.ok:
        raise HTTPException(502, f"Wix verify product failed: {r.status_code} {r.text}")

    return _safe_json_response(r)


def _wix_v1_patch_product_basic(
    wix_id: str,
    access_token: str,
    *,
    name: str,
    description: str,
) -> Dict[str, Any]:
    """
    IMPORTANT:
    On ne considère PAS un 200 comme un succès réel.
    On vérifie via un GET derrière.
    """

    url = f"{WIX_API_BASE}/stores/v1/products/{wix_id}"
    headers = _headers(access_token)

    payload_candidates = [
        {
            "label": "wrapped_product",
            "payload": {
                "product": {
                    "name": name,
                    "description": description,
                }
            },
        },
        {
            "label": "flat_payload",
            "payload": {
                "name": name,
                "description": description,
            },
        },
    ]

    attempts: List[Dict[str, Any]] = []

    for candidate in payload_candidates:
        label = candidate["label"]
        payload = candidate["payload"]

        print("PATCH PRODUCT FORMAT:", label)
        print("PATCH PRODUCT PAYLOAD:", json.dumps(payload, ensure_ascii=False)[:4000])

        r = requests.patch(
            url,
            headers=headers,
            json=payload,
            timeout=30,
        )

        print("PATCH STATUS:", r.status_code)
        print("PATCH RESPONSE:", r.text[:4000])

        patch_json = _safe_json_response(r)

        attempts.append({
            "format": label,
            "status_code": r.status_code,
            "response": patch_json,
        })

        if not r.ok:
            continue

        verified = _wix_v1_verify_product(wix_id, access_token)
        verified_name = _extract_product_name(verified)
        verified_description = _extract_product_description(verified)

        print("VERIFIED NAME:", verified_name)
        print("VERIFIED DESCRIPTION LENGTH:", len(verified_description or ""))

        if verified_name == name:
            return {
                "ok": True,
                "patch_format": label,
                "patch_response": patch_json,
                "verified_product": verified,
                "verified_name": verified_name,
                "verified_description": verified_description,
            }

    raise HTTPException(
        502,
        f"Wix patch product did not apply changes. Attempts={json.dumps(attempts, ensure_ascii=False)[:8000]}"
    )


def _wix_v1_query_variants(wix_id: str, access_token: str) -> List[Dict[str, Any]]:
    r = requests.post(
        f"{WIX_API_BASE}/stores/v1/products/{wix_id}/variants/query",
        headers=_headers(access_token),
        json={"query": {"paging": {"limit": 100}}},
        timeout=30,
    )

    print("QUERY VARIANTS STATUS:", r.status_code)
    print("QUERY VARIANTS RESPONSE:", r.text[:4000])

    if not r.ok:
        raise HTTPException(502, f"Wix query variants failed: {r.status_code} {r.text}")

    data = _safe_json_response(r)
    items = data.get("variants") or data.get("items") or []
    if not isinstance(items, list):
        return []
    return items


def _wix_v1_patch_variants(
    wix_id: str,
    access_token: str,
    updates: List[Dict[str, str]],
) -> Dict[str, Any]:
    payload_candidates = [
        {"variants": [{"id": u["id"], "sku": u["sku"]} for u in updates]},
        {"variants": [{"id": u["id"], "variant": {"sku": u["sku"]}} for u in updates]},
    ]

    last_error = None
    attempt_logs = []

    for payload in payload_candidates:
        print("PATCH VARIANTS PAYLOAD:", json.dumps(payload, ensure_ascii=False)[:4000])

        r = requests.patch(
            f"{WIX_API_BASE}/stores/v1/products/{wix_id}/variants",
            headers=_headers(access_token),
            json=payload,
            timeout=30,
        )

        print("PATCH VARIANTS STATUS:", r.status_code)
        print("PATCH VARIANTS RESPONSE:", r.text[:4000])

        if r.ok:
            return _safe_json_response(r)

        attempt_logs.append({
            "status_code": r.status_code,
            "response": r.text[:2000],
            "payload": payload,
        })
        last_error = f"{r.status_code} {r.text}"

    raise HTTPException(502, f"Wix patch variants failed: {last_error} | attempts={json.dumps(attempt_logs, ensure_ascii=False)[:8000]}")


def _wix_v3_get_product(wix_id: str, access_token: str) -> Dict[str, Any]:
    r = requests.get(
        f"{WIX_API_BASE}/stores/v3/products/{wix_id}",
        headers=_headers(access_token),
        timeout=30,
    )

    print("V3 GET PRODUCT STATUS:", r.status_code)
    print("V3 GET PRODUCT RESPONSE:", r.text[:4000])

    if not r.ok:
        raise HTTPException(502, f"Wix v3 get product failed: {r.status_code} {r.text}")

    return _safe_json_response(r)


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

    print("GET/CREATE INFO SECTION PAYLOAD:", json.dumps(payload, ensure_ascii=False)[:4000])

    r = requests.post(
        f"{WIX_API_BASE}/stores/v3/info-sections/get-or-create",
        headers=_headers(access_token),
        json=payload,
        timeout=30,
    )

    print("GET/CREATE INFO SECTION STATUS:", r.status_code)
    print("GET/CREATE INFO SECTION RESPONSE:", r.text[:4000])

    if not r.ok:
        raise HTTPException(502, f"Wix get-or-create info section failed: {r.status_code} {r.text}")

    return _safe_json_response(r)


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
        if section.get("id") and section.get("id") not in current_ids:
            merged.append(section)

    payload_candidates = [
        {"revision": revision, "infoSections": merged},
        {"product": {"revision": revision, "infoSections": merged}},
    ]

    last_error = None
    logs = []

    for payload in payload_candidates:
        print("PATCH PRODUCT INFO SECTIONS PAYLOAD:", json.dumps(payload, ensure_ascii=False)[:4000])

        r = requests.patch(
            f"{WIX_API_BASE}/stores/v3/products/{wix_id}",
            headers=_headers(access_token),
            json=payload,
            timeout=30,
        )

        print("PATCH PRODUCT INFO SECTIONS STATUS:", r.status_code)
        print("PATCH PRODUCT INFO SECTIONS RESPONSE:", r.text[:4000])

        if r.ok:
            return _safe_json_response(r)

        logs.append({
            "status_code": r.status_code,
            "response": r.text[:2000],
            "payload": payload,
        })
        last_error = f"{r.status_code} {r.text}"

    raise HTTPException(502, f"Wix v3 patch info sections failed: {last_error} | attempts={json.dumps(logs, ensure_ascii=False)[:8000]}")


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

        plan = _build_plan_for_parent(
            parent=parent_row,
            variants=variants,
        )

        if not wix_id:
            errors += 1
            results.append({
                "wix_id": "",
                "db_parent_id": plan["db_parent_id"],
                "error": "missing wix_id",
                "rollback": None,
            })
            continue

        try:
            print("=" * 100)
            print("PUSH APPLY START")
            print("DB PARENT ID:", plan["db_parent_id"])
            print("WIX ID:", wix_id)
            print("TARGET NAME:", plan["target_name"])

            current_product_data = _wix_v1_get_product(wix_id, access_token)
            current_variants = _wix_v1_query_variants(wix_id, access_token)
            rollback = _rollback_snapshot(wix_id, current_product_data, current_variants)

            before_name = _extract_product_name(current_product_data)
            before_description = _extract_product_description(current_product_data)

            print("BEFORE NAME:", before_name)
            print("BEFORE DESCRIPTION LENGTH:", len(before_description or ""))

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

            verified_product = product_resp.get("verified_product") or {}
            verified_name = _extract_product_name(verified_product)
            verified_description = _extract_product_description(verified_product)

            if verified_name != plan["target_name"]:
                raise HTTPException(
                    502,
                    f"Product name not applied. expected={plan['target_name']!r} got={verified_name!r}"
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
                        if section_obj and section_obj.get("id"):
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
                "before_name": before_name,
                "name_updated_to": verified_name,
                "description_length_after": len(verified_description or ""),
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
        "ok": errors == 0,
        "mode": "apply",
        "success": success,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }
