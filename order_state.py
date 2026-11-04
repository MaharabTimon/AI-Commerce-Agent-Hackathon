# -*- coding: utf-8 -*-
"""
Order State Machine for Messenger Bot.
Blueprint: "AI decides meaning. Python decides actions."

States:
- NORMAL: Idle conversational browsing state.
- PRODUCT_DISCUSSION: Customer currently evaluating/asking about a specific product.
- ORDER_STARTED: Customer initiated order intent (e.g. 'order dibo', 'eta nibo').
- COLLECT_DETAILS: Gathering missing contact fields (Name, Phone, Delivery Address).
- ORDER_REVIEW: All fields collected, order summary presented for customer confirmation.
- CONFIRMED: Customer confirmed order, dispatched to Telegram seller channel.
- CANCELLED: Customer cancelled or negated order, resets cleanly to NORMAL.
"""

import html
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from intent_service import (
        classify_intent,
        extract_phone,
        extract_labeled_entities,
        extract_unlabeled_contact_info,
        check_is_negated,
        normalize_bengali_numerals
    )
except ImportError:
    from vercel.intent_service import (
        classify_intent,
        extract_phone,
        extract_labeled_entities,
        extract_unlabeled_contact_info,
        check_is_negated,
        normalize_bengali_numerals
    )

logger = logging.getLogger(__name__)


class OrderState:
    NORMAL = "NORMAL"
    PRODUCT_DISCUSSION = "PRODUCT_DISCUSSION"
    ORDER_STARTED = "ORDER_STARTED"
    COLLECT_DETAILS = "COLLECT_DETAILS"
    ORDER_REVIEW = "ORDER_REVIEW"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


# Color variant patterns
COLOR_PATTERNS = [
    r"\b(arctic\s*white)\b",
    r"\b(space\s*gr[ae]y)\b",
    r"\b(midnight\s*black)\b",
    r"\b(jet\s*black)\b",
    r"\b(phantom\s*black)\b",
    r"\b(natural\s*titanium)\b",
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
    "natural titanium": "Natural Titanium",
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


BANGLISH_MARKERS = {
    "koto", "ache", "kemon", "bhai", "vai", "korbo", "chai", "daam", "dam",
    "er", "amader", "apnar", "eta", "eita", "eyta", "hobe", "nibo", "ki", "kon",
    "konta", "shob", "din", "deya", "holo", "ar", "thikana", "nam", "naam",
    "bhalo", "valo", "shathe", "sathe", "lagbe", "janiye", "den", "kore", "nei", "nai",
    "ektu", "kom", "komer", "modhe", "modhje", "oyta", "oytar", "eitar", "etar",
    "vitore", "baire", "porbe", "bolun", "bolben", "paren", "dekhi", "dekhte", "thik"
}


def detect_language(text: str, current_lang: Optional[str] = None) -> str:
    """Detects session language with stickiness."""
    if re.search(r"[ঀ-৿]", text):
        return "bangla"

    words = set(re.findall(r"[a-zA-Z]+", text.lower()))
    if not words:
        return current_lang or "banglish"

    if words.intersection(BANGLISH_MARKERS):
        return "banglish"

    conversational_english = {
        "what", "is", "the", "can", "you", "tell", "me", "how", "much",
        "do", "i", "want", "would", "like", "please", "could", "should", "give", "yes", "confirm", "cancel"
    }
    if len(words.intersection(conversational_english)) >= 2:
        return "english"

    if current_lang:
        return current_lang

    return "banglish"


def extract_product_mention_from_order_text(text: str) -> Optional[str]:
    """
    Extracts potential product names from order phrases like
    'Aoc OLED 27 order dibo', 'Iphone 15 order korbo', 'G9 nibo'.
    """
    clean = text.strip()
    clean = re.sub(
        r'\b(?:order|odar|odr|dibo|debo|korbo|kori|din|den|nibo|kinbo|kinte\s*chai|nite\s*chai|'
        r'korte\s*chai|want\s*to\s*buy|buy|place\s*order|please|vai|bhai|bro|amake|ami|'
        r'er\s*ta|ta|ti|টা|টি|অর্ডার|দিব|দেব|করব|নিব|কিনব)\b',
        ' ',
        clean,
        flags=re.IGNORECASE
    )
    clean = re.sub(r'[:=–\-?.,!]', ' ', clean).strip()
    tokens = [w for w in clean.split() if len(w) > 1]
    if tokens:
        return " ".join(tokens)
    return None


class OrderSession:
    """Holds active customer order and conversation session data."""
    def __init__(self, sender_id: str):
        self.sender_id: str = str(sender_id).strip()
        self.state: str = OrderState.NORMAL
        self.product_name: Optional[str] = None
        self.product_price: Optional[str] = None
        self.product_color: Optional[str] = None
        self.quantity: int = 1
        self.customer_name: Optional[str] = None
        self.customer_phone: Optional[str] = None
        self.customer_address: Optional[str] = None
        self.language: str = "banglish"
        self.invalid_phone_reason: Optional[str] = None
        self.last_order_time: float = 0.0
        self.last_order_product: Optional[str] = None
        self.last_order_name: Optional[str] = None
        self.last_cancelled_time: float = 0.0
        self.created_at: float = time.time()
        self.updated_at: float = time.time()

    def is_contact_complete(self) -> bool:
        """Returns True if all 3 required customer details are present."""
        return bool(self.customer_name and self.customer_phone and self.customer_address)

    def is_order_complete(self) -> bool:
        """Returns True if customer contact info AND product are confirmed."""
        return bool(self.is_contact_complete() and self.product_name)

    def get_missing_fields(self) -> List[str]:
        """Returns list of missing fields necessary to finalize an order."""
        missing = []
        if not self.product_name:
            missing.append("product")
        if not self.customer_name:
            missing.append("name")
        if not self.customer_phone:
            missing.append("phone")
        if not self.customer_address:
            missing.append("address")
        return missing

    def reset(self):
        """Resets active order flow to NORMAL state while preserving session history."""
        self.state = OrderState.NORMAL
        self.product_name = None
        self.product_price = None
        self.product_color = None
        self.quantity = 1
        self.customer_name = None
        self.customer_phone = None
        self.customer_address = None
        self.invalid_phone_reason = None
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "sender_id": self.sender_id,
            "state": self.state,
            "product_name": self.product_name,
            "product_price": self.product_price,
            "product_color": self.product_color,
            "quantity": self.quantity,
            "customer_name": self.customer_name,
            "customer_phone": self.customer_phone,
            "customer_address": self.customer_address,
            "language": self.language,
            "is_contact_complete": self.is_contact_complete(),
            "is_order_complete": self.is_order_complete(),
            "missing_fields": self.get_missing_fields(),
            "last_order_time": self.last_order_time,
            "updated_at": self.updated_at
        }


