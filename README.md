# AI Commerce Agent — Facebook Messenger

**Not a chatbot. An AI employee for your shop.**

Customers chat in English, Bangla, or Banglish → the agent understands, searches the real catalog, shows photos, takes orders without overselling, and alerts the seller on Telegram. The seller runs the whole business from Telegram.

![Meta AI](https://img.shields.io/badge/AI-Meta_Model_API-0082FB) ![Messenger](https://img.shields.io/badge/Platform-Facebook_Messenger-0082FB) ![Python](https://img.shields.io/badge/Python-3.12-3776AB) ![Deploy](https://img.shields.io/badge/Deploy-Vercel-000000) ![Tests](https://img.shields.io/badge/Tests-150+_checks_passing-brightgreen)

**Live:** https://facebook-messenger-ai-bot.vercel.app

## The 60-second demo

1. Customer: *"do you have saree?"* → real sarees, real prices (e.g. `4,200৳`)
2. *"Banarasi saree ta dekha jabe?"* → the actual product **photo arrives in chat**
3. *"can you show a picture?"* → it knows which product you mean — even on a fresh server
4. Order: name → phone → address → **review summary** → *confirm* → seller's Telegram lights up
5. Seller asks `/sales`, `/language`, `/stop`, `/resume` on Telegram — full remote control

## Why it's different

| Standard Messenger bot | This agent |
|---|---|
| Keyword rules / one LLM call | Tool-based agent: AI reasons, **tools act, database is truth** |
| Hallucinates price & stock | Prices and stock only from the catalog — never invented |
| Stateless | 5-turn context + current-product memory that survives serverless cold starts, **cleared when an order completes** |
| Demo dies when the API does | Catalog-grounded fallback replies + instant Telegram alert to the seller |
| Seller needs a dashboard team | Seller runs everything from Telegram |

## Features

**Customer side (Messenger)**
- English / Bangla / Banglish auto-detection, typo-tolerant fuzzy product search
- Photo in: send a product image → finds similar catalog products (Meta Vision)
- Photo out: ask *"chobi dao / show me"* → native product photo, correct product resolved from context
- Conversational ordering with explicit review & confirmation — never auto-confirms
- Order tracking, cancellations, returns under hard business rules
- Human escalation with a clean seller summary when things get sensitive

**Seller side (Telegram)**
- Import **any dataset**: JSON, JSONL, CSV/TSV, XLSX, XML, Shopify exports, messy pasted text — ~70 field aliases, variants expanded
- Currency brains: detects `$`/`USD`/`৳`/`tk`, converts USD→BDT, and **asks you BDT or USD** when the file doesn't say
- Stock & price editing, low-stock alerts, `/orders` `/sales` `/hotleads` `/insights`
- `/language` — replies in Auto (per-customer), English, or Bangla
- `/stop` & `/resume` — pause the bot and take over personally

**Production hardening**
- Oversell-proof atomic stock reservation; strict order state machine
- Circuit breaker + rate pacing so the bot can never spam Meta's API
- Webhook signature verification, prompt-injection defenses, role-checked tools, audit trail
- Every price shown as `4,200৳`; no raw links dumped in chat — photos ride as attachments
- 150+ automated checks covering imports, orders, money, memory, and failure paths

## Architecture

```text
Customer ──► Messenger ──► Webhook ──► Meta Spark (fast, low-reasoning)
                                          │  reasons, never touches data
                                          ▼
                                    Tool layer (checked, audited)
                                          │
                          ┌───────────────┼────────────────┐
                          ▼               ▼                ▼
                      Catalog/RAG       Orders          Knowledge
                       (Turso DB)    (stock-safe)      (policies)
                                          │
Seller ◄── Telegram alerts & control ◄────┘
```

## Run it yourself

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the tokens below
python messenger.py
```

| Variable | What |
|---|---|
| `PAGE_ACCESS_TOKEN`, `PAGE_ID` | Facebook Page |
| `META_MODEL_API_KEY` | Meta Model API (chat + vision, one key) |
| `LLAMA_MODEL_NAME` | default `muse-spark-1.2` (fast) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Seller bot + owner chat |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | Catalog database |
| `CATALOG_USD_BDT_RATE` | optional, default 120 |

## Tests

```bash
python tests/test_hackathon.py   # 150+ checks, no external calls needed
```

## Honest limitations

Orders and some session state live in process memory (a redeploy resets them); catalog and product context are database-backed. Webhook signature enforcement activates when `META_APP_SECRET` is set. Built for the Meta hackathon — the entire AI stack (chat + vision) runs on Meta APIs only.

## License

MIT — see [LICENSE](LICENSE).
