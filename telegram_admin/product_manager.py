import html
import logging
from typing import Dict, List, Optional, Tuple

from database import get_product_repository

logger = logging.getLogger(__name__)


def _sync_rag_index():
    """Trigger real-time re-indexing of RAG knowledge base for Messenger bot."""
    try:
        from rag_service import get_rag
        rag = get_rag()
        rag.load_and_index()
        logger.info("Successfully re-indexed RAG memory after catalog mutation.")
    except Exception as e:
        logger.warning(f"Failed to auto-refresh RAG index: {e}")


def format_product_card(product: Dict) -> str:
    """Renders a product's details into clean, elegant HTML for Telegram."""
    p_id = product.get("id")
    name = html.escape(str(product.get("name", "Unknown Product")))
    brand = html.escape(str(product.get("brand", "Unknown")))
    category = html.escape(str(product.get("category", "General")))
    raw_price = product.get("price_bdt")
    try:
        price_val = int(raw_price) if raw_price is not None else 0
    except (ValueError, TypeError):
        price_val = 0
    price = product.get("price_formatted") or f"৳{price_val:,}"

    raw_stock = product.get("stock")
    try:
        stock = int(raw_stock) if raw_stock is not None else (1 if product.get("in_stock", True) else 0)
    except (ValueError, TypeError):
        stock = 0
    in_stock = bool(product.get("in_stock", True) and stock > 0)
    stock_label = f"🟢 In Stock ({stock} units)" if in_stock else "🔴 Out of Stock (0)"
    color = html.escape(str(product.get("color", "") or "Standard"))
    specs = html.escape(str(product.get("specifications", "") or "No specs listed."))

    lines = [
        f"📱 <b>{name}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏷️ <b>Brand:</b> {brand}",
        f"📂 <b>Category:</b> {category}",
        f"💰 <b>Price:</b> <code>{price}</code>",
        f"📊 <b>Stock Status:</b> {stock_label}",
        f"🎨 <b>Color / Variant:</b> {color}",
        f"🆔 <b>Product ID:</b> <code>#{p_id}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📝 <b>Specifications:</b>\n<i>{specs}</i>"
    ]
    return "\n".join(lines)


def format_store_summary() -> str:
    """Generates the Store Summary overview card in HTML."""
    repo = get_product_repository()
    summary = repo.get_summary()

    total = summary.get("total_products", 0)
    in_stock = summary.get("in_stock_count", 0)
    out_of_stock = summary.get("out_of_stock_count", 0)
    categories = summary.get("total_categories", 0)
    brands = summary.get("total_brands", 0)
    total_val = summary.get("total_catalog_value", 0)
    low_stock = summary.get("low_stock_count", 0)

    lines = [
        "📈 <b>STORE CATALOG SUMMARY</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📦 <b>Total Products:</b> {total:,}",
        f"🟢 <b>In Stock:</b> {in_stock:,}",
        f"🔴 <b>Out of Stock:</b> {out_of_stock:,}",
        f"⚠️ <b>Low Stock Alert (≤3):</b> {low_stock:,}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📂 <b>Categories:</b> {categories}",
        f"🏷️ <b>Brands:</b> {brands}",
        f"💵 <b>Total Inventory Value:</b> ৳{total_val:,}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "⚡ <i>Synced live with Facebook Messenger bot.</i>"
    ]
    return "\n".join(lines)


def get_products_page(page: int = 1, per_page: int = 6) -> Tuple[List[Dict], int]:
    """Retrieves paginated products and total page count."""
    repo = get_product_repository()
    all_prods = repo.get_all_products()
    total_items = len(all_prods)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    safe_page = max(1, min(page, total_pages))

    start_idx = (safe_page - 1) * per_page
    end_idx = start_idx + per_page
    return all_prods[start_idx:end_idx], total_pages


