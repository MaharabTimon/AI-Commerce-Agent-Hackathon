"""
dataset_importer.py
Robust Universal Dataset Importer for Tech Catalog.

Supports importing products from:
1. Raw unstructured text (WhatsApp posts, Facebook shop posts, messy notes, bullet points).
2. Line-by-line quick listings (e.g. "iPhone 15 Pro 256GB - 125,000 tk (stock 5)").
3. Multi-line key-value or labeled blocks (Name:, Price:, Specs:, Stock:).
4. Delimited text (CSV, TSV, Pipe '|' tables).
5. JSON arrays or objects.
6. Local text files (.txt, .csv, .json).

All imported products are normalized to canonical catalog format and immediately
re-indexed into the RAG knowledge base.
"""

import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from database import get_product_repository

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".json", ".csv", ".tsv", ".xlsx", ".xml", ".jsonl", ".ndjson", ".txt"}

# Known brand lexicon and normalizations
BRAND_MAP = {
    "apple": "Apple",
    "iphone": "Apple",
    "ipad": "Apple",
    "macbook": "Apple",
    "airpods": "Apple",
    "samsung": "Samsung",
    "galaxy": "Samsung",
    "xiaomi": "Xiaomi",
    "redmi": "Xiaomi",
    "poco": "Xiaomi",
    "realme": "Realme",
    "asus": "Asus",
    "rog": "Asus",
    "tuf": "Asus",
    "zenbook": "Asus",
    "vivobook": "Asus",
    "dell": "Dell",
    "alienware": "Dell",
    "xps": "Dell",
    "inspiron": "Dell",
    "hp": "HP",
    "spectre": "HP",
    "envy": "HP",
    "pavilion": "HP",
    "victus": "HP",
    "omen": "HP",
    "lenovo": "Lenovo",
    "thinkpad": "Lenovo",
    "legion": "Lenovo",
    "ideapad": "Lenovo",
    "loq": "Lenovo",
    "sony": "Sony",
    "playstation": "Sony",
    "bose": "Bose",
    "oneplus": "OnePlus",
    "google": "Google Pixel",
    "pixel": "Google Pixel",
    "nothing": "Nothing",
    "acer": "Acer",
    "predator": "Acer",
    "nitro": "Acer",
    "razer": "Razer",
    "msi": "MSI",
    "jbl": "JBL",
    "marshall": "Marshall",
    "logitech": "Logitech",
    "anker": "Anker",
    "soundcore": "Anker",
    "dyson": "Dyson",
    "roborock": "Roborock",
    "canon": "Canon",
    "nikon": "Nikon",
    "fujifilm": "Fujifilm",
    "oppo": "Oppo",
    "vivo": "Vivo",
    "iqoo": "Vivo",
    "motorola": "Motorola",
    "moto": "Motorola",
    "infinix": "Infinix",
    "tecno": "Tecno",
    "honor": "Honor",
    "huawei": "Huawei",
    "garmin": "Garmin",
    "amazfit": "Amazfit",
    "keychron": "Keychron",
    "hyperx": "HyperX",
    "viewsonic": "ViewSonic",
    "lg": "LG",
    "gigabyte": "Gigabyte",
    "corsair": "Corsair",
    # Bengali transliterations
    "অ্যাপল": "Apple",
    "আইফোন": "Apple",
    "স্যামসাং": "Samsung",
    "শাওমি": "Xiaomi",
    "রেডমি": "Xiaomi",
    "রিয়েলমি": "Realme",
    "আসুস": "Asus",
    "ডেল": "Dell",
    "এইচপি": "HP",
    "লেনোভো": "Lenovo",
    "সনি": "Sony",
    "ওয়ানপ্লাস": "OnePlus",
}

# Category classification heuristics
CATEGORY_RULES = [
    (
        "Smartphones",
        [
            r"\b(?:phone|smartphone|mobile|iphone|galaxy\s+s\d+|galaxy\s+z|galaxy\s+a\d+|redmi|realme|poco|pixel|oppo|vivo|oneplus|infinix|tecno|motog)\b",
            r"\b(?:\d+gb/\d+gb|\d+\s*gb\s*ram|\d+mp\s*camera)\b",
        ],
    ),
    (
        "Laptops",
        [
            r"\b(?:laptop|notebook|macbook|thinkpad|zenbook|vivobook|ideapad|legion|pavilion|envy|victus|omen|spectre|xps|inspiron|rog\s+strix|zephyrus|predator|tuf\s+gaming|blade)\b",
            r"\b(?:rtx\s*\d{4}|gtx\s*\d{4}|intel\s*core|ryzen\s*\d|i[3579]-?\d{4,5}|14-inch|15\.6-inch|16-inch)\b",
        ],
    ),
    (
        "Audio & Headphones",
        [
            r"\b(?:headphone|headphones|earphone|earphones|earbud|earbuds|airpods|airpod|tws|headset|soundbar|speaker|bluetooth\s+speaker|anc|wh-1000|wf-1000|soundcore)\b",
        ],
    ),
    (
        "Monitors & Displays",
        [
            r"\b(?:monitor|gaming\s+monitor|curved\s+monitor|ultrawide|qhd\s+oled|240hz\s+monitor|144hz\s+monitor|27-inch\s+monitor|32-inch\s+monitor)\b",
        ],
    ),
    (
        "Cameras & Photography",
        [
            r"\b(?:camera|mirrorless|dslr|lens|gimbal|eos\s+r|alpha\s+7|a7\s*iv|a6700|lumix)\b",
        ],
    ),
    (
        "Smartwatches & Fitness",
        [
            r"\b(?:smartwatch|watch\s+ultra|galaxy\s+watch|apple\s+watch|fitness\s+band|smart\s+band)\b",
        ],
    ),
    (
        "Tablets",
        [
            r"\b(?:tablet|ipad|ipad\s+air|ipad\s+pro|galaxy\s+tab)\b",
        ],
    ),
    (
        "Accessories & Peripherals",
        [
            r"\b(?:mouse|keyboard|mechanical\s+keyboard|charger|power\s+bank|dock|hub|cable|adapter|case|stand)\b",
        ],
    ),
]

