"""Meta Llama chat service (Hackathon edition, Meta Model API only).

The only LLM service in this edition. No other provider is imported.
Transport is plain `requests` against an OpenAI-compatible endpoint so
there are no native/binary dependencies on serverless.
"""
import logging
import random
import re
import threading
import time
from collections import OrderedDict
from typing import List, Optional

import requests

import config
from order_state import detect_language, get_order_state_machine
from rag_service import get_rag

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10  # 5 full turns of careful context (not permanent)
MAX_PROMPT_CHARS = 12000
REQUEST_TIMEOUT = 20.0 if config.is_serverless() else 30.0

FALLBACK_REPLY = "Sorry, ami ektu technical issue face korchi. Ektu por abar bolben please."


class BoundedHistoryStore:
    """Thread-safe, memory-bounded, TTL-aware conversation cache."""

    def __init__(self, max_users: int = 500, ttl_seconds: int = 1800):
        # Smart memory management: keep only a few recent messages per user,
        # expire idle context after 30 minutes, hard-bound total users.
        self.max_users = max_users
        self.ttl_seconds = ttl_seconds
        self._history: "OrderedDict[str, tuple]" = OrderedDict()
        self._lock = threading.RLock()

    def get(self, user_id: str) -> List[dict]:
        if not user_id:
            return []
        with self._lock:
            now = time.time()
            row = self._history.get(user_id)
            if row is None:
                return []
            hist, ts = row
            if now - ts >= self.ttl_seconds:
                del self._history[user_id]
                return []
            self._history.move_to_end(user_id)
            self._history[user_id] = (hist, now)
            return list(hist)

    def _sweep_expired(self, now: float) -> None:
        """Drop idle-expired histories opportunistically (bounded size)."""
        if len(self._history) < 32:
            return
        stale = [uid for uid, (_, ts) in self._history.items()
                 if now - ts >= self.ttl_seconds]
        for uid in stale[:64]:
            self._history.pop(uid, None)

    def append_turn(self, user_id: str, user_text: str, bot_text: str,
                    max_len: int = MAX_HISTORY_MESSAGES):
        if not user_id or not user_text or not bot_text:
            return
        with self._lock:
            now = time.time()
            self._sweep_expired(now)
            hist = list(self._history.get(user_id, ([], 0))[0])
            # Dedupe an exact replay (webhook retry) so context never doubles.
            if hist and hist[-2:] == [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": bot_text},
            ]:
                self._history[user_id] = (hist, now)
                return
            hist.append({"role": "user", "content": user_text[:1200]})
            hist.append({"role": "assistant", "content": bot_text[:1200]})
            hist = hist[-max_len:]
            if user_id not in self._history and len(self._history) >= self.max_users:
                self._history.popitem(last=False)
            self._history[user_id] = (hist, now)

    def clear(self, user_id: str):
        with self._lock:
            self._history.pop(user_id, None)


_history_store = BoundedHistoryStore()


def _strip_urls(text: str) -> str:
    """Replies never carry raw URLs: photos/links travel as attachments."""
    if not text:
        return ""
    cleaned = re.sub(r"https?://\S+", "", str(text))
    # Drop labels left dangling by the removed URL ("Image:", "Link:").
    cleaned = re.sub(r"(?im)^\s*(image|link|url|photo)\s*:\s*$", "", cleaned)
    cleaned = re.sub(r"(?i)\b(image|link|url)\s*:\s*$", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def clear_user_history(user_id: str) -> None:
    """Forget a customer's chat context (used when an order completes)."""
    try:
        _history_store.clear(user_id)
    except Exception:
        pass
_session = requests.Session()

_llm_alert_ts = 0.0
_llm_alert_lock = threading.Lock()


def _alert_owner_llm_failure(exc) -> None:
    """Tell the seller on Telegram when the AI API stops answering.

    Throttled to once per 5 minutes: the seller hears about the outage
    without being spammed while it persists.
    """
    global _llm_alert_ts
    now = time.time()
    with _llm_alert_lock:
        if now - _llm_alert_ts < 300.0:
            return
        _llm_alert_ts = now
    token = getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "") or ""
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id,
                  "text": "AI reply service is not answering right now. "
                          "Customers are receiving catalog fallback replies. "
                          f"Last error: {str(exc)[:160]}"},
            timeout=6)
    except Exception:
        logger.warning("owner LLM-failure alert failed", exc_info=True)


def clear_history(user_id: str) -> None:
    _history_store.clear(user_id)


def _language_directive(lang: str) -> str:
    if lang == "bangla":
        return ("[LANGUAGE DIRECTIVE: Customer language is BENGALI SCRIPT. "
                "Reply strictly in natural polite Bengali script.]")
    if lang == "english":
        return ("[LANGUAGE DIRECTIVE: Customer language is ENGLISH. "
                "Reply strictly in polite concise English.]")
    return ("[LANGUAGE DIRECTIVE: Customer language is Romanized BANGLISH. "
            "Reply strictly in natural polite Romanized Banglish (English alphabet). "
            "Vary sentence openings. Do NOT switch to English for product names.]")


