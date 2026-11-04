import html
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Union

import config
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bot_state import is_bot_active, set_bot_active
from telegram_admin.admin_auth import is_admin
from telegram_admin.admin_keyboards import (
    back_to_menu_keyboard,
    confirm_delete_keyboard,
    export_format_keyboard,
    main_menu_keyboard,
    product_detail_keyboard,
    products_list_keyboard,
    quick_price_keyboard,
    quick_stock_keyboard,
)
from telegram_admin.admin_state import (
    AdminState,
    clear_admin_session,
    get_admin_session,
)
from telegram_admin.product_manager import (
    adjust_price_pct,
    adjust_stock_delta,
    create_product,
    delete_product,
    format_product_card,
    format_store_summary,
    get_product,
    get_products_page,
    search_products_page,
    update_color,
    update_price_exact,
    update_specs,
    update_stock_exact,
)

logger = logging.getLogger(__name__)

# --- Webhook redelivery + import spam guards ---------------------------------
# Telegram redelivers an update when the webhook is slow (big imports take
# seconds). Without dedupe the same document import restarts forever and
# spams "Processing dataset..." messages. These guards make every update
# and every file idempotent, and serialize imports per chat.
_seen_updates: "OrderedDict[int, float]" = OrderedDict()
_update_lock = threading.Lock()
_processed_files: Dict[str, float] = {}
_active_imports: set = set()
_import_guard = threading.Lock()
_last_callback: Dict[str, float] = {}


def _is_duplicate_update(update: Dict[str, Any]) -> bool:
    update_id = update.get("update_id")
    if update_id is None:
        return False
    now = time.time()
    with _update_lock:
        while _seen_updates and now - next(iter(_seen_updates.values())) > 600:
            _seen_updates.popitem(last=False)
        if update_id in _seen_updates:
            return True
        if len(_seen_updates) >= 2000:
            _seen_updates.popitem(last=False)
        _seen_updates[update_id] = now
        return False


def _file_already_imported(unique_id: str) -> bool:
    if not unique_id:
        return False
    with _import_guard:
        done_at = _processed_files.get(unique_id)
        return bool(done_at and time.time() - done_at < 1800)


def _mark_file_imported(unique_id: str):
    if not unique_id:
        return
    with _import_guard:
        _processed_files[unique_id] = time.time()
        if len(_processed_files) > 500:
            oldest = sorted(_processed_files, key=_processed_files.get)[:250]
            for key in oldest:
                _processed_files.pop(key, None)



_tg_session = requests.Session()
_retries = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504, 429],
    raise_on_status=False
)
_tg_session.mount("https://", HTTPAdapter(max_retries=_retries, pool_connections=10, pool_maxsize=20))


def _get_bot_token() -> str:
    return getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""


def _fit_telegram_text(text: str, limit: int = 4000) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    suffix = "\n<i>...truncated, full data in catalog.</i>"
    return text[: limit - len(suffix)].rstrip() + suffix


def _cb_id(data: str, index: int = 1):
    """Safe numeric callback field: stale/malformed buttons return None."""
    try:
        return int(str(data).split(":")[index])
    except (IndexError, ValueError):
        return None


def send_tg_message(
    chat_id: Union[int, str],
    text: str,
    reply_markup: Optional[Dict] = None,
    parse_mode: str = "HTML"
) -> Optional[int]:
    """Sends a text message to a Telegram chat."""
    text = _fit_telegram_text(text)
    token = _get_bot_token()
    if not token or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = _tg_session.post(url, json=payload, timeout=10)
        if resp.ok:
            return resp.json().get("result", {}).get("message_id")
        else:
            logger.error(f"Telegram sendMessage error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Error calling Telegram sendMessage: {e}")
    return None


def send_tg_document(
    chat_id: Union[int, str],
    file_bytes: bytes,
    file_name: str,
    caption: str = ""
) -> bool:
    """Sends a document / file attachment to Telegram chat."""
    token = _get_bot_token()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    files = {"document": (file_name, file_bytes)}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    try:
        resp = _tg_session.post(url, data=data, files=files, timeout=30)
        return resp.ok
    except Exception as e:
        logger.error(f"Telegram sendDocument error: {e}")
        return False


def edit_tg_message(
    chat_id: Union[int, str],
    message_id: int,
    text: str,
    reply_markup: Optional[Dict] = None,
    parse_mode: str = "HTML"
) -> bool:
    """Edits an existing text message and/or its inline keyboard in place."""
    text = _fit_telegram_text(text)
    token = _get_bot_token()
    if not token or not chat_id or not message_id:
        return False
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = _tg_session.post(url, json=payload, timeout=10)
        if resp.ok:
            return True
        elif "message is not modified" in resp.text:
            return True
        elif "message to edit not found" in resp.text:
            logger.debug(f"Target message {message_id} not found to edit; fallback will send fresh message.")
            return False
        else:
            logger.error(f"Telegram editMessageText error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Error calling Telegram editMessageText: {e}")
    return False


def delete_tg_message(chat_id: Union[int, str], message_id: int) -> bool:
    """Deletes an existing message from chat to eliminate clutter."""
    token = _get_bot_token()
    if not token or not chat_id or not message_id:
        return False
    url = f"https://api.telegram.org/bot{token}/deleteMessage"
    try:
        resp = _tg_session.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
        return resp.ok
    except Exception as e:
        logger.warning(f"Error calling Telegram deleteMessage: {e}")
        return False


def edit_or_send_tg_message(
    chat_id: Union[int, str],
    message_id: Optional[int],
    text: str,
    reply_markup: Optional[Dict] = None,
    parse_mode: str = "HTML"
) -> Optional[int]:
    """
    Attempts to edit an existing message in-place.
    If editing fails (e.g., message not found, expired, or non-text),
    gracefully falls back to sending a new message so the user never encounters a frozen UI.
    Returns the message_id of the edited or newly sent message.
    """
    if message_id:
        success = edit_tg_message(chat_id, message_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        if success:
            return message_id
    sent_id = send_tg_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    return sent_id


def answer_callback_query(callback_query_id: str, text: Optional[str] = None):
    """Answers a Telegram callback query to dismiss the loading animation."""
    token = _get_bot_token()
    if not token or not callback_query_id:
        return
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        _tg_session.post(url, json=payload, timeout=5)
    except Exception:
        pass


def show_main_menu(chat_id: Union[int, str], message_id: Optional[int] = None, user_id: Optional[Any] = None) -> Optional[int]:
    """Displays the Seller Control Panel main menu."""
    from database import has_active_catalog
    has_db = has_active_catalog()

    if not has_db:
        text = (
            "⚠️ <b>NO PRODUCT DATABASE FOUND!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>Messenger Auto-Reply:</b> ⏸️ <i>Muted (Awaiting Catalog)</i>\n\n"
            "Your store catalog is currently empty or database is disconnected.\n"
            "Messenger will <b>NOT</b> reply to customers until catalog products are added.\n\n"
            "📥 <b>Please add or upload your products:</b>\n"
            "1️⃣ <b>Paste raw text directly here in chat:</b>\n"
            "   • E.g.: <code>iPhone 15 Pro 256GB - 120,000 tk (Stock: 5)</code>\n"
            "2️⃣ <b>Or upload a file:</b>\n"
            "   • Send a <code>.txt</code>, <code>.csv</code>, or <code>.json</code> document\n\n"
            "Tap an action below to add products:"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "📥 Import Dataset", "callback_data": "menu_import"},
                    {"text": "➕ Add Product", "callback_data": "menu_add_product"}
                ],
                [
                    {"text": "🔄 Replace Dataset", "callback_data": "menu_replace"},
                    {"text": "❓ Help", "callback_data": "menu_help"}
                ],
                [
                    {"text": "❌ Close Panel", "callback_data": "menu_close"}
                ]
            ]
        }
    else:
        bot_status = "🟢 Active" if is_bot_active() else "🔴 Paused"
        text = (
            "🎛️ <b>SELLER CONTROL PANEL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>Messenger Auto-Reply:</b> {bot_status}\n"
            "Welcome! Manage your product catalog, prices, and stock in real-time.\n"
            "Select an action below:"
        )
        kb = main_menu_keyboard()

    active_id = edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
    if user_id:
        session = get_admin_session(user_id)
        if active_id:
            session.panel_message_id = active_id
    return active_id