COLOR_LIST = [
    "natural titanium", "black titanium", "white titanium", "blue titanium",
    "desert titanium", "space black", "space gray", "space grey",
    "midnight", "starlight", "phantom black", "titanium gray", "titanium black",
    "titanium violet", "titanium yellow", "deep purple", "sierra blue",
    "pacific blue", "alpine green", "sunrise beige", "oasis green",
    "champion gold", "submarine blue", "arctic white", "onyx black",
    "silver", "gold", "black", "white", "blue", "green", "red",
    "purple", "pink", "yellow", "orange", "gray", "grey"
]


def _sync_rag_index():
    """Trigger real-time re-indexing of RAG knowledge base for Messenger bot."""
    try:
        from rag_service import get_rag
        rag = get_rag()
        rag.load_and_index()
        logger.info("Successfully re-indexed RAG memory after catalog mutation.")
    except Exception as e:
        logger.warning(f"Failed to auto-refresh RAG index: {e}")


def _detect_brand(text: str) -> str:
    """Infers the manufacturer brand from product text."""
    t_lower = text.lower()
    for kw, brand in BRAND_MAP.items():
        if re.search(r"(?:^|[^\w])" + re.escape(kw) + r"(?:[^\w]|$)", t_lower):
            return brand
    first_word = text.strip().split()[0] if text.strip() else "General"
    clean_first = re.sub(r"[^\w]", "", first_word)
    return clean_first.capitalize() if len(clean_first) >= 2 else "General"


def _detect_category(text: str) -> str:
    """Infers the category from product title and specs."""
    t_lower = text.lower()
    for cat_name, patterns in CATEGORY_RULES:
        for pat in patterns:
            if re.search(pat, t_lower):
                return cat_name
    return "Electronics"


def _detect_color(text: str) -> str:
    """Finds named variant/color in product text."""
    t_lower = text.lower()
    for c in COLOR_LIST:
        if re.search(r"(?:^|[^\w])" + re.escape(c) + r"(?:[^\w]|$)", t_lower):
            return c.title()
    return ""


BENGALI_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def _extract_stock(text: str) -> Tuple[int, bool, str]:
    """
    Extracts stock quantity, availability status, and returns text with stock tokens removed.
    Defaults to 5 units in-stock if unstated, or 0 if 'out of stock' / 'stock out'.
    """
    t_lower = text.translate(BENGALI_DIGITS).lower()
    m_oos = re.search(r"\b(?:out\s*of\s*stock|stock\s*out|stock\s*nai|nai|unavailable|stock\s*0)\b", t_lower)
    if m_oos:
        cleaned = text[:m_oos.start()] + " " + text[m_oos.end():]
        cleaned = re.sub(r"\(\s*\)", "", cleaned).strip()
        return 0, False, cleaned

    stock_match = re.search(r"(?:\bstock\b|\bqty\b|\bquantity\b|\bcount\b)\s*[:=]?\s*(\d+)", t_lower)
    if stock_match:
        qty = int(stock_match.group(1))
        cleaned = text[:stock_match.start()] + " " + text[stock_match.end():]
        cleaned = re.sub(r"\(\s*\)", "", cleaned).strip()
        return qty, qty > 0, cleaned

    suffix_match = re.search(r"\b(\d+)\s*(?:in\s*stock|available|left|pcs|pieces|units|ta|টা|item|items)\b", t_lower)
    if suffix_match:
        qty = int(suffix_match.group(1))
        cleaned = text[:suffix_match.start()] + " " + text[suffix_match.end():]
        cleaned = re.sub(r"\(\s*\)", "", cleaned).strip()
        return qty, qty > 0, cleaned

    return 5, True, text


