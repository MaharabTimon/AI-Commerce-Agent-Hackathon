import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple
from telegram_service import send_telegram_order

logger = logging.getLogger(__name__)

ORDER_INTENT_PATTERNS = [
    r"\border\b", r"\border\s*korbo\b", r"\border\s*korte\s*chai\b", 
    r"\border\s*dibo\b", r"\border\s*debo\b",
    r"\bkinte\s*chai\b", r"\bkinbo\b", r"\bkivabe\s*kinbo\b", 
    r"\bkivabe\s*order\b", r"\bwant\s*to\s*buy\b", r"\bwant\s*to\s*order\b", 
    r"\bbuy\s*now\b", r"\bhow\s*to\s*order\b", r"\bplace\s*order\b", 
    r"\bnibo\b", r"\bnite\s*chai\b", r"\bনিব\b", r"\bনিতে\s*চাই\b",
    r"অর্ডার\s*করব", r"অর্ডার\s*করতে\s*চাই", r"কিনতে\s*চাই", r"অর্ডার"
]

BANGLISH_MARKERS = {
    "koto", "ache", "kemon", "bhai", "vai", "korbo", "chai", "daam", "dam", 
    "er", "amader", "apnar", "eta", "eita", "eyta", "hobe", "nibo", "ki", "kon", 
    "konta", "shob", "din", "deya", "holo", "ar", "thikana", "nam", "naam",
    "bhalo", "valo", "shathe", "sathe", "lagbe", "janiye", "den", "kore", "nei", "nai",
    "ektu", "kom", "komer", "modhe", "modhje", "oyta", "oytar", "eitar", "etar",
    "vitore", "baire", "porbe", "bolun", "bolben", "paren", "dekhi", "dekhte"
}

COLOR_PATTERNS = [
    r"\b(arctic\s*white)\b",
    r"\b(space\s*gr[ae]y)\b",
    r"\b(midnight\s*black)\b",
    r"\b(jet\s*black)\b",
    r"\b(phantom\s*black)\b",
    r"\b(titanium\s*gr[ae]y)\b",
    r"\b(rose\s*gold)\b",
    r"\b(black|white|gr[ae]y|silver|gold|blue|red|green|purple|pink|yellow|orange|carbon|bronze|starlight|midnight)\b",
    r"(কালো|সাদা|ব্ল্যাক|হোয়াইট|গ্রে|সিলভার|নীল|লাল)"
]

COLOR_TRANSLATION = {
    "কালো": "Black",
    "সাদা": "White",
    "ব্ল্যাক": "Black",
    "হোয়াইট": "White",
    "গ্রে": "Gray",
    "সিলভার": "Silver",
    "নীল": "Blue",
    "লাল": "Red",
    "grey": "Gray",
    "space grey": "Space Gray",
}


def extract_color(text: str) -> Optional[str]:
    """Extracts requested color variant from customer text."""
    if not text:
        return None
    t_lower = text.lower()
    for pat in COLOR_PATTERNS:
        m = re.search(pat, t_lower)
        if m:
            matched = m.group(1).strip()
            if matched in COLOR_TRANSLATION:
                return COLOR_TRANSLATION[matched]
            return matched.title()
    return None


BENGALI_DIGITS = "০১২৩৪৫৬৭৮৯"
WESTERN_DIGITS = "0123456789"
BENGALI_TO_WESTERN = str.maketrans(BENGALI_DIGITS, WESTERN_DIGITS)


def normalize_text_numerals(text: str) -> str:
    """Translates Bengali script numerals (০-৯) to standard Western numerals (0-9)."""
    return text.translate(BENGALI_TO_WESTERN)


class OrderSession:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.state: str = "IDLE"  # IDLE, AWAITING_INFO, COMPLETED
        self.product_name: Optional[str] = None
        self.product_price: Optional[str] = None
        self.product_color: Optional[str] = None
        self.name: Optional[str] = None
        self.phone: Optional[str] = None
        self.address: Optional[str] = None
        self.language: str = "banglish"
        self.last_order_time: float = 0.0
        self.last_order_product: Optional[str] = None
        self.last_order_name: Optional[str] = None

    def reset(self):
        self.state = "IDLE"
        self.product_name = None
        self.product_price = None
        self.product_color = None
        self.name = None
        self.phone = None
        self.address = None


