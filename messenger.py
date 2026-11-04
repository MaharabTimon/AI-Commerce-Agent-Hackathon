import hashlib
import hmac
import html
import logging
import os
import threading
import time
from collections import OrderedDict
from flask import Flask, request, jsonify, Response
import requests
import config
from bot_state import is_bot_active
from database import has_active_catalog
from meta_llama_service import generate_llama_reply
from messenger_service import send_message, send_sender_action, send_image
import safety
from analytics import get_analytics
from rag_service import get_rag
from order_state import get_order_state_machine
from vision_service import analyze_product_image, format_vision_context
from telegram_service import (
    start_telegram_polling,
    handle_telegram_command,
    register_telegram_webhook
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


class MessageDeduplicator:
    """
    Thread-safe deduplication cache to prevent duplicate processing
    when Facebook re-delivers webhooks due to network retries.
    """
    def __init__(self, max_size: int = 5000, ttl_seconds: int = 600):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._processed: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def is_duplicate(self, mid: str) -> bool:
        if not mid:
            return False
        with self._lock:
            now = time.time()
            while self._processed and (now - next(iter(self._processed.values())) > self.ttl_seconds):
                self._processed.popitem(last=False)
            if mid in self._processed:
                return True
            if len(self._processed) >= self.max_size:
                self._processed.popitem(last=False)
            self._processed[mid] = now
            return False


_deduplicator = MessageDeduplicator()


def _discard_mid(mid):
    if not mid:
        return
    with _deduplicator._lock:
        _deduplicator._processed.pop(mid, None)


def _on_breaker_trip(reason: str):
    """L5 fuse: freeze the bot and alert the owner on Telegram."""
    try:
        from bot_state import set_bot_active
        set_bot_active(False)
    except Exception:
        logger.exception("breaker trip: failed to freeze bot state")
    token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "parse_mode": "HTML",
                      "text": "<b>HIGH MESSAGE VOLUME. SAFETY FUSE ACTIVATED.</b>\n"
                              f"Reason: {html.escape(reason)}\n"
                              "Bot paused to protect the Page. Send /resume to reactivate."},
                timeout=6)
        except Exception:
            logger.exception("breaker trip: Telegram alert failed")


safety.breaker.on_trip = _on_breaker_trip

_last_missing_catalog_alert_ts: float = 0.0
_alert_lock = threading.Lock()

_last_bot_issue_alert_ts: float = 0.0


def alert_admin_bot_issue(sender_id: str, preview_msg: str, reason: str):
    """Throttled Telegram alert when a customer message could not be answered.

    The seller sees the failure and can reply manually in Page Inbox instead
    of the customer silently getting nothing. At most once per 5 minutes.
    """
    global _last_bot_issue_alert_ts
    now = time.time()
    with _alert_lock:
        if (now - _last_bot_issue_alert_ts) < 300.0:
            return
        _last_bot_issue_alert_ts = now
    token = getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "") or ""
    if not token or not chat_id:
        return
    clean = html.escape((preview_msg or "")[:120])
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "parse_mode": "HTML",
                  "text": "<b>CUSTOMER MESSAGE NEEDS MANUAL REPLY</b>\n"
                          f"Customer: <code>{html.escape(str(sender_id))}</code>\n"
                          f"Message: {clean}\nReason: {html.escape(str(reason)[:80])}\n"
                          "Please reply from Facebook Page Inbox."},
            timeout=6)
    except Exception:
        logger.warning("bot-issue Telegram alert failed", exc_info=True)


CUSTOMER_FALLBACK_TEXT = ("Apnar message ta peyechi. Ekhon ektu technical somoshya hocche, "
                          "tobe amader team khub shighroi apnake reply korbe.")

import re as _re_img

_IMAGE_ASK_WORDS = ("image", "photo", "picture", "pic", "show me", "show", "chobi", "ছবি",
                    "dekhi", "dekha", "dekhao", "pathao", "পাঠাও", "দেখাও", "dekhte", "দেখতে",
                    "send", "img")


