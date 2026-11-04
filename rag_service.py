import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple, Set

from rapidfuzz import fuzz, process
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
except Exception:
    TfidfVectorizer = None
    cosine_similarity = None

from database import get_product_repository, BaseProductRepository

logger = logging.getLogger(__name__)


def _specs_for_llm(specs) -> str:
    """Specs for the model: drop Link:/Image: URLs so it never copies them.

    The photo itself is attached separately by the send layer.
    """
    s = str(specs or "")
    s = re.sub(r"\|\s*(Link|Image):\s*https?://\S+", "", s)
    s = re.sub(r"https?://\S+", "", s)
    return s.strip()

# Category synonyms mapping (English, Banglish, Bengali)
CATEGORY_SYNONYMS: Dict[str, List[str]] = {
    "Laptops": [
        "laptop", "laptops", "leptop", "leptops", "notebook", "notebooks", 
        "ultrabook", "macbook", "macbooks", "ল্যাপটপ", "লেপটপ"
    ],
    "Smartphones": [
        "phone", "phones", "smartphone", "smartphones", "mobile", "mobiles", 
        "cellphone", "android", "iphone", "iphones", "ফোন", "মোবাইল", "স্মার্টফোন"
    ],
    "Audio & Headphones": [
        "headphone", "headphones", "headfone", "headfones", "earphone", "earphones", 
        "earbud", "earbuds", "airpod", "airpods", "tws", "speaker", "speakers", 
        "headset", "audio", "sound", "হেডফোন", "ইয়ারফোন", "স্পিকার"
    ],
    "Smartwatches & Fitness": [
        "watch", "watches", "smartwatch", "smartwatches", "smart watch", "fitness band", 
        "ghori", "watch band", "ঘড়ি", "ঘড়ি", "স্মার্টওয়াচ"
    ],
    "Cameras & Photography": [
        "camera", "cameras", "cam", "cams", "dslr", "action cam", "gimbal", "ক্যামেরা"
    ],
    "PC Components & Gaming": [
        "graphics card", "graphic card", "motherboard", "pc build", "gaming pc", 
        "desktop pc", "গ্রাফিক্স কার্ড", "মাদারবোর্ড"
    ],
    "Monitors & Displays": [
        "monitor", "monitors", "pc monitor", "gaming monitor", "computer monitor", "মনিটর"
    ],
    "Tablets": [
        "tablet", "tablets", "tab", "tabs", "ipad", "ipads", "ট্যাবলেট", "ট্যাব"
    ],
    "Accessories & Peripherals": [
        "mouse", "mice", "keyboard", "keyboards", "power bank", "powerbank", "charger", 
        "hub", "dock", "cable", "accessories", "মাউস", "কীবোর্ড"
    ],
    "Smart Home & Appliances": [
        "vacuum", "purifier", "air purifier", "smart home", "cleaner", "ভ্যাকুয়াম"
    ]
}

# Root Brand mappings including English and Bengali transliterations
BRAND_ROOT_MAP: Dict[str, str] = {
    # English
    "asus": "Asus",
    "rog": "Asus",
    "tuf": "Asus",
    "zenbook": "Asus",
    "apple": "Apple",
    "iphone": "Apple",
    "iphones": "Apple",
    "ipad": "Apple",
    "ipads": "Apple",
    "macbook": "Apple",
    "mac": "Apple",
    "airpod": "Apple",
    "airpods": "Apple",
    "dell": "Dell",
    "alienware": "Dell",
    "lenovo": "Lenovo",
    "thinkpad": "Lenovo",
    "legion": "Lenovo",
    "hp": "HP",
    "samsung": "Samsung",
    "galaxy": "Samsung",
    "xiaomi": "Xiaomi",
    "redmi": "Xiaomi",
    "poco": "Xiaomi",
    "sony": "Sony",
    "bose": "Bose",
    "oneplus": "OnePlus",
    "google": "Google Pixel",
    "pixel": "Google Pixel",
    "nothing": "Nothing",
    "acer": "Acer Swift",
    "predator": "Acer Swift",
    "razer": "Razer",
    "msi": "MSI",
    "jbl": "JBL",
    "marshall": "Marshall",
    "logitech": "Logitech",
    "anker": "Anker",
    "dyson": "Dyson",
    "roborock": "Roborock",
    "nvidia": "NVIDIA GeForce",
    "amd": "AMD",
    "intel": "Intel Core",
    "canon": "Canon EOS",
    "nikon": "Nikon Z",
    "fujifilm": "Fujifilm",
    "oppo": "Oppo",
    "vivo": "Vivo",
    "realme": "Realme",
    "garmin": "Garmin",
    "amazfit": "Amazfit",
    "huawei": "Huawei Watch",
    "fitbit": "Fitbit",
    "keychron": "Keychron",
    "hyperx": "HyperX",
    # Bengali Script Transliterations
    "স্যামসাং": "Samsung",
    "স্যামসং": "Samsung",
    "অ্যাপল": "Apple",
    "এপল": "Apple",
    "আইফোন": "Apple",
    "ডেল": "Dell",
    "দেল": "Dell",
    "এইচপি": "HP",
    "এইচ পি": "HP",
    "আসুস": "Asus",
    "লেনোভো": "Lenovo",
    "লেনভো": "Lenovo",
    "শাওমি": "Xiaomi",
    "শাওমী": "Xiaomi",
    "রেডমি": "Xiaomi",
    "সনি": "Sony",
    "বোস": "Bose",
    "ওয়ানপ্লাস": "OnePlus",
    "ওয়ানপ্লাস": "OnePlus",
    "গুগল": "Google Pixel",
    "পিক্সেল": "Google Pixel",
    "জেবিএল": "JBL",
    "লজিটেক": "Logitech",
    "রিয়েলমি": "Realme",
    "রিয়েলমি": "Realme",
    "অপ্পো": "Oppo",
    "ভিভো": "Vivo",
    "মার্শাল": "Marshall",
    "আংকার": "Anker",
    "এংকার": "Anker",
    "ইন্টেল": "Intel Core",
    "ক্যানন": "Canon EOS",
    "নিকন": "Nikon Z"
}