def search_products_page(query: str, page: int = 1, per_page: int = 6) -> Tuple[List[Dict], int]:
    """Performs lexical search and returns paginated matching products."""
    repo = get_product_repository()
    matches = repo.search_lexical(query, limit=100)
    total_items = len(matches)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    safe_page = max(1, min(page, total_pages))

    start_idx = (safe_page - 1) * per_page
    end_idx = start_idx + per_page
    return matches[start_idx:end_idx], total_pages


def get_product(product_id: int) -> Optional[Dict]:
    """Fetches a single product by ID."""
    repo = get_product_repository()
    return repo.get_by_id(product_id)


def update_price_exact(product_id: int, new_price: int) -> Optional[Dict]:
    """Sets an exact price for a product and triggers live sync."""
    if new_price < 0:
        return None
    repo = get_product_repository()
    updated = repo.update_product(product_id, {
        "price_bdt": new_price,
        "price_formatted": f"BDT {new_price:,}"
    })
    if updated:
        _sync_rag_index()
    return updated


def adjust_price_pct(product_id: int, percentage: float) -> Optional[Dict]:
    """Adjusts price by percentage (+5%, -10%, etc.)."""
    repo = get_product_repository()
    prod = repo.get_by_id(product_id)
    if not prod:
        return None
    raw_p = prod.get("price_bdt")
    try:
        curr_price = int(raw_p) if raw_p is not None else 0
    except (ValueError, TypeError):
        curr_price = 0
    new_price = max(100, int(round(curr_price * (1.0 + (percentage / 100.0)))))
    return update_price_exact(product_id, new_price)


def update_stock_exact(product_id: int, new_stock: int) -> Optional[Dict]:
    """Sets exact stock level (stock cannot be negative)."""
    clean_stock = max(0, new_stock)
    repo = get_product_repository()
    updated = repo.update_product(product_id, {
        "stock": clean_stock,
        "in_stock": clean_stock > 0,
        "status": "ACTIVE" if clean_stock > 0 else "OUT_OF_STOCK"
    })
    if updated:
        _sync_rag_index()
    return updated


def adjust_stock_delta(product_id: int, delta: int) -> Optional[Dict]:
    """Adjusts stock by +1, -1, +5, etc."""
    repo = get_product_repository()
    prod = repo.get_by_id(product_id)
    if not prod:
        return None
    raw_s = prod.get("stock")
    try:
        curr_stock = int(raw_s) if raw_s is not None else (1 if prod.get("in_stock", True) else 0)
    except (ValueError, TypeError):
        curr_stock = 0
    new_stock = max(0, curr_stock + delta)
    return update_stock_exact(product_id, new_stock)


def delete_product(product_id: int) -> bool:
    """Deletes a product and refreshes RAG memory."""
    repo = get_product_repository()
    success = repo.delete_product(product_id)
    if success:
        _sync_rag_index()
    return success


def create_product(product_data: Dict) -> Dict:
    """Inserts a new product into the database and refreshes RAG memory."""
    repo = get_product_repository()
    created = repo.add_product(product_data)
    _sync_rag_index()
    return created


def update_color(product_id: int, new_color: str) -> Optional[Dict]:
    """Updates color/variant for a product and triggers live sync."""
    repo = get_product_repository()
    updated = repo.update_product(product_id, {"color": str(new_color).strip()})
    if updated:
        _sync_rag_index()
    return updated


def update_specs(product_id: int, new_specs: str) -> Optional[Dict]:
    """Updates specifications for a product and triggers live sync."""
    repo = get_product_repository()
    updated = repo.update_product(product_id, {"specifications": str(new_specs).strip()})
    if updated:
        _sync_rag_index()
    return updated


def import_raw_products(raw_text: str, replace: bool = False) -> Dict:
    """Imports raw text products and triggers live RAG re-indexing."""
    from dataset_importer import import_raw_catalog_text
    return import_raw_catalog_text(raw_text, replace=replace)


def import_catalog_file_path(file_path: str, replace: bool = False) -> Dict:
    """Imports a file (.txt, .csv, .json) and triggers live RAG re-indexing."""
    from dataset_importer import import_catalog_file
    return import_catalog_file(file_path, replace=replace)