def _extract_text(data: dict) -> tuple:
    """(text, finish_reason) from any chat-completion shape. Never raises."""
    choices = (data or {}).get("choices") or []
    if not choices:
        return "", ""
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return text.strip(), str(choice.get("finish_reason") or "")


def _call_llama(messages: list, model: str, max_tokens: int = 2048, deadline_ts: float = 0.0) -> str:
    api_key = config.META_MODEL_API_KEY
    if not api_key:
        raise ValueError("META_MODEL_API_KEY is not configured")
    url = f"{config.META_MODEL_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # Reasoning models think before answering. Start generous; if reasoning
    # eats the budget (finish_reason=length, empty text), double it once
    # instead of failing the customer reply.
    budget = max_tokens
    last_exc = None
    for attempt in range(3):
        # reasoning_effort=low: Spark models answer in ~3-6s instead of
        # burning the whole deadline thinking (live-benchmarked).
        payload = {"model": model, "messages": messages, "temperature": 0.4,
                   "max_tokens": budget, "reasoning_effort": "low"}
        try:
            _timeout = REQUEST_TIMEOUT
            if deadline_ts:
                _remaining = deadline_ts - time.time()
                if _remaining <= 1.0:
                    raise RuntimeError("LLM deadline reached")
                _timeout = max(3.0, min(REQUEST_TIMEOUT, _remaining))
            resp = _session.post(url, json=payload, headers=headers, timeout=_timeout)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.6)
                continue
            resp.raise_for_status()
            text, finish = _extract_text(resp.json())
            if text and finish != "length":
                return text
            if finish == "length" and budget < 8192:
                budget *= 2
                last_exc = ValueError("reasoning consumed token budget")
                continue
            raise ValueError("empty content from Meta Model API")
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.6)
    raise RuntimeError(str(last_exc))


def _catalog_fallback_reply(text: str, lang: str, user_id=None) -> str:
    """Deterministic answer from verified catalog data when the LLM fails.

    The bot must never go silent or leak placeholders: prices and names
    here come straight from the repository-backed RAG index.
    """
    try:
        rag = get_rag()
        products = rag.search(text, top_k=3)
        if not products and user_id:
            products = rag.get_session(user_id).last_products[:3]
        if products:
            parts = []
            for prod in products:
                price = prod.get("price_formatted") or f"{prod.get('price_bdt', 0):,}৳"
                stock = "" if prod.get("in_stock", True) else " (out of stock)"
                parts.append(f"{prod.get('name')} {price}{stock}")
            joined = ", ".join(parts)
            _q = (text or "").lower()
            _top = products[0]
            _top_price = _top.get("price_formatted") or f"{_top.get('price_bdt', 0):,}৳"
            if any(w in _q for w in ("color", "colour", "rong")):
                _colors = []
                for _p in products:
                    _c = str(_p.get("color") or "").strip()
                    if _c and _c not in _colors:
                        _colors.append(_c)
                if _colors:
                    if lang == "english":
                        return f"{_top.get('name')} comes in {', '.join(_colors[:4])}. Price {_top_price}."
                    return f"{_top.get('name')} {_colors[0]} color e ache. Dam {_top_price}."
            if any(w in _q for w in ("price", "dam", "daam", "koto", "cost")):
                if lang == "english":
                    return f"{_top.get('name')} is {_top_price}. Would you like to order it?"
                return f"{_top.get('name')} er dam {_top_price}. Order korte chaile bolun."
            if any(w in _q for w in ("stock", "ache", "available", "have")):
                _avail = "in stock" if _top.get("in_stock", True) else "currently out of stock"
                if lang == "english":
                    return f"Yes, {_top.get('name')} is {_avail} at {_top_price}."
                return f"Ji, {_top.get('name')} amader kache {_top_price} e paben."
            if lang == "bangla":
                return random.choice([f"Amader kache egulo ache: {joined}. Konta somporke details janben?", f"Egulo available ache: {joined}. Kontar details chan bolun.", f"Ei product gulo paben: {joined}. Konta niye janaben?"])
            if lang == "english":
                return random.choice([f"We have these available: {joined}. Which one would you like details on?", f"Here's what we have right now: {joined}. Konta dekhte chan?", f"These are in stock: {joined}. Which one catches your eye?"])
            return random.choice([f"Amader kache egulo ache: {joined}. Konta somporke details janben?", f"Egulo available ache: {joined}. Kontar details chan bolun.", f"Ei product gulo paben: {joined}. Konta niye janaben?"])
        categories = sorted({p.get("category") for p in rag.products if p.get("category")})
        if categories:
            cats = ", ".join(categories[:6])
            if lang == "english":
                return f"We currently stock: {cats}. Tell me what you are looking for and I will find it."
            return f"Amader ekhane {cats} category er product ache. Apni ki khujchen bolun, ami khuje dicchi."
    except Exception:
        logger.exception("catalog fallback failed")
    return FALLBACK_REPLY