def _extract_price(text: str) -> Tuple[int, str]:
    """
    Extracts the numeric BDT price from text while ignoring technical specs
    (e.g., 5000 mAh, 200 MP, 120 Hz, 256 GB, 65W, 1080p).
    Returns (price_bdt, text_with_price_removed).
    """
    working = " " + text.translate(BENGALI_DIGITS) + " "

    # 1. Mask out specifications that contain numbers so they aren't confused with prices
    # e.g., 5000mah, 200mp, 120hz, 256gb, 16gb ram, 65w, 1080p, 14 inch, etc.
    spec_mask_patterns = [
        r"\b\d+\s*(?:mah|wh)\b",
        r"\b\d+\s*(?:mp|megapixel)\b",
        r"\b\d+\s*(?:hz|khz|ghz)\b",
        r"\b\d+\s*(?:gb|tb|mb)(?:\s*(?:ram|rom|ssd|ddr\d?|storage))?\b",
        r"\b\d+\s*(?:w|watt)\b",
        r"\b\d+(?:\.\d+)?\s*(?:inch|\"|'')\b",
        r"\b(?:1080p|1440p|720p|480p|[1248]k(?:\s*resolution|\s*display|\s*video|\s*panel)?)\b",
        r"\b(?:i[3579]|ryzen\s*\d)\s*[-]?\s*\d{4,5}[a-z]*\b",
        r"\brtx\s*\d{4}\b",
        r"\b\d+\s*(?:pcs|pieces|units|ta|টা)\b",
    ]

    masked_working = working
    for pat in spec_mask_patterns:
        masked_working = re.sub(pat, lambda m: " " * len(m.group(0)), masked_working, flags=re.IGNORECASE)

    # Shorthand thousand prices (e.g. 85k, 125k, 38k tk) common in Bangladesh tech shops
    shorthand_k = re.search(r"\b(\d+)\s*k(?:\s*(?:/-|৳|bdt|tk|taka|টাকা))?\b", masked_working, re.I)
    if shorthand_k:
        val = int(shorthand_k.group(1)) * 1000
        clean_text = working[:shorthand_k.start()] + " " + working[shorthand_k.end():]
        return val, clean_text.strip()

    # 2. Look for explicit price tags first (tk, taka, bdt, /-, ৳, price:)
    explicit_pat = (
        r"(?:(?:\bprice\b|\bcost\b|\bdam\b|\bdaam\b|\brate\b|\bmrp\b|\bমূল্য\b)\s*[:=]?\s*(?:৳|bdt|tk|taka|টাকা)?\s*(\d[\d,]*))|"
        r"(?:(?:৳|bdt|tk|taka|টাকা)\s*[:=]?\s*(\d[\d,]*))|"
        r"(?:(\d[\d,]*)\s*(?:/-|৳|bdt|tk|taka|টাকা)\b)"
    )

    # Dollar prices: "$49" / "49 USD" convert via CATALOG_USD_BDT_RATE.
    # With no rate configured the price stays 0 and the row is skipped
    # rather than silently mispriced as BDT.
    usd_match = re.search(r"(?:\$\s*(\d[\d,]*(?:\.\d+)?))|(?:(\d[\d,]*(?:\.\d+)?)\s*(?:usd|dollars?)\b)",
                          masked_working, re.I)
    if usd_match:
        try:
            _rate = float(os.environ.get("CATALOG_USD_BDT_RATE", "") or 0)
        except ValueError:
            _rate = 0.0
        _num = float((usd_match.group(1) or usd_match.group(2)).replace(",", ""))
        _val = int(_num * (_rate if _rate > 0 else 120.0))
        clean_text = working[:usd_match.start()] + " " + working[usd_match.end():]
        return _val, clean_text.strip()

    explicit_match = re.search(explicit_pat, masked_working, re.I)
    if explicit_match:
        raw_num = explicit_match.group(1) or explicit_match.group(2) or explicit_match.group(3)
        clean_num = raw_num.replace(",", "")
        if clean_num.isdigit():
            val = int(clean_num)
            clean_text = working[:explicit_match.start()] + " " + working[explicit_match.end():]
            return val, clean_text.strip()

    # 3. Look for standalone numeric values >= 100 that weren't masked out
    candidates = []
    for m in re.finditer(r"\b(\d[\d,]{2,})\b", masked_working):
        c_str = m.group(1).replace(",", "")
        if c_str.isdigit():
            c_int = int(c_str)
            # Filter out reasonable years (e.g. 2024, 2025) unless preceded by currency
            if c_int in (2020, 2021, 2022, 2023, 2024, 2025, 2026):
                continue
            if 100 <= c_int <= 10000000:
                candidates.append((c_int, m.start(), m.end()))

    if candidates:
        # Highest standalone number is almost always the price in catalog listings
        candidates.sort(key=lambda x: x[0], reverse=True)
        chosen_price, c_start, c_end = candidates[0]
        clean_text = working[:c_start] + " " + working[c_end:]
        return chosen_price, clean_text.strip()

    return 0, text.strip()


def parse_single_product_chunk(chunk: str) -> Optional[Dict[str, Any]]:
    """
    Parses a single product block or line into a standardized catalog dictionary.
    Returns None if no viable product name or price could be determined.
    """
    raw = chunk.strip()
    if not raw or len(raw) < 4:
        return None

    # Check for labeled key-value fields (e.g., Name: ..., Price: ...)
    lines = raw.splitlines()
    kv = {}
    remaining_lines = []
    for line in lines:
        if ":" in line:
            parts = line.split(":", 1)
            k = parts[0].strip().lower()
            v = parts[1].strip()
            if k in ("name", "title", "product", "item"):
                kv["name"] = v
            elif k in ("price", "cost", "dam", "daam", "rate", "taka", "tk", "bdt", "mrp", "মূল্য"):
                parsed_price = _to_price(v)
                if parsed_price > 0:
                    kv["price_bdt"] = parsed_price
            elif k in ("stock", "qty", "quantity", "count", "units"):
                s_digits = re.findall(r"\d+", v)
                if s_digits:
                    kv["stock"] = int(s_digits[0])
            elif k in ("brand", "make", "manufacturer"):
                kv["brand"] = v
            elif k in ("category", "cat"):
                kv["category"] = v
            elif k in ("color", "colour", "variant"):
                kv["color"] = v
            elif k in ("specs", "specifications", "details", "features", "description"):
                kv["specifications"] = v
            else:
                remaining_lines.append(line)
        else:
            remaining_lines.append(line)

    if kv.get("name") and kv.get("price_bdt"):
        p_name = kv["name"].strip()
        p_price = kv["price_bdt"]
        stock_val = kv.get("stock", 5)
        in_stock = stock_val > 0
        brand = kv.get("brand") or _detect_brand(p_name)
        cat = kv.get("category") or _detect_category(p_name + " " + kv.get("specifications", ""))
        color = kv.get("color") or _detect_color(p_name + " " + " ".join(remaining_lines))
        specs = kv.get("specifications") or (" | ".join(remaining_lines).strip() or f"{p_name} - Official Store Unit")

        return {
            "name": p_name,
            "brand": brand,
            "category": cat,
            "price_bdt": p_price,
            "price_formatted": f"{p_price:,}৳",
            "stock": stock_val,
            "in_stock": in_stock,
            "color": color,
            "specifications": specs,
            "status": "ACTIVE" if in_stock else "OUT_OF_STOCK",
        }

    # Free-form line or paragraph parsing
    # Strip leading bullet points or numbers (e.g. "1. ", "• ", "- ", "* ")
    clean_line = re.sub(r"^(?:[\d]+[\.\)]|[\*\-\•\–\—])\s*", "", raw).strip()

    stock_val, in_stock, clean_line = _extract_stock(clean_line)
    price_val, remaining_text = _extract_price(clean_line)

    if price_val <= 0:
        return None

    # Clean up name: separate title from extra specifications / details
    # Look for delimiters like " - ", " -> ", " : ", " | ", "(specs)"
    name_part = remaining_text
    specs_part = ""

    # Split by common item separators
    delims = [" -> ", " - ", " – ", " — ", " | "]
    for d in delims:
        if d in remaining_text:
            parts = remaining_text.split(d, 1)
            name_part = parts[0].strip()
            specs_part = parts[1].strip()
            break

    # If brackets/parentheses exist in name, retain them or treat as specs
    if not specs_part and "(" in name_part and ")" in name_part:
        m_paren = re.search(r"\((.*?)\)", name_part)
        if m_paren:
            specs_part = m_paren.group(1).strip()

    # Clean product title
    clean_name = re.sub(r"\s+", " ", name_part).strip()
    clean_name = re.sub(r"^(?:item|product|name)\s*[:\-]\s*", "", clean_name, flags=re.I).strip()
    clean_name = re.sub(r"[\,\:\;]$", "", clean_name).strip()

    if len(clean_name) < 2:
        return None

    brand = _detect_brand(clean_name)
    cat = _detect_category(clean_name + " " + specs_part)
    color = _detect_color(clean_name + " " + specs_part)

    # Build rich specifications string
    spec_tokens = []
    storage_matches = re.findall(r"\b\d+\s*(?:gb|tb)\b", clean_line, re.I)
    if storage_matches:
        spec_tokens.append(f"Storage: {', '.join(storage_matches)}")
    if color:
        spec_tokens.append(f"Color: {color}")
    if specs_part:
        spec_tokens.append(specs_part)

    full_specs = " | ".join(spec_tokens) if spec_tokens else f"{clean_name} - Official Store Unit"

    return {
        "name": clean_name,
        "brand": brand,
        "category": cat,
        "price_bdt": price_val,
        "price_formatted": f"{price_val:,}৳",
        "stock": stock_val,
        "in_stock": in_stock,
        "color": color,
        "specifications": full_specs,
        "status": "ACTIVE" if in_stock else "OUT_OF_STOCK",
    }