def parse_product_input(text: str) -> Dict[str, Any]:
    """
    Parses natural language or structured product entries into catalog product data.
    Handles inputs like:
      - 'iPhone 15 Pro 256GB Natural Titanium 125000 taka stock 5'
      - 'Samsung Galaxy S24 Ultra 512GB Titanium Black 145000 tk stock: 10'
      - 'Sony WH-1000XM5 Black 38000 taka'
      - Structured key-value formats (Name:, Price:, Stock:, etc.)
    """
    clean_text = text.strip()
    result = {
        "name": "",
        "brand": "General",
        "category": "Electronics",
        "price_bdt": 0,
        "price_formatted": "",
        "stock": 1,
        "in_stock": True,
        "color": "",
        "specifications": "",
        "status": "ACTIVE"
    }

    # 1. Key:value structured format check
    lines = clean_text.splitlines()
    kv_pairs = {}
    for line in lines:
        if ":" in line:
            parts = line.split(":", 1)
            k = parts[0].strip().lower()
            v = parts[1].strip()
            if k in ("name", "title", "product", "item"):
                kv_pairs["name"] = v
            elif k in ("price", "cost", "dam", "daam", "rate", "taka", "tk", "bdt", "মূল্য"):
                p_digits = re.findall(r"\d+", v.replace(",", ""))
                if p_digits:
                    kv_pairs["price_bdt"] = int(p_digits[0])
            elif k in ("stock", "qty", "quantity", "count", "units", "ইন_স্টক"):
                s_digits = re.findall(r"\d+", v)
                if s_digits:
                    kv_pairs["stock"] = int(s_digits[0])
            elif k in ("brand", "make", "manufacturer", "ব্র্যান্ড"):
                kv_pairs["brand"] = v
            elif k in ("category", "cat", "ক্যাটাগরি"):
                kv_pairs["category"] = v
            elif k in ("color", "colour", "variant", "edition", "কালার", "রং"):
                kv_pairs["color"] = v
            elif k in ("specs", "specifications", "description", "details", "বর্ণনা"):
                kv_pairs["specifications"] = v

    if kv_pairs.get("name") or kv_pairs.get("price_bdt"):
        result.update(kv_pairs)
        if result["price_bdt"]:
            result["price_formatted"] = f"BDT {result['price_bdt']:,}"
        result["in_stock"] = result["stock"] > 0
        result["status"] = "ACTIVE" if result["in_stock"] else "OUT_OF_STOCK"
        return result

    # 2. Natural language parsing
    working = " " + clean_text + " "

    # Stock
    stock_val = 1
    stock_match = re.search(r"(?:\bstock\b|\bqty\b|\bquantity\b|\bcount\b)\s*[:=]?\s*(\d+)", working, re.I)
    if stock_match:
        stock_val = int(stock_match.group(1))
        working = working[:stock_match.start()] + " " + working[stock_match.end():]
    else:
        pcs_match = re.search(r"\b(\d+)\s*(?:pcs|pieces|units|ta|টা|item|items)\b", working, re.I)
        if pcs_match:
            stock_val = int(pcs_match.group(1))
            working = working[:pcs_match.start()] + " " + working[pcs_match.end():]

    result["stock"] = max(0, stock_val)
    result["in_stock"] = result["stock"] > 0
    result["status"] = "ACTIVE" if result["in_stock"] else "OUT_OF_STOCK"

    # Price
    price_val = 0
    explicit_price = re.search(
        r"(?:(?:price|cost|dam|daam|cost|মূল্য)\s*[:=]?\s*(\d[\d,]*))|"
        r"(?:(?:৳|bdt|tk|taka|টাকা)\s*[:=]?\s*(\d[\d,]*))|"
        r"(?:(\d[\d,]*)\s*(?:৳|bdt|tk|taka|টাকা)\b)",
        working,
        re.I
    )
    if explicit_price:
        raw_num = explicit_price.group(1) or explicit_price.group(2) or explicit_price.group(3)
        clean_num = raw_num.replace(",", "")
        if clean_num.isdigit():
            price_val = int(clean_num)
            working = working[:explicit_price.start()] + " " + working[explicit_price.end():]
    else:
        candidates = []
        for m in re.finditer(r"\b(\d[\d,]*)\b(?!\s*(?:gb|tb|mb|hz|ghz|mah|w|watt|mp|inch|mm|core|k)\b)", working, re.I):
            c_str = m.group(1).replace(",", "")
            if c_str.isdigit() and int(c_str) >= 500:
                candidates.append((int(c_str), m.start(), m.end()))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            chosen_price, c_start, c_end = candidates[0]
            price_val = chosen_price
            working = working[:c_start] + " " + working[c_end:]

    result["price_bdt"] = price_val
    if price_val > 0:
        result["price_formatted"] = f"BDT {price_val:,}"

    # Brand
    detected_brand = "General"
    brand_keywords = [
        ("apple", "Apple"), ("iphone", "Apple"), ("ipad", "Apple"), ("macbook", "Apple"),
        ("samsung", "Samsung"), ("galaxy", "Samsung"),
        ("xiaomi", "Xiaomi"), ("redmi", "Xiaomi"), ("poco", "Xiaomi"),
        ("asus", "Asus"), ("rog", "Asus"), ("tuf", "Asus"), ("zenbook", "Asus"),
        ("dell", "Dell"), ("alienware", "Dell"), ("xps", "Dell"),
        ("hp", "HP"), ("spectre", "HP"), ("envy", "HP"), ("victus", "HP"), ("omen", "HP"),
        ("lenovo", "Lenovo"), ("thinkpad", "Lenovo"), ("legion", "Lenovo"), ("ideapad", "Lenovo"),
        ("sony", "Sony"), ("playstation", "Sony"),
        ("google", "Google Pixel"), ("pixel", "Google Pixel"),
        ("oneplus", "OnePlus"),
        ("nothing", "Nothing"),
        ("acer", "Acer"), ("predator", "Acer"),
        ("razer", "Razer"), ("msi", "MSI"),
        ("jbl", "JBL"), ("bose", "Bose"), ("marshall", "Marshall"),
        ("anker", "Anker"), ("logitech", "Logitech"),
        ("oppo", "Oppo"), ("vivo", "Vivo"), ("realme", "Realme")
    ]
    w_lower = working.lower()
    for kw, b_name in brand_keywords:
        if re.search(r"\b" + re.escape(kw) + r"\b", w_lower):
            detected_brand = b_name
            break
    result["brand"] = detected_brand

    # Category
    detected_cat = "Electronics"
    if any(k in w_lower for k in ("phone", "iphone", "galaxy", "smartphone", "mobile", "android")):
        detected_cat = "Smartphones"
    elif any(k in w_lower for k in ("laptop", "macbook", "notebook", "thinkpad", "zenbook", "chromebook", "ultrabook")):
        detected_cat = "Laptops"
    elif any(k in w_lower for k in ("wh-1000", "wf-1000", "headphone", "earphone", "earbud", "earbuds", "airpod", "airpods", "tws", "speaker", "audio", "soundbar")):
        detected_cat = "Audio & Headphones"
    elif any(k in w_lower for k in ("watch", "smartwatch", "fitness band")):
        detected_cat = "Smartwatches & Fitness"
    elif any(k in w_lower for k in ("camera", "dslr", "gimbal")):
        detected_cat = "Cameras & Photography"
    elif any(k in w_lower for k in ("monitor", "display", "screen")):
        detected_cat = "Monitors & Displays"
    elif any(k in w_lower for k in ("tablet", "ipad", "tab")):
        detected_cat = "Tablets"
    elif any(k in w_lower for k in ("mouse", "keyboard", "charger", "cable", "power bank", "hub", "dock")):
        detected_cat = "Accessories & Peripherals"
    result["category"] = detected_cat

    # Color
    color_patterns = [
        "natural titanium", "black titanium", "white titanium", "blue titanium",
        "desert titanium", "space black", "space gray", "space grey",
        "midnight", "starlight", "phantom black", "titanium gray", "titanium black",
        "titanium violet", "titanium yellow", "deep purple", "sierra blue",
        "pacific blue", "alpine green", "silver", "gold", "black", "white",
        "blue", "green", "red", "purple", "pink", "yellow", "orange", "gray", "grey"
    ]
    detected_color = ""
    for cp in color_patterns:
        if re.search(r"\b" + re.escape(cp) + r"\b", w_lower):
            detected_color = cp.title()
            break
    result["color"] = detected_color

    clean_rem = re.sub(r"\s+", " ", working).strip()
    result["name"] = clean_rem if clean_rem else clean_text

    specs_parts = []
    storage_matches = re.findall(r"\b\d+\s*(?:gb|tb)\b", clean_text, re.I)
    if storage_matches:
        specs_parts.append(f"Storage: {', '.join(storage_matches)}")
    if detected_color:
        specs_parts.append(f"Color: {detected_color}")
    if specs_parts:
        result["specifications"] = " | ".join(specs_parts)
    else:
        result["specifications"] = f"{result['name']} - Official Store Unit"

    return result