# Product inquiry keywords indicating intent to explore catalog/pricing/specifications
INQUIRY_KEYWORDS = [
    "price", "cost", "daam", "dam", "koto", "koto taka", "taka", "tk", "bdt", "টাকা",
    "specs", "specification", "specifications", "feature", "features", "ram", "storage", 
    "display", "screen", "refresh rate", "refresh", "hz", "processor", "camera", "battery",
    "laptop", "phone", "headphone", "monitor", "watch", "have", "stock", "available", 
    "buy", "show", "tell me", "about", "ache", "asche", "asbe", "kobe",
    "eyta", "eita", "eta", "oyta", "color", "colour", "rong", "variant", "edition",
    "ki ki", "kon kon", "konta", "better", "difference", "compare", 
    "list", "দাম", "মূল্য", "কতো", "আছে", "কী কী", "কোন কোন", "স্পেসিফিকেশন", "বাজেট", "budget"
]

COMPARISON_KEYWORDS = [
    "better", "difference", "compare", "which one", "which is better", "which is best",
    "konta better", "konta bhalo", "konta valo", "konta beshi valo", "konta beshi bhalo",
    "konta nibo", "konta nile bhalo hobe", "parthokko", "tulanay", "তুলনা", "কোনটা ভালো", "কোনটি ভালো"
]

SPEC_TOKENS = [
    "rtx", "gtx", "intel", "amd", "ryzen", "core i3", "core i5", "core i7", "core i9",
    "oled", "amoled", "144hz", "120hz", "165hz", "240hz", "4k", "1080p", "1440p",
    "128gb", "256gb", "512gb", "1tb", "2tb", "8gb", "16gb", "32gb", "64gb",
    "snapdragon", "m1", "m2", "m3", "m4", "bionic", "plus", "pro", "max", "ultra"
]

GREETING_OR_CHITCHAT_PATTERNS = [
    r"^\s*(?:hi|hello|hey|hola|salam|assalamu\s*alaikum|kemon\s*achen|kemon\s*acho|ki\s*obostha|ki\s*khobor|bhai|vai|bro|boss|sir)\b[?!.\s]*$",
    r"^\s*(?:হাই|হ্যালো|হেলো|সালাম|আসসালামু\s*আলাইকুম|কেমন\s*আছেন|কেমন\s*আছো|কি\s*অবস্থা|কি\s*খবর|ভাই)\b[?!.\s]*$",
    r"^\s*(?:ok|okay|thik\s*ache|accha|acha|done|great|thanks|thank\s*you|dhonnobad|ধন্যবাদ|ঠিক\s*আছে|আচ্ছা)\b[?!.\s]*$",
    r"^\s*(?:kemon\s*achen|kemon\s*acho|ki\s*obostha|ki\s*khobor)\b[?!.\s]*$",
    r"^\s*(?:কেমন\s*আছেন|কেমন\s*আছো|কি\s*অবস্থা|কি\s*খবর)\b[?!.\s]*$",
]


class ConversationSession:
    """Stores multi-turn conversational context for an active user."""
    def __init__(self):
        self.last_category: Optional[str] = None
        self.last_brand: Optional[str] = None
        self.last_products: List[Dict] = []
        self.recent_products: List[Dict] = []  # Ring buffer of up to 4 recent distinct products across turns
        self.last_query: str = ""

    def update(
        self,
        category: Optional[str] = None,
        brand: Optional[str] = None,
        products: Optional[List[Dict]] = None,
        query: str = ""
    ):
        if category:
            self.last_category = category
        if brand:
            self.last_brand = brand
        if products:
            self.last_products = products
            for p in products:
                p_id = p.get("id") or p.get("name")
                self.recent_products = [x for x in self.recent_products if (x.get("id") or x.get("name")) != p_id]
                self.recent_products.append(p)
            if len(self.recent_products) > 4:
                self.recent_products = self.recent_products[-4:]
        if query:
            self.last_query = query


TYPO_MAP = {
    "leptop": "laptop", "labtop": "laptop", "phn": "phone", "fone": "phone",
    "headfone": "headphone", "hedphone": "headphone", "moniter": "monitor",
    "camra": "camera", "wathc": "watch", "charjer": "charger", "chargar": "charger",
    "keybord": "keyboard", "mause": "mouse", "iphon": "iphone", "samsang": "samsung",
    "dam": "price", "daam": "price",
}


def normalize_query(query: str) -> str:
    """Lowercase, collapse whitespace, fix common Banglish typos."""
    q = " ".join((query or "").lower().split())[:1000]
    return " ".join(TYPO_MAP.get(w, w) for w in q.split())


ORDINALS = {"first": 0, "prothom": 0, "1st": 0, "second": 1, "dwitiyo": 1, "ditiyo": 1, "2nd": 1,
            "third": 2, "tritiyo": 2, "3rd": 2, "last": -1, "sesher": -1}


def extract_ordinal(query: str) -> Optional[int]:
    """'second one', 'dwitiyo ta' -> index into the last shown product list."""
    q = f" {(query or '').lower()} "
    for word, idx in ORDINALS.items():
        if f" {word} " in q or f" {word}ta" in q or f"{word} ta" in q:
            return idx
    return None


OCCASIONS = {"wedding": ["wedding", "biye", "বিয়ে", "holud", "walima"],
             "eid": ["eid", "ঈদ"], "office": ["office", "formal", "অফিস"],
             "gift": ["gift", "upohar", "উপহার"], "party": ["party", "পার্টি"]}


