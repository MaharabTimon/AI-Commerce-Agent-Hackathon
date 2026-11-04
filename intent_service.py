# -*- coding: utf-8 -*-
"""
AI Intent Classifier for Messenger Bot.
Blueprint: "AI decides meaning. Python decides actions."

Classifies incoming customer messages into standard intents:
- PLACE_ORDER
- CANCEL_ORDER
- ORDER_STATUS
- CUSTOMER_DETAILS
- PRODUCT_SEARCH
- PRODUCT_PRICE
- PRODUCT_COLOR
- PRODUCT_SPEC
- PRODUCT_COMPARISON
- PRODUCT_RECOMMENDATION
- GREETING
- HUMAN_SUPPORT
- GENERAL_QUESTION
- UNKNOWN

Flawlessly separates positive order intent ("order dibo", "eta nibo")
from negation/cancellation ("order dibo na", "na thak", "bad dao", "cancel").
Handles Bangla, Banglish, and English seamlessly.
"""

import json
import logging
import re
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Standard Bengali numerals mapping
BENGALI_DIGITS = "\u09e6\u09e7\u09e8\u09e9\u09ea\u09eb\u09ec\u09ed\u09ee\u09ef"
WESTERN_DIGITS = "0123456789"
BENGALI_TO_WESTERN = str.maketrans(BENGALI_DIGITS, WESTERN_DIGITS)


def normalize_bengali_numerals(text: str) -> str:
    """Translates Bengali digits (০-৯) to ASCII digits (0-9)."""
    if not text:
        return ""
    return text.translate(BENGALI_TO_WESTERN)


# Bangladeshi mobile number regex: (013-019) with optional +88 / 880 prefix
BD_PHONE_PATTERN = re.compile(r'(?<!\d)(?:(?:\+?880?|880?)?)(01[3-9]\d{8})(?!\d)')

# Negation and Cancellation Patterns (Banglish, Bengali script, and English)
NEGATION_PATTERNS = [
    # Banglish negation of order / purchase
    r'\b(?:order|odar|odr)\s*(?:dibo|debo|korbo|korte|dite)?\s*na\b',
    r'\b(?:dibo|debo|korbo)\s*na\s*(?:order|odar)?\b',
    r'\b(?:nibo|kinbo|nite|kinte)\s*na\b',
    r'\b(?:chai|chay)\s*na\b',
    r'\bna\s*thak(?:e)?\b',
    r'\b(?:lagbe|lagbo)\s*na\b',
    r'\b(?:ar\s*)?lagbe\s*na\b',
    r'\bdorkar\s*(?:nai|nei)\b',
    r'\b(?:bad|baad)\s*(?:dao|den|diye\s*den)\b',
    r'\b(?:order\s*)?cancel(?:\s*kore?\s*(?:den|dao|din))?\b',
    r'\b(?:cancel|abort)\s*(?:my\s*)?(?:order|it)?\b',
    r'\b(?:not\s+buying|no\s+thanks|not\s+interested)\b',
    r"\b(?:do\s*not|don't|dont)\s*(?:want|need|order)\b",
    r'\bdrop\s*(?:the\s*)?order\b',
    
    # Bengali Script negation
    r'অর্ডার\s*(?:দিব|দেব|করব|করবো|করতে|দিতে)?\s*না',
    r'(?:দিব|দেব|করব|করবো)\s*না\s*অর্ডার',
    r'(?:নিব|কিনব|নিতে|কিনতে)\s*না',
    r'না\s*থাক',
    r'লাগবে\s*না',
    r'দরকার\s*নেই',
    r'দরকার\s*নাই',
    r'বাদ\s*(?:দাও|দিন)',
    r'বাতিল\s*(?:করুন|করো|হবে)',
    r'অর্ডার\s*(?:বাতিল|ক্যান্সেল|ক্যানসেল)',
    r'(?:ক্যানসেল|ক্যান্সেল)'
]

NEGATION_RE = re.compile('|'.join(f'(?:{p})' for p in NEGATION_PATTERNS), re.IGNORECASE)