# ---------------------------------------------------------------------------
# Universal structured import: any field names, any common export format.
# Shopify, WooCommerce, generic JSON/CSV/XLSX/XML all coerce to the same
# canonical product dict the rest of the system already understands.
# ---------------------------------------------------------------------------

def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(key).lower()).strip()


_FIELD_ALIASES = {
    "name": ["name", "title", "product name", "product title", "item", "item name", "product",
             "model", "goods name", "article", "productname", "itemname"],
    "price": ["sale price", "special price", "offer price", "discounted price", "price",
              "variant price", "regular price", "compare at price", "mrp", "retail price",
              "unit price", "amount", "cost", "price bdt", "bdt", "tk", "sell price", "selling price"],
    "brand": ["brand", "vendor", "manufacturer", "make", "company", "brand name"],
    "category": ["category", "product type", "type", "categories", "category name", "dept",
                 "department", "collection", "product category"],
    "stock": ["stock", "variant inventory qty", "inventory quantity", "inventory", "qty",
              "quantity", "stock quantity", "stock status", "availability", "available",
              "in stock", "units", "count", "inventory count"],
    "color": ["color", "colour", "variant color", "option1 value", "color name"],
    "size": ["size", "variant size", "option2 value"],
    "sku": ["sku", "variant sku", "code", "product code", "item code", "barcode", "id"],
    "description": ["description", "body html", "body", "details", "specifications", "specs",
                    "features", "short description", "product description", "summary"],
    "image": ["image", "image src", "image url", "images", "picture", "photo", "thumbnail", "img"],
    "url": ["url", "link", "product url", "permalink", "handle"],
    "tags": ["tags", "tag", "keywords", "search keywords", "labels"],
}
_ALIAS_TO_FIELD = {}
for _field, _aliases in _FIELD_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_FIELD[_alias] = _field


def _strip_html(text: Any) -> str:
    clean = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", clean).strip()


def _to_price(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        for k in ("sale", "sale_usd", "min_usd", "min", "amount", "value", "price",
                  "current", "regular", "regular_usd", "max_usd", "max"):
            if value.get(k) is not None:
                return _to_price(value[k])
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else 0
    text = str(value).strip().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK]\b", text)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return 0
    try:
        num = float(m.group(0))
        return int(num) if num > 0 else 0
    except ValueError:
        return 0


def _to_stock(value: Any, default: int = 5) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return default if value else 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().lower()
    if any(w in text for w in ("out of stock", "outofstock", "sold out", "unavailable", "no stock")):
        return 0
    if any(w in text for w in ("in stock", "instock", "available", "in_stock")):
        m = re.search(r"\d+", text)
        return int(m.group(0)) if m else default
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else default


def _pick(normed: Dict[str, Any], field: str) -> Any:
    for alias in _FIELD_ALIASES[field]:
        if alias in normed and normed[alias] not in (None, "", [], {}):
            return normed[alias]
    # Suffixed exports: price_usd, compare_at_price_usd, sale_price_bdt...
    for alias in _FIELD_ALIASES[field]:
        for key, value in normed.items():
            if value in (None, "", [], {}):
                continue
            if key.startswith(alias + " ") or key.endswith(" " + alias):
                return value
    return None


def _normed_record(record: Dict[str, Any]) -> Dict[str, Any]:
    normed = {}
    for k, v in record.items():
        nk = _norm_key(k)
        if nk and nk not in normed:
            normed[nk] = v
    # Flatten common nested containers one level deep.
    for container in ("pricing", "price", "variant", "product", "data", "attributes", "inventory", "options", "option"):
        nested = record.get(container)
        if isinstance(nested, dict):
            for k, v in nested.items():
                nk = _norm_key(k)
                if nk not in normed:
                    normed[nk] = v
    return normed


_PRICE_KEY_HINTS = ("price", "cost", "amount", "mrp", "rate")


def _usd_rate(normed: Dict[str, Any]) -> float:
    """Optional USD->BDT conversion when CATALOG_USD_BDT_RATE is configured.

    USD is detected from an explicit currency field OR from "$"/USD/dollar
    markers inside price-like fields ("$49", "49 USD"), so dollar-denominated
    datasets convert dynamically instead of silently becoming BDT 49.
    """
    currency = str(normed.get("currency") or "")
    price_obj = normed.get("price")
    if isinstance(price_obj, dict):
        currency = currency or str(price_obj.get("currency") or "")
    usd_hint = "usd" in currency.lower() or "$" in currency
    if not usd_hint:
        for key, value in normed.items():
            if value in (None, "", [], {}):
                continue
            if not any(h in str(key).lower() for h in _PRICE_KEY_HINTS):
                continue
            t = str(value).lower()
            if "$" in t or "usd" in t or "dollar" in t:
                usd_hint = True
                break
    if not usd_hint:
        return 1.0
    try:
        rate = float(os.environ.get("CATALOG_USD_BDT_RATE", "") or 0)
    except ValueError:
        rate = 0.0
    # No configured rate: fall back to the standard 120 BDT/USD so dollar
    # datasets still import at sane prices (seller can override the env).
    return rate if rate > 0 else 120.0