def extract_occasion(query: str) -> Optional[str]:
    q = (query or "").lower()
    for occasion, words in OCCASIONS.items():
        if any(w in q for w in words):
            return occasion
    return None


def extract_budget(query: str) -> Optional[int]:
    """Extract an upper budget in BDT: 'under 50000', '50k budget', '50000 er moddhe'."""
    q = (query or "").lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*k\b", q)
    if m and ("budget" in q or "under" in q or "moddhe" in q or "er mod" in q or "tk" in q or "taka" in q):
        return int(float(m.group(1)) * 1000)
    m = re.search(r"(?:under|below|budget|moddhe|modhe|vitore|max)\D{0,12}(\d{4,7})", q)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{4,7})\s*(?:taka|tk|bdt|er moddhe|er modhe|er vitore)", q)
    if m:
        return int(m.group(1))
    return None


class RobustProductRAG:
    """
    RAG engine using the pluggable database repository abstraction.
    Features:
    - Safe matrix index mapping (no sequential ID assumptions)
    - Thread-safe, bounded multi-turn session storage
    - Strict greeting/chit-chat isolation (does not trigger RAG without product intent)
    - Multi-turn comparison memory across turns
    - Bengali brand transliterations
    - Priority-based query routing (specific search before brand summaries)
    """

    def __init__(self, repo: Optional[BaseProductRepository] = None):
        self.repo: BaseProductRepository = repo or get_product_repository()
        self.products: List[Dict] = []
        self.category_to_products: Dict[str, List[Dict]] = {}
        self.category_to_brands: Dict[str, Set[str]] = {}
        self.brand_to_categories: Dict[str, Set[str]] = {}
        self.all_brands: Set[str] = set()

        # Safe index mapping: product_id -> tfidf_matrix row index
        self.id_to_matrix_idx: Dict[int, int] = {}

        # TF-IDF Index
        self.corpus: List[str] = []
        self.vectorizer: Optional[TfidfVectorizer] = None
        self.tfidf_matrix = None

        # Thread-safe user session cache with TTL
        self._sessions: OrderedDict[str, Tuple[ConversationSession, float]] = OrderedDict()
        self._session_lock = threading.RLock()
        self._index_lock = threading.RLock()

        # Cross-container freshness: another process (e.g. the Telegram
        # importer on serverless) may replace the catalog without touching
        # this container's in-memory index.
        self._catalog_fingerprint = None
        self._fingerprint_checked_at = 0.0
        self._fingerprint_ttl = 30.0
        self._session_ttl = 1800  # 30 min idle: context expires, nothing kept forever
        self._max_sessions = 500

        self.load_and_index()

    def get_session(self, user_id: str) -> ConversationSession:
        with self._session_lock:
            now = time.time()
            if user_id in self._sessions:
                session, ts = self._sessions[user_id]
                if now - ts < self._session_ttl:
                    self._sessions.move_to_end(user_id)
                    self._sessions[user_id] = (session, now)
                    return session
                else:
                    del self._sessions[user_id]

            new_session = ConversationSession()
            if len(self._sessions) >= self._max_sessions:
                self._sessions.popitem(last=False)
            self._sessions[user_id] = (new_session, now)
            return new_session

    def _refresh_if_stale(self) -> None:
        """Reload the index when another process replaced the catalog.

        Compares a cheap (count, max-updated) fingerprint from the repository
        and re-indexes on change. Never raises; repos without a fingerprint
        simply skip the check.
        """
        try:
            get_fp = getattr(self.repo, "get_catalog_fingerprint", None)
            if not callable(get_fp):
                return
            now = time.time()
            if self._fingerprint_checked_at and (now - self._fingerprint_checked_at) < self._fingerprint_ttl:
                return
            self._fingerprint_checked_at = now
            fp = get_fp()
            if fp is None:
                return
            if self._catalog_fingerprint is None:
                self._catalog_fingerprint = fp
                return
            if fp != self._catalog_fingerprint:
                inv = getattr(self.repo, "invalidate_cache", None)
                if callable(inv):
                    try:
                        inv()
                    except Exception:
                        pass
                self.load_and_index()
                self._catalog_fingerprint = fp
        except Exception:
            logger.debug("RAG freshness check failed", exc_info=True)

    def load_and_index(self):
        """Loads products from repository and builds taxonomy and TF-IDF search index atomically."""
        prods = self.repo.get_all_products()
        # Visibility (spec 11): hidden, draft, and archived items are never shown.
        prods = [p for p in prods
                 if str(p.get("status", "ACTIVE")).upper() not in ("HIDDEN", "DRAFT", "ARCHIVED")
                 and str(p.get("visibility", "visible")).lower() not in ("hidden", "draft", "archived")]
        cat_to_prods = {}
        cat_to_brands = {}
        brand_to_cats = {}
        all_brands = set()
        corpus = []
        id_to_matrix_idx = {}

        for idx, p in enumerate(prods):
            p_id = p.get("id")
            if p_id is not None:
                id_to_matrix_idx[p_id] = idx

            cat = p.get("category", "General")
            brand = p.get("brand", "Unknown")

            cat_to_prods.setdefault(cat, []).append(p)
            cat_to_brands.setdefault(cat, set()).add(brand)
            brand_to_cats.setdefault(brand, set()).add(cat)
            all_brands.add(brand)

            # Build rich document for lexical search
            doc = (
                f"{p.get('name', '')} {brand} {cat} "
                f"{p.get('price_formatted', '')} {p.get('specifications', '')}"
            ).lower()
            corpus.append(doc)

        vectorizer = None
        tfidf_matrix = None
        if corpus and TfidfVectorizer is not None:
            try:
                vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english", max_features=5000)
                tfidf_matrix = vectorizer.fit_transform(corpus)
            except Exception as e:
                logger.warning(f"Error computing TF-IDF matrix: {e}")

        with self._index_lock:
            self.products = prods
            self.category_to_products = cat_to_prods
            self.category_to_brands = cat_to_brands
            self.brand_to_categories = brand_to_cats
            self.all_brands = all_brands
            self.corpus = corpus
            self.id_to_matrix_idx = id_to_matrix_idx
            self.vectorizer = vectorizer
            self.tfidf_matrix = tfidf_matrix

        logger.info(f"RAG loaded {len(self.products)} products across {len(self.category_to_products)} categories.")

        # Remember what we just indexed so _refresh_if_stale() can spot a
        # catalog replaced by another process afterwards.
        try:
            get_fp = getattr(self.repo, "get_catalog_fingerprint", None)
            if callable(get_fp):
                fp = get_fp()
                if fp is not None:
                    self._catalog_fingerprint = fp
        except Exception:
            pass

    def has_product_intent(self, query: str, session: Optional[ConversationSession] = None) -> bool:
        """
        Determines whether the customer is actually inquiring about products/specs/pricing.
        Returns False for greetings, small talk, acknowledgements, or general chat.
        """
        if query is None or not isinstance(query, str):
            return False
        query = query[:1000]
        q_lower = query.lower().strip()
        if not q_lower:
            return False

        # 1. Filter out customer contact/order info (phone number or name/address patterns)
        if re.search(r"(?:\+?880?|880?)?01[3-9]\d{8}", query) or re.search(r"\b(name|address|thikana|phone)\s*:", q_lower):
            return False

        # 2. Positive intent priority: Check if direct brand, category, spec, comparison, or inquiry keywords exist
        has_cat = bool(self.detect_category(query))
        has_brand = bool(self.detect_brand(query))
        if has_cat or has_brand:
            return True

        if self.is_comparison_query(query):
            return True

        if any(token in q_lower for token in SPEC_TOKENS):
            return True

        words = set(re.findall(r"\w+", q_lower))

        # 3. Active session follow-ups
        if session and session.last_products:
            followup_tokens = {
                "eyta", "eita", "eta", "oyta", "eitar", "etar", "oytar", "konta",
                "tk", "taka", "dam", "daam", "price", "cost", "koto", "asche", "asbe",
                "stock", "available", "specs", "display", "screen", "camera", "battery",
                "ram", "rom", "storage", "color", "colour", "rong", "valo", "bhalo",
                "difference", "better", "compare", "nibo", "kinbo", "lagbe"
            }
            if len(words) <= 6 or bool(words.intersection(followup_tokens)):
                return True

        # 4. Product inquiry keywords
        for k in INQUIRY_KEYWORDS:
            if " " in k:
                if k in q_lower:
                    return True
            else:
                if k in words:
                    return True

        # 5. Pure small talk / greetings / acknowledgements check
        for pat in GREETING_OR_CHITCHAT_PATTERNS:
            if re.match(pat, q_lower):
                return False

        return False

    def detect_category(self, query: str) -> Optional[str]:
        """Detects if query mentions any known category or synonym."""
        q_lower = query.lower().strip()
        words = [w.strip("?,.!;:\'\"()[]{}") for w in q_lower.split() if w.strip("?,.!;:\'\"()[]{}")]
        word_set = set(words)
        word_set.update(re.findall(r"\w+", q_lower))

        # 1. Direct token match
        for cat, synonyms in CATEGORY_SYNONYMS.items():
            for syn in synonyms:
                if syn in word_set:
                    return cat

        # Multi-word category synonyms (e.g. 'graphics card', 'smart watch', 'air purifier')
        for cat, synonyms in CATEGORY_SYNONYMS.items():
            for syn in synonyms:
                if " " in syn and syn in q_lower:
                    return cat

        # 2. Fuzzy match only when query has at least 4 characters
        if len(q_lower) >= 4:
            for cat, synonyms in CATEGORY_SYNONYMS.items():
                for syn in synonyms:
                    if len(syn) >= 4 and fuzz.ratio(syn, q_lower) >= 82:
                        return cat
                    for w in words:
                        if len(w) >= 4 and fuzz.ratio(syn, w) >= 85:
                            return cat

        return None

    def detect_brand(self, query: str) -> Optional[str]:
        """Fuzzy detects brand mentions in query, handling Bengali script and typos."""
        q_lower = query.lower().strip()
        words = [w.strip("?,.!;:\'\"()[]{}") for w in q_lower.split() if w.strip("?,.!;:\'\"()[]{}")]
        word_set = set(words)
        word_set.update(re.findall(r"\w+", q_lower))

        # 1. Direct check against root map (covers English + Bengali)
        for key, root_brand in BRAND_ROOT_MAP.items():
            if key in word_set:
                return root_brand

        # Multi-word brands (e.g. 'google pixel', 'asus rog', 'acer swift')
        for key, root_brand in BRAND_ROOT_MAP.items():
            if " " in key and key in q_lower:
                return root_brand

        # 2. Fuzzy token match for words with length >= 4
        for w in words:
            if len(w) >= 4:
                best_match = process.extractOne(w, list(BRAND_ROOT_MAP.keys()), scorer=fuzz.ratio)
                if best_match and best_match[1] >= 82:
                    return BRAND_ROOT_MAP[best_match[0]]

                best_brand = process.extractOne(w, list(self.all_brands), scorer=fuzz.ratio)
                if best_brand and best_brand[1] >= 85:
                    return best_brand[0]

        return None

    def is_comparison_query(self, query: str) -> bool:
        """Checks if customer wants to compare previously discussed items."""
        q_lower = query.lower()
        return any(k in q_lower for k in COMPARISON_KEYWORDS)

    def is_broad_category_inquiry(self, query: str, detected_category: Optional[str]) -> bool:
        """
        Determines if the customer is asking an exploratory question about what models/items
        exist in a category (e.g. 'Ki ki model laptop ache', 'what phones do you have?').
        """
        if not detected_category:
            return False

        q_lower = query.lower()
        broad_triggers = [
            "ki ki", "kik i", "kon kon", "konkon", "konta", "what model", "which model", 
            "what laptops", "what phones", "what monitors", "available", "list", "show me", 
            "catalogue", "catalog", "dekhan", "ache", "have", "কোন কোন", "কী কী", "আছে"
        ]
        return any(t in q_lower for t in broad_triggers)

    def get_category_root_brands(self, category: str) -> List[str]:
        """Returns clean, simplified brand names for a category."""
        raw_brands = self.category_to_brands.get(category, set())
        root_brands = []
        clean_map = {
            "asus rog": "Asus", "asus zenbook": "Asus", "asus": "Asus",
            "hp envy": "HP", "hp spectre": "HP", "hp": "HP",
            "lenovo legion": "Lenovo", "lenovo thinkpad": "Lenovo", "lenovo": "Lenovo",
            "dell xps": "Dell", "dell": "Dell",
            "apple macbook": "Apple", "apple": "Apple",
            "acer swift": "Acer", "acer": "Acer",
            "samsung galaxy": "Samsung", "samsung": "Samsung",
            "xiaomi": "Xiaomi", "sony": "Sony", "google pixel": "Google Pixel"
        }
        for b in raw_brands:
            clean_name = clean_map.get(b.lower()) or b.split()[0]
            if clean_name not in root_brands:
                root_brands.append(clean_name)

        priority = ["HP", "Lenovo", "Dell", "Asus", "Apple", "Samsung", "Xiaomi", "Sony", "Acer"]
        ordered = [p for p in priority if p in root_brands]
        for r in root_brands:
            if r not in ordered:
                ordered.append(r)
        return ordered

    def get_category_overview(self, category: str) -> str:
        """Generates clean inventory overview of brands for a broad category question."""
        root_brands = self.get_category_root_brands(category)
        top_brands = root_brands[:4]

        lines = [
            f"=== OFFICIAL INVENTORY SUMMARY: {category.upper()} ===",
            f"Top available brands: {', '.join(top_brands)}.",
            "=== END INVENTORY SUMMARY ===\n"
            f"GUIDANCE: When customer asks if we have {category}: "
            f"Respond naturally mentioning only top brands in 1 short conversational sentence without rigid formulaic openings. "
            "Do NOT list series or model names. Do NOT use bullet points or dashes. "
            "NEVER say 'stock e ache' or 'in stock' for in-stock items!"
        ]
        return "\n".join(lines)

    def get_brand_category_overview(self, brand: str, category: str) -> Optional[str]:
        """Retrieves top models for a specific brand inside a category."""
        matching_products = []
        b_clean = brand.lower()

        for p in self.products:
            if p.get("category") == category:
                p_brand = p.get("brand", "").lower()
                p_name = p.get("name", "").lower()
                if b_clean in p_brand or b_clean in p_name:
                    matching_products.append(p)

        if not matching_products:
            return None

        lines = [
            f"=== OFFICIAL INVENTORY: {brand.upper()} {category.upper()} ===",
            "Top models available in our store:"
        ]
        for p in matching_products[:3]:
            stock_str = "In Stock" if p.get("in_stock", True) else "Currently Out of Stock"
            lines.append(
                f"- Product: {p.get('name')}\n"
                f"  Price: {p.get('price_formatted')}\n"
                f"  Status: {stock_str}"
            )
        lines.append(
            "=== END INVENTORY DATA ===\n"
            "GUIDANCE: When customer asks if we have this brand or what models we have: "
            "Mention 2-3 popular models with prices naturally in 1 conversational sentence without dashes or bullet points. "
            "NEVER say 'stock e ache' or 'in stock' for in-stock items! Only mention stock if Out of Stock. "
            "Do NOT add repetitive closing questions."
        )
        return "\n".join(lines)

    def find_family_siblings(self, product: Dict, max_siblings: int = 3) -> List[Dict]:
        """
        Finds sibling models in the same product line/series to provide rich context
        for colors, editions, and variants across any dataset structure.
        """
        name = product.get("name", "")
        cat = product.get("category", "")
        p_id = product.get("id")

        words = name.split()
        if len(words) >= 3 and any(k in name.lower() for k in ("tuf gaming", "rog strix", "pro max", "ideapad gaming")):
            stem = " ".join(words[:3])
        elif len(words) >= 2:
            stem = " ".join(words[:2])
        else:
            stem = name

        siblings = []
        for p in self.products:
            if p.get("id") == p_id:
                continue
            if stem.lower() in p.get("name", "").lower() and p.get("category") == cat:
                siblings.append(p)
                if len(siblings) >= max_siblings:
                    break
        return siblings

    def get_feature_overview(self, query: str) -> Optional[str]:
        """
        Answers broad exploratory questions about features or variants across the store
        (e.g., 'kon model er color option ache?', 'kono color option ache?').
        """
        q_lower = query.lower()
        if any(w in q_lower for w in ("color", "colour", "rong", "shades")):
            color_keywords = ["carbon", "white", "gray", "grey", "midnight", "black", "silver", "gold"]
            matched_prods = [p for p in self.products if any(c in p.get("name", "").lower() for c in color_keywords)]
            if matched_prods:
                lines = [
                    "=== STORE INVENTORY - MODELS WITH DISTINCT COLOR EDITIONS ===",
                    "The customer asked which models or products have color choices:"
                ]
                for p in matched_prods[:6]:
                    lines.append(f"- {p.get('name')}: Price {p.get('price_formatted')} ({p.get('category')})")
                lines.append(
                    "=== END CATALOG DATA ===\n"
                    "GUIDANCE: Answer directly in 1 short sentence stating models that have color options, without rigid opening phrases."
                )
                return "\n".join(lines)
        return None

    def search_specific_products(
        self,
        query: str,
        category_filter: Optional[str] = None,
        top_k: int = 4
    ) -> List[Tuple[Dict, float]]:
        """
        Robust search with candidate re-ranking and safe matrix index lookup.
        """
        self._refresh_if_stale()
        if not query or not isinstance(query, str) or not query.strip():
            return []
        top_k = max(1, min(10, int(top_k or 4)))
        cleaned_query = normalize_query(query)
        budget = extract_budget(query)
        candidates = self.category_to_products.get(category_filter, []) if category_filter else self.products
        if budget:
            candidates = [p for p in candidates if 0 < (p.get("price_bdt") or 0) <= budget]

        # TF-IDF vector similarity
        tfidf_scores = None
        if self.vectorizer and self.tfidf_matrix is not None and cosine_similarity is not None:
            query_vec = self.vectorizer.transform([cleaned_query])
            tfidf_scores = cosine_similarity(query_vec, self.tfidf_matrix).flatten()

        results = []
        for p in candidates:
            p_id = p.get("id")
            matrix_idx = self.id_to_matrix_idx.get(p_id)
            name = p.get("name", "").lower()
            brand = p.get("brand", "").lower()

            # Fuzzy scores
            fuzzy_name = fuzz.token_set_ratio(cleaned_query, name) / 100.0
            fuzzy_partial = fuzz.partial_ratio(cleaned_query, name) / 100.0
            fuzzy_brand = fuzz.partial_ratio(cleaned_query, brand) / 100.0 if brand else 0
            best_fuzzy = max(fuzzy_name, fuzzy_partial * 0.9, fuzzy_brand * 0.75)

            tf_score = 0.0
            if tfidf_scores is not None and matrix_idx is not None and matrix_idx < len(tfidf_scores):
                tf_score = float(tfidf_scores[matrix_idx])

            # Dynamic combined score (in-stock items rank above out-of-stock)
            score = (0.60 * best_fuzzy) + (0.40 * tf_score)
            if p.get("in_stock", True) and (p.get("stock", 1) or 0) > 0:
                score += 0.03

            # Bonus for exact model tokens
            words = [w for w in cleaned_query.split() if len(w) > 2]
            name_words = name.split()
            matched_words = sum(1 for w in words if any(w in nw for nw in name_words))
            if matched_words > 0:
                score += (matched_words / max(len(words), 1)) * 0.25

            if score >= 0.25:
                results.append((p, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def format_context_for_llm(self, query: str, user_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Main RAG entrypoint. Resolves multi-turn queries, broad category requests,
        brand-specific catalog lookups, and clever fallbacks.

        CRITICAL: If user message has no product intent (e.g. pure greeting or casual chat),
        returns (None, "no_product_intent") so the LLM replies naturally without inventory dumping.
        """
        self._refresh_if_stale()
        if query is None or not isinstance(query, str) or not query.strip():
            return None, "empty_query"
        query = query.strip()[:1000]
        if not self.products:
            return None, "empty_catalog"
        session = self.get_session(user_id) if user_id else ConversationSession()
        q_lower = query.lower().strip()

        # Ordinal reference: "second one", "dwitiyo ta" means a shown product.
        ordinal = extract_ordinal(query)
        if ordinal is not None and session.last_products:
            try:
                chosen = session.last_products[ordinal]
            except IndexError:
                chosen = None
            if chosen is not None:
                fresh = None
                looked_up = False
                try:
                    if chosen.get("id") is not None and hasattr(self.repo, "get_by_id"):
                        fresh = self.repo.get_by_id(chosen["id"])
                        looked_up = True
                except Exception:
                    fresh = None
                if fresh is not None:
                    _st = str(fresh.get("status", "ACTIVE")).upper()
                    _vis = str(fresh.get("visibility", "visible")).lower()
                    if _st in ("HIDDEN", "DRAFT", "ARCHIVED") or _vis in ("hidden", "draft", "archived"):
                        fresh = None
                if fresh is None and looked_up:
                    return ("The product you mentioned is no longer available. "
                            "Please tell me what else you are looking for."), "ordinal_gone"
                if fresh is not None:
                    chosen = fresh
                session.update(products=[chosen], query=query)
                stock = "In Stock" if chosen.get("in_stock", True) else "Currently Out of Stock"
                return (f"=== REFERENCED PRODUCT ===\n- Product: {chosen.get('name')}\n"
                        f"  Price: {chosen.get('price_formatted')}\n"
                        f"  Availability: {stock}\n  Specifications: {_specs_for_llm(chosen.get('specifications'))}\n"
                        "GUIDANCE: The customer means this exact product. Answer about it directly."), "ordinal_product"

        # Step 0: Filter out pure greetings / chit-chat with NO product intent
        if not self.has_product_intent(query, session=session):
            return None, "no_product_intent"

        # Step 1: Detect Category & Brand in current query
        detected_category = self.detect_category(query)
        detected_brand = self.detect_brand(query)

        attr_keywords = [
            "price", "koto", "specs", "details", "stock", "er", "dam", "color", "colour", "rong",
            "warranty", "size", "display", "screen", "feature", "ram", "storage", "camera", "battery",
            "refresh", "hz", "asche", "asbe", "eyta", "eita", "eta", "oyta", "tk", "taka", "bdt"
        ]

        # Context Guard: If customer is discussing an existing product, spec/attribute queries must NOT hijack category
        has_active_context = bool(session.last_products or session.last_category or session.last_brand)
        is_attribute_query = any(k in q_lower for k in attr_keywords) or len(q_lower.split()) <= 3

        if has_active_context and is_attribute_query and not (detected_brand and detected_brand != session.last_brand):
            active_category = session.last_category or detected_category
            active_brand = session.last_brand or detected_brand
        else:
            active_category = detected_category or session.last_category
            active_brand = detected_brand or (session.last_brand if not detected_category else None)

        # Step 2: Handle Comparison intent ("konta better?", "which is best?", "konta valo?")
        if self.is_comparison_query(query):
            comp_candidates = session.last_products if len(session.last_products) >= 2 else (session.recent_products or session.last_products)
            # Filter strictly by active category or brand to prevent cross-category pollution
            if active_category:
                comp_products = [p for p in comp_candidates if p.get("category") == active_category]
            elif active_brand:
                comp_products = [p for p in comp_candidates if active_brand.lower() in p.get("brand", "").lower()]
            else:
                comp_products = comp_candidates

            # Fallback to category/brand top products if no recent items match
            if not comp_products and active_brand and active_category:
                comp_products = [
                    p for p in self.products
                    if p.get("category") == active_category and active_brand.lower() in p.get("brand", "").lower()
                ][:3]

            if comp_products:
                lines = [
                    "=== ACTIVE PRODUCT COMPARISON CONTEXT ===",
                    "The customer wants to compare the products previously discussed:"
                ]
                for p in comp_products:
                    is_in_stock = bool(p.get("in_stock", True) and p.get("stock", 1) > 0)
                    stock_str = "In Stock" if is_in_stock else "Out of Stock"
                    lines.append(
                        f"- {p.get('name')}: Price {p.get('price_formatted')}, Status: {stock_str}\n"
                        f"  Specifications: {_specs_for_llm(p.get('specifications'))}"
                    )
                lines.append(
                    "=== END COMPARISON CONTEXT ===\n"
                    "GUIDANCE: Act like an experienced sales consultant. Objectively compare these models highlighting "
                    "which one offers better value or performance. Conclude directly without repetitive closing questions."
                )
                return "\n".join(lines), "comparison"
            else:
                return None, "comparison_no_products"

        # Step 2.5: Broad Feature / Option Inquiry (e.g. "kon model er color option ache?")
        if any(phrase in q_lower for phrase in ("kon model", "kono model", "kon kon model", "option ache")) and any(w in q_lower for w in ("color", "colour", "rong")):
            feature_ctx = self.get_feature_overview(query)
            if feature_ctx:
                return feature_ctx, "feature_overview"
        elif not detected_brand and not detected_category and not (session.last_products and any(k in q_lower for k in attr_keywords)):
            feature_ctx = self.get_feature_overview(query)
            if feature_ctx:
                return feature_ctx, "feature_overview"

        # Step 3: Priority Specific Product Search
        search_target = query
        has_pronoun = any(pr in q_lower.split() for pr in ("eyta", "eita", "eta", "oyta", "eitar", "etar", "oytar", "it", "this", "konta"))
        is_attr_or_followup = any(k in q_lower for k in attr_keywords) or has_pronoun or len(q_lower.split()) <= 3

        if not detected_brand and session.last_products and is_attr_or_followup:
            search_target = f"{session.last_products[0].get('name')} {query}"

        matches = self.search_specific_products(search_target, category_filter=active_category, top_k=3)
        if not matches and active_category:
            matches = self.search_specific_products(search_target, category_filter=None, top_k=3)

        # If an active brand was established in session, prioritize matches matching that brand
        if active_brand and matches:
            matches.sort(key=lambda x: (1 if active_brand.lower() in x[0].get('brand', '').lower() else 0, x[1]), reverse=True)

        # If a high-confidence specific product was matched (score >= 0.40), prioritize it!
        if matches and matches[0][1] >= 0.40:
            top_match = matches[0]
            filtered_matches = [top_match]
            for p, sc in matches[1:]:
                if p.get("brand", "").lower() == top_match[0].get("brand", "").lower() and sc >= 0.45:
                    filtered_matches.append((p, sc))
            matches = filtered_matches

            top_products = [p for p, _ in matches]
            session.update(
                category=top_products[0].get("category"),
                brand=top_products[0].get("brand"),
                products=top_products,
                query=query
            )

            lines = ["=== OFFICIAL STORE CATALOG DATA (VERIFIED MATCHES) ==="]
            for p, _ in matches:
                is_in_stock = bool(p.get('in_stock', True) and p.get('stock', 1) > 0)
                stock_status = "In Stock" if is_in_stock else "Currently Out of Stock"
                price_str = p.get('price_formatted') or f"{p.get('price_bdt', 0):,}৳"
                lines.append(
                    f"- Product: {p.get('name')}\n"
                    f"  Category: {p.get('category')}\n"
                    f"  Price: {price_str}\n"
                    f"  Availability: {stock_status}\n"
                    f"  Specifications: {_specs_for_llm(p.get('specifications'))}"
                )
                if not is_in_stock:
                    lines.append(f"  [IMPORTANT: This model is Currently OUT OF STOCK. State its exact catalog price ({price_str}) and inform customer politely that it is currently out of stock.]")

            # Sibling editions in the same product line for series context
            siblings = self.find_family_siblings(matches[0][0])
            if siblings:
                lines.append("- Related Models & Editions in this Series:")
                for s in siblings[:2]:
                    lines.append(f"  Edition: {s.get('name')} (Price: {s.get('price_formatted')})")

            lines.append(
                "=== END OF CATALOG DATA ===\n"
                "GUIDANCE: Answer concisely (1 sentence). State the exact requested product's price, color, or specs directly. "
                "CRITICAL: Always state the exact Price listed above. NEVER confuse camera megapixels, display refresh rate, or other spec numbers with the price! "
                "If an item is marked Out of Stock, mention clearly that it is currently out of stock while stating its actual price. "
                "Do NOT volunteer other products or alternative brands unless requested or out of stock. "
                "Do NOT use asterisks (*), dashes (–, -), or hyphens in Banglish words. Speak like a natural human on Messenger."
            )
            return "\n".join(lines), "specific_products"

        # Step 4: Handle Broad Category Inquiry ("Ki ki model laptop ache?", "laptop ache?")
        if detected_category and self.is_broad_category_inquiry(query, detected_category) and not detected_brand:
            session.update(category=detected_category, query=query)
            return self.get_category_overview(detected_category), "category_overview"

        # Step 5: Handle Brand + Category Inquiry ("Asus laptop ache?", "Dell er laptop ache?")
        if active_brand and active_category:
            brand_cat_context = self.get_brand_category_overview(active_brand, active_category)
            if brand_cat_context:
                matched_items = [
                    p for p in self.products
                    if p.get("category") == active_category and active_brand.lower() in p.get("brand", "").lower()
                ]
                session.update(category=active_category, brand=active_brand, products=matched_items[:3], query=query)
                return brand_cat_context, "brand_category"
            else:
                other_categories = self.brand_to_categories.get(active_brand, set())
                available_in_category = list(self.category_to_brands.get(active_category, set()))

                clever_lines = [
                    f"=== INVENTORY NOTICE: {active_brand.upper()} NOT IN {active_category.upper()} ===",
                    f"- The customer asked for '{active_brand}' in the category '{active_category}'.",
                    f"- We do NOT have {active_brand} {active_category} in our inventory."
                ]
                if other_categories:
                    clever_lines.append(
                        f"- NOTE: We DO have '{active_brand}' in other categories: {', '.join(other_categories)}."
                    )
                if available_in_category:
                    clever_lines.append(
                        f"- Available top brands in {active_category}: {', '.join(available_in_category[:6])}."
                    )
                clever_lines.append(
                    "=== END NOTICE ===\n"
                    "GUIDANCE: Politely state that we do not have that brand in that category, and mention available alternatives."
                )
                return "\n".join(clever_lines), "clever_fallback_brand"

        # Step 6: Moderate specific match fallback
        if matches and matches[0][1] >= 0.30:
            top_products = [p for p, _ in matches]
            session.update(
                category=top_products[0].get("category"),
                brand=top_products[0].get("brand"),
                products=top_products,
                query=query
            )
            lines = ["=== OFFICIAL STORE CATALOG DATA (VERIFIED MATCHES) ==="]
            for p, _ in matches:
                stock_status = "In Stock" if p.get("in_stock", True) else "Currently Out of Stock"
                lines.append(
                    f"- Product: {p.get('name')}\n"
                    f"  Category: {p.get('category')}\n"
                    f"  Price: {p.get('price_formatted')}\n"
                    f"  Availability: {stock_status}\n"
                    f"  Specifications: {_specs_for_llm(p.get('specifications'))}"
                )

            # Sibling editions in the same product line
            siblings = self.find_family_siblings(matches[0][0])
            if siblings:
                lines.append("- Related Models & Editions in this Series:")
                for s in siblings[:2]:
                    lines.append(f"  * {s.get('name')} (Price: {s.get('price_formatted')})")

            lines.append("=== END OF CATALOG DATA ===")
            return "\n".join(lines), "specific_products"

        # Step 7: Category Fallback
        if active_category:
            avail_brands = list(self.category_to_brands.get(active_category, set()))
            clever_lines = [
                f"=== INVENTORY GUIDANCE: ITEM NOT FOUND IN {active_category.upper()} ===",
                f"- The requested item was not found in our catalog.",
                f"- Available brands in {active_category}: {', '.join(avail_brands[:6])}.",
                "GUIDANCE: Politely state we do not have that specific item, and mention our available brands."
            ]
            return "\n".join(clever_lines), "fallback_category"

        # Nothing matched: remember the unmet demand for the owner digest.
        try:
            from analytics import get_analytics
            get_analytics().record_demand(query)
        except Exception:
            pass
        return None, "unknown"


    def search(self, query: str, top_k: int = 4) -> List[Dict]:
        """Convenience: product dicts only, best first. Never raises."""
        try:
            return [p for p, _ in self.search_specific_products(query, top_k=top_k)]
        except Exception:
            logger.exception("RAG search failed")
            return []


# Singleton instance
_robust_rag_instance: Optional[RobustProductRAG] = None
_rag_lock = threading.Lock()


def get_rag() -> RobustProductRAG:
    """Returns singleton instance of RobustProductRAG."""
    global _robust_rag_instance
    if _robust_rag_instance is None:
        with _rag_lock:
            if _robust_rag_instance is None:
                _robust_rag_instance = RobustProductRAG()
    return _robust_rag_instance


if __name__ == "__main__":
    import sys
    if sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    rag = get_rag()
    test_queries = [
        ("Pure Greeting", "হাই"),
        ("English Greeting", "hello"),
        ("Casual Chit-Chat", "kemon achen?"),
        ("Broad laptop inquiry", "laptop ache?"),
        ("Specific laptop inquiry", "Dell XPS 13 laptop er price koto?"),
        ("Bengali brand inquiry", "স্যামসাং ফোন এর দাম কত?"),
        ("Comparison", "konta better?")
    ]

    print("=" * 60)
    print("TESTING REFINED RAG ROUTING")
    print("=" * 60)

    for label, q in test_queries:
        ctx, action = rag.format_context_for_llm(q, user_id="test_sim_user")
        print(f"\n[{label}] Query: '{q}' -> Action: {action}")
        if ctx:
            print("Context Preview: " + ctx.split("\n")[0])
        else:
            print("Context: None (Correct! Natural AI response, no RAG pollution)")
