"""Sentiment, sales stage, lead scoring, escalation (spec 31-37, 82).

Deterministic keyword scoring in English, Bangla, and Banglish. The LLM
is never required for safety decisions: frustration, urgency, fraud, and
human requests escalate from rules first. Prompt-injection attempts are
detected here and must never reach tool execution.
"""
import re
import threading
import time

SIGNALS = {
    "angry": ["angry", "furious", "worst", "scam", "fraud", "cheat", "thief", "fake product",
              "rag", "khub kharap", "baje", "প্রতার", "রাগ", "খুব খারাপ", "জালিয়াত"],
    "frustrated": ["not working", "still waiting", "again and again", "no reply", "very slow",
                   "hoyna", "hocche na", "abar", "হচ্ছে না", "আবার", "দেরি"],
    "urgent": ["urgent", "urgently", "asap", "today", "tomorrow", "before friday", "emergency",
               "joldi", "tarahura", "আজই", "জরুরি", "তাড়াতাড়ি"],
    "confused": ["confused", "don't understand", "do not understand", "bujhlam na", "bujhi na",
                 "mane ki", "বুঝলাম না", "মানে কি"],
    "excited": ["great", "awesome", "love it", "perfect", "wow", "darun", "দারুণ", "অসাধারণ"],
    "hesitant": ["maybe", "thinking", "not sure", "vabchi", "bhabchi", "pore janabo", "ভাবছি", "পরে জানাব"],
    "positive": ["thanks", "thank you", "good", "nice", "dhonnobad", "ধন্যবাদ", "ভালো"],
    "human": ["human", "real person", "agent", "call me", "phone koren", "manusher sathe",
              "মানুষের সাথে", "এজেন্ট", "কল করুন"],
}
INJECTION_PATTERNS = [
    r"ignore (your|all|previous) (instructions|rules|prompt)",
    r"reveal (your|the) (system )?prompt",
    r"show (me )?(your|the) (system )?prompt",
    r"delete all (products|orders|data)",
    r"(give|show) me (the )?database",
    r"execute (sql|query|code)",
    r"you are now (a|an|in)",
    r"system prompt (ta|ti)? (dao|den|bolo|show)",
]
ORDER_WORDS = ["order", "kinbo", "nibo", "buy", "purchase", "confirm", "অর্ডার", "কিনব", "নিব"]
PRICE_WORDS = ["price", "dam", "daam", "koto", "how much", "দাম", "কত"]
BUDGET_WORDS = ["budget", "taka", "tk", "bdt", "er moddhe", "টাকা"]


def _signal_hit(t: str, signal: str) -> bool:
    # Short signals need word boundaries ("rag" must not fire on "storage");
    # multi-word/Bangla phrases keep substring matching.
    if len(signal) <= 4 and signal.isascii() and " " not in signal:
        return re.search(r"(?<![a-z])" + re.escape(signal) + r"(?![a-z])", t) is not None
    return signal in t


def detect_sentiment(text) -> str:
    t = f" {(text or '').lower()} "
    for label in ("angry", "human", "frustrated", "urgent", "confused", "excited", "hesitant", "positive"):
        if any(_signal_hit(t, s) for s in SIGNALS[label]):
            return label
    return "neutral"


def is_injection_attempt(text) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in INJECTION_PATTERNS)


def sales_stage(text, has_product=False, in_checkout=False) -> str:
    t = (text or "").lower()
    if in_checkout:
        return "READY_TO_BUY"
    if any(w in t for w in ("return", "refund", "problem", "complaint", "tracking", "order status",
                            "order kothay", "amar order", "where is my order", "অর্ডার কোথায়")):
        return "SUPPORT"
    if any(w in t for w in ORDER_WORDS):
        return "READY_TO_BUY"
    if has_product and any(w in t for w in PRICE_WORDS + ["available", "ache", "color", "size"]):
        return "HIGH_INTENT"
    if has_product:
        return "INTERESTED"
    return "BROWSING"


def lead_score(text, has_product=False, has_budget=False, in_checkout=False) -> int:
    score = 10
    t = (text or "").lower()
    stage = sales_stage(text, has_product, in_checkout)
    score += {"BROWSING": 0, "INTERESTED": 20, "HIGH_INTENT": 40, "READY_TO_BUY": 65, "SUPPORT": 5}[stage]
    if has_budget or any(w in t for w in BUDGET_WORDS):
        score += 10
    if detect_sentiment(text) == "urgent":
        score += 10
    return max(0, min(100, score))


def escalation(text, misunderstood_count=0):
    """Returns (needed, priority, reason). Priority: LOW/MEDIUM/HIGH/CRITICAL."""
    t = (text or "").lower()
    sentiment = detect_sentiment(text)
    if any(w in t for w in ("fraud", "scam", "police", "mamla", "প্রতার", "জালিয়াত")):
        return True, "CRITICAL", "fraud or scam accusation"
    if sentiment in ("angry", "human"):
        return True, "HIGH", "angry customer or human requested"
    if any(w in t for w in ("refund", "return", "missing", "not delivered", "harano", "ফেরত",
                            "chargeback", "legal", "mamla", "damaged", "broken", "wrong item")):
        return True, "HIGH", "return, refund, or missing delivery"
    if misunderstood_count >= 2:
        return True, "MEDIUM", "repeated misunderstanding"
    if sentiment == "frustrated":
        return True, "MEDIUM", "frustrated customer"
    return False, "LOW", ""


def build_summary(customer_id, text, product_name="", budget="", stage="", priority="LOW") -> str:
    return (
        "CUSTOMER SUMMARY\n"
        f"Customer: {customer_id}\nStage: {stage or sales_stage(text, bool(product_name))}\n"
        f"Product: {product_name or '-'}\nBudget: {budget or '-'}\n"
        f"Sentiment: {detect_sentiment(text)}\nPriority: {priority}\n"
        f"Latest message: {(text or '')[:200]}")


_tickets = []
_ticket_lock = threading.Lock()


_ticket_seq = [1000]


def create_ticket(user_id, summary, priority="MEDIUM"):
    with _ticket_lock:
        # One open ticket per customer: repeats update, never pile up.
        for t in _tickets:
            if t["user_id"] == user_id and t["status"] == "OPEN":
                t["summary"] = summary
                t["priority"] = priority
                return dict(t)
        _ticket_seq[0] += 1
        ticket = {"id": f"TKT-{_ticket_seq[0]:04d}", "user_id": user_id,
                  "summary": summary, "priority": priority,
                  "at": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "OPEN"}
        _tickets.append(ticket)
        del _tickets[:-200]  # hard cap: oldest tickets evicted
        return dict(ticket)


def open_tickets():
    with _ticket_lock:
        return [dict(t) for t in _tickets if t["status"] == "OPEN"]