# Backwards compatibility alias
_parse_natural_product_input = parse_product_input


def handle_callback_query(cb: Dict[str, Any]):
    """Processes inline keyboard button clicks."""
    cb_id = cb.get("id")
    if cb_id:
        try:
            answer_callback_query(cb_id)
        except Exception:
            pass

    from_user = cb.get("from", {})
    user_id = from_user.get("id")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    data = cb.get("data", "")

    if not is_admin(user_id, chat_id):
        send_tg_message(chat_id, "⛔ <i>Unauthorized. Access restricted to store administrators.</i>")
        return

    session = get_admin_session(user_id)
    if message_id:
        session.panel_message_id = message_id

    _cb_key = f"{user_id}:{data}"
    _now = time.time()
    if data.startswith(("stock_adj:", "price_pct:", "stock_set", "prod_delete_confirm")):
        if _now - _last_callback.get(_cb_key, 0.0) < 1.5:
            return
        _last_callback[_cb_key] = _now

    try:
        # 0a. Reply-language picker
        if data.startswith("lang:"):
            from language_settings import set_language_mode
            _mode = set_language_mode(data.split(":", 1)[1])
            _labels = {"auto": "Auto (adapt to customer)", "en": "English", "bn": "Bangla"}
            if cb_id:
                answer_callback_query(cb_id, text=f"Language: {_labels.get(_mode, _mode)}")
            edit_or_send_tg_message(
                chat_id, message_id,
                f"\U0001f310 <b>Default reply language set to: {_labels.get(_mode, _mode)}</b>\n"
                f"Messenger replies will follow this from the next customer message.",
                reply_markup=main_menu_keyboard())
            return

        # 0b. Import currency answer (BDT/USD chosen by the seller)
        if data.startswith("impcur:"):
            _cur = data.split(":", 1)[1].upper()
            if _cur not in ("BDT", "USD"):
                return
            _pend = dict(session.data or {})
            session.clear()
            _do_currency_import(chat_id, session, _pend, _cur)
            return

        # 0. Close Control Panel
        if data == "menu_close":
            if message_id:
                delete_tg_message(chat_id, message_id)
            if session.prompt_message_id:
                delete_tg_message(chat_id, session.prompt_message_id)
            session.clear()
            if cb_id:
                answer_callback_query(cb_id, text="Control panel closed.")
            return

        # 0b. No-operation button (e.g. page number label)
        if data == "noop":
            return

        # 1. Main menu navigation
        if data == "menu_main":
            session.clear()
            show_main_menu(chat_id, message_id, user_id=user_id)
            return

        # Check if database has products for catalog viewing actions
        from database import has_active_catalog
        if not has_active_catalog() and (data.startswith("menu_products") or data.startswith("menu_stock") or data.startswith("menu_prices") or data == "menu_summary"):
            text = (
                "⚠️ <b>NO PRODUCT DATABASE FOUND!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Your catalog is currently empty or database is disconnected.\n"
                "Messenger will not reply to customers until products are added.\n\n"
                "📥 <b>Please add or upload your products:</b>\n"
                "• Paste product listings directly in chat\n"
                "• Or upload a <code>.txt</code> / <code>.csv</code> / <code>.json</code> document."
            )
            kb = {
                "inline_keyboard": [
                    [{"text": "📥 Import Dataset", "callback_data": "menu_import"}, {"text": "➕ Add Product", "callback_data": "menu_add_product"}],
                    [{"text": "🏠 Main Menu", "callback_data": "menu_main"}]
                ]
            }
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 2. Products List
        if data == "menu_products" or data.startswith("menu_products:"):
            page_str = data.split(":")[1] if ":" in data else "1"
            page = int(page_str) if page_str.isdigit() else 1
            products, total_pages = get_products_page(page=page, per_page=6)
            page = max(1, min(page, total_pages)) if total_pages > 0 else 1
            text = f"📦 <b>PRODUCT CATALOG</b> (Page {page}/{max(1, total_pages)})\nSelect a product to view or edit:"
            kb = products_list_keyboard(products, page, total_pages, action_prefix="prod_view", nav_prefix="menu_products")
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 3. Stock List
        if data == "menu_stock" or data.startswith("menu_stock:"):
            page_str = data.split(":")[1] if ":" in data else "1"
            page = int(page_str) if page_str.isdigit() else 1
            products, total_pages = get_products_page(page=page, per_page=6)
            page = max(1, min(page, total_pages)) if total_pages > 0 else 1
            text = f"📊 <b>STOCK MANAGEMENT</b> (Page {page}/{max(1, total_pages)})\nSelect a product to adjust stock:"
            kb = products_list_keyboard(products, page, total_pages, action_prefix="prod_edit_stock", nav_prefix="menu_stock")
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 4. Prices List
        if data == "menu_prices" or data.startswith("menu_prices:"):
            page_str = data.split(":")[1] if ":" in data else "1"
            page = int(page_str) if page_str.isdigit() else 1
            products, total_pages = get_products_page(page=page, per_page=6)
            page = max(1, min(page, total_pages)) if total_pages > 0 else 1
            text = f"💰 <b>PRICE MANAGEMENT</b> (Page {page}/{max(1, total_pages)})\nSelect a product to adjust price:"
            kb = products_list_keyboard(products, page, total_pages, action_prefix="prod_edit_price", nav_prefix="menu_prices")
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 4b. Search Pagination
        if data.startswith("search_p:"):
            page_str = data.split(":")[1] if ":" in data else "1"
            page = int(page_str) if page_str.isdigit() else 1
            query = session.data.get("search_query", "")
            matches, total_pages = search_products_page(query, page=page, per_page=6) if query else ([], 0)
            page = max(1, min(page, total_pages)) if total_pages > 0 else 1
            if not matches:
                edit_or_send_tg_message(chat_id, message_id, f"🔍 No products matching <b>'{html.escape(query)}'</b>.", reply_markup=back_to_menu_keyboard())
            else:
                msg_text = f"🔍 <b>Search results for '{html.escape(query)}'</b> (Page {page}/{max(1, total_pages)}):\nSelect a product to view or edit:"
                kb = products_list_keyboard(matches, page, total_pages, action_prefix="prod_view", nav_prefix="search_p")
                edit_or_send_tg_message(chat_id, message_id, msg_text, reply_markup=kb)
            return

        # 5. Store Summary
        if data == "menu_summary":
            summary_text = format_store_summary()
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, summary_text, reply_markup=kb)
            return

        # 6. Search Prompt
        if data == "menu_search":
            session.set_state(AdminState.WAITING_SEARCH)
            text = "🔍 <b>Search Products</b>\nPlease type a product name, brand, or model to search:"
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 7. Add Product Prompt
        if data == "menu_add_product":
            session.set_state(AdminState.WAITING_ADD_PRODUCT)
            text = (
                "➕ <b>Add New Product</b>\n\n"
                "You can type the product details in natural language, for example:\n"
                "<code>iPhone 15 Pro 256GB Natural Titanium 125000 taka stock 5</code>\n\n"
                "Or type just the product name to begin step-by-step entry:"
            )
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 8. Import Dataset Prompt
        if data == "menu_import":
            session.set_state(AdminState.WAITING_IMPORT_FILE)
            text = (
                "📥 <b>Import Dataset (Raw Text or File)</b>\n\n"
                "You can import products in <b>any format</b>:\n\n"
                "1️⃣ <b>Paste raw text directly here:</b>\n"
                "   • Paste messy product lists, Facebook or WhatsApp posts\n"
                "   • Single or multiple products with specs, prices, stock\n\n"
                "2️⃣ <b>Upload a file:</b>\n"
                "   • <code>.txt</code> (raw text file)\n"
                "   • <code>.csv</code> or <code>.json</code>\n\n"
                "<i>Example raw text:</i>\n"
                "<code>Realme 12 Pro+ 5G - 38000 tk (Stock: 5)\n"
                "Asus ROG Zephyrus G14 - 185000 BDT, RTX 4060, 16GB\n"
                "Sony WH-1000XM5 - 32,000 taka</code>\n\n"
                "Send your raw text or file now:"
            )
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 9. Replace Dataset Prompt
        if data == "menu_replace":
            session.set_state(AdminState.WAITING_IMPORT_FILE, replace_all=True)
            text = (
                "🔄 <b>Replace Catalog Dataset</b>\n\n"
                "Paste raw text or upload a <code>.txt</code> / <code>.json</code> / <code>.csv</code> file to <b>completely replace</b> the current catalog.\n\n"
                "⚠️ <b>Warning:</b> This will overwrite existing products with your new list.\n\n"
                "Send your raw text or file now:"
            )
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 10. Export Dataset Prompt
        if data == "menu_export":
            text = "📤 <b>Export Catalog</b>\nChoose the export format you want to download:"
            kb = export_format_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 10b. Export Actions
        if data.startswith("export_act:"):
            fmt = data.split(":")[1]
            from dataset_importer import export_catalog_to_text
            from database import get_product_repository
            repo = get_product_repository()
            prods = repo.get_all_products()

            if fmt == "txt":
                txt_content = export_catalog_to_text()
                send_tg_document(chat_id, txt_content.encode("utf-8"), "catalog_export.txt", caption=f"📄 Catalog Export ({len(prods)} products in raw text)")
            elif fmt == "json":
                json_content = json.dumps(prods, indent=2, ensure_ascii=False)
                send_tg_document(chat_id, json_content.encode("utf-8"), "products.json", caption=f"📁 Catalog Export ({len(prods)} products in JSON)")
            elif fmt == "csv":
                import io, csv
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=["id", "name", "brand", "category", "price_bdt", "price_formatted", "stock", "in_stock", "color", "specifications"])
                writer.writeheader()
                for p in prods:
                    writer.writerow({k: p.get(k, "") for k in writer.fieldnames})
                send_tg_document(chat_id, buf.getvalue().encode("utf-8"), "products.csv", caption=f"📊 Catalog Export ({len(prods)} products in CSV)")

            text = (
                f"📤 <b>Catalog Export ({fmt.upper()})</b>\n\n"
                f"Total products in catalog: <b>{len(prods)}</b>\n"
                f"File format: <code>.{fmt}</code>\n"
                f"Status: 🟢 Synced and up-to-date.\n\n"
                "⚡ <i>Catalog file sent to chat.</i>"
            )
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 11. Help Menu
        if data == "menu_help":
            text = (
                "❓ <b>Seller Control Panel Help</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "• <b>/start or /menu</b>: Open this main control panel\n"
                "• <b>/stop</b>: Pause customer auto-replies on Messenger\n"
                "• <b>/resume</b>: Re-enable automated replies\n"
                "• <b>Quick Search</b>: Tap Search or type any model name\n"
                "• <b>Live Sync</b>: All stock and price changes update Messenger instantly in 0ms\n"
                "• <b>Add Product</b>: Type natural text like <code>iPhone 15 Pro 125000 tk stock 5</code>"
            )
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 12. View Product Details
        if data.startswith("prod_view:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            prod = get_product(pid)
            if not prod:
                edit_or_send_tg_message(chat_id, message_id, "⚠️ Product not found.", reply_markup=back_to_menu_keyboard())
                return
            card = format_product_card(prod)
            kb = product_detail_keyboard(pid, prod.get("in_stock", True))
            edit_or_send_tg_message(chat_id, message_id, card, reply_markup=kb)
            return

        # 13. Quick Stock Actions
        if data.startswith("prod_edit_stock:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            prod = get_product(pid)
            if not prod:
                edit_or_send_tg_message(chat_id, message_id, "⚠️ Product not found.", reply_markup=back_to_menu_keyboard())
                return
            stock_val = prod.get("stock", 1 if prod.get("in_stock", True) else 0)
            card = format_product_card(prod) + "\n\n<i>Use the buttons below for quick stock updates:</i>"
            kb = quick_stock_keyboard(pid, stock_val)
            edit_or_send_tg_message(chat_id, message_id, card, reply_markup=kb)
            return

        if data.startswith("stock_adj:"):
            parts = data.split(":")
            pid, delta = _cb_id(data, 1), _cb_id(data, 2)
            if pid is None or delta is None:
                return
            updated = adjust_stock_delta(pid, delta)
            if updated:
                stock_val = updated.get("stock", 0)
                card = format_product_card(updated) + f"\n\n✅ <b>Stock adjusted by {delta:+d}! Current stock: {stock_val}</b>"
                kb = quick_stock_keyboard(pid, stock_val)
                edit_or_send_tg_message(chat_id, message_id, card, reply_markup=kb)
            else:
                edit_or_send_tg_message(chat_id, message_id, "⚠️ Product not found or could not be updated.", reply_markup=back_to_menu_keyboard())
            return

        if data.startswith("stock_set_zero:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            updated = update_stock_exact(pid, 0)
            if updated:
                card = format_product_card(updated) + "\n\n🔴 <b>Marked as Out of Stock (0)!</b>"
                kb = quick_stock_keyboard(pid, 0)
                edit_or_send_tg_message(chat_id, message_id, card, reply_markup=kb)
            else:
                edit_or_send_tg_message(chat_id, message_id, "⚠️ Product not found or could not be updated.", reply_markup=back_to_menu_keyboard())
            return

        if data.startswith("stock_set_10:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            updated = update_stock_exact(pid, 10)
            if updated:
                card = format_product_card(updated) + "\n\n🟢 <b>Stock updated to 10 (In Stock)!</b>"
                kb = quick_stock_keyboard(pid, 10)
                edit_or_send_tg_message(chat_id, message_id, card, reply_markup=kb)
            else:
                edit_or_send_tg_message(chat_id, message_id, "⚠️ Product not found or could not be updated.", reply_markup=back_to_menu_keyboard())
            return

        if data.startswith("stock_prompt_exact:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            session.set_state(AdminState.WAITING_STOCK_EDIT, product_id=pid)
            text = f"📊 <b>Set Exact Stock for #{pid}</b>\nPlease enter the exact stock number (e.g. <code>15</code>):"
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 14. Quick Price Actions
        if data.startswith("prod_edit_price:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            prod = get_product(pid)
            if not prod:
                edit_or_send_tg_message(chat_id, message_id, "⚠️ Product not found.", reply_markup=back_to_menu_keyboard())
                return
            card = format_product_card(prod) + "\n\n<i>Adjust price by percentage or enter exact value:</i>"
            kb = quick_price_keyboard(pid)
            edit_or_send_tg_message(chat_id, message_id, card, reply_markup=kb)
            return

        if data.startswith("price_pct:"):
            parts = data.split(":")
            pid = _cb_id(data, 1)
            try:
                pct = float(parts[2])
            except (IndexError, ValueError):
                return
            if pid is None:
                return
            updated = adjust_price_pct(pid, pct)
            if updated:
                new_p = updated.get("price_formatted")
                card = format_product_card(updated) + f"\n\n✅ <b>Price adjusted by {pct:+.0f}%! New price: {new_p}</b>"
                kb = quick_price_keyboard(pid)
                edit_or_send_tg_message(chat_id, message_id, card, reply_markup=kb)
            else:
                edit_or_send_tg_message(chat_id, message_id, "⚠️ Product not found or could not be updated.", reply_markup=back_to_menu_keyboard())
            return

        if data.startswith("price_prompt_exact:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            session.set_state(AdminState.WAITING_PRICE_EDIT, product_id=pid)
            text = f"💰 <b>Set Exact Price for #{pid}</b>\nPlease enter the new price in BDT (e.g. <code>125000</code>):"
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 15. Variant and Specifications Actions
        if data.startswith("prod_edit_color:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            session.set_state(AdminState.WAITING_COLOR_EDIT, product_id=pid)
            text = f"🎨 <b>Update Variant / Color for #{pid}</b>\nPlease enter the new color or variant name (e.g. <code>Natural Titanium</code>):"
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        if data.startswith("prod_edit_specs:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            session.set_state(AdminState.WAITING_SPECS_EDIT, product_id=pid)
            text = f"📝 <b>Update Specifications for #{pid}</b>\nPlease enter the updated specs description:"
            kb = back_to_menu_keyboard()
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        # 16. Delete Product Actions
        if data.startswith("prod_delete_prompt:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            prod = get_product(pid)
            if not prod:
                edit_or_send_tg_message(chat_id, message_id, "⚠️ Product not found.", reply_markup=back_to_menu_keyboard())
                return
            p_name = html.escape(str(prod.get("name", "Product")))
            text = f"⚠️ <b>Delete Confirmation</b>\n\nAre you sure you want to delete <b>{p_name}</b> (ID #{pid}) from the catalog?"
            kb = confirm_delete_keyboard(pid)
            edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            return

        if data.startswith("prod_delete_confirm:"):
            pid = _cb_id(data)
            if pid is None:
                edit_or_send_tg_message(chat_id, message_id, "This button has expired. Please reopen the panel with /menu.", reply_markup=back_to_menu_keyboard())
                return
            deleted = delete_product(pid)
            if deleted:
                products, total_pages = get_products_page(page=1, per_page=6)
                text = f"🗑️ <b>Product #{pid} successfully deleted!</b>\n\n📦 <b>PRODUCT CATALOG:</b>"
                kb = products_list_keyboard(products, 1, total_pages)
                edit_or_send_tg_message(chat_id, message_id, text, reply_markup=kb)
            else:
                edit_or_send_tg_message(chat_id, message_id, f"⚠️ Could not delete Product #{pid} (item may have already been removed).", reply_markup=back_to_menu_keyboard())
            return

    except Exception as e:
        logger.exception(f"Unhandled error handling Telegram callback '{data}': {e}")
        try:
            edit_or_send_tg_message(chat_id, message_id, "⚠️ An error occurred processing your request. Please try again.", reply_markup=back_to_menu_keyboard())
        except Exception:
            pass


def handle_admin_text_message(msg: Dict[str, Any]):
    """Handles textual commands and state inputs from administrators."""
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if not is_admin(user_id, chat_id):
        # Ignore non-administrators
        return

    session = get_admin_session(user_id)
    clean_lower = text.lower()

    msg_id = msg.get("message_id")
    clean_lower = text.lower()

    # If the user typed input while in an awaiting state, auto-delete user message to keep chat clean
    if session.state != AdminState.IDLE and msg_id:
        delete_tg_message(chat_id, msg_id)

    # Core slash commands
    if clean_lower.startswith(("/start", "/menu", "/admin", "/panel")):
        session.clear()
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        show_main_menu(chat_id, session.panel_message_id, user_id=user_id)
        return

    if clean_lower in ("/close", "close"):
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        if session.panel_message_id:
            delete_tg_message(chat_id, session.panel_message_id)
        session.clear()
        return

    if clean_lower.startswith("/stop"):
        session.clear()
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        set_bot_active(False)
        show_main_menu(chat_id, session.panel_message_id, user_id=user_id)
        return

    if clean_lower.startswith("/resume"):
        session.clear()
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        try:
            import safety as _safety
            _safety.breaker.reset()
        except Exception:
            pass
        set_bot_active(True)
        show_main_menu(chat_id, session.panel_message_id, user_id=user_id)
        return

    if clean_lower.startswith(("/language", "/lang")):
        session.clear()
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        from language_settings import get_language_mode
        _cur = get_language_mode()
        _labels = {"auto": "Auto (adapt to customer)", "en": "English", "bn": "Bangla"}
        kb = {
            "inline_keyboard": [
                [{"text": "🤖 Auto (adapt to customer)", "callback_data": "lang:auto"},
                 {"text": "🇬🇧 English", "callback_data": "lang:en"}],
                [{"text": "🇧🇩 Bangla", "callback_data": "lang:bn"}],
                [{"text": "🏠 Main Menu", "callback_data": "menu_main"}]
            ]
        }
        out_msg_id = edit_or_send_tg_message(
            chat_id, session.panel_message_id,
            f"\U0001f310 <b>DEFAULT REPLY LANGUAGE</b>\n"
            f"Current: <b>{_labels.get(_cur, _cur)}</b>\n\n"
            f"<b>Auto</b> adapts to each customer's language.\n"
            f"<b>English</b> / <b>Bangla</b> force every Messenger reply into that language.",
            reply_markup=kb)
        if out_msg_id:
            session.panel_message_id = out_msg_id
        return

    if clean_lower.startswith("/products"):
        session.clear()
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        from database import has_active_catalog
        if not has_active_catalog():
            text_out = (
                "⚠️ <b>NO PRODUCT DATABASE FOUND!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Your store catalog is empty or database is disconnected.\n"
                "Messenger will NOT reply to customers until catalog products are added.\n\n"
                "📥 <b>Please add or upload your products:</b>\n"
                "• Paste raw text product listings in chat\n"
                "• Or upload a <code>.txt</code> / <code>.csv</code> / <code>.json</code> document."
            )
            kb = {
                "inline_keyboard": [
                    [{"text": "📥 Import Dataset", "callback_data": "menu_import"}, {"text": "➕ Add Product", "callback_data": "menu_add_product"}],
                    [{"text": "🏠 Main Menu", "callback_data": "menu_main"}]
                ]
            }
            out_msg_id = edit_or_send_tg_message(chat_id, session.panel_message_id, text_out, reply_markup=kb)
            if out_msg_id:
                session.panel_message_id = out_msg_id
            return
        products, total_pages = get_products_page(1, per_page=6)
        out_msg_id = edit_or_send_tg_message(
            chat_id,
            session.panel_message_id,
            "📦 <b>PRODUCT CATALOG:</b>\nSelect a product to view or edit:",
            reply_markup=products_list_keyboard(products, 1, total_pages, action_prefix="prod_view", nav_prefix="menu_products")
        )
        if out_msg_id:
            session.panel_message_id = out_msg_id
        return

    if clean_lower.startswith("/stock"):
        session.clear()
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        from database import has_active_catalog
        if not has_active_catalog():
            text_out = (
                "⚠️ <b>NO PRODUCT DATABASE FOUND!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Your store catalog is empty or database is disconnected.\n"
                "Messenger will NOT reply to customers until catalog products are added.\n\n"
                "📥 <b>Please add or upload your products:</b>\n"
                "• Paste raw text product listings in chat\n"
                "• Or upload a <code>.txt</code> / <code>.csv</code> / <code>.json</code> document."
            )
            kb = {
                "inline_keyboard": [
                    [{"text": "📥 Import Dataset", "callback_data": "menu_import"}, {"text": "➕ Add Product", "callback_data": "menu_add_product"}],
                    [{"text": "🏠 Main Menu", "callback_data": "menu_main"}]
                ]
            }
            out_msg_id = edit_or_send_tg_message(chat_id, session.panel_message_id, text_out, reply_markup=kb)
            if out_msg_id:
                session.panel_message_id = out_msg_id
            return
        products, total_pages = get_products_page(1, per_page=6)
        out_msg_id = edit_or_send_tg_message(
            chat_id,
            session.panel_message_id,
            "📊 <b>QUICK STOCK CONTROL:</b>\nSelect a product to adjust stock units:",
            reply_markup=products_list_keyboard(products, 1, total_pages, action_prefix="prod_edit_stock", nav_prefix="menu_stock")
        )
        if out_msg_id:
            session.panel_message_id = out_msg_id
        return

    if clean_lower.startswith("/search"):
        session.set_state(AdminState.WAITING_SEARCH)
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        out_msg_id = edit_or_send_tg_message(
            chat_id,
            session.panel_message_id,
            "🔍 <b>Search Catalog</b>\nType any brand, model name, or keyword:",
            reply_markup=back_to_menu_keyboard()
        )
        if out_msg_id:
            session.panel_message_id = out_msg_id
        return

    if clean_lower.startswith("/import"):
        session.set_state(AdminState.WAITING_IMPORT_FILE)
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        text_out = (
            "📥 <b>Import Dataset (Raw Text or File)</b>\n\n"
            "Paste raw text directly here (e.g. WhatsApp list or messy notes)\n"
            "OR upload a <code>.txt</code> / <code>.csv</code> / <code>.json</code> document."
        )
        out_msg_id = edit_or_send_tg_message(chat_id, session.panel_message_id, text_out, reply_markup=back_to_menu_keyboard())
        if out_msg_id:
            session.panel_message_id = out_msg_id
        return

    # Seller intelligence commands + natural-language copilot (spec 48, 49, 64)
    _intel_reply = None
    try:
        from intelligence import seller_summary, hidden_opportunities, demand_report, merchandiser_pick, why_not_buying
        from customer_memory import get_memory as _gm2
        from orders import get_order_store as _gos2
        from audit import get_audit_log as _gal
        _q = clean_lower
        if _q.startswith("/orders") or "today's orders" in _q or "todays orders" in _q:
            _rows = _gos2().list_recent(10)
            _intel_reply = ("Recent orders:\n" + "\n".join(
                f"{o['id']} | {o['state']} | BDT {o['total_bdt']:,} | {o.get('phone', '')}" for o in _rows)) if _rows else "No orders yet."
        elif _q.startswith("/hotleads") or "hot lead" in _q:
            _leads = _gm2().hot_leads(10)
            _intel_reply = ("Hot leads:\n" + "\n".join(
                f"{l['user_id']} | score {l['lead_score']} | {l['stage']} | budget BDT {l['budget_bdt']:,}" for l in _leads)) if _leads else "No hot leads yet."
        elif _q.startswith("/sales") or _q.startswith("/insights") or "what's happening" in _q or "whats happening" in _q:
            _intel_reply = seller_summary()
        elif _q.startswith("/opportunities") or "hidden opportunit" in _q or "might have missed" in _q or "don't know" in _q or "dont know" in _q:
            _intel_reply = hidden_opportunities()
        elif _q.startswith("/demand"):
            _intel_reply = demand_report()
        elif _q.startswith("/promote") or "should i promote" in _q:
            _intel_reply = merchandiser_pick()
        elif "why" in _q and ("not buying" in _q or "aren't people buying" in _q or "arent people buying" in _q):
            _intel_reply = why_not_buying(text)
        elif _q.startswith("/audit"):
            _intel_reply = _gal().format_recent(10)
        elif "low on stock" in _q or "low stock" in _q:
            from intelligence import low_stock as _ls
            _low = _ls()
            _intel_reply = ("Low stock:\n" + "\n".join(f"{p.get('name')} - {p.get('stock')} left" for p in _low)) if _low else "Nothing is low on stock."
    except Exception:
        logger.exception("seller intelligence command failed")
        _intel_reply = "Could not load that report right now."
    if _intel_reply is not None:
        session.clear()
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        out_msg_id = edit_or_send_tg_message(chat_id, session.panel_message_id,
                                             html.escape(_intel_reply),
                                             reply_markup=back_to_menu_keyboard())
        if out_msg_id:
            session.panel_message_id = out_msg_id
        return

    if clean_lower.startswith("/help"):
        session.clear()
        if msg_id:
            delete_tg_message(chat_id, msg_id)
        text_out = (
            "❓ <b>Seller Control Panel Help</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "• <b>/start or /menu</b>: Open main control panel\n"
            "• <b>/products</b>: View & browse catalog\n"
            "• <b>/search</b>: Search models and prices\n"
            "• <b>/stock</b>: View & update stock quantities\n"
            "• <b>/import</b>: Import raw text or files\n"
            "• <b>/stop</b>: Pause customer auto-replies on Messenger\n"
            "• <b>/resume</b>: Re-enable automated replies\n"
            "• <b>/close</b>: Dismiss the control panel widget"
        )
        out_msg_id = edit_or_send_tg_message(chat_id, session.panel_message_id, text_out, reply_markup=back_to_menu_keyboard())
        if out_msg_id:
            session.panel_message_id = out_msg_id
        return

    # Handle active conversation states (in-place widget updates)
    if session.state == AdminState.WAITING_SEARCH:
        target_panel = session.panel_message_id
        session.set_state(AdminState.IDLE, search_query=text)
        matches, total_pages = search_products_page(text, page=1, per_page=6)
        if not matches:
            res_id = edit_or_send_tg_message(chat_id, target_panel, f"🔍 No products matching <b>'{html.escape(text)}'</b>.", reply_markup=back_to_menu_keyboard())
        else:
            msg_text = f"🔍 <b>Search results for '{html.escape(text)}'</b> (Page 1/{max(1, total_pages)}):\nSelect a product to view or edit:"
            kb = products_list_keyboard(matches, 1, total_pages, action_prefix="prod_view", nav_prefix="search_p")
            res_id = edit_or_send_tg_message(chat_id, target_panel, msg_text, reply_markup=kb)
        if res_id:
            session.panel_message_id = res_id
        return

    if session.state == AdminState.WAITING_STOCK_EDIT:
        pid = session.data.get("product_id")
        target_panel = session.panel_message_id
        clean_nums = re.findall(r"\d+", text.replace(",", ""))
        if clean_nums and pid:
            new_stock = int(clean_nums[0])
            updated = update_stock_exact(int(pid), new_stock)
            if updated:
                session.clear()
                card = format_product_card(updated) + f"\n\n✅ <b>Stock updated to {new_stock}!</b>"
                res_id = edit_or_send_tg_message(chat_id, target_panel, card, reply_markup=quick_stock_keyboard(int(pid), new_stock))
                if res_id:
                    session.panel_message_id = res_id
                return
        res_id = edit_or_send_tg_message(
            chat_id,
            target_panel,
            "⚠️ <b>Invalid stock number.</b> Please enter numbers only (e.g. <code>15</code>) or tap below to cancel:",
            reply_markup=back_to_menu_keyboard()
        )
        if res_id:
            session.panel_message_id = res_id
        return

    if session.state == AdminState.WAITING_PRICE_EDIT:
        pid = session.data.get("product_id")
        target_panel = session.panel_message_id
        clean_nums = re.findall(r"\d+", text.replace(",", ""))
        if clean_nums and pid:
            new_price = int(clean_nums[0])
            updated = update_price_exact(int(pid), new_price)
            if updated:
                session.clear()
                card = format_product_card(updated) + f"\n\n✅ <b>Price updated to ৳{new_price:,}!</b>"
                res_id = edit_or_send_tg_message(chat_id, target_panel, card, reply_markup=quick_price_keyboard(int(pid)))
                if res_id:
                    session.panel_message_id = res_id
                return
        res_id = edit_or_send_tg_message(
            chat_id,
            target_panel,
            "⚠️ <b>Invalid price amount.</b> Please enter numbers only in BDT (e.g. <code>125000</code>) or tap below to cancel:",
            reply_markup=back_to_menu_keyboard()
        )
        if res_id:
            session.panel_message_id = res_id
        return

    if session.state == AdminState.WAITING_COLOR_EDIT:
        pid = session.data.get("product_id")
        target_panel = session.panel_message_id
        session.clear()
        if pid:
            updated = update_color(int(pid), text.strip())
            if updated:
                card = format_product_card(updated) + f"\n\n✅ <b>Color / Variant updated to '{html.escape(text.strip())}'!</b>"
                kb = product_detail_keyboard(int(pid), updated.get("in_stock", True))
                res_id = edit_or_send_tg_message(chat_id, target_panel, card, reply_markup=kb)
                if res_id:
                    session.panel_message_id = res_id
                return
        res_id = edit_or_send_tg_message(chat_id, target_panel, "⚠️ Failed to update color.", reply_markup=back_to_menu_keyboard())
        if res_id:
            session.panel_message_id = res_id
        return

    if session.state == AdminState.WAITING_SPECS_EDIT:
        pid = session.data.get("product_id")
        target_panel = session.panel_message_id
        session.clear()
        if pid:
            updated = update_specs(int(pid), text.strip())
            if updated:
                card = format_product_card(updated) + "\n\n✅ <b>Specifications updated!</b>"
                kb = product_detail_keyboard(int(pid), updated.get("in_stock", True))
                res_id = edit_or_send_tg_message(chat_id, target_panel, card, reply_markup=kb)
                if res_id:
                    session.panel_message_id = res_id
                return
        res_id = edit_or_send_tg_message(chat_id, target_panel, "⚠️ Failed to update specifications.", reply_markup=back_to_menu_keyboard())
        if res_id:
            session.panel_message_id = res_id
        return

    if session.state == AdminState.WAITING_IMPORT_FILE:
        target_panel = session.panel_message_id
        is_replace = bool(session.data.get("replace_all", False))
        from dataset_importer import import_raw_catalog_text, detect_currency
        if detect_currency(text) is None:
            _ask_import_currency(chat_id, session, {
                "pending_text": text,
                "replace_all": is_replace,
            })
            return
        try:
            res = import_raw_catalog_text(text, replace=is_replace)
        except Exception as _imp_err:
            logger.exception("text catalog import crashed")
            res = {"success": False, "message": f"Import error: {_imp_err}"}
        if res.get("success"):
            session.clear()
        if res.get("success"):
            prods = res.get("products", [])
            total_imported = res.get("count", len(prods))
            in_stock_count = sum(1 for p in prods if p.get("in_stock", True))
            mode_label = "Replaced entire catalog with" if is_replace else "Successfully imported"

            preview_lines = []
            for p in prods[:5]:
                name = html.escape(p.get("name", "Product"))
                price = p.get("price_formatted") or f"৳{p.get('price_bdt', 0):,}"
                preview_lines.append(f"• <b>{name}</b> ({price})")

            if len(prods) > 5:
                preview_lines.append(f"<i>...and {len(prods) - 5} more products</i>")

            preview_text = "\n".join(preview_lines)

            msg_text = (
                f"\U0001f389 <b>{mode_label} {total_imported} product(s)!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"\U0001f7e2 <b>In Stock:</b> {in_stock_count}\n"
                f"\U0001f534 <b>Out of Stock:</b> {total_imported - in_stock_count}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Preview:</b>\n{preview_text}\n\n"
                f"⚡ <i>RAG knowledge base re-indexed and live for customers!</i>"
            )
            res_id = edit_or_send_tg_message(chat_id, target_panel, msg_text, reply_markup=main_menu_keyboard())
            if res_id:
                session.panel_message_id = res_id
            return
        else:
            err_msg = html.escape(res.get("message", "Could not parse products from text."))
            # Stay in import state so the seller can fix and resend.
            session.set_state(AdminState.WAITING_IMPORT_FILE, replace_all=is_replace)
            res_id = edit_or_send_tg_message(
                chat_id,
                target_panel,
                f"⚠️ <b>Import failed:</b> {err_msg}\n\n"
                f"Please ensure each product has at least a <b>name</b> and a <b>price</b>. Send the corrected dataset, or tap Main Menu to cancel.\n\n"
                f"<i>Example:</i>\n<code>Realme 12 Pro+ 5G - 38000 tk (Stock: 5)</code>",
                reply_markup=back_to_menu_keyboard()
            )
            if res_id:
                session.panel_message_id = res_id
            return

    if session.state == AdminState.WAITING_ADD_PRODUCT:
        target_panel = session.panel_message_id
        # Check if user pasted a multi-product bulk list or text block
        from dataset_importer import parse_raw_catalog_text, import_raw_catalog_text
        bulk_prods = parse_raw_catalog_text(text)
        if len(bulk_prods) >= 2:
            session.clear()
            res = import_raw_catalog_text(text, replace=False)
            total_imported = res.get("count", len(bulk_prods))
            prods = res.get("products", bulk_prods)
            preview_lines = [f"• <b>{html.escape(p.get('name', ''))}</b> ({p.get('price_formatted', '')})" for p in prods[:5]]
            if len(prods) > 5:
                preview_lines.append(f"<i>...and {len(prods) - 5} more products</i>")
            msg_text = (
                f"🎉 <b>Batch Import: Added {total_imported} products to catalog!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(preview_lines) + "\n\n"
                f"⚡ <i>RAG knowledge base re-indexed and ready!</i>"
            )
            res_id = edit_or_send_tg_message(chat_id, target_panel, msg_text, reply_markup=main_menu_keyboard())
            if res_id:
                session.panel_message_id = res_id
            return

        # Check if user had a pending product from step-by-step entry
        pending = session.data.get("pending_product")
        if pending and not pending.get("price_bdt"):
            # Step 2: user entered price for pending product
            clean_digits = re.findall(r"\d+", text.replace(",", ""))
            if clean_digits:
                price_val = int(clean_digits[0])
                pending["price_bdt"] = price_val
                pending["price_formatted"] = f"BDT {price_val:,}"
                stock_match = re.search(r"(?:stock|qty)\s*[:=]?\s*(\d+)", text, re.I)
                if stock_match:
                    pending["stock"] = int(stock_match.group(1))
                    pending["in_stock"] = pending["stock"] > 0
                created = create_product(pending)
                session.clear()
                card = format_product_card(created)
                msg_text = f"🎉 <b>Product successfully added to catalog!</b>\n\n{card}"
                res_id = edit_or_send_tg_message(chat_id, target_panel, msg_text, reply_markup=product_detail_keyboard(created["id"], created.get("in_stock", True)))
                if res_id:
                    session.panel_message_id = res_id
                return
            else:
                res_id = edit_or_send_tg_message(chat_id, target_panel, "⚠️ Please enter a numeric price amount (e.g. <code>125000</code>):", reply_markup=back_to_menu_keyboard())
                if res_id:
                    session.panel_message_id = res_id
                return

        # Single-shot natural language or structured parse
        parsed = parse_product_input(text)
        if parsed.get("price_bdt"):
            # Complete product info provided
            created = create_product(parsed)
            session.clear()
            card = format_product_card(created)
            msg_text = f"🎉 <b>Product successfully added to catalog!</b>\n\n{card}"
            res_id = edit_or_send_tg_message(chat_id, target_panel, msg_text, reply_markup=product_detail_keyboard(created["id"], created.get("in_stock", True)))
            if res_id:
                session.panel_message_id = res_id
            return
        elif parsed.get("name") and len(parsed["name"]) >= 2:
            # Name provided, but price missing -> Step-by-step entry
            session.set_state(AdminState.WAITING_ADD_PRODUCT, pending_product=parsed)
            p_name = html.escape(parsed["name"])
            msg_text = (
                f"📝 Product: <b>{p_name}</b>\n\n"
                "Please enter the price in BDT (e.g. <code>125000</code> or <code>125000 stock 5</code>):"
            )
            res_id = edit_or_send_tg_message(chat_id, target_panel, msg_text, reply_markup=back_to_menu_keyboard())
            if res_id:
                session.panel_message_id = res_id
            return
        else:
            res_id = edit_or_send_tg_message(
                chat_id,
                target_panel,
                "⚠️ Could not recognize product details. Please provide at least a product name and price.\n\n"
                "Example: <code>iPhone 15 Pro 256GB Natural Titanium 125000 taka stock 5</code>",
                reply_markup=back_to_menu_keyboard()
            )
            if res_id:
                session.panel_message_id = res_id
            return

    # Idle paste of a product list: import it instead of menu-bouncing.
    if session.state == AdminState.IDLE and len(text) >= 20:
        try:
            from dataset_importer import parse_raw_catalog_text, import_raw_catalog_text
            if len(parse_raw_catalog_text(text)) >= 2:
                res = import_raw_catalog_text(text, replace=False)
                if res.get("success"):
                    res_id = edit_or_send_tg_message(
                        chat_id, session.panel_message_id,
                        f"Imported {res.get('count', 0)} product(s) from your pasted list.",
                        reply_markup=main_menu_keyboard())
                    if res_id:
                        session.panel_message_id = res_id
                    return
        except Exception:
            logger.exception("idle paste import failed")

    # Fallback default: show main menu (throttled to avoid menu spam)
    _last_menu = session.data.get("_last_menu_ts", 0.0)
    if time.time() - _last_menu > 10.0:
        session.data["_last_menu_ts"] = time.time()
        show_main_menu(chat_id, session.panel_message_id, user_id=user_id)


def _ask_import_currency(chat_id, session, pend: Dict[str, Any]):
    """Dataset did not state a currency: ask the seller before importing."""
    session.set_state(AdminState.WAITING_IMPORT_CURRENCY, **pend)
    kb = {
        "inline_keyboard": [
            [{"text": "🇧🇩 BDT (Taka)", "callback_data": "impcur:BDT"},
             {"text": "💵 USD (Dollar)", "callback_data": "impcur:USD"}],
            [{"text": "🏠 Main Menu", "callback_data": "menu_main"}]
        ]
    }
    out_id = edit_or_send_tg_message(
        chat_id, session.panel_message_id,
        "\U0001f4b1 <b>Currency not found in this dataset.</b>\n"
        "Are the prices in <b>BDT (Taka)</b> or <b>USD (Dollar)</b>?\n"
        "<i>USD prices are converted to BDT at the configured rate (default 120).</i>",
        reply_markup=kb)
    if out_id:
        session.panel_message_id = out_id


def _report_import_result(chat_id, session, res, file_label: str, is_replace: bool):
    prods = res.get("products", [])
    total_imported = res.get("count", len(prods))
    in_stock_count = sum(1 for x in prods if x.get("in_stock", True))
    mode_label = "Replaced entire catalog with" if is_replace else "Successfully imported"
    preview_lines = [f"• <b>{html.escape(x.get('name', ''))}</b> ({x.get('price_formatted', '')})" for x in prods[:5]]
    if len(prods) > 5:
        preview_lines.append(f"<i>...and {len(prods) - 5} more products</i>")
    msg_text = (
        f"\U0001f389 <b>{mode_label} {total_imported} product(s) from '{html.escape(file_label)}'!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"\U0001f7e2 <b>In Stock:</b> {in_stock_count}\n"
        f"\U0001f534 <b>Out of Stock:</b> {total_imported - in_stock_count}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Preview:</b>\n" + "\n".join(preview_lines) + "\n\n"
        f"⚡ <i>RAG knowledge base re-indexed and live for customers!</i>"
    )
    res_id = edit_or_send_tg_message(chat_id, session.panel_message_id, msg_text, reply_markup=main_menu_keyboard())
    if res_id:
        session.panel_message_id = res_id


def _do_currency_import(chat_id, session, pend: Dict[str, Any], currency: str):
    """Finish a pending import after the seller picked BDT or USD."""
    is_replace = bool(pend.get("replace_all", False))
    try:
        if pend.get("pending_text") is not None:
            from dataset_importer import import_raw_catalog_text
            res = import_raw_catalog_text(pend["pending_text"], replace=is_replace,
                                          currency_override=currency)
            if res.get("success"):
                _report_import_result(chat_id, session, res, "pasted dataset", is_replace)
            else:
                edit_or_send_tg_message(chat_id, session.panel_message_id,
                                        f"⚠️ <b>Import failed:</b> {html.escape(res.get('message', ''))}",
                                        reply_markup=back_to_menu_keyboard())
            return
        file_id = pend.get("pending_file_id")
        file_name = pend.get("pending_file_name") or "dataset.txt"
        token = _get_bot_token()
        if not token or not file_id:
            edit_or_send_tg_message(chat_id, session.panel_message_id,
                                    "⚠️ The uploaded file expired. Please send it again.",
                                    reply_markup=back_to_menu_keyboard())
            return
        file_resp = _tg_session.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}",
                                    timeout=15).json()
        file_path = file_resp.get("result", {}).get("file_path")
        if not file_path:
            edit_or_send_tg_message(chat_id, session.panel_message_id,
                                    "⚠️ Could not re-download the file. Please send it again.",
                                    reply_markup=back_to_menu_keyboard())
            return
        content_bytes = _tg_session.get(f"https://api.telegram.org/file/bot{token}/{file_path}",
                                        timeout=30).content
        from dataset_importer import import_catalog_bytes
        res = import_catalog_bytes(content_bytes, file_name=file_name, replace=is_replace,
                                   currency_override=currency)
        if res.get("success"):
            if pend.get("pending_unique"):
                _mark_file_imported(pend["pending_unique"])
            _report_import_result(chat_id, session, res, file_name, is_replace)
        else:
            edit_or_send_tg_message(chat_id, session.panel_message_id,
                                    f"⚠️ <b>Import failed:</b> {html.escape(res.get('message', ''))}",
                                    reply_markup=back_to_menu_keyboard())
    except Exception as e:
        logger.error(f"currency import failed: {e}", exc_info=True)
        edit_or_send_tg_message(chat_id, session.panel_message_id,
                                f"⚠️ Error processing file: {html.escape(str(e))}",
                                reply_markup=back_to_menu_keyboard())


def handle_admin_document(msg: Dict[str, Any]) -> bool:
    """Handles uploaded document files (.txt, .csv, .json, .xlsx) for catalog import."""
    doc = msg.get("document")
    if not doc:
        return False

    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    chat = msg.get("chat", {})
    chat_id = chat.get("id")

    if not is_admin(user_id, chat_id):
        return False

    session = get_admin_session(user_id)
    file_id = doc.get("file_id")
    file_unique_id = doc.get("file_unique_id") or file_id or ""
    file_name = doc.get("file_name", "dataset.txt")
    token = _get_bot_token()

    if not token or not file_id:
        return False

    # Same file again (redelivery or re-upload): never import twice.
    if _file_already_imported(file_unique_id):
        send_tg_message(chat_id, "✅ <i>This file was already imported. Send a different file if the catalog changed.</i>")
        return True
    # One import at a time per chat: a second file waits instead of looping.
    with _import_guard:
        if chat_id in _active_imports:
            send_tg_message(chat_id, "⏳ <i>An import is already running. Please wait for it to finish.</i>")
            return True
        _active_imports.add(chat_id)

    is_replace = bool(session.data.get("replace_all", False))
    session.clear()

    # Exactly one progress message per import.
    prog_id = send_tg_message(
        chat_id,
        f"⏳ <b>Processing dataset...</b>\nDownloading and indexing <code>{html.escape(file_name)}</code>. Please wait, do not resend the file."
    )
    if prog_id:
        session.panel_message_id = prog_id

    try:
        get_file_url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
        file_resp = _tg_session.get(get_file_url, timeout=15).json()
        file_path = file_resp.get("result", {}).get("file_path")
        if not file_path:
            edit_or_send_tg_message(chat_id, session.panel_message_id, "⚠️ Failed to retrieve uploaded file from Telegram.", reply_markup=back_to_menu_keyboard())
            return True

        _ext = ("." + file_name.rsplit(".", 1)[-1].lower()) if "." in file_name else ""
        if _ext not in (".txt", ".csv", ".json", ".tsv", ".md", ".xml", ".xlsx", ".jsonl", ".ndjson"):
            edit_or_send_tg_message(chat_id, session.panel_message_id, "Unsupported file type. Send .json, .csv, .xlsx, .xml, .jsonl or .txt.", reply_markup=back_to_menu_keyboard())
            return True
        if (doc.get("file_size") or 0) > 5_000_000:
            edit_or_send_tg_message(chat_id, session.panel_message_id, "File too large (max 5 MB).", reply_markup=back_to_menu_keyboard())
            return True
        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        content_bytes = _tg_session.get(download_url, timeout=30).content
        if len(content_bytes) > 5_000_000:
            edit_or_send_tg_message(chat_id, session.panel_message_id, "File too large (max 5 MB).", reply_markup=back_to_menu_keyboard())
            return True

        # Currency stated in the dataset wins; otherwise ask the seller.
        if _ext != ".xlsx":
            from dataset_importer import _decode_bytes, detect_currency
            if detect_currency(_decode_bytes(content_bytes)) is None:
                _ask_import_currency(chat_id, session, {
                    "pending_file_id": file_id,
                    "pending_file_name": file_name,
                    "pending_unique": file_unique_id,
                    "replace_all": is_replace,
                })
                return True

        from dataset_importer import import_catalog_bytes
        res = import_catalog_bytes(content_bytes, file_name=file_name, replace=is_replace)

        # Delete the user's uploaded document message to keep chat spotless
        doc_msg_id = msg.get("message_id")
        if doc_msg_id:
            delete_tg_message(chat_id, doc_msg_id)

        if res.get("success"):
            total_imported = res.get("count", 0)
            prods = res.get("products", [])
            in_stock_count = sum(1 for p in prods if p.get("in_stock", True))
            mode_label = "Replaced entire catalog with" if is_replace else "Successfully imported"
            preview_lines = [f"• <b>{html.escape(p.get('name', ''))}</b> ({p.get('price_formatted', '')})" for p in prods[:5]]
            if len(prods) > 5:
                preview_lines.append(f"<i>...and {len(prods) - 5} more products</i>")

            msg_text = (
                f"🎉 <b>{mode_label} {total_imported} product(s) from '{html.escape(file_name)}'!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 <b>In Stock:</b> {in_stock_count}\n"
                f"🔴 <b>Out of Stock:</b> {total_imported - in_stock_count}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Preview:</b>\n" + "\n".join(preview_lines) + "\n\n"
                f"⚡ <i>RAG knowledge base re-indexed and live for customers!</i>"
            )
            res_id = edit_or_send_tg_message(chat_id, session.panel_message_id, msg_text, reply_markup=main_menu_keyboard())
            if res_id:
                session.panel_message_id = res_id
        else:
            err_msg = html.escape(res.get("message", "Could not parse products from file."))
            res_id = edit_or_send_tg_message(chat_id, session.panel_message_id, f"⚠️ <b>Import failed:</b> {err_msg}", reply_markup=back_to_menu_keyboard())
            if res_id:
                session.panel_message_id = res_id
        if res.get("success"):
            _mark_file_imported(file_unique_id)
        return True
    except Exception as e:
        logger.error(f"Error downloading/importing document: {e}", exc_info=True)
        res_id = edit_or_send_tg_message(chat_id, session.panel_message_id, f"⚠️ Error processing file: {html.escape(str(e))}", reply_markup=back_to_menu_keyboard())
        if res_id:
            session.panel_message_id = res_id
        return True
    finally:
        with _import_guard:
            _active_imports.discard(chat_id)


def dispatch_telegram_update(update: Dict[str, Any]) -> bool:
    """Central entrypoint routing incoming Telegram webhook / polling updates."""
    if not update or not isinstance(update, dict):
        return False
    if _is_duplicate_update(update):
        logger.info(f"Ignoring redelivered Telegram update {update.get('update_id')}")
        return True

    # 1. Callback query (inline button clicks)
    cb = update.get("callback_query")
    if cb:
        handle_callback_query(cb)
        return True

    # 2. Text message, document, or channel post
    msg = update.get("message") or update.get("channel_post")
    if msg:
        if msg.get("document"):
            if handle_admin_document(msg):
                return True
        handle_admin_text_message(msg)
        return True

    return False