def _model_queue(primary: Optional[str]) -> list:
    queue, seen = [], set()
    for m in [primary or config.LLAMA_MODEL_NAME, *config.LLAMA_FALLBACK_MODELS]:
        if m and m not in seen:
            seen.add(m)
            queue.append(m)
    return queue


def generate_llama_reply(user_prompt, user_id=None, system_prompt=None,
                         model=None, extra_context=None) -> str:
    """Generate a sales reply via Meta Llama with RAG + order-state hooks."""
    text = (user_prompt or "").strip()
    if not text:
        return ""
    text = text[:4000]

    # Order flow runs before the LLM so checkout stays deterministic.
    if user_id and not extra_context:
        try:
            rag = get_rag()
            session = rag.get_session(user_id)
            top = session.last_products[0] if session.last_products else {}
            is_order, order_reply, _ = get_order_state_machine().process_turn(
                sender_id=user_id, user_message=text,
                current_product=top.get("name"),
                current_price=top.get("price_formatted"))
            if is_order and order_reply:
                _history_store.append_turn(user_id, text, order_reply)
                return order_reply
        except Exception:
            logger.exception("order-state pre-check failed; continuing to LLM")

    if user_id:
        order_session = get_order_state_machine().get_session(user_id)
        lang = detect_language(text, current_lang=order_session.language)
        order_session.language = lang
    else:
        lang = detect_language(text)

    # Seller override (/language on Telegram): en/bn force every reply,
    # auto keeps adapting to the customer's own language.
    try:
        from language_settings import get_language_mode
        _mode = get_language_mode()
        if _mode == "en":
            lang = "english"
        elif _mode == "bn":
            lang = "bangla"
    except Exception:
        pass

    sections = [system_prompt or config.SYSTEM_PROMPT, _language_directive(lang),
                "PRICE FORMAT: Always write prices as number then the taka sign, e.g. 215,880\u09f3. "
                "Never write 'BDT' after a number and never use a raw unformatted number. "
                "Never include URLs, links, or 'Image:' references in your reply: product photos are attached separately."]
    # Verified policy/FAQ knowledge and remembered preferences (never invent).
    try:
        from knowledge import get_knowledge_base
        kb_context = get_knowledge_base().answer_context(text)
        if kb_context:
            sections.append(kb_context)
    except Exception:
        logger.exception("knowledge retrieval failed")
    if user_id:
        try:
            from customer_memory import get_memory
            pref = get_memory().preference_context(user_id)
            if pref:
                sections.append(pref)
        except Exception:
            logger.exception("preference lookup failed")
    if extra_context:
        sections.append(str(extra_context)[:6000])
    else:
        try:
            rag = get_rag()
            catalog_context, _action = rag.format_context_for_llm(text, user_id=user_id)
            if catalog_context:
                sections.append(f"[Store Inventory Database Result]\n{catalog_context}")
                if user_id:
                    s = rag.get_session(user_id)
                    if s.last_products:
                        top = s.last_products[0]
                        get_order_state_machine().set_active_product(
                            user_id, top.get("name"), price=top.get("price_formatted"))
            elif rag.is_comparison_query(text):
                sections.append(
                    "Notice: customer asked for a comparison but no specific products "
                    "were discussed yet. Ask concisely which models to compare.")
        except Exception:
            logger.exception("RAG retrieval failed; replying without catalog context")

    messages = [{"role": "system", "content": "\n\n".join(sections)[:MAX_PROMPT_CHARS]}]
    if user_id:
        messages.extend(_history_store.get(user_id))
    messages.append({"role": "user", "content": text})

    last_error = None
    deadline = time.time() + 25.0  # fail over to catalog fallback fast
    for model_name in _model_queue(model):
        if time.time() > deadline:
            logger.warning("LLM deadline reached; using catalog fallback")
            break
        try:
            reply = _call_llama(messages, model_name, deadline_ts=deadline)
            reply = _strip_urls(reply)
            if not reply:
                raise RuntimeError("reply was only a URL")
            if user_id:
                _history_store.append_turn(user_id, text, reply)
            return reply
        except Exception as exc:
            last_error = exc
            logger.warning("Meta Llama model %s failed: %s", model_name, exc)
    logger.error("All Meta Llama models failed: %s", last_error)
    _alert_owner_llm_failure(last_error)
    return _catalog_fallback_reply(text, lang, user_id=user_id)


# Public alias used across the app.
generate_reply = generate_llama_reply