# Positive Order Patterns (Banglish, Bengali, English)
POSITIVE_ORDER_PATTERNS = [
    r'\b(?:order|odar|odr)\s*(?:dibo|debo|korbo|korte\s*chai|kori|din|den)\b',
    r'\b(?:kinte\s*chai|kinbo|kivabe\s*kinbo|kivabe\s*order)\b',
    r'\b(?:nibo|nite\s*chai|eta\s*nibo|eita\s*nibo|eyta\s*nibo|ami\s*nibo)\b',
    r'\b(?:want\s*to\s*(?:buy|order)|place\s*order|buy\s*now|how\s*to\s*order|order\s*this)\b',
    r'অর্ডার\s*(?:করব|করবো|করতে\s*চাই|দিব|দেব|করুন)',
    r'(?:কিনতে\s*চাই|কিনব|নিব|নিতে\s*চাই|এটা\s*নিব|এইটা\s*নিব)'
]

POSITIVE_ORDER_RE = re.compile('|'.join(f'(?:{p})' for p in POSITIVE_ORDER_PATTERNS), re.IGNORECASE)

# Explicit Customer Field Separators
LABEL_KEYS = (
    r'(?:name|naam|nam|নাম|address|thikana|ঠিকানা|phone|phn|mobile|ফোন|'
    r'নাম্বার|number|qty|quantity|পরিমাণ|কালার|color|প্রোডাক্ট|product)'
)
FIELD_LOOKAHEAD = rf'(?=(?:\s+(?:{LABEL_KEYS})[\s:=–\-]+)|$)'


def extract_phone(text: str) -> Optional[str]:
    """
    Extracts an 11-digit Bangladeshi mobile number.
    Converts Bengali numerals and handles dashes/spaces.
    """
    if not text:
        return None
    norm = normalize_bengali_numerals(text)

    # 1. Direct standard regex
    m = BD_PHONE_PATTERN.search(norm)
    if m:
        return m.group(1)

    # 2. Handles sequences with spaces or dashes, e.g. 01737-996284, +880 1737 996284
    candidates = re.findall(r'(?<!\d)(?:(?:\+?880?|880?)?)([0-9\s\-]{11,18})(?!\d)', norm)
    for c in candidates:
        digits = re.sub(r'\D', '', c)
        if digits.startswith('880') and len(digits) == 13:
            digits = digits[2:]
        elif digits.startswith('88') and len(digits) == 13:
            digits = digits[2:]
        if len(digits) == 11 and digits.startswith('01') and digits[2] in '3456789':
            return digits

    return None


def extract_labeled_entities(text: str) -> Dict[str, Optional[Any]]:
    """
    Extracts labeled entities like Name, Address, Phone, and Quantity from structured customer input.
    """
    results: Dict[str, Optional[Any]] = {
        "name": None,
        "phone": None,
        "address": None,
        "quantity": None,
        "product_reference": None
    }
    if not text:
        return results

    # Name
    name_m = re.search(
        rf'(?i)(?:^|[\s,;])(?:name|naam|nam|নাম|amar\s+naam|amar\s+nam)[\s:=–\-]+(.+?){FIELD_LOOKAHEAD}',
        text
    )
    if name_m:
        val = name_m.group(1).strip(" ,;-\t\r\n")
        if val:
            results["name"] = val

    # Address
    addr_m = re.search(
        rf'(?i)(?:^|[\s,;])(?:address|thikana|ঠিকানা|location|লোকেশন|delivery\s*address)[\s:=–\-]+(.+?){FIELD_LOOKAHEAD}',
        text
    )
    if addr_m:
        val = addr_m.group(1).strip(" ,;-\t\r\n")
        if val:
            results["address"] = val

    # Phone
    phone_m = re.search(
        rf'(?i)(?:^|[\s,;])(?:phone|phn|mobile|ফোন|নাম্বার|number)[\s:=–\-]+(.+?){FIELD_LOOKAHEAD}',
        text
    )
    if phone_m:
        phone_raw = phone_m.group(1).strip(" ,;-\t\r\n")
        extracted = extract_phone(phone_raw)
        if extracted:
            results["phone"] = extracted

    # Fallback phone if not found in label
    if not results["phone"]:
        results["phone"] = extract_phone(text)

    # Quantity
    qty_m = re.search(r'(?i)\b(\d+|[১-৯])\s*(?:ta|ti|টা|টি|piece|pieces|pcs|pc|খানা)\b', text)
    if qty_m:
        norm_qty = normalize_bengali_numerals(qty_m.group(1))
        try:
            results["quantity"] = int(norm_qty)
        except ValueError:
            pass

    return results


