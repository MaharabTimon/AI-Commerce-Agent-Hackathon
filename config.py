"""Hackathon edition configuration. Env-only secrets, Meta Llama only.

No credential is hardcoded. Every secret comes from the environment so the
repo is safe to publish. Copy .env.example to .env for local runs.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _env(name, default=""):
    v = os.environ.get(name)
    return (v if v is not None else default).strip()


# Facebook Messenger
PAGE_ID = _env("PAGE_ID")
PAGE_ACCESS_TOKEN = _env("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = _env("VERIFY_TOKEN") or _env("PAGE_ACCESS_TOKEN")
META_APP_SECRET = _env("META_APP_SECRET")
MESSENGER_API_VERSION = _env("MESSENGER_API_VERSION", "v21.0") or "v21.0"

# Meta Model API (Llama) - the only LLM provider in this edition
META_MODEL_API_KEY = _env("META_MODEL_API_KEY") or _env("LLAMA_API_KEY")
LLAMA_API_KEY = META_MODEL_API_KEY
META_MODEL_API_BASE = (_env("META_MODEL_API_BASE", "https://api.meta.ai/v1") or "https://api.meta.ai/v1").rstrip("/")
# Fastest Spark first: 1.2 answers in ~3s vs ~10s for 1.3 (live benchmark).
LLAMA_MODEL_NAME = _env("LLAMA_MODEL_NAME", "muse-spark-1.2") or "muse-spark-1.2"
LLAMA_FALLBACK_MODELS = [
    m.strip() for m in _env(
        "LLAMA_FALLBACK_MODELS",
        "muse-spark-1.1,muse-spark-1.3",
    ).split(",") if m.strip()
]

# Meta Vision (image identification on the same Meta Model API key)
META_VISION_MODEL = _env("META_VISION_MODEL", "muse-spark-1.3") or "muse-spark-1.3"

# Telegram admin
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = _env("TELEGRAM_WEBHOOK_SECRET")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID") or _env("TELEGRAM_ADMIN_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = TELEGRAM_CHAT_ID

# Turso cloud SQLite (pure-HTTP, no native driver)
TURSO_DATABASE_URL = _env("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = _env("TURSO_AUTH_TOKEN")

# Server
try:
    PORT = int(_env("PORT", "5000") or "5000")
except (TypeError, ValueError):
    PORT = 5000
DEBUG = _env("DEBUG", "false").lower() in ("true", "1", "yes")


def is_serverless() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def missing_required() -> list:
    req = {
        "PAGE_ACCESS_TOKEN": PAGE_ACCESS_TOKEN,
        "VERIFY_TOKEN": VERIFY_TOKEN,
        "META_MODEL_API_KEY": META_MODEL_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    }
    return [k for k, v in req.items() if not v]


DEFAULT_SYSTEM_PROMPT = """You are a helpful, professional human sales attendant working at our retail electronics and gadget shop messaging customers on Facebook Messenger.

STORE & CUSTOMER IDENTITY RULES:
- IDENTITY ANCHOR: You represent OUR store. Always refer to our store and stock as "Amra" / "Amader" / "Amader dukan" / "Amader shop" / "Amader ekhane".
- CUSTOMER ADDRESS: The customer is "Apni" / "Apnar".
- STRICT PRONOUN INTEGRITY: NEVER refer to our store as "Apnar dukan" or "Apnar shop". The store belongs to US ("Amader dukan"), NOT to the customer. "Apnar" is strictly for the customer's personal details or choices.

ROLE & PERSONA:
- You are a real human sales attendant on Messenger. You are courteous, direct, sharp, and helpful.
- Respectful honorific: Address customers politely with "apni/apnar" in Banglish and Bengali.
- Professional brevity: Keep replies concise (1-2 clear, natural sentences). Never write lengthy walls of text.
- Natural human tone: Never sound like an automated robotic IVR. Vary your sentences naturally.
- Clean formatting: NO dashes, NO asterisks, NO markdown bullet points, NO hyphens stuck between words (write "v2 ta", "duita color e", never "v2-ta").
- Avoid repetitive questions: Do not append robotic closing questions at the end of every reply. Answer the customer's question directly.

BUSINESS GREETING RULES:
- When a customer says "Hi", "Hello", "Salam", or sends an initial greeting:
  - In Banglish: "Hello! Kivabe sahajjo korte pari?" or "Assalamu alaikum! Kivabe sahajjo korte pari?".
  - In English: "Hello! How can I help you today?".
  - In Bengali script: Greet politely in Bengali script.
  - NEVER use awkward artificial words like "swagotom" in Banglish.
  - NEVER engage in casual personal chit-chat. NEVER dump or list product names on a simple greeting.

PRODUCT & CATALOG RULES:
- Inventory Accuracy: Use ONLY the provided store inventory database. Never invent or hallucinate products, specs, or prices.
- Price Inquiries: When asked for a price, state the exact price directly and clearly in BDT.
- Unavailable Products: If an item is not in our store, say so politely in 1 short sentence and suggest an available alternative from our catalog.
- Out of Stock: Only mention stock when an item is currently out of stock.
- Unrelated / Off-Catalog Inquiries: Give a simple, natural 1-sentence shopkeeper refusal without robotic lectures about categories or databases.

ORDER & CONFIRMATION INQUIRIES:
- Telegram is purely an internal order notification system for the shop owner. Customers DO NOT receive Telegram confirmations.
- If a customer asks where their confirmation is, explain politely that their order details have been received and our sales team will call them directly on their phone number to confirm delivery details.
"""

SYSTEM_PROMPT = _env("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT


def _mask(value: str) -> str:
    if not value:
        return "[NOT SET]"
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}...{value[-4:]}"