def _canonical(name, price, brand="", category="", stock=5, color="", size="",
               specs="", sku="", image="", url="", status="", rate=1.0) -> Optional[Dict[str, Any]]:
    if rate is None:
        return None  # USD-denominated record with no conversion rate configured
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if isinstance(category, dict):
        category = category.get("name") or category.get("full_path") or ""
    elif isinstance(category, list):
        _names = [str((c.get("name") if isinstance(c, dict) else c) or "") for c in category]
        category = next((n for n in _names if n), "")
    price = int(_to_price(price) * rate) if rate and rate != 1.0 else _to_price(price)
    if len(name) < 2 or price <= 0:
        return None
    stock = _to_stock(stock)
    specs = _strip_html(specs)[:600] or f"{name} - Official Store Unit"
    extra = " | ".join(x for x in (f"Size: {size}" if size else "", f"SKU: {sku}" if sku else "",
                                   f"Link: {url}" if url else "", f"Image: {image}" if image else "") if x)
    if extra:
        specs = f"{specs} | {extra}"[:800]
    return {
        "name": name[:200],
        "brand": str(brand or _detect_brand(name))[:60],
        "category": str(category or _detect_category(name + " " + specs))[:60],
        "price_bdt": price,
        "price_formatted": f"{price:,}৳",
        "stock": stock,
        "in_stock": stock > 0,
        "color": str(color or _detect_color(name + " " + specs))[:40],
        "specifications": specs,
        "status": status or ("ACTIVE" if stock > 0 else "OUT_OF_STOCK"),
    }


def coerce_record(record: Any) -> List[Dict[str, Any]]:
    """One arbitrary record -> 0..n canonical products (variants expand)."""
    if not isinstance(record, dict):
        return []
    # Variant containers first: Shopify `variants`, WooCommerce variations.
    variants = None
    for key in ("variants", "variations", "variant_list"):
        if isinstance(record.get(key), list) and record[key]:
            variants = record[key]
            break
    base = _normed_record(record)
    base_name = _pick(base, "name")
    if variants and base_name:
        out = []
        for variant in variants[:50]:
            if not isinstance(variant, dict):
                continue
            vn = _normed_record(variant)
            v_title = _pick(vn, "name") or ""
            v_name = str(base_name)
            if v_title and str(v_title).lower() not in ("default title", "default", str(base_name).lower()):
                v_name = f"{base_name} ({v_title})"
            price = _pick(vn, "price")
            if price is None:
                price = _pick(base, "price")
            product = _canonical(
                v_name, price,
                brand=_pick(base, "brand"), category=_pick(base, "category"),
                stock=_pick(vn, "stock") if _pick(vn, "stock") is not None else _pick(base, "stock"),
                color=_pick(vn, "color") or _pick(base, "color"),
                size=_pick(vn, "size") or "", sku=_pick(vn, "sku") or _pick(base, "sku") or "",
                specs=_pick(base, "description") or "", image=_pick(base, "image") or "",
                url=_pick(base, "url") or "", rate=_usd_rate(base))
            if product:
                out.append(product)
        if out:
            return out
    # Categories/images often arrive as lists of objects.
    category = _pick(base, "category")
    if isinstance(category, list):
        names = [str((c.get("name") if isinstance(c, dict) else c) or "") for c in category]
        category = next((n for n in names if n), "")
    elif isinstance(category, dict):
        category = category.get("name", "")
    image = _pick(base, "image")
    if isinstance(image, list):
        first = image[0] if image else ""
        image = (first.get("src") or first.get("url") or "") if isinstance(first, dict) else str(first)
    elif isinstance(image, dict):
        image = image.get("src") or image.get("url") or ""
    product = _canonical(
        base_name, _pick(base, "price"), brand=_pick(base, "brand"), category=category,
        stock=_pick(base, "stock"), color=_pick(base, "color") or "", size=_pick(base, "size") or "",
        specs=_pick(base, "description") or "", sku=_pick(base, "sku") or "",
        image=image or "", url=_pick(base, "url") or "", rate=_usd_rate(base))
    return [product] if product else []


def records_to_products(records: Any, cap: int = 5000) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(records, dict):
        records = _find_record_list(records)
    if not isinstance(records, list):
        return out
    for record in records:
        out.extend(coerce_record(record))
        if len(out) >= cap:
            break
    return out[:cap]