def extract_unlabeled_contact_info(text: str) -> Dict[str, Optional[str]]:
    """
    Fallback parser for unlabeled 2-or-3 token contact lines,
    e.g. 'Timon 01737996284 Uttara Dhaka' or 'রবিউল ০১৯১৪৭৮৮৫৮৯ গাজীপুর'.
    """
    results: Dict[str, Optional[str]] = {"name": None, "phone": None, "address": None}
    phone = extract_phone(text)
    if not phone:
        return results
    results["phone"] = phone

    norm = normalize_bengali_numerals(text)
    match_span = None
    for m in re.finditer(re.escape(phone), norm):
        match_span = m.span()
        break
    if not match_span:
        for m in re.finditer(r'(?<!\d)(?:(?:\+?880?|880?)?)' + re.escape(phone) + r'(?!\d)', norm):
            match_span = m.span()
            break

    if match_span:
        before = text[:match_span[0]].strip(" ,;-\t\r\n")
        after = text[match_span[1]:].strip(" ,;-\t\r\n")

        # Case 1: Name before phone, Address after phone
        if before and after:
            if len(before.split()) <= 4 and not re.search(r'\b(road|sector|house|uttara|dhaka|mirpur)\b', before, re.IGNORECASE):
                results["name"] = before
                results["address"] = after
            else:
                results["address"] = before
                results["name"] = after
        elif before and not after:
            if len(before.split()) <= 3 and not re.search(r'\b(road|sector|house|uttara|dhaka|mirpur)\b', before, re.IGNORECASE):
                results["name"] = before
            else:
                results["address"] = before
        elif after and not before:
            if len(after.split()) <= 3 and not re.search(r'\b(road|sector|house|uttara|dhaka|mirpur)\b', after, re.IGNORECASE):
                results["name"] = after
            else:
                results["address"] = after

    return results


def check_is_negated(text: str) -> bool:
    """Checks whether the text expresses negation or cancellation."""
    if not text:
        return False
    return bool(NEGATION_RE.search(text))


# Semantic classification uses Meta Llama (see _llama_json). No other LLM.