class BoundedSessionStore:
    """
    Thread-safe, bounded, TTL-aware session storage.
    Prevents memory leaks in high-volume production deployments.
    """
    def __init__(self, max_size: int = 2000, ttl_seconds: int = 86400):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._sessions: OrderedDict[str, Tuple[OrderSession, float]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, user_id: str) -> OrderSession:
        with self._lock:
            now = time.time()
            if user_id in self._sessions:
                session, ts = self._sessions[user_id]
                if now - ts < self.ttl_seconds:
                    self._sessions.move_to_end(user_id)
                    self._sessions[user_id] = (session, now)
                    return session
                else:
                    # Expired session
                    del self._sessions[user_id]

            new_session = OrderSession(user_id)
            if len(self._sessions) >= self.max_size:
                self._sessions.popitem(last=False)  # Evict least recently used
            self._sessions[user_id] = (new_session, now)
            return new_session

    def reset(self, user_id: str):
        with self._lock:
            if user_id in self._sessions:
                session, _ = self._sessions[user_id]
                session.reset()


_session_store = BoundedSessionStore()


def get_order_session(user_id: str) -> OrderSession:
    return _session_store.get(user_id)


def has_order_intent(text: str) -> bool:
    """Detects if message indicates intent to place an order or buy."""
    t_lower = text.lower()
    for pat in ORDER_INTENT_PATTERNS:
        if re.search(pat, t_lower):
            return True
    return False


def detect_language(text: str, current_lang: Optional[str] = None) -> str:
    """
    Session-sticky language detector:
    - Pure Bangla script -> 'bangla'
    - Banglish markers -> 'banglish'
    - Conversational English sentences -> 'english'
    - Short queries / product titles / numbers / neutral words -> stick to current_lang
    """
    # 1. Check for Bengali Unicode script range
    if re.search(r"[\u0980-\u09FF]", text):
        return "bangla"

    words = set(re.findall(r"\b[a-zA-Z]+\b", text.lower()))
    if not words:
        return current_lang or "banglish"

    # 2. If it contains Banglish marker words -> strictly Banglish
    if words.intersection(BANGLISH_MARKERS):
        return "banglish"

    # 3. Check for genuine English conversational structure
    conversational_english = {
        "what", "is", "the", "can", "you", "tell", "me", "how", "much", 
        "do", "i", "want", "would", "like", "please", "could", "should", "give"
    }
    if len(words.intersection(conversational_english)) >= 2:
        return "english"

    # 4. If user already established a session language, STICK to it!
    # (e.g. "ASUS TUF Gaming G9 Neo 49?", "price?", "specs?", "144Hz?")
    if current_lang:
        return current_lang

    # Default friendly Banglish for new conversations
    return "banglish"


def extract_phone(text: str) -> Optional[str]:
    """
    Extracts 11-digit Bangladeshi mobile number (013-019).
    Translates Bengali digits (০-৯) to 0-9.
    Strips non-digit separators (dashes, spaces) and ignores Facebook PSIDs.
    """
    norm = normalize_text_numerals(text)

    # Match sequences of digits optionally separated by spaces or dashes
    # Negative lookbehind and lookahead to reject long IDs (e.g. 17-digit PSID)
    candidates = re.findall(r"(?<!\d)(?:\+?880?|880?)?([0-9\s\-]{11,18})(?!\d)", norm)
    for c in candidates:
        digits = re.sub(r"\D", "", c)
        if digits.startswith("880"):
            digits = digits[2:]  # leaves 01...
        elif digits.startswith("88") and len(digits) == 13:
            digits = digits[2:]
        if len(digits) == 11 and digits.startswith("01") and digits[2] in "3456789":
            return digits

    return None


# Detail parsing uses Meta Llama via intent_service._llama_json.