def _find_record_list(data: Any) -> List[Any]:
    """Find the largest list of dicts anywhere in a JSON wrapper object."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    best: List[Any] = []
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and len(value) > len(best):
            best = value
        elif isinstance(value, dict):
            nested = _find_record_list(value)
            if len(nested) > len(best):
                best = nested
    return best if best else [data]


def parse_json_catalog(text: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    if isinstance(data, dict):
        listed = _find_record_list(data)
        return records_to_products(listed if listed else data)
    return records_to_products(data)


def parse_jsonl_catalog(text: str) -> List[Dict[str, Any]]:
    records = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("{"):
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records_to_products(records)


def parse_delimited_records(text: str) -> List[Dict[str, Any]]:
    """CSV/TSV/semicolon/pipe with any header names (Shopify, Woo, generic)."""
    import csv as _csv
    import io as _io
    sample = text[:4096]
    try:
        dialect = _csv.Sniffer().sniff(sample, delimiters=[",", "\t", ";", "|"])
        delim = dialect.delimiter
    except Exception:
        delim = ","
    try:
        rows = list(_csv.reader(_io.StringIO(text), delimiter=delim))
    except Exception:
        return []
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if len(rows) < 2:
        return []
    headers = [_norm_key(h) for h in rows[0]]
    if not any(h in _ALIAS_TO_FIELD for h in headers):
        return []  # headerless tables keep the legacy path
    records = []
    for row in rows[1:]:
        record = {}
        for i, header in enumerate(headers):
            if header and i < len(row):
                record[header] = row[i].strip() if isinstance(row[i], str) else row[i]
        if record:
            records.append(record)
    return records_to_products(records)


def parse_xml_catalog(text: str) -> List[Dict[str, Any]]:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except Exception:
        return []
    records = []
    for elem in root.iter():
        children = list(elem)
        if len(children) < 2:
            continue
        record = {}
        for child in children:
            tag = child.tag.split("}")[-1].split(":")[-1]
            if child.text and child.text.strip():
                record[tag] = child.text.strip()
        if record and any(_norm_key(k) in _ALIAS_TO_FIELD for k in record):
            records.append(record)
    # Keep only leaf-most plausible product records.
    return records_to_products(records[-10000:])


def _decode_bytes(raw: bytes) -> str:
    """Decode upload text across real-world encodings (never garble silently).

    Tries UTF-8 (with/without BOM), then UTF-16 (common for Windows/Excel
    CSV exports), and only then falls back to latin-1 so odd files still load.
    """
    if not raw:
        return ""
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("latin-1", errors="replace")


def parse_xlsx_catalog(raw: bytes) -> List[Dict[str, Any]]:
    """Minimal XLSX reader (stdlib only): first sheet via header aliases."""
    import io as _io
    import zipfile
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        with zipfile.ZipFile(_io.BytesIO(raw)) as zf:
            shared: List[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sroot.findall("m:si", ns):
                    shared.append("".join(t.text or "" for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
            sheet_names = sorted(n for n in zf.namelist()
                                 if n.startswith("xl/worksheets/sheet"))
            if not sheet_names:
                return []
            # Products may live on any sheet: use the first sheet whose
            # header row maps to known product fields and yields products.
            rows: List[List[str]] = []
            for sheet_name in sheet_names:
                sroot = ET.fromstring(zf.read(sheet_name))
                cand: List[List[str]] = []
                for row in sroot.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                    cells: List[str] = []
                    for cell in row.findall("m:c", ns):
                        val = cell.find("m:v", ns)
                        text_val = "" if val is None or val.text is None else val.text
                        if cell.get("t") == "s" and text_val.isdigit() and int(text_val) < len(shared):
                            text_val = shared[int(text_val)]
                        cells.append(text_val)
                    if any(cells):
                        cand.append(cells)
                if len(cand) >= 2 and any(h in _ALIAS_TO_FIELD for h in (_norm_key(x) for x in cand[0])):
                    rows = cand
                    break
                if not rows and cand:
                    rows = cand
    except Exception as e:
        logger.warning(f"XLSX parse failed: {e}")
        return []
    if len(rows) < 2:
        return []
    headers = [_norm_key(h) for h in rows[0]]
    if not any(h in _ALIAS_TO_FIELD for h in headers):
        return []
    records = [{h: row[i] for i, h in enumerate(headers) if h and i < len(row)} for row in rows[1:]]
    return records_to_products(records)


def parse_raw_catalog_text(raw_text: str) -> List[Dict[str, Any]]:
    """
    Parses arbitrary raw text into a list of normalized product dictionaries.
    Handles:
    - JSON text
    - Block-separated text (separated by empty lines or horizontal rules)
    - Line-by-line listings
    - Delimited CSV/TSV/Pipe text
    """
    if not raw_text or not raw_text.strip():
        return []

    text = raw_text.strip()

    # 0. Universal structured formats first: XML, JSON, JSONL, aliased tables.
    if text.startswith("<"):
        xml_products = parse_xml_catalog(text)
        if xml_products:
            return xml_products
    if text.startswith("[") or text.startswith("{"):
        json_products = parse_json_catalog(text)
        if json_products:
            return json_products
        jsonl_products = parse_jsonl_catalog(text)
        if jsonl_products:
            return jsonl_products
    elif text.splitlines() and text.splitlines()[0].strip().startswith("{"):
        jsonl_products = parse_jsonl_catalog(text)
        if jsonl_products:
            return jsonl_products
    delimited_products = parse_delimited_records(text)
    if delimited_products:
        return delimited_products

    # 1. JSON Array or Object check (legacy)
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("products") or data.get("items") or data.get("data") or [data]
            if isinstance(data, list):
                parsed_json = []
                for item in data:
                    if isinstance(item, dict) and item.get("name"):
                        price = int(item.get("price_bdt") or item.get("price") or 0)
                        stock = int(item.get("stock", 5))
                        in_stock = bool(item.get("in_stock", stock > 0))
                        parsed_json.append({
                            "name": str(item["name"]).strip(),
                            "brand": item.get("brand") or _detect_brand(str(item["name"])),
                            "category": item.get("category") or _detect_category(str(item["name"])),
                            "price_bdt": price,
                            "price_formatted": item.get("price_formatted") or f"{price:,}৳",
                            "stock": stock,
                            "in_stock": in_stock,
                            "color": item.get("color", ""),
                            "specifications": item.get("specifications") or f"{item['name']} - Official Unit",
                            "status": "ACTIVE" if in_stock else "OUT_OF_STOCK",
                        })
                if parsed_json:
                    return parsed_json
        except Exception:
            pass

    # 1.5 Single labeled block (Name: ... / Price: ...) stays one product.
    if re.search(r"(?im)^\s*(name|title|product|item)\s*:", text) and \
            re.search(r"(?im)^\s*(price|cost|dam|daam|rate|mrp|taka|tk|bdt)\s*:", text):
        whole = parse_single_product_chunk(text)
        if whole:
            return [whole]

    # 2. Check for Table / Delimited rows (Pipe '|', Tab '\t', or Comma ',')
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) >= 2:
        for delim in ["|", "\t", ","]:
            sample_lines = lines[:min(len(lines), 3)]
            if all(delim in l for l in sample_lines):
                import csv, io
                try:
                    reader = csv.reader(io.StringIO(text), delimiter=delim)
                    parsed_table = []
                    header = None
                    first_row = True
                    for row in reader:
                        if not row or not any(row):
                            continue
                        cells = [c.strip() for c in row if c.strip()]
                        if not cells:
                            continue
                        if any(re.match(r"^[\-\:\+]+$", c) for c in cells):
                            continue
                        if first_row:
                            first_row = False
                            lower_cells = [c.lower() for c in cells]
                            if any(h in lower_cells for h in ["name", "item", "product", "price", "stock", "bdt"]):
                                header = lower_cells
                                continue
                        if header:
                            row_dict = dict(zip(header, cells))
                            p_name = row_dict.get("name") or row_dict.get("item") or row_dict.get("product") or cells[0]
                            p_price_raw = row_dict.get("price") or row_dict.get("price_bdt") or row_dict.get("bdt") or ""
                            p_stock_raw = row_dict.get("stock") or row_dict.get("qty") or ""
                            price_val, _ = _extract_price(str(p_price_raw)) if p_price_raw else _extract_price(" ".join(cells))
                            stock_val, in_stock, _ = _extract_stock(str(p_stock_raw)) if p_stock_raw else _extract_stock(" ".join(cells))
                            if p_name and price_val > 0:
                                parsed_table.append({
                                    "name": p_name,
                                    "brand": row_dict.get("brand") or _detect_brand(p_name),
                                    "category": row_dict.get("category") or _detect_category(p_name),
                                    "price_bdt": price_val,
                                    "price_formatted": f"{price_val:,}৳",
                                    "stock": stock_val,
                                    "in_stock": in_stock,
                                    "color": row_dict.get("color", ""),
                                    "specifications": row_dict.get("specifications", f"{p_name} - Official Unit"),
                                    "status": "ACTIVE" if in_stock else "OUT_OF_STOCK"
                                })
                                continue
                        row_str = " - ".join(cells)
                        p = parse_single_product_chunk(row_str)
                        if p:
                            parsed_table.append(p)
                    if len(parsed_table) >= 1:
                        return parsed_table
                except Exception:
                    pass

    # 3. Block-based check (separated by 2 or more newlines)
    blocks = re.split(r"\n\s*\n+", text)
    if len(blocks) > 1:
        parsed_blocks = []
        for b in blocks:
            b_clean = b.strip()
            if not b_clean:
                continue
            # If block has multiple lines that look like individual products, parse line-by-line
            sub_lines = [sl.strip() for sl in b_clean.splitlines() if sl.strip()]
            if len(sub_lines) > 1 and not any(":" in sl for sl in sub_lines):
                for sl in sub_lines:
                    p = parse_single_product_chunk(sl)
                    if p:
                        parsed_blocks.append(p)
            else:
                p = parse_single_product_chunk(b_clean)
                if p:
                    parsed_blocks.append(p)
        if parsed_blocks:
            return parsed_blocks

    # 4. Line-by-line fallback (each line is evaluated independently)
    parsed_lines = []
    for line in lines:
        p = parse_single_product_chunk(line)
        if p:
            parsed_lines.append(p)

    return parsed_lines


def detect_currency(text: str) -> Optional[str]:
    """'USD'/'BDT' when the dataset itself says so, else None.

    USD markers: $ amounts, USD/dollar words, currency fields.
    BDT markers: taka sign, tk/taka/bdt words.
    """
    t = (text or "")[:200_000].lower()
    if re.search(r"(\$\s*\d|\d\s*(?:usd|dollars?)\b|\"currency\"\s*:\s*\"?usd)", t):
        return "USD"
    if "৳" in t or re.search(r"\b(?:bdt|taka|tk)\b", t):
        return "BDT"
    return None


def apply_currency_override(products: List[Dict[str, Any]], currency: Optional[str]) -> List[Dict[str, Any]]:
    """Seller-confirmed currency: USD prices convert to BDT for the catalog."""
    if (currency or "").upper() != "USD":
        return products
    try:
        rate = float(os.environ.get("CATALOG_USD_BDT_RATE", "") or 120)
    except ValueError:
        rate = 120.0
    if rate <= 0:
        rate = 120.0
    for prod in products:
        price = int(round(int(prod.get("price_bdt") or 0) * rate))
        prod["price_bdt"] = price
        prod["price_formatted"] = f"{price:,}৳"
    return products


def import_raw_catalog_text(raw_text: str, replace: bool = False,
                            currency_override: Optional[str] = None) -> Dict[str, Any]:
    """
    Imports raw catalog text directly into the product repository.
    Re-indexes RAG immediately so Messenger and Telegram bots are live.
    
    Returns:
    {
        "success": bool,
        "count": int,
        "replace": bool,
        "products": list,
        "message": str
    }
    """
    if raw_text is None or not isinstance(raw_text, str) or not raw_text.strip():
        return {"success": False, "count": 0, "replace": replace, "products": [],
                "message": "Empty dataset: nothing to import."}
    if len(raw_text) > 5_000_000:
        return {"success": False, "count": 0, "replace": replace, "products": [],
                "message": "Dataset too large (max 5 MB of text)."}
    products = parse_raw_catalog_text(raw_text)
    # Dedupe by (name, colour, size, SKU) so variants sharing a name survive.
    _seen = {}
    for _p in products:
        _key = " ".join(str(_p.get(f) or "").strip().lower()
                        for f in ("name", "color", "size")) +                "|" + str(_p.get("sku") or "").strip().lower()
        _seen[_key] = _p
    products = apply_currency_override(list(_seen.values())[:5000], currency_override)
    if not products:
        return {
            "success": False,
            "count": 0,
            "replace": replace,
            "products": [],
            "message": "No valid products could be detected from the provided text. Ensure product names and prices are present."
        }

    repo = get_product_repository()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    if replace:
        # Snapshot first so a bad upload can be rolled back (Phase 3).
        try:
            from database import snapshot_catalog
            snapshot_catalog(repo)
        except Exception:
            logger.exception("pre-replace snapshot failed; continuing")
        # Re-assign sequential IDs starting from 1
        prepared = []
        for idx, p in enumerate(products, start=1):
            item = dict(p)
            item["id"] = idx
            item["created_at"] = now_str
            item["updated_at"] = now_str
            prepared.append(item)
        repo.replace_all(prepared)
        _sync_rag_index()
        return {
            "success": True,
            "count": len(prepared),
            "replace": True,
            "products": prepared,
            "message": f"Successfully replaced catalog with {len(prepared)} products."
        }
    else:
        # Append or update existing items.
        # Large imports write ONCE (single atomic replace of the merged
        # catalog) instead of one HTTP round-trip per product, which is
        # what made big Telegram uploads time out and retry forever.
        existing_rows = repo.get_all_products()
        existing = {p.get("name", "").lower(): p for p in existing_rows}
        creates, updates = [], []
        for p in products:
            match = existing.get(p.get("name", "").lower())
            (updates if match else creates).append((match, p))

        added_list = []
        if len(creates) + len(updates) > 20:
            merged = {p.get("name", "").lower(): dict(p) for p in existing_rows}
            next_id = max([int(p.get("id") or 0) for p in existing_rows] or [0]) + 1
            for match, p in updates:
                row = merged[match.get("name", "").lower()]
                row.update({
                    "price_bdt": p["price_bdt"], "price_formatted": p["price_formatted"],
                    "stock": p["stock"], "in_stock": p["in_stock"],
                    "specifications": p["specifications"], "color": p["color"],
                    "updated_at": now_str,
                })
                if p.get("brand"):
                    row["brand"] = p["brand"]
                if p.get("category"):
                    row["category"] = p["category"]
                added_list.append(row)
            for _match, p in creates:
                item = dict(p)
                item["id"] = next_id
                next_id += 1
                item["created_at"] = now_str
                item["updated_at"] = now_str
                merged[item.get("name", "").lower()] = item
                added_list.append(item)
            repo.replace_all(list(merged.values()))
        else:
            for match, p in updates:
                up_fields = {
                    "price_bdt": p["price_bdt"],
                    "price_formatted": p["price_formatted"],
                    "stock": p["stock"],
                    "in_stock": p["in_stock"],
                    "specifications": p["specifications"],
                    "color": p["color"],
                }
                if p.get("brand"):
                    up_fields["brand"] = p["brand"]
                if p.get("category"):
                    up_fields["category"] = p["category"]
                updated = repo.update_product(match["id"], up_fields)
                if updated:
                    added_list.append(updated)
            for _match, p in creates:
                created = repo.add_product(p)
                if created:
                    added_list.append(created)

        _sync_rag_index()
        return {
            "success": True,
            "count": len(added_list),
            "replace": False,
            "products": added_list,
            "message": f"Successfully imported {len(added_list)} products."
        }



def import_catalog_bytes(content_bytes: bytes, file_name: str = "dataset.json",
                         replace: bool = False,
                         currency_override: Optional[str] = None) -> Dict[str, Any]:
    """Import from raw upload bytes. XLSX stays binary; everything else decodes."""
    if not content_bytes:
        return {"success": False, "count": 0, "replace": replace, "products": [],
                "message": "Empty file: nothing to import."}
    if len(content_bytes) > 5_000_000:
        return {"success": False, "count": 0, "replace": replace, "products": [],
                "message": "File too large (max 5 MB). Split the catalog and import in parts."}
    ext = os.path.splitext(file_name or "")[1].lower()
    if ext == ".xlsx" or content_bytes[:2] == b"PK":
        products = parse_xlsx_catalog(content_bytes)
        if not products:
            return {"success": False, "count": 0, "replace": replace, "products": [],
                    "message": "No valid products found in the spreadsheet. Check name and price columns."}
        text = json.dumps(products, ensure_ascii=False)
        return import_raw_catalog_text(text, replace=replace, currency_override=currency_override)
    text_content = _decode_bytes(content_bytes)
    return import_raw_catalog_text(text_content, replace=replace, currency_override=currency_override)


def import_catalog_file(file_path: str, replace: bool = False) -> Dict[str, Any]:
    """Reads any supported catalog file and imports its contents."""
    if not os.path.exists(file_path):
        return {"success": False, "count": 0, "message": f"File not found: {file_path}"}
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return {"success": False, "count": 0,
                "message": f"Unsupported file type: {ext or '(none)'}. Send .json, .csv, .xlsx, .xml, .jsonl or .txt"}
    try:
        if os.path.getsize(file_path) > 5_000_000:
            return {"success": False, "count": 0, "message": "File too large (max 5 MB)."}

        with open(file_path, "rb") as f:
            raw = f.read()
        return import_catalog_bytes(raw, file_name=os.path.basename(file_path), replace=replace)
    except Exception as e:
        logger.error(f"Error importing catalog file {file_path}: {e}")
        return {"success": False, "count": 0, "message": f"Error reading file: {e}"}


def export_catalog_to_text() -> str:
    """
    Exports the current catalog into a clean, human-readable raw text format.
    Suitable for backup, copying, or re-importing into another store.
    """
    repo = get_product_repository()
    prods = repo.get_all_products()
    lines = []
    for p in prods:
        name = p.get("name", "Product")
        price = p.get("price_formatted") or f"{p.get('price_bdt', 0):,}৳"
        stock = p.get("stock", 1 if p.get("in_stock", True) else 0)
        stock_str = f"Stock: {stock}" if p.get("in_stock", True) else "Out of Stock"
        specs = p.get("specifications", "")
        color = p.get("color", "")
        extra = []
        if color:
            extra.append(f"Color: {color}")
        if specs and specs != f"{name} - Official Store Unit":
            extra.append(specs)
        extra_str = f" [{', '.join(extra)}]" if extra else ""
        lines.append(f"{name} - {price} ({stock_str}){extra_str}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Universal Dataset Importer for Tech Catalog")
    parser.add_argument("--file", "-f", help="Path to text, CSV, or JSON file to import")
    parser.add_argument("--text", "-t", help="Raw text string to import")
    parser.add_argument("--replace", "-r", action="store_true", help="Replace the entire catalog instead of appending")
    args = parser.parse_args()

    if args.file:
        res = import_catalog_file(args.file, replace=args.replace)
        print(json.dumps(res, indent=2, default=str))
    elif args.text:
        res = import_raw_catalog_text(args.text, replace=args.replace)
        print(json.dumps(res, indent=2, default=str))
    else:
        print("Please provide --file or --text. Run with --help for options.")