def _dynamic_show_reply(product, user_text) -> str:
    """Varied, human phrasing for showing a product (never one fixed line)."""
    import random as _random
    name = product.get("name")
    try:
        price = f"{int(product.get('price_bdt') or 0):,}à§³"
    except (TypeError, ValueError):
        price = product.get("price_formatted") or ""
    try:
        from order_state import detect_language
        lang = detect_language(user_text or "")
    except Exception:
        lang = "banglish"
    if lang == "english":
        pool = [f"Sure! Here's the {name} â€” {price}.",
                f"Of course, this is the {name} for {price}.",
                f"Here's the {name} â€” {price}. Like it?"]
    elif lang == "bangla":
        pool = [f"à¦à¦‡ à¦¯à§‡ {name} â€” à¦¦à¦¾à¦® {price}.",
                f"à¦…à¦¬à¦¶à§à¦¯à¦‡, à¦à¦‡ à¦¹à¦šà§à¦›à§‡ {name}, à¦¦à¦¾à¦® {price}."]
    else:
        pool = [f"Ei je {name} â€” dam {price}.",
                f"Oboshshoi, ei hocche {name}, dam {price}.",
                f"Ei dekhun {name} ta â€” {price}."]
    return _random.choice(pool)


def _resolve_product_for_image(sender_id, user_text):
    """Which product does this image ask mean?

    Order: a product named in this message, then this container's session,
    then the DB-persisted current product (survives fresh containers).
    """
    try:
        _rag = get_rag()
        # A product actually named in this message wins, but a generic
        # "show a picture?" must fall through to conversation context:
        # require real name-token overlap so search cannot grab a random
        # product for a bare follow-up.
        _low_text = str(user_text or "").lower()
        _best_named, _best_named_ov = None, 0
        for _cand, _sc in _rag.search_specific_products(_low_text[:200], top_k=4):
            if not _product_image_url(_cand):
                continue
            _words = [w for w in str(_cand.get("name", "")).lower().replace("-", " ").split() if len(w) > 2]
            _ov = sum(1 for w in _words if w in _low_text)
            if _ov > _best_named_ov:
                _best_named, _best_named_ov = _cand, _ov
        if _best_named is not None and _best_named_ov >= 1:
            return _best_named
        _sess = _rag.get_session(sender_id)
        if _sess.last_products:
            return _sess.last_products[0]
    except Exception:
        pass
    try:
        import conversation_context as _ctx
        _pid = _ctx.get_current_product_id(sender_id)
        if _pid is not None:
            from database import get_product_repository
            _prod = get_product_repository().get_by_id(_pid)
            if _prod:
                return _prod
    except Exception:
        pass
    return None


def _product_image_url(product) -> str:
    """Best image URL for a catalog product (direct field or specs text)."""
    if not product:
        return ""
    for key in ("image", "image_url", "photo"):
        direct = str(product.get(key) or "").strip()
        if direct.startswith("http"):
            return direct.rstrip("',]}")
    specs = str(product.get("specifications") or "")
    idx = specs.lower().find("image:")
    segment = specs[idx:] if idx >= 0 else specs
    m = _re_img.search(r"https?://[^\s'\"|]+", segment)
    if m:
        return m.group(0).rstrip("',]}")
    return ""