def ai_parse_customer_details(raw_text: str) -> Dict[str, Optional[str]]:
    """
    Uses Meta Llama to parse messy customer order text (even if unpunctuated or in Banglish)
    into clean Name, Phone Number, properly formatted Delivery Address, and Product if mentioned.
    """
    raw_text = ("" if raw_text is None else str(raw_text))[:4000]
    fallback_phone = extract_phone(raw_text)
    if not raw_text.strip():
        return {"name": None, "phone": None, "address": None, "product": None, "color": None}

    prompt = (
        "You are an intelligent data parser for an online electronics shop in Bangladesh.\n"
        "A customer sent a message with their order details. Even if the text is messy, unpunctuated, "
        "run-on, or written in Banglish/English/Bengali, extract and cleanly format the fields:\n"
        "- 'name': ONLY the customer's actual person name (e.g. 'Rabiul', 'Tanvir'). Do NOT include phone words, 'number', or address words in the name!\n"
        "- 'phone': The 11-digit phone number (e.g. '01914788589').\n"
        "- 'address': The full delivery address. You MAY fix obvious spelling/typos (e.g. 'utttara' -> 'Uttara', 'aveneu' -> 'Avenue'), but you MUST PRESERVE EVERY single detail provided: building name, shop name, road name, avenue, sector, house/holding number, and area (e.g. 'Kingdom, Shah Makhdum Avenue, Sector 15, Uttara, Dhaka'). NEVER omit, truncate, or drop any landmark, avenue, or building details!\n"
        "- 'product': If the customer mentions which product, item, or model they want to order (e.g. 'Dell XPS', 'HP Envy', 'Samsung S24'), extract it. If not mentioned, return null.\n"
        "- 'color': If the customer specifies a color or variant (e.g. 'Black', 'White', 'Silver', 'Arctic White'), extract it. If not mentioned, return null.\n\n"
        f"Customer Message: \"{raw_text}\"\n\n"
        "Return STRICTLY a JSON object with this schema:\n"
        "{\n"
        '  "name": "Clean name or null",\n'
        '  "phone": "Phone number or null",\n'
        '  "address": "Clean formatted address or null",\n'
        '  "product": "Product mentioned or null",\n'
        '  "color": "Color variant or null"\n'
        "}"
    )

    try:
        import config as _config
        if not _config.META_MODEL_API_KEY:
            res = rule_based_extract(raw_text)
            res["product"] = None
            return res
        from intent_service import _llama_json
        data = _llama_json("Return strictly one JSON object, no prose.", prompt)

        name = data.get("name")
        phone = data.get("phone") or fallback_phone
        address = data.get("address")
        product = data.get("product")
        color = data.get("color") or extract_color(raw_text)

        # Sanitize null string representations
        if name in [None, "null", "None", "", "unknown"]:
            name = None
        if phone in [None, "null", "None", "", "unknown"]:
            phone = fallback_phone
        if address in [None, "null", "None", "", "unknown"]:
            address = None
        if product in [None, "null", "None", "", "unknown"]:
            product = None
        if color in [None, "null", "None", "", "unknown"]:
            color = None

        return {"name": name, "phone": phone, "address": address, "product": product, "color": color}
    except Exception as e:
        logger.warning(f"AI customer detail parsing error: {e}")
        res = rule_based_extract(raw_text)
        res["product"] = None
        return res


def rule_based_extract(text: str) -> Dict[str, Optional[str]]:
    """Fallback extraction if AI parser is unavailable."""
    phone = extract_phone(text)
    name = None
    address = None
    color = extract_color(text)

    name_match = re.search(r"(?:name|naam|নাম)\s*[:=-]\s*([^\n,]+)", text, re.IGNORECASE)
    if name_match:
        name = name_match.group(1).strip()

    addr_match = re.search(r"(?:address|thikana|ঠিকানা)\s*[:=-]\s*([^\n]+)", text, re.IGNORECASE)
    if addr_match:
        address = addr_match.group(1).strip()

    return {"name": name, "phone": phone, "address": address, "product": None, "color": color}