def _fast_rule_classify(user_message: str, current_context: Optional[dict] = None) -> Optional[dict]:
    """
    Ultra-fast rule / regex intent classifier (< 1ms).
    Handles obvious positive orders, negations, greetings, and customer detail messages.
    """
    clean_text = (user_message or "").strip()
    if not clean_text:
        return {
            "intent": "UNKNOWN",
            "confidence": 0.5,
            "extracted_entities": {
                "name": None, "phone": None, "address": None, "quantity": None, "product_reference": None
            },
            "is_negated": False
        }

    is_neg = check_is_negated(clean_text)
    entities = extract_labeled_entities(clean_text)
    if not entities["name"] and not entities["address"]:
        unlabeled = extract_unlabeled_contact_info(clean_text)
        if unlabeled.get("name"):
            entities["name"] = unlabeled["name"]
        if unlabeled.get("address"):
            entities["address"] = unlabeled["address"]

    t_lower = clean_text.lower()

    # 1. Immediate Negation / Cancellation
    if is_neg:
        return {
            "intent": "CANCEL_ORDER",
            "confidence": 0.98,
            "extracted_entities": entities,
            "is_negated": True
        }

    # 2. Customer Contact Details (e.g. provides phone number or full labeled form)
    has_contact = bool(entities["phone"] or (entities["name"] and entities["address"]))
    if has_contact:
        return {
            "intent": "CUSTOMER_DETAILS",
            "confidence": 0.96,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 3. Explicit Positive Order Intent
    if POSITIVE_ORDER_RE.search(clean_text):
        # Extract product reference if mentioned
        prod_ref = None
        for ref in ["eta", "eita", "eyta", "oyta", "এইটা", "এটা", "ওইটা"]:
            if re.search(rf'\b{ref}\b', t_lower):
                prod_ref = ref
                break
        if not prod_ref and current_context and current_context.get("active_product"):
            prod_ref = current_context["active_product"]
        entities["product_reference"] = prod_ref

        return {
            "intent": "PLACE_ORDER",
            "confidence": 0.97,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 4. Standard Greetings
    greeting_re = re.compile(
        r'^(?:hi|hello|hey|salam|assalamu\s*alaikum|slaam|hlw|hlo|good\s*morning|good\s*evening|'
        r'হাই|হ্যালো|সালাম|আসসালামু\s*আলাইকুম)[\s!.,?]*$',
        re.IGNORECASE
    )
    if greeting_re.match(clean_text):
        return {
            "intent": "GREETING",
            "confidence": 0.98,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 5. Order Status Query
    status_re = re.compile(
        r'\b(?:order\s*status|track(?:ing)?\s*order|amar\s*order\s*(?:kothay|koi|kobe\s*asbe|kobe\s*pabo)|delivery\s*status|'
        r'অর্ডার\s*(?:স্ট্যাটাস|কোথায়|কবে\s*পাব|ট্র্যাক))\b',
        re.IGNORECASE
    )
    if status_re.search(clean_text):
        return {
            "intent": "ORDER_STATUS",
            "confidence": 0.95,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 5.5. Product Availability / Search Query
    search_re = re.compile(
        r'\b(?:ache|aase|ase|available|pabo|paowa\s*jabe|stock\s*e|ki\s*ki\s*(?:model|phone|laptop|monitor|item|product)|'
        r'আছে|পাওয়া\s*যাবে|স্টকে\s*আছে)\b',
        re.IGNORECASE
    )
    if search_re.search(clean_text):
        return {
            "intent": "PRODUCT_SEARCH",
            "confidence": 0.95,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 6. Product Price Query
    price_re = re.compile(
        r'\b(?:dam|daam|price|cost|taka|rate)\s*(?:koto|kemon|ki)?\b|\b(?:koto\s*dam|koto\s*price|how\s*much)\b|'
        r'(?:দাম\s*কত|প্রাইস\s*কত|কত\s*টাকা)',
        re.IGNORECASE
    )
    if price_re.search(clean_text):
        return {
            "intent": "PRODUCT_PRICE",
            "confidence": 0.95,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 7. Product Color Query
    color_re = re.compile(
        r'\b(?:color|colour|rong)\s*(?:ki\s*ki|koyta|ache)?\b|\b(?:what\s*colors?|available\s*colors?)\b|'
        r'(?:কালার\s*(?:কি\s*কি|কয়টা|আছে)|রং\s*(?:কি\s*কি|কয়টা))',
        re.IGNORECASE
    )
    if color_re.search(clean_text):
        return {
            "intent": "PRODUCT_COLOR",
            "confidence": 0.95,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 8. Product Spec Query
    spec_re = re.compile(
        r'\b(?:spec|specs|specification|specifications|ram|storage|processor|display|battery|refresh\s*rate|warranty)\b|'
        r'(?:র‍্যাম|ব্যাটারি|স্পেসিফিকেশন)',
        re.IGNORECASE
    )
    if spec_re.search(clean_text):
        return {
            "intent": "PRODUCT_SPEC",
            "confidence": 0.92,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 9. Product Comparison Query
    comp_re = re.compile(
        r'\b(?:vs|versus|compare|comparison|konta\s*bhalo|konta\s*better|which\s*is\s*better)\b|'
        r'(?:কোনটা\s*ভালো|তুলনা)',
        re.IGNORECASE
    )
    if comp_re.search(clean_text):
        return {
            "intent": "PRODUCT_COMPARISON",
            "confidence": 0.93,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 10. Product Recommendation Query
    rec_re = re.compile(
        r'\b(?:recommend|suggest|suggestion|recommendation|bhalo\s*(?:hobe|ekta)|best\s*(?:phone|laptop|headphone|budget))\b|'
        r'(?:সাজেস্ট|কোনটা\s*নিলে\s*ভালো)',
        re.IGNORECASE
    )
    if rec_re.search(clean_text):
        return {
            "intent": "PRODUCT_RECOMMENDATION",
            "confidence": 0.92,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 11. Human Support Query
    human_re = re.compile(
        r'\b(?:human|agent|support|admin|call\s*me|phone\s*den|manush\s*er\s*sathe|kotha\s*bolbo)\b|'
        r'(?:মানুষের\s*সাথে\s*কথা|এজেন্ট|সাপোর্ট|কল\s*দিন)',
        re.IGNORECASE
    )
    if human_re.search(clean_text):
        return {
            "intent": "HUMAN_SUPPORT",
            "confidence": 0.94,
            "extracted_entities": entities,
            "is_negated": False
        }

    # 12. General Question Query
    general_re = re.compile(
        r'\b(?:location|shop\s*kothay|showroom|delivery\s*charge|delivery\s*koto|return\s*policy)\b|'
        r'(?:শোরুম\s*কোথায়|ডেলিভারি\s*চার্জ)',
        re.IGNORECASE
    )
    if general_re.search(clean_text):
        return {
            "intent": "GENERAL_QUESTION",
            "confidence": 0.92,
            "extracted_entities": entities,
            "is_negated": False
        }

    return None


def _llama_json(system_instruction: str, user_text: str) -> dict:
    """One Meta Llama JSON call. Raises on any failure; callers fall back."""
    import config as _config
    from meta_llama_service import _call_llama
    if not _config.META_MODEL_API_KEY:
        raise ValueError("META_MODEL_API_KEY not configured")
    raw = _call_llama(
        [{"role": "system", "content": system_instruction},
         {"role": "user", "content": user_text}],
        _config.LLAMA_MODEL_NAME)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in Llama reply")
    return json.loads(raw[start:end + 1])


def _llm_classify(user_message: str, current_context: Optional[dict] = None) -> dict:
    """
    Deep semantic intent classification using Meta Llama JSON mode.
    """
    if not (user_message or "").strip():
        return {
            "intent": "UNKNOWN",
            "confidence": 0.5,
            "extracted_entities": {
                "name": None, "phone": None, "address": None, "quantity": None, "product_reference": None
            },
            "is_negated": False
        }

    ctx_desc = ""
    if current_context:
        ctx_desc = f"Current Session Context: {json.dumps(current_context, ensure_ascii=False)}\n"

    system_instruction = (
        "You are an AI intent classification and entity extraction engine for a Bangladeshi electronics e-commerce store.\n"
        "Your job is to strictly classify customer messages into one of the following intent labels:\n"
        "- PLACE_ORDER: Positive intent to purchase or order ('order dibo', 'eta nibo', 'i want to buy').\n"
        "- CANCEL_ORDER: Explicit cancellation or negative intent ('order dibo na', 'na thak', 'bad dao', 'cancel').\n"
        "- ORDER_STATUS: Tracking existing orders ('order status', 'amar order kothay').\n"
        "- CUSTOMER_DETAILS: Providing name, phone, or address details.\n"
        "- PRODUCT_SEARCH: Inquiring if products/categories are in stock ('laptop ache?').\n"
        "- PRODUCT_PRICE: Asking price/cost ('dam koto', 'price?').\n"
        "- PRODUCT_COLOR: Inquiring about color/variant availability.\n"
        "- PRODUCT_SPEC: Inquiring about technical specs, RAM, battery, etc.\n"
        "- PRODUCT_COMPARISON: Comparing two or more items ('which is better', 'konta bhalo').\n"
        "- PRODUCT_RECOMMENDATION: Asking for suggestions or best products under a budget.\n"
        "- GREETING: Casual hello, hi, salam, etc.\n"
        "- HUMAN_SUPPORT: Asking for real agent, support, or phone call.\n"
        "- GENERAL_QUESTION: Store hours, location, warranty, delivery charges.\n"
        "- UNKNOWN: Irrelevant or unclassifiable text.\n\n"
        "CRITICAL RULES:\n"
        "1. NEGATION SAFETY: If customer says 'order dibo na', 'order korbo na', 'na thak', 'lagbe na', 'dibo na order', "
        "or 'cancel', you MUST set 'is_negated': true and intent: 'CANCEL_ORDER'. NEVER label it as PLACE_ORDER!\n"
        "2. ENTITY EXTRACTION: Extract customer 'name', 'phone' (11 digits), 'address', 'quantity' (integer), "
        "and 'product_reference'. If a field is not found, return null.\n\n"
        "Output STRICTLY JSON with this schema:\n"
        "{\n"
        '  "intent": "INTENT_LABEL",\n'
        '  "confidence": 0.95,\n'
        '  "extracted_entities": {\n'
        '    "name": "string or null",\n'
        '    "phone": "string or null",\n'
        '    "address": "string or null",\n'
        '    "quantity": 1,\n'
        '    "product_reference": "string or null"\n'
        '  },\n'
        '  "is_negated": false\n'
        "}"
    )

    try:
        data = _llama_json(system_instruction, f"{ctx_desc}Customer Message: \"{user_message[:2000]}\"")

        intent = data.get("intent", "UNKNOWN").upper()
        confidence = float(data.get("confidence", 0.8))
        entities = data.get("extracted_entities", {})
        is_negated = bool(data.get("is_negated", False))

        cleaned_entities = {
            "name": entities.get("name") if entities.get("name") not in (None, "null", "None", "") else None,
            "phone": entities.get("phone") if entities.get("phone") not in (None, "null", "None", "") else None,
            "address": entities.get("address") if entities.get("address") not in (None, "null", "None", "") else None,
            "quantity": int(entities["quantity"]) if entities.get("quantity") and str(entities["quantity"]).isdigit() else None,
            "product_reference": entities.get("product_reference") if entities.get("product_reference") not in (None, "null", "None", "") else None
        }

        return {
            "intent": intent,
            "confidence": confidence,
            "extracted_entities": cleaned_entities,
            "is_negated": is_negated
        }
    except Exception as e:
        logger.warning(f"Meta Llama intent classification error: {e}")
        return {
            "intent": "UNKNOWN",
            "confidence": 0.4,
            "extracted_entities": {
                "name": None, "phone": None, "address": None, "quantity": None, "product_reference": None
            },
            "is_negated": False
        }


def classify_intent(user_message: str, current_context: Optional[dict] = None) -> dict:
    """
    Main entry point for AI Intent Classification.
    Combines high-speed rule matching with deep semantic LLM reasoning.
    
    Guarantees:
    - Negations like 'order dibo na' are ALWAYS flagged with is_negated=True.
    - BD Phone numbers are ALWAYS extracted via regex regardless of LLM quirks.
    - Zero crashes on unexpected inputs.
    """
    if user_message is None or not isinstance(user_message, str):
        user_message = "" if user_message is None else str(user_message)
    clean_text = user_message.strip()[:4000]
    if not clean_text:
        return {
            "intent": "UNKNOWN",
            "confidence": 0.0,
            "extracted_entities": {
                "name": None, "phone": None, "address": None, "quantity": None, "product_reference": None
            },
            "is_negated": False
        }

    # 1. Fast Rule-Based Pass
    rule_res = _fast_rule_classify(clean_text, current_context=current_context)
    if rule_res and rule_res["confidence"] >= 0.90:
        return rule_res

    # 2. Semantic LLM Pass for complex or conversational phrases
    llm_res = _llm_classify(clean_text, current_context=current_context)

    # 3. Defensive Reconciliations: Rules always override critical safety invariants
    # Check 1: Invariant Negation
    if check_is_negated(clean_text):
        llm_res["is_negated"] = True
        llm_res["intent"] = "CANCEL_ORDER"
        llm_res["confidence"] = max(llm_res["confidence"], 0.98)

    # Check 2: BD Phone number extraction safety
    rule_phone = extract_phone(clean_text)
    if rule_phone:
        llm_res["extracted_entities"]["phone"] = rule_phone
        if llm_res["intent"] in ("UNKNOWN", "GENERAL_QUESTION"):
            llm_res["intent"] = "CUSTOMER_DETAILS"

    # Check 3: If LLM failed completely (UNKNOWN with low confidence), fall back to rule result
    if llm_res["intent"] == "UNKNOWN" and rule_res:
        return rule_res

    return llm_res


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    print("Testing Intent Classifier...")
    test_cases = [
        "order dibo na",
        "order dibo",
        "eta nibo",
        "na thak lagbe na",
        "bad dao",
        "cancel",
        "Name: Timon address: Uttara phone: 01737996284",
        "01737996284",
        "iPhone 15 er dam koto?",
        "koyta color ache?",
        "hi",
        "amar order kothay?"
    ]
    for tc in test_cases:
        res = classify_intent(tc)
        print(f"Message: {tc:45} -> Intent: {res['intent']:18} Negated: {res['is_negated']} Phone: {res['extracted_entities']['phone']}")