class OrderStateMachine:
    """
    Thread-safe, deterministic Order State Machine for Messenger Bot.
    Coordinates the order lifecycle:
    NORMAL -> PRODUCT_DISCUSSION -> ORDER_STARTED -> COLLECT_DETAILS -> ORDER_REVIEW -> CONFIRMED (or CANCELLED).
    """
    def __init__(self, max_sessions: int = 5000, ttl_seconds: int = 86400):
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self._sessions: OrderedDict[str, Tuple[OrderSession, float]] = OrderedDict()
        self._lock = threading.RLock()

    def get_session(self, sender_id: str) -> OrderSession:
        """
        Thread-safe retrieval of customer OrderSession.
        Evicts expired or LRU sessions to prevent memory leaks.
        """
        sid = str(sender_id).strip()
        with self._lock:
            now = time.time()
            if sid in self._sessions:
                session, ts = self._sessions[sid]
                if now - ts < self.ttl_seconds:
                    self._sessions.move_to_end(sid)
                    self._sessions[sid] = (session, now)
                    return session
                else:
                    del self._sessions[sid]

            new_session = OrderSession(sid)
            if len(self._sessions) >= self.max_sessions:
                self._sessions.popitem(last=False)
            self._sessions[sid] = (new_session, now)
            return new_session

    def reset_session(self, sender_id: str):
        """Resets the customer's order session to NORMAL state."""
        sid = str(sender_id).strip()
        with self._lock:
            session = self.get_session(sid)
            session.reset()

    def set_active_product(
        self,
        sender_id: str,
        product_name: str,
        price: Optional[str] = None,
        color: Optional[str] = None
    ):
        """Sets active product context for a user session."""
        session = self.get_session(sender_id)
        with self._lock:
            session.product_name = product_name
            if price:
                session.product_price = price
            if color:
                session.product_color = color
            if session.state == OrderState.NORMAL:
                session.state = OrderState.PRODUCT_DISCUSSION
            session.updated_at = time.time()

    def extract_fields(
        self,
        user_message: str,
        session: OrderSession,
        entities: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Single-turn multi-field entity extractor.
        Extracts Name, BD Phone (013-019), and Delivery Address simultaneously.
        Once a field is populated, it is preserved and not requested again.
        """
        extracted = {}

        # 1. Inspect pre-extracted entities from intent_service
        if entities:
            if entities.get("name") and not session.customer_name:
                session.customer_name = entities["name"]
                extracted["name"] = entities["name"]
            if entities.get("phone") and not session.customer_phone:
                raw_cand = str(entities["phone"]).strip()
                valid_cand = extract_phone(raw_cand)
                if valid_cand:
                    session.customer_phone = valid_cand
                    session.invalid_phone_reason = None
                    extracted["phone"] = valid_cand
                else:
                    digits = re.sub(r'\D', '', raw_cand)
                    if len(digits) == 10 and digits.startswith(('13', '14', '15', '16', '17', '18', '19')):
                        full_phone = f"0{digits}"
                        session.customer_phone = full_phone
                        session.invalid_phone_reason = None
                        extracted["phone"] = full_phone
                    elif len(digits) == 10 and digits.startswith('01'):
                        session.invalid_phone_reason = f"Apnar phone number e 10 digit peyechi ({digits}), 11 digit er full number ta ektu bolben please?"
                    elif len(digits) < 11:
                        session.invalid_phone_reason = f"Apnar phone number e {len(digits)} digit peyechi, 11 digit er full number ta ektu bolben please?"
                    elif len(digits) > 11:
                        session.invalid_phone_reason = f"Apnar phone number e {len(digits)} digit peyechi, 11 digit er number ta ektu bolben please?"
            if entities.get("address") and not session.customer_address:
                session.customer_address = entities["address"]
                extracted["address"] = entities["address"]
            if entities.get("quantity"):
                session.quantity = entities["quantity"]
                extracted["quantity"] = entities["quantity"]
            if entities.get("product_reference") and not session.product_name:
                session.product_name = entities["product_reference"]
                extracted["product_name"] = entities["product_reference"]

        # 2. Extract labeled fields from user text (e.g. 'Name: Timon address: Uttara phone: 01737996284')
        labeled = extract_labeled_entities(user_message)
        if labeled.get("name") and not session.customer_name:
            session.customer_name = labeled["name"]
            extracted["name"] = labeled["name"]
        if labeled.get("phone") and not session.customer_phone:
            session.customer_phone = labeled["phone"]
            extracted["phone"] = labeled["phone"]
        if labeled.get("address") and not session.customer_address:
            session.customer_address = labeled["address"]
            extracted["address"] = labeled["address"]
        if labeled.get("quantity"):
            session.quantity = labeled["quantity"]
            extracted["quantity"] = labeled["quantity"]

        # Check for unlabelled name at start of message before labels e.g. "Timon number:017..."
        if not session.customer_name:
            m_prefix_name = re.match(
                r'^\s*([A-Za-z\u0980-\u09FF\s]{2,30})\s+(?:number|phone|phn|mobile|address|thikana|ঠিকানা|নাম্বার)\s*[:=-]',
                user_message,
                re.IGNORECASE
            )
            if m_prefix_name:
                cand_name = m_prefix_name.group(1).strip()
                if cand_name and len(cand_name.split()) <= 4 and cand_name.lower() not in ("hi", "hello", "vai", "bhai", "sure", "ok", "yes"):
                    session.customer_name = cand_name
                    extracted["name"] = cand_name

        # 3. Direct BD Phone regex extraction
        if not session.customer_phone:
            phone_cand = extract_phone(user_message)
            if phone_cand:
                session.customer_phone = phone_cand
                session.invalid_phone_reason = None
                extracted["phone"] = phone_cand
            else:
                # Check for possible incomplete or 10-digit phone number
                m_labeled_phone = re.search(
                    r'(?:phone|number|mobile|phn|ফোন|নাম্বার)\s*[:=-]?\s*([0-9\s\-]+)',
                    user_message,
                    re.IGNORECASE
                )
                raw_cand = None
                if m_labeled_phone:
                    raw_cand = re.sub(r'\D', '', m_labeled_phone.group(1))
                else:
                    seqs = re.findall(r'\b\d{9,12}\b', user_message)
                    if seqs:
                        raw_cand = seqs[0]

                if raw_cand:
                    # 10 digits starting with 1 (e.g. 1784898584) -> prepend 0 -> valid BD number!
                    if len(raw_cand) == 10 and raw_cand.startswith(('13', '14', '15', '16', '17', '18', '19')):
                        full_phone = f"0{raw_cand}"
                        session.customer_phone = full_phone
                        session.invalid_phone_reason = None
                        extracted["phone"] = full_phone
                    elif len(raw_cand) == 10 and raw_cand.startswith('01'):
                        session.invalid_phone_reason = f"Apnar phone number e 10 digit peyechi ({raw_cand}), 11 digit er full number ta ektu bolben please?"
                    elif len(raw_cand) < 11:
                        session.invalid_phone_reason = f"Apnar phone number e {len(raw_cand)} digit peyechi, 11 digit er full number ta ektu bolben please?"
                    elif len(raw_cand) > 11:
                        session.invalid_phone_reason = f"Apnar phone number e {len(raw_cand)} digit peyechi, 11 digit er number ta ektu bolben please?"

        # 4. Unlabeled format extraction (e.g. 'Timon 01737996284 Uttara Dhaka')
        if not session.customer_name or not session.customer_address:
            unlabeled = extract_unlabeled_contact_info(user_message)
            if unlabeled.get("name") and not session.customer_name:
                session.customer_name = unlabeled["name"]
                extracted["name"] = unlabeled["name"]
            if unlabeled.get("address") and not session.customer_address:
                session.customer_address = unlabeled["address"]
                extracted["address"] = unlabeled["address"]

        # 5. Color extraction
        col = extract_color(user_message)
        if col and not session.product_color:
            session.product_color = col
            extracted["color"] = col

        session.updated_at = time.time()
        return extracted

    def format_order_summary(self, session: OrderSession) -> str:
        """Formats clean, professional Order Summary for user review."""
        lang = session.language
        prod = session.product_name or "Selected Item"
        col = session.product_color
        qty = session.quantity or 1
        price = session.product_price or "Inquire with Sales"
        name = session.customer_name or "Valued Customer"
        phone = session.customer_phone or "N/A"
        addr = session.customer_address or "N/A"

        color_line = f"🎨 Color: {col}\n" if col else ""

        if lang == "bangla":
            color_line_bn = f"🎨 কালার: {col}\n" if col else ""
            summary = (
                "📋 অর্ডারের বিবরণ:\n"
                f"📦 পণ্য: {prod}\n"
                f"{color_line_bn}"
                f"🔢 পরিমাণ: {qty}\n"
                f"💰 মূল্য: {price}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 নাম: {name}\n"
                f"📞 ফোন: {phone}\n"
                f"📍 ঠিকানা: {addr}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "অর্ডারটি কনফার্ম করতে 'Confirm' বা 'হ্যাঁ' বলুন (বাতিল করতে 'Cancel')।"
            )
        elif lang == "english":
            summary = (
                "📋 Order Summary:\n"
                f"📦 Product: {prod}\n"
                f"{color_line}"
                f"🔢 Quantity: {qty}\n"
                f"💰 Price: {price}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Name: {name}\n"
                f"📞 Phone: {phone}\n"
                f"📍 Address: {addr}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "To confirm your order, please reply 'Confirm' or 'Yes' (to cancel reply 'Cancel')."
            )
        else:
            summary = (
                "📋 Order Summary:\n"
                f"📦 Product: {prod}\n"
                f"{color_line}"
                f"🔢 Quantity: {qty}\n"
                f"💰 Price: {price}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Name: {name}\n"
                f"📞 Phone: {phone}\n"
                f"📍 Address: {addr}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Order confirm korte 'Confirm' ba 'Yes' bolun (cancel korte 'Cancel')."
            )

        return summary

    def format_missing_fields_prompt(self, session: OrderSession) -> str:
        """
        Politely requests ONLY the remaining missing fields.
        Does not ask for fields that were already provided.
        """
        lang = session.language
        missing = session.get_missing_fields()

        # If product itself is missing
        if "product" in missing and not session.product_name:
            if lang == "bangla":
                return "Sure! আপনি কোন প্রোডাক্ট বা কোন মডেলটি অর্ডার করতে চান, একটু জানাবেন please?"
            elif lang == "english":
                return "Sure! Which product or model would you like to order? Please provide the model name."
            else:
                return "Sure! Apni kon product ba model ta order korte chan, ektu bolben please?"

        # Missing contact details
        contact_missing = [m for m in missing if m != "product"]
        if not contact_missing:
            return self.format_order_summary(session)

        # If user provided a malformed / 10-digit phone number, give explicit helpful feedback
        if getattr(session, "invalid_phone_reason", None) and "phone" in missing:
            reason = session.invalid_phone_reason
            session.invalid_phone_reason = None
            if lang == "bangla":
                return "আপনার ফোন নম্বরে ১০ ডিজিট পেয়েছি, অনুগ্রহ করে ১১ ডিজিটের সম্পূর্ণ নম্বরটি জানাবেন please?"
            elif lang == "english":
                return "The phone number provided has 10 digits. Could you please provide the full 11-digit mobile number?"
            else:
                return f"Sure! {reason}"

        if lang == "bangla":
            labels = {
                "name": "নাম",
                "phone": "ফোন নম্বর",
                "address": "ডেলিভারি ঠিকানা"
            }
            items = [labels[m] for m in contact_missing if m in labels]
            items_str = " ও ".join(items) if len(items) <= 2 else ", ".join(items[:-1]) + " ও " + items[-1]
            return f"Sure! অর্ডার কনফার্ম করতে আপনার {items_str} একটু জানাবেন please?"
        elif lang == "english":
            labels = {
                "name": "Name",
                "phone": "Phone Number",
                "address": "Delivery Address"
            }
            items = [labels[m] for m in contact_missing if m in labels]
            items_str = " and ".join(items) if len(items) <= 2 else ", ".join(items[:-1]) + ", and " + items[-1]
            return f"Sure! To confirm your order, could you please provide your {items_str}?"
        else:
            labels = {
                "name": "Name",
                "phone": "Phone Number",
                "address": "Delivery Address"
            }
            items = [labels[m] for m in contact_missing if m in labels]
            items_str = " ar ".join(items) if len(items) <= 2 else ", ".join(items[:-1]) + " ar " + items[-1]
            return f"Sure! Order confirm korte apnar {items_str} ta ektu bolben please?"

    def cancel_order(self, sender_id: str) -> Tuple[bool, str]:
        """
        Immediate order cancellation:
        Cancels active order in progress, resets state to NORMAL,
        and returns localized cancellation confirmation.
        """
        session = self.get_session(sender_id)
        with self._lock:
            lang = session.language
            session.state = OrderState.CANCELLED
            session.last_cancelled_time = time.time()
            session.reset()

            if lang == "bangla":
                msg = "ঠিক আছে, অর্ডারটি বাতিল করে দিলাম। পরে কিছু প্রয়োজন হলে অবশ্যই জানাবেন!"
            elif lang == "english":
                msg = "Alright, your order has been cancelled. Feel free to let us know if you need anything else!"
            else:
                msg = "Thik ache, order ta cancel kore dilam. Pore jodi kichu dorkar hoy, obosshoy bolben!"

            return True, msg

    def confirm_order(self, sender_id: str, platform: str = "Facebook Messenger") -> Tuple[bool, str]:
        """
        Transitions state to CONFIRMED, forwards order to Telegram,
        records order metadata, and resets session to NORMAL.
        """
        session = self.get_session(sender_id)
        with self._lock:
            if not session.is_order_complete():
                return False, self.format_missing_fields_prompt(session)
            # Orders dispatch only from the explicit review step, never
            # implicitly the moment contact details happen to complete.
            if session.state != OrderState.ORDER_REVIEW:
                return False, self.format_order_summary(session)

            session.state = OrderState.CONFIRMED
            prod_name = session.product_name or "Unknown Product"
            price = session.product_price or "Inquire with Sales"
            cust_name = session.customer_name or "Customer"
            cust_phone = session.customer_phone or "Unknown"
            cust_addr = session.customer_address or "Unknown"
            color = session.product_color
            lang = session.language

            # Dispatch order to seller Telegram channel
            try:
                from telegram_service import send_telegram_order
                dispatched = send_telegram_order(
                    product_name=prod_name,
                    price=price,
                    customer_name=cust_name,
                    customer_phone=cust_phone,
                    customer_address=cust_addr,
                    customer_id=session.sender_id,
                    platform=platform,
                    color=color,
                    quantity=int(getattr(session, "quantity", 1) or 1)
                )
                if not dispatched:
                    # Keep the session in review so the order is not lost
                    # and the customer is not told a false success.
                    session.state = OrderState.ORDER_REVIEW
                    return False, "Order ta pathate ektu somoshya hoyeche. Abar try korchi, ektu wait korun."
            except Exception as e:
                logger.error(f"Error forwarding order to Telegram: {e}")
                session.state = OrderState.ORDER_REVIEW
                return False, "Order ta pathate ektu somoshya hoyeche. Abar try korchi, ektu wait korun."

            session.last_order_time = time.time()
            session.last_order_product = prod_name
            session.last_order_name = cust_name

            # Order placed: forget chat/product context (clean start next time).
            try:
                import conversation_context as _ctx
                _ctx.clear(session.sender_id)
            except Exception:
                pass
            try:
                from meta_llama_service import clear_user_history
                clear_user_history(session.sender_id)
            except Exception:
                pass

            # Reset session state for future conversations
            session.reset()

            display_prod = f"{prod_name} ({color})" if color else prod_name

            if lang == "bangla":
                msg = (
                    f"ধন্যবাদ {cust_name}! আপনার {display_prod} এর অর্ডারটি কনফার্ম করা হয়েছে। "
                    f"আমাদের সেলস টিম শীঘ্রই কল করে ডেলিভারি কনফার্ম করবে।"
                )
            elif lang == "english":
                msg = (
                    f"Thank you {cust_name}! Your order for {display_prod} has been confirmed. "
                    f"Our sales team will contact you shortly to confirm delivery."
                )
            else:
                msg = (
                    f"Dhonnobad {cust_name}! {display_prod} er order details peyechi ebong confirm kora hoyeche. "
                    f"Amader sales team shighroi call kore delivery details janiye dibe."
                )

            return True, msg

    def process_turn(
        self,
        sender_id: str,
        user_message: str,
        current_product: Optional[str] = None,
        current_price: Optional[str] = None,
        current_color: Optional[str] = None,
        auto_confirm: bool = False
    ) -> Tuple[bool, Optional[str], dict]:
        """
        Coordinates full state machine lifecycle for a single user turn.
        
        Returns:
            (is_order_turn: bool, response_message: Optional[str], session_snapshot: dict)
        """
        session = self.get_session(sender_id)
        with self._lock:
            # 1. Update session sticky language
            session.language = detect_language(user_message, current_lang=session.language)

            # 2. Contextual intent classification
            ctx = {
                "state": session.state,
                "active_product": session.product_name or current_product,
                "language": session.language
            }
            intent_result = classify_intent(user_message, current_context=ctx)
            intent = intent_result.get("intent", "UNKNOWN")
            is_negated = intent_result.get("is_negated", False)
            entities = intent_result.get("extracted_entities", {})

            try:
                _q = int(getattr(session, "quantity", 1) or 1)
            except (TypeError, ValueError):
                _q = 1
            session.quantity = max(1, min(20, _q))

            # 3. Synchronize external product context if currently discussing
            if current_product and session.state in (OrderState.NORMAL, OrderState.PRODUCT_DISCUSSION):
                session.product_name = current_product
                if current_price:
                    session.product_price = current_price
                if current_color:
                    session.product_color = current_color
                if session.state == OrderState.NORMAL:
                    session.state = OrderState.PRODUCT_DISCUSSION

            # 4. Immediate Cancellation Check:
            # If in ANY active order state and intent is CANCEL_ORDER or negated
            in_order_states = (
                session.state in (OrderState.ORDER_STARTED, OrderState.COLLECT_DETAILS, OrderState.ORDER_REVIEW)
            )
            if (intent == "CANCEL_ORDER" or is_negated) and in_order_states:
                logger.info(f"User {sender_id} cancelled order in state {session.state}.")
                _, cancel_msg = self.cancel_order(sender_id)
                return True, cancel_msg, session.to_dict()

            # If user negated outside of order flow, do not intercept
            if intent == "CANCEL_ORDER" or is_negated:
                return False, None, session.to_dict()

            # 5. Handle ORDER_REVIEW State:
            if session.state == OrderState.ORDER_REVIEW:
                t_lower = user_message.strip().lower()
                confirm_signals = [
                    "confirm", "yes", "ha", "thik ache", "ok", "হাঁ", "ঠিক আছে",
                    "হ্যাঁ", "confirm koro", "confirm korun", "place order", "done"
                ]
                is_confirm = any(re.search(rf"\b{re.escape(sig)}\b", t_lower) for sig in confirm_signals) or (intent == "PLACE_ORDER")
                if is_confirm:
                    _, confirm_msg = self.confirm_order(sender_id)
                    return True, confirm_msg, session.to_dict()

                # User may be correcting or supplying an updated field in review
                extracted = self.extract_fields(user_message, session, entities)
                if extracted:
                    return True, self.format_order_summary(session), session.to_dict()

                # If user asked a new question while reviewing, leave state in ORDER_REVIEW and let normal RAG respond
                return False, None, session.to_dict()

            # 6. Check for immediate re-submission of contact details right after confirmed order (< 5 mins)
            has_contact = bool(extract_phone(user_message) or re.search(r'\b(name|address|thikana|phone)\s*[:=-]', user_message, re.IGNORECASE))
            if has_contact and (time.time() - session.last_order_time < 300) and session.state == OrderState.NORMAL:
                name_str = f"{session.last_order_name}, " if session.last_order_name else ""
                prod = session.last_order_product or "order"
                if session.language == "bangla":
                    msg = f"{name_str}আপনার {prod} এর বিস্তারিত তথ্য পেয়েছি। সেলস টিম শীঘ্রই যোগাযোগ করবে।"
                elif session.language == "english":
                    msg = f"{name_str}your details for {prod} are already recorded. Our sales team will contact you shortly."
                else:
                    msg = f"{name_str}{prod} er details already record kora hoyeche. Amader sales team shighroi call korbe."
                return True, msg, session.to_dict()

            # Check for customer confirmation/acknowledgement words right after placed order (< 5 mins)
            t_clean = user_message.strip().lower()
            post_confirm_words = ["confirm", "confirmed", "ok", "okay", "thik ache", "done", "yes", "ha", "হাঁ", "ঠিক আছে", "কনফার্ম", "accha"]
            if any(re.search(rf"\b{re.escape(w)}\b", t_clean) for w in post_confirm_words) and (time.time() - session.last_order_time < 300) and session.state == OrderState.NORMAL:
                name_str = f" {session.last_order_name}" if session.last_order_name else ""
                if session.language == "bangla":
                    msg = f"আপনার অর্ডারটি ইতিমধ্যে কনফার্ম করা হয়েছে{name_str}। আমাদের সেলস টিম শীঘ্রই যোগাযোগ করবে, ধন্যবাদ!"
                elif session.language == "english":
                    msg = f"Your order is already confirmed{name_str}! Our sales team will contact you shortly, thank you!"
                else:
                    msg = f"Apnar order already confirm kora hoyeche{name_str}! Amader sales team shighroi call korbe, dhonnobad!"
                return True, msg, session.to_dict()

            # 7. Positive Order Intent Trigger (e.g. 'order dibo', 'eta nibo')
            if intent == "PLACE_ORDER":
                session.state = OrderState.ORDER_STARTED

                # Auto-resolve product mentioned directly in order sentence
                mentioned_query = extract_product_mention_from_order_text(user_message)
                if mentioned_query:
                    try:
                        from rag_service import get_rag
                        rag = get_rag()
                        matches = rag.search_specific_products(mentioned_query, top_k=1)
                        if matches and matches[0][1] >= 0.28:
                            matched_prod = matches[0][0]
                            session.product_name = matched_prod.get("name")
                            session.product_price = matched_prod.get("price_formatted")
                    except Exception as e:
                        logger.warning(f"Error matching product from order text: {e}")

                if not session.product_name and current_product:
                    session.product_name = current_product
                if not session.product_price and current_price:
                    session.product_price = current_price
                if not session.product_color and current_color:
                    session.product_color = current_color

                # Extract contact fields provided within the same order message
                self.extract_fields(user_message, session, entities)

                if session.is_order_complete():
                    if auto_confirm:
                        _, confirm_msg = self.confirm_order(sender_id)
                        return True, confirm_msg, session.to_dict()
                    session.state = OrderState.ORDER_REVIEW
                    return True, self.format_order_summary(session), session.to_dict()
                else:
                    session.state = OrderState.COLLECT_DETAILS
                    return True, self.format_missing_fields_prompt(session), session.to_dict()

            # 8. Customer Details Intent Trigger (e.g. user sent phone/address while product is known)
            if intent == "CUSTOMER_DETAILS" or (has_contact and session.state in (OrderState.PRODUCT_DISCUSSION, OrderState.COLLECT_DETAILS, OrderState.ORDER_STARTED)):
                if session.state in (OrderState.NORMAL, OrderState.PRODUCT_DISCUSSION):
                    if current_product:
                        session.product_name = current_product
                        session.product_price = current_price
                        session.state = OrderState.COLLECT_DETAILS
                    elif session.product_name:
                        session.state = OrderState.COLLECT_DETAILS

                self.extract_fields(user_message, session, entities)

                if session.is_order_complete():
                    if auto_confirm:
                        _, confirm_msg = self.confirm_order(sender_id)
                        return True, confirm_msg, session.to_dict()
                    session.state = OrderState.ORDER_REVIEW
                    return True, self.format_order_summary(session), session.to_dict()
                elif session.state in (OrderState.ORDER_STARTED, OrderState.COLLECT_DETAILS):
                    return True, self.format_missing_fields_prompt(session), session.to_dict()

            # 9. Handle In-Progress Order States (COLLECT_DETAILS or ORDER_STARTED) with Robust Escape Hatches:
            if session.state in (OrderState.COLLECT_DETAILS, OrderState.ORDER_STARTED):
                # ESCAPE HATCH A: Customer greets (Hello, Hi, Salam) -> release order lock
                if intent == "GREETING":
                    logger.info(f"User {sender_id} sent greeting while in {session.state}; releasing order lock.")
                    session.state = OrderState.NORMAL
                    return False, None, session.to_dict()

                # ESCAPE HATCH B: Customer asks catalog / product question -> release order lock to let RAG/AI answer
                if intent in (
                    "PRODUCT_SEARCH", "PRODUCT_PRICE", "PRODUCT_SPEC",
                    "PRODUCT_COLOR", "PRODUCT_COMPARISON", "PRODUCT_RECOMMENDATION",
                    "GENERAL_QUESTION", "HUMAN_SUPPORT"
                ):
                    logger.info(f"User {sender_id} asked catalog query ({intent}) in {session.state}; releasing order lock to answer.")
                    session.state = OrderState.NORMAL
                    return False, None, session.to_dict()

                # ESCAPE HATCH C: Customer switches to a new product order (e.g. 'Iphone 15 order dibo')
                if intent == "PLACE_ORDER":
                    mentioned = extract_product_mention_from_order_text(user_message)
                    if mentioned:
                        try:
                            from rag_service import get_rag
                            rag = get_rag()
                            matches = rag.search_specific_products(mentioned, top_k=1)
                            if matches and matches[0][1] >= 0.28:
                                session.product_name = matches[0][0].get("name")
                                session.product_price = matches[0][0].get("price_formatted")
                                logger.info(f"User switched product in {session.state} to: {session.product_name}")
                        except Exception:
                            pass
                    self.extract_fields(user_message, session, entities)
                    if session.is_order_complete():
                        if auto_confirm:
                            _, confirm_msg = self.confirm_order(sender_id)
                            return True, confirm_msg, session.to_dict()
                        session.state = OrderState.ORDER_REVIEW
                        return True, self.format_order_summary(session), session.to_dict()
                    session.state = OrderState.COLLECT_DETAILS
                    return True, self.format_missing_fields_prompt(session), session.to_dict()

                # Normal path: Extract contact fields
                self.extract_fields(user_message, session, entities)

                if session.is_order_complete():
                    if auto_confirm:
                        _, confirm_msg = self.confirm_order(sender_id)
                        return True, confirm_msg, session.to_dict()
                    session.state = OrderState.ORDER_REVIEW
                    return True, self.format_order_summary(session), session.to_dict()
                else:
                    return True, self.format_missing_fields_prompt(session), session.to_dict()

            # Not an active order flow turn
            return False, None, session.to_dict()


# Global Singleton Instance for clean imports
order_state_machine = OrderStateMachine()


def get_order_state_machine() -> OrderStateMachine:
    return order_state_machine


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    print("Testing Order State Machine...")

    sm = OrderStateMachine()
    user_id = "test_user_timon_404"

    # Test 1: Product Discussion -> Order Start -> Single-Turn Multi-Field Extraction
    print("\n--- Scenario 1: Multi-Field Single-Turn Extraction ---")
    sm.set_active_product(user_id, "iPhone 15 Pro Max", price="BDT 155,000", color="Natural Titanium")
    h, reply, s = sm.process_turn(user_id, "order dibo")
    print(f"User: order dibo\nBot: {reply}\nState: {s['state']}\n")

    h, reply, s = sm.process_turn(user_id, "Name: Timon address: Uttara phone: 01737996284")
    print(f"User: Name: Timon address: Uttara phone: 01737996284\nBot:\n{reply}\nState: {s['state']}\n")

    h, reply, s = sm.process_turn(user_id, "confirm")
    print(f"User: confirm\nBot: {reply}\nState: {s['state']}\n")

    # Test 2: Immediate Cancellation
    print("\n--- Scenario 2: Immediate Cancellation ---")
    sm.set_active_product(user_id, "MacBook Air M2", price="BDT 125,000")
    h, reply, s = sm.process_turn(user_id, "order korte chai")
    print(f"User: order korte chai\nBot: {reply}\nState: {s['state']}\n")

    h, reply, s = sm.process_turn(user_id, "order dibo na")
    print(f"User: order dibo na\nBot: {reply}\nState: {s['state']}\n")

    # Test 3: Incremental Missing Fields
    print("\n--- Scenario 3: Incremental Details Collection ---")
    sm.set_active_product(user_id, "Sony WH-1000XM5", price="BDT 38,000")
    h, reply, s = sm.process_turn(user_id, "eta nibo")
    print(f"User: eta nibo\nBot: {reply}\nState: {s['state']}\n")

    h, reply, s = sm.process_turn(user_id, "01737996284")
    print(f"User: 01737996284\nBot: {reply}\nState: {s['state']}\n")

    h, reply, s = sm.process_turn(user_id, "Tanvir")
    print(f"User: Tanvir\nBot: {reply}\nState: {s['state']}\n")

    h, reply, s = sm.process_turn(user_id, "Dhanmondi, Dhaka")
    print(f"User: Dhanmondi, Dhaka\nBot:\n{reply}\nState: {s['state']}\n")