def extract_customer_info(text: str, session: OrderSession) -> Tuple[bool, str]:
    """
    Extracts name, phone, address, and product using AI structuring.
    Returns (is_complete, response_message).
    NEVER finalizes an order without a specific product!
    """
    from rag_service import get_rag
    rag = get_rag()

    # Parse messy customer input through AI structuring
    parsed = ai_parse_customer_details(text)

    if parsed.get("name") and not session.name:
        session.name = parsed["name"]
    if parsed.get("phone") and not session.phone:
        session.phone = parsed["phone"]
    if parsed.get("address") and not session.address:
        session.address = parsed["address"]

    # Check if a color was extracted from the message or parsed details
    msg_color = parsed.get("color") or extract_color(text)
    if msg_color and not session.product_color:
        session.product_color = msg_color

    # Check if a product was extracted from the message
    if parsed.get("product") and not session.product_name:
        prod_matches = rag.search_specific_products(parsed["product"], top_k=1)
        if prod_matches and prod_matches[0][1] >= 0.35:
            top_p, _ = prod_matches[0]
            session.product_name = top_p.get("name")
            session.product_price = top_p.get("price_formatted")
        else:
            session.product_name = parsed["product"]
            session.product_price = "Inquire with Sales"

    # If product is still missing, check if the raw text matches any catalog product directly
    if not session.product_name:
        direct_matches = rag.search_specific_products(text, top_k=1)
        if direct_matches and direct_matches[0][1] >= 0.45:
            top_p, _ = direct_matches[0]
            session.product_name = top_p.get("name")
            session.product_price = top_p.get("price_formatted")

    has_contact = bool(session.name and session.phone and session.address)
    has_product = bool(session.product_name and session.product_name != "Electronics Product")

    # 1. Complete: Both contact info AND a specific product are confirmed!
    if has_contact and has_product:
        # Idempotency: never dispatch the same order twice within 60s.
        _now = time.time()
        if (session.last_order_product == session.product_name
                and session.last_order_name == session.name
                and _now - (session.last_order_time or 0) < 60):
            return True, "Order already received. Amader sales team shighroi call kore confirm korbe."
        product_name = session.product_name
        price = session.product_price or "Inquire with Sales"
        cust_name = session.name
        cust_phone = session.phone
        cust_addr = session.address
        user_id = session.user_id
        lang = session.language
        chosen_color = session.product_color or extract_color(product_name)

        send_telegram_order(
            product_name=product_name,
            price=price,
            customer_name=cust_name,
            customer_phone=cust_phone,
            customer_address=cust_addr,
            customer_id=user_id,
            platform="Facebook Messenger",
            color=chosen_color
        )

        # Record last order metadata
        session.last_order_time = time.time()
        session.last_order_product = product_name
        session.last_order_name = cust_name

        display_name = f"{product_name} ({chosen_color})" if chosen_color else product_name

        # Reset session state so future conversations start clean
        session.reset()

        if lang == "bangla":
            msg = (
                f"{cust_name}, {display_name} এর অর্ডার পেয়েছি। "
                f"আমাদের সেলস টিম শীঘ্রই কল করে অর্ডারটি কনফার্ম করবে।"
            )
        elif lang == "english":
            msg = (
                f"Got it {cust_name}! Your order details for {display_name} have been received. "
                f"Our sales team will contact you shortly to confirm."
            )
        else:
            msg = (
                f"Got it {cust_name}! {display_name} er order details peyechi. "
                f"Amader sales team shighroi call kore confirm korbe."
            )

        return True, msg

    # 2. Contact info is complete, BUT product is still missing (e.g. user provided name, phone, address first)
    if has_contact and not has_product:
        if session.language == "bangla":
            msg = f"{session.name}, আপনার নাম, ফোন ও ঠিকানা পেয়েছি। আপনি কোন প্রোডাক্ট বা কোন মডেলটি নিতে চান, একটু বলবেন?"
        elif session.language == "english":
            msg = f"Got it {session.name}! We have received your name, phone, and delivery address. Which product or model would you like to order?"
        else:
            msg = f"Got it {session.name}! Apnar details peyechi. Apni kon product ba model ta order korte chan, ektu bolben?"
        return False, msg

    # 3. If contact info is still missing, politely request remaining fields
    missing_items = []
    if not session.name:
        missing_items.append("Name" if session.language != "bangla" else "নাম")
    if not session.phone:
        missing_items.append("Phone Number" if session.language != "bangla" else "ফোন নম্বর")
    if not session.address:
        missing_items.append("Delivery Address" if session.language != "bangla" else "ডেলিভারি ঠিকানা")

    if session.language == "bangla":
        missing_text = " ও ".join(missing_items)
        msg = f"অর্ডার কনফার্ম করতে আপনার {missing_text} প্রয়োজন, একটু জানাবেন?"
    elif session.language == "english":
        missing_text = " and ".join(missing_items)
        msg = f"To confirm your order, please provide your {missing_text}."
    else:
        missing_text = " ar ".join(missing_items)
        msg = f"Order confirm korte apnar {missing_text} lagbe, ektu bolben?"

    return False, msg


def handle_order_flow(
    user_id: str,
    user_message: str,
    current_product: Optional[str] = None,
    current_price: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """
    Coordinates order flow using the robust OrderStateMachine.
    Adheres strictly to 'AI decides meaning. Python decides actions.'
    Returns (is_order_message, bot_response).
    """
    try:
        from order_state import get_order_state_machine
        sm = get_order_state_machine()
        is_order, reply, _ = sm.process_turn(
            sender_id=user_id,
            user_message=user_message,
            current_product=current_product,
            current_price=current_price
        )
        return is_order, reply
    except Exception as e:
        logger.error(f"Error in handle_order_flow: {e}", exc_info=True)
        return False, None



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing Phone & Detail Parsing...")
    sample_text = "রবিউল ০১৯১৪৭৮৮৫৮৯ গাজীপুর"
    phone_extracted = extract_phone(sample_text)
    print(f"Extracted phone: {phone_extracted}")
