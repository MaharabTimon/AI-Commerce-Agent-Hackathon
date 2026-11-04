"""Business knowledge base with retrieval (spec 41, 42, 69, 70, 77).

Policies and FAQs live as small documents. Answers retrieve only the
relevant chunks instead of injecting everything into the prompt. Seller
corrections update documents at runtime, and repeated unanswered
questions surface as FAQ suggestions. Safe answers are cached briefly;
inventory is never cached here.
"""
import re
import threading
import time
from collections import Counter

CACHE_TTL = 300

def _default_docs():
    # Charges and windows come from BusinessRules so env changes stay truthful.
    try:
        from business_rules import get_rules
        r = get_rules()
        dhaka, outside, window = r.delivery_charge_dhaka, r.delivery_charge_outside, r.return_window_days
        cod = "Cash on delivery (COD) is available across Bangladesh." if r.cod_enabled else "Cash on delivery is currently disabled; prepaid payment only."
    except Exception:
        dhaka, outside, window, cod = 60, 120, 7, "Cash on delivery (COD) is available across Bangladesh."
    return [
    {"id": "delivery", "title": "Delivery policy",
     "text": f"Delivery inside Dhaka takes 1-2 working days, charge BDT {dhaka}. Outside Dhaka takes 2-4 working days, charge BDT {outside}. Orders placed before 3pm are dispatched the same day."},
    {"id": "payment", "title": "Payment and COD",
     "text": f"{cod} bKash and Nagad payments are also accepted. Payment is collected when the product is delivered for COD orders."},
    {"id": "returns", "title": "Return and exchange policy",
     "text": f"Products can be returned or exchanged within {window} days if unused, unwashed, and with tags intact. Defective products are replaced free of charge. Refunds are processed after the returned item is received and checked, and always need seller approval."},
    {"id": "warranty", "title": "Warranty",
     "text": "Electronics carry the official brand warranty stated on the product page. Please keep the invoice for warranty claims."},
    {"id": "hours", "title": "Store hours and contact",
     "text": "The shop is open Saturday to Thursday, 10am to 8pm. Friday 3pm to 8pm. Customers can message this Page anytime; the team replies during opening hours."},
    ]


DEFAULT_DOCS = _default_docs()

STOP = {"the", "a", "an", "is", "are", "ki", "ke", "er", "ta", "e", "o", "and", "or", "to", "of"}


def _tokens(text):
    return [w for w in re.findall(r"[a-z0-9\u0980-\u09FF]+", (text or "").lower()) if w not in STOP and len(w) > 1]


class KnowledgeBase:
    def __init__(self, docs=None):
        self._docs = {d["id"]: dict(d) for d in (docs or DEFAULT_DOCS)}
        self._cache = {}
        self._misses = Counter()
        self._lock = threading.RLock()

    def search(self, query, top_k=2):
        q = set(_tokens(query))
        if not q:
            return []
        key = " ".join(sorted(q))
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[1] < CACHE_TTL:
                return list(hit[0])
        scored = []
        for doc in self._docs.values():
            words = set(_tokens(doc["title"] + " " + doc["text"]))
            overlap = len(q & words)
            if overlap:
                scored.append((overlap, doc))
        scored.sort(key=lambda row: row[0], reverse=True)
        # A single shared word is never enough evidence for a policy answer.
        if scored and scored[0][0] < 2:
            scored = []
        results = [{"id": d["id"], "title": d["title"], "text": d["text"], "score": s}
                   for s, d in scored[:max(1, min(5, int(top_k)))]]
        with self._lock:
            if results:
                self._cache[key] = (results, now)
                if len(self._cache) > 256:  # bounded answer cache
                    self._cache.pop(next(iter(self._cache)))
            else:
                self._misses[" ".join(sorted(q))[:80]] += 1
                if len(self._misses) > 500:
                    self._misses = Counter(dict(self._misses.most_common(250)))
        return results

    def answer_context(self, query) -> str:
        rows = self.search(query)
        if not rows:
            return ""
        body = "\n".join(f"[{r['title']}] {r['text']}" for r in rows)
        return f"[Verified business knowledge - answer only from this, say you will check if missing]\n{body}"

    def upsert(self, doc_id, title, text):
        clean_id = re.sub(r"[^a-z0-9_-]", "", str(doc_id).lower())[:40] or "policy"
        with self._lock:
            self._docs[clean_id] = {"id": clean_id, "title": str(title)[:120], "text": str(text)[:2000]}
            self._cache.clear()
        return self._docs[clean_id]

    def faq_suggestions(self, n=5):
        with self._lock:
            return self._misses.most_common(max(1, min(20, int(n))))

    def count(self):
        return len(self._docs)


_kb = KnowledgeBase()


def get_knowledge_base() -> KnowledgeBase:
    return _kb