def alert_admin_missing_catalog(sender_id: str, preview_msg: str):
    """
    Sends an internal alert to the Telegram admin when a customer messages the page
    while the product database is disconnected or empty.
    Throttled to at most once every 5 minutes to avoid spamming the owner.
    """
    global _last_missing_catalog_alert_ts
    now = time.time()
    with _alert_lock:
        if (now - _last_missing_catalog_alert_ts) < 300.0:  # 5-minute cooldown
            return
        _last_missing_catalog_alert_ts = now

    token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    clean_preview = html.escape(preview_msg[:120]) if preview_msg else "<i>[Attachment or Media]</i>"
    alert_text = (
        "⚠️ <b>CUSTOMER MESSAGE RECEIVED (BOT MUTED)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Customer ID:</b> <code>{sender_id}</code>\n"
        f"💬 <b>Message:</b> {clean_preview}\n\n"
        "⏸️ <b>Bot Action:</b> Muted (Awaiting Catalog)\n"
        "The store catalog is currently empty or database is disconnected.\n\n"
        "📥 <b>Action Required:</b>\n"
        "• Upload your product dataset (.txt / .csv) in this chat\n"
        "• Or type /start to access the Control Panel\n"
        "• Or reply to the customer manually on Facebook Page Inbox."
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": alert_text, "parse_mode": "HTML"}, timeout=6)
        logger.info(f"Dispatched missing catalog alert to Telegram admin for customer {sender_id}.")
    except Exception as e:
        logger.warning(f"Failed to send missing catalog alert to Telegram: {e}")


@app.route('/', methods=['GET'])
@app.route('/api', methods=['GET'])
def index():
    """Health check and status dashboard."""
    active = is_bot_active()
    from health import health_snapshot
    return jsonify({
        "status": "online",
        "service": "AI Commerce Agent (Hackathon)",
        "bot_active": active,
        "health": health_snapshot(),
        "mode": "Vercel Serverless" if os.environ.get("VERCEL") == "1" else "Standard Server",
        "endpoints": {
            "facebook_webhook": "/webhook",
            "telegram_webhook": "/telegram-webhook",
            "setup_telegram_webhook": "/setup-telegram"
        }
    }), 200


@app.route('/webhook', methods=['GET'])
@app.route('/api/webhook', methods=['GET'])
@app.route('/api/webhook.py', methods=['GET'])
def verify_webhook():
    """Webhook verification endpoint required by Facebook Messenger platform."""
    mode = request.args.get('hub.mode') or request.args.get('hub_mode')
    token = request.args.get('hub.verify_token') or request.args.get('hub_verify_token')
    challenge = request.args.get('hub.challenge') or request.args.get('hub_challenge')

    if mode and token:
        expected = (config.VERIFY_TOKEN or "").strip()
        # Fail closed: no configured token means verification can never pass.
        ok = bool(expected) and hmac.compare_digest(str(token), expected)
        if mode == 'subscribe' and ok and challenge:
            logger.info("Facebook Webhook verified successfully!")
            return Response(str(challenge), status=200, mimetype='text/plain')
        else:
            logger.warning(f"Verification failed. Provided token does not match: {token}")
            return "Forbidden", 403

    return "Bad Request", 400


@app.route('/webhook', methods=['POST'])
@app.route('/api/webhook', methods=['POST'])
@app.route('/api/webhook.py', methods=['POST'])
def handle_webhook_events():
    """Processes incoming messages and postbacks from Facebook Messenger."""
    # HMAC signature verification (fail closed when the secret is configured).
    secret = (getattr(config, "META_APP_SECRET", "") or "").strip()
    if secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        digest = "sha256=" + hmac.new(secret.encode(), request.get_data() or b"", hashlib.sha256).hexdigest()
        if not sig or not hmac.compare_digest(sig, digest):
            logger.warning("Rejected webhook POST with bad signature")
            return "Invalid signature", 403
    else:
        # No app secret configured: signature check is off. Logged once at
        # debug level so production logs stay clean.
        if not getattr(handle_webhook_events, "_secret_noted", False):
            handle_webhook_events._secret_noted = True
            logger.debug("Webhook signature check off (no META_APP_SECRET)")
    if request.content_length and request.content_length > 1_000_000:
        return "Payload too large", 413
    body = request.get_json(silent=True)
    if not body:
        return "Invalid JSON payload", 400

    if body.get('object') != 'page':
        return "Not Found", 404

    entries = body.get('entry', [])
    if not isinstance(entries, list):
        return "EVENT_RECEIVED", 200
    for entry in entries[:20]:
        if not isinstance(entry, dict):
            continue
        messaging_events = entry.get('messaging', [])
        if not isinstance(messaging_events, list):
            continue
        for event in messaging_events:
            sender_id = event.get('sender', {}).get('id')
            if not sender_id:
                continue

            # 1. Guard against Page self-messaging
            if str(sender_id) == str(config.PAGE_ID):
                logger.info("Ignoring webhook event sent by the Page itself.")
                continue

            # 2. Drop stale retries from Meta (> 45 seconds old - safe for Vercel cold starts)
            event_time = event.get('timestamp')
            if event_time:
                age_ms = (time.time() * 1000) - event_time
                if age_ms > 45000:
                    logger.info(f"Dropping stale/retried webhook from {sender_id} (age: {age_ms/1000:.1f}s)")
                    alert_admin_bot_issue(sender_id, "", "stale_event_dropped")
                    continue

            try:
                # Process incoming message
                if 'message' in event:
                    message_data = event['message']

                    # Ignore echo messages from the page itself
                    if message_data.get('is_echo'):
                        continue

                    # Deduplicate retried webhook events from Facebook
                    mid = message_data.get('mid')
                    if mid and _deduplicator.is_duplicate(mid):
                        logger.info(f"Duplicate message ignored: mid={mid}")
                        continue

                    # Check if bot is stopped via Telegram /stop
                    if not is_bot_active():
                        logger.info(f"Bot is currently STOPPED via Telegram command. Ignoring message from {sender_id} so human can take over.")
                        continue

                    user_text = (message_data.get('text') or '').strip()[:4000] if isinstance(message_data.get('text'), str) else ''
                    attachments = message_data.get('attachments') or []
                    if not isinstance(attachments, list):
                        attachments = []

                    # L2 anti-loop: mute rapid identical repeats (bot ping-pong).
                    if user_text and safety.anti_loop.is_looping(sender_id, user_text):
                        logger.warning(f"Anti-loop mute for {sender_id}")
                        continue
                    if safety.breaker.is_tripped():
                        logger.warning("Circuit breaker tripped; staying silent")
                        continue

                    # Check if active product database exists and is populated
                    if not has_active_catalog():
                        logger.warning(f"No active database catalog found. Ignoring message from {sender_id} to prevent uninformed replies.")
                        alert_admin_missing_catalog(sender_id, user_text)
                        continue

                    # Exclude stickers/likes (Meta sends them as image attachments with sticker_id)
                    image_attachment = next(
                        (
                            att for att in attachments 
                            if att.get('type') == 'image' 
                            and att.get('payload', {}).get('url') 
                            and not att.get('payload', {}).get('sticker_id')
                        ),
                        None
                    )

                    # Scenario A: Customer sent an image (photo of product or gadget)
                    if image_attachment:
                        img_url = image_attachment['payload']['url']
                        logger.info(f"Received image attachment from {sender_id}: {img_url[:60]}...")
                        # Send typing indicator immediately
                        send_sender_action(sender_id, "typing_on")

                        try:
                            # 1. Analyze image using Meta Vision service
                            vision_res = analyze_product_image(img_url)
                            logger.info(f"Meta Vision result for {sender_id}: {vision_res.get('product_name', 'None')} (success={vision_res.get('success')})")

                            # 2. Search catalog via RAG
                            catalog_matches = []
                            rag = get_rag()
                            if vision_res.get("success"):
                                search_query = vision_res.get("search_query") or vision_res.get("product_name")
                                catalog_matches = rag.search_specific_products(search_query, top_k=3)

                                # Update order state machine with identified product if catalog match found
                                if catalog_matches:
                                    top_prod = catalog_matches[0][0]
                                    get_order_state_machine().set_active_product(
                                        sender_id,
                                        top_prod.get("name"),
                                        price=top_prod.get("price_formatted")
                                    )

                            # 3. Format vision context for Llama response generator
                            vision_ctx = format_vision_context(vision_res, catalog_matches)

                            # If user also sent caption text with the photo, preserve user's inquiry
                            prompt_text = user_text if user_text else "[Customer uploaded a product photo]"

                            ai_reply = generate_llama_reply(
                                prompt_text,
                                user_id=sender_id,
                                extra_context=vision_ctx
                            )

                            if ai_reply and ai_reply.strip():
                                send_message(sender_id, ai_reply.strip())
                            else:
                                fallback = "Photo ta peyechi! Amader team apnake shighroi inform korbe."
                                send_message(sender_id, fallback)

                        except Exception as e:
                            logger.error(f"Error handling image from {sender_id}: {e}", exc_info=True)
                            send_message(sender_id, "Photo ta analyze korte ektu somoshya hoyeche. Model name ta text e bolben please?")
                        finally:
                            send_sender_action(sender_id, "typing_off")

                    # Scenario B: Customer sent a text message
                    elif user_text:
                        logger.info(f"Received text from {sender_id}: {user_text}")

                        # Prompt-injection guard: never obey or tool-execute it.
                        from sentiment import is_injection_attempt, escalation, build_summary, create_ticket, detect_sentiment, sales_stage, lead_score
                        if is_injection_attempt(user_text):
                            logger.warning(f"Prompt injection blocked from {sender_id}")
                            send_message(sender_id, "Dukkhito, ei bisoye sahajjo korte parbo na. Product ba order niye kichu janar thakle bolun.")
                            continue

                        # Human handoff: ticket + summary to the seller, bot steps back.
                        _need, _priority, _reason = escalation(user_text)
                        if _need:
                            _ticket = create_ticket(sender_id, build_summary(sender_id, user_text, priority=_priority), _priority)
                            logger.warning(f"Escalation {_ticket['id']} ({_priority}: {_reason}) for {sender_id}")
                            send_message(sender_id, "Apnar bisoyti amader team er kache pouche diyechi. Ekjon manush shighroi apnake reply korbe.")
                            try:
                                _tok, _chat = getattr(config, "TELEGRAM_BOT_TOKEN", ""), getattr(config, "TELEGRAM_CHAT_ID", "")
                                if _tok and _chat:
                                    requests.post(f"https://api.telegram.org/bot{_tok}/sendMessage",
                                                  json={"chat_id": _chat, "parse_mode": "HTML",
                                                        "text": f"<b>ESCALATION {_priority}</b>\n{html.escape(_ticket['summary'])}"},
                                                  timeout=6)
                            except Exception:
                                logger.exception("escalation alert failed")
                            continue

                        # Real order tracking by phone or order id (never fabricated).
                        _low = user_text.lower()
                        if any(k in _low for k in ("order status", "where is my order", "amar order", "track order", "অর্ডার কোথায়")):
                            from orders import get_order_store as _gos
                            from intent_service import extract_phone as _extract_phone
                            _store = _gos()
                            _phone = _extract_phone(user_text)
                            _found = _store.find_by_phone(_phone) if _phone else []
                            if not _found:
                                import re as _re
                                _m = _re.search(r"ORD-\d+", user_text.upper())
                                _one = _store.get(_m.group(0)) if _m else None
                                _found = [_one] if _one else []
                            if _found:
                                send_message(sender_id, _store.tracking_text(_found[-1]["id"]))
                            else:
                                send_message(sender_id, "Apnar order khuje pacchi na. Order er somoy deya phone number ta pathaben please?")
                            continue

                        # Send typing indicator ONLY once if the event is fresh (< 4 seconds old)
                        _typing_sent = bool(send_sender_action(sender_id, "typing_on"))

                        get_analytics().record_event("product_inquiry")
                        # L3 human pacing: brief natural pause before replying.
                        safety.human_delay()
                        # Generate response from Meta Llama / RAG / Order State Machine
                        ai_reply = generate_llama_reply(user_text, user_id=sender_id)

                        # Send reply only when Llama produced a valid response
                        if ai_reply and ai_reply.strip():
                            # Showing a product must read like a person,
                            # not a template: if the AI denied a product
                            # that is verifiably in the catalog, answer
                            # dynamically from the real product instead.
                            if any(w in _low for w in _IMAGE_ASK_WORDS):
                                _named = _resolve_product_for_image(sender_id, user_text)
                                if _named is not None and any(
                                        d in ai_reply.lower() for d in
                                        ("do not have", "don't have", "not available", "sorry, we", "nai", "nei")):
                                    ai_reply = _dynamic_show_reply(_named, user_text)
                            if not send_message(sender_id, ai_reply.strip()):
                                alert_admin_bot_issue(sender_id, user_text, "send_failed")
                            elif any(w in _low for w in _IMAGE_ASK_WORDS):
                                # Customer asked to see it: send the real
                                # product photo as a native attachment. The
                                # session may live in another serverless
                                # container, so fall back to resolving the
                                # product from this turn's own text/reply.
                                try:
                                    _rag = get_rag()
                                    _sess = _rag.get_session(sender_id)
                                    _top = _sess.last_products[0] if _sess.last_products else None
                                    _img = _product_image_url(_top)
                                    if not _img:
                                        # Pick the candidate whose name is
                                        # actually mentioned in this reply.
                                        _reply_low = str(ai_reply).lower()
                                        _best, _best_overlap = None, 0
                                        for _q in (user_text, ai_reply):
                                            for _cand, _sc in _rag.search_specific_products(str(_q)[:200], top_k=4):
                                                if not _product_image_url(_cand):
                                                    continue
                                                _words = [w for w in str(_cand.get("name", "")).lower().replace("-", " ").split() if len(w) > 2]
                                                _overlap = sum(1 for w in _words if w in _reply_low)
                                                if _overlap > _best_overlap:
                                                    _best, _best_overlap = _cand, _overlap
                                        if _best is not None:
                                            _img = _product_image_url(_best)
                                    if _img:
                                        if not send_image(sender_id, _img):
                                            send_message(sender_id, f"Product er chobi ta ekhane dekhte paren: {_img}")
                                except Exception:
                                    logger.exception("product image send failed")
                            try:
                                from customer_memory import get_memory as _gm
                                _rag_s = get_rag().get_session(sender_id)
                                _mem = _gm()
                                _mem.touch(sender_id,
                                           sentiment_stage=sales_stage(user_text, bool(_rag_s.last_products)),
                                           score=lead_score(user_text, bool(_rag_s.last_products),
                                                            bool(__import__("rag_service").extract_budget(user_text))))
                                if _rag_s.last_products:
                                    _mem.note_view(sender_id, _rag_s.last_products[0])
                                    try:
                                        import conversation_context as _ctx
                                        _ctx.set_current_product(sender_id, _rag_s.last_products[0].get("id"))
                                    except Exception:
                                        pass
                            except Exception:
                                logger.exception("customer memory update failed")
                        else:
                            logger.warning(f"Llama produced empty reply for {sender_id}; sending fallback.")
                            if not send_message(sender_id, CUSTOMER_FALLBACK_TEXT):
                                alert_admin_bot_issue(sender_id, user_text, "fallback_send_failed")
                            alert_admin_bot_issue(sender_id, user_text, "empty_ai_reply")

                        if _typing_sent:
                            send_sender_action(sender_id, "typing_off")

                    elif not user_text and not attachments:
                        logger.info(f"Received empty/unsupported message from {sender_id}")
                        send_message(sender_id, "Apnar message ta thikmoto aseni. Text likhe ba product er chobi pathiye abar try korun.")

                    # Scenario C: Customer sent a non-image attachment (audio, video, sticker, location)
                    elif attachments:
                        logger.info(f"Received non-image attachment from {sender_id}")
                        fallback_reply = (
                            "Thank you for reaching out! Currently, I can understand text messages and product photos. "
                            "Feel free to ask me any question or send a product photo!"
                        )
                        send_message(sender_id, fallback_reply)


                # Handle postback events
                elif 'postback' in event:
                    if not is_bot_active():
                        logger.info(f"Bot is currently STOPPED via Telegram command. Ignoring postback from {sender_id}.")
                        continue

                    if not has_active_catalog():
                        logger.warning(f"No active database catalog found. Ignoring postback from {sender_id}.")
                        continue

                    payload = str(event['postback'].get('payload', '') or '')[:500]
                    logger.info(f"Received postback from {sender_id}: {payload}")
                    if payload:
                        ai_reply = generate_llama_reply(f"User clicked action: {payload}", user_id=sender_id)
                        if ai_reply and ai_reply.strip():
                            if not send_message(sender_id, ai_reply.strip()):
                                alert_admin_bot_issue(sender_id, payload, "postback_send_failed")
                        else:
                            send_message(sender_id, CUSTOMER_FALLBACK_TEXT)
                            alert_admin_bot_issue(sender_id, payload, "postback_empty_reply")

            except Exception as e:
                logger.error(f"Error processing messaging event for sender {sender_id}: {e}", exc_info=True)
                try:
                    _discard_mid((event.get("message") or {}).get("mid"))
                except Exception:
                    pass
                try:
                    if sender_id:
                        send_sender_action(sender_id, "typing_off")
                except Exception:
                    pass
                try:
                    if sender_id and is_bot_active():
                        send_message(sender_id, CUSTOMER_FALLBACK_TEXT)
                except Exception:
                    pass
                alert_admin_bot_issue(sender_id, "", "handler_exception")

    return "EVENT_RECEIVED", 200


@app.route('/telegram-webhook', methods=['POST'])
@app.route('/api/telegram-webhook', methods=['POST'])
@app.route('/api/telegram.py', methods=['POST'])
def handle_telegram_webhook():
    """
    Webhook endpoint for Telegram bot updates on Vercel.
    Receives /start, /stop, and /test commands.
    """
    _tg_secret = (getattr(config, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()
    if _tg_secret:
        _given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(_given, _tg_secret):
            logger.warning("Rejected Telegram webhook with bad secret token")
            return "Forbidden", 403
    update = request.get_json(silent=True)
    if update:
        try:
            from telegram_admin import dispatch_telegram_update
            if dispatch_telegram_update(update):
                return "OK", 200
        except (ImportError, ModuleNotFoundError):
            pass
        except Exception as e:
            logger.error(f"Error dispatching webhook update to telegram_admin: {e}")

        msg = update.get("channel_post") or update.get("message")
        if msg:
            text = msg.get("text", "")
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            msg_id = msg.get("message_id")
            if text and text.strip().startswith("/"):
                handle_telegram_command(text, chat_id, msg_id)
    return "OK", 200


@app.route('/setup-telegram', methods=['GET'])
@app.route('/api/setup-telegram', methods=['GET'])
@app.route('/api/setup.py', methods=['GET'])
def setup_telegram():
    """
    One-click setup endpoint to register the Telegram Webhook pointing to this Vercel deployment.
    """
    _setup_expected = (getattr(config, "TELEGRAM_WEBHOOK_SECRET", "") or config.VERIFY_TOKEN or "").strip()
    _setup_given = (request.args.get("secret") or "").strip()
    if _setup_expected and not hmac.compare_digest(_setup_given, _setup_expected):
        return jsonify({"status": "error", "message": "setup secret required"}), 403
    if not _setup_expected and config.is_serverless():
        return jsonify({"status": "error", "message": "setup disabled: no secret configured"}), 403
    # Detect public base URL
    host = request.headers.get('x-forwarded-host') or request.host
    proto = request.headers.get('x-forwarded-proto') or ('https' if request.is_secure else 'http')
    base_url = f"{proto}://{host}"
    
    webhook_url = f"{base_url.rstrip('/')}/telegram-webhook"
    result = register_telegram_webhook(webhook_url)
    
    return jsonify({
        "status": "success" if result.get("ok") else "error",
        "registered_url": webhook_url,
        "telegram_response": result,
        "instructions": "Webhook registered. Commands /start and /stop will now reach this serverless bot directly from Telegram!"
    }), 200


# Start polling ONLY if running locally as a standalone process (NOT in Vercel / serverless)
is_serverless = bool(
    os.environ.get("VERCEL")
    or os.environ.get("VERCEL_ENV")
    or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
)
if not is_serverless:
    start_telegram_polling()


if __name__ == '__main__':
    logger.info(f"Starting Messenger Meta Llama Bot on port {config.PORT} (Bot Active: {is_bot_active()})...")
    app.run(host='0.0.0.0', port=config.PORT, debug=False, use_reloader=False)
