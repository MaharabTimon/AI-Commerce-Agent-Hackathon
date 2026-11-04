<div align="center">

# ðŸ›ï¸ AI Commerce Agent

### *Not a chatbot. An AI employee for your shop.*

Customers chat in **English Â· Bangla Â· Banglish** â†’ the agent understands, searches the real catalog, sends product photos, takes orders without overselling, and pings the seller on Telegram. **The seller runs the whole business from Telegram.**

[![Meta AI](https://img.shields.io/badge/AI-Meta_Model_API-0082FB?style=for-the-badge&logo=meta&logoColor=white)](https://ai.meta.com)
[![Messenger](https://img.shields.io/badge/Platform-Messenger-0082FB?style=for-the-badge&logo=messenger&logoColor=white)](https://developers.facebook.com/docs/messenger-platform)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
![Vercel](https://img.shields.io/badge/Deployed-Vercel-000000?style=for-the-badge&logo=vercel&logoColor=white)
[![Tests](https://img.shields.io/badge/Tests-150%2B_passing-2EA44F?style=for-the-badge)](#-tests)


</div>

---

## ðŸŽ¬ The 60-Second Demo

| Step | What happens |
|---|---|
| ðŸ’¬ *"do you have saree?"* | Real sarees, real prices â€” `4,200à§³`, never invented |
| ðŸ“¸ *"Banarasi saree ta dekha jabe?"* | The **actual product photo** arrives in chat |
| ðŸ§  *"can you show a picture?"* | It knows which product you mean â€” even on a fresh server |
| ðŸ§¾ Order â†’ review summary â†’ **confirm** | No order exists until the customer says yes |
| ðŸ“² Seller's Telegram lights up | Order details, sales, hot leads, one tap away |

---

## âš”ï¸ Why It's Different

<div align="center">

| ðŸ¤– Standard Messenger Bot | ðŸš€ This Agent |
|---|---|
| Keyword rules, one LLM call | **Tool-based agent** â€” AI reasons, tools act, database is truth |
| Hallucinates price & stock | Prices and stock **only** from the catalog |
| Stateless, forgets everything | 5-turn context + product memory across servers, **wiped clean when an order completes** |
| Dies silently when the API does | Catalog-grounded fallback replies + **instant Telegram alert** to the seller |
| Needs a dashboard & a dev team | Seller runs everything from **Telegram** |

</div>

---

## âœ¨ Features

### ðŸ’¬ Customer Side â€” Messenger
- ðŸŒ **Trilingual brain** â€” English / Bangla / Banglish auto-detected, typo-tolerant fuzzy search
- ðŸ“· **Photo in** â€” send a product image, get matching catalog products (Meta Vision)
- ðŸ“¸ **Photo out** â€” ask *"chobi dao / show me"* and the real photo lands in chat
- ðŸ›’ **Conversational ordering** â€” browse â†’ review summary â†’ explicit confirm
- ðŸ“¦ **Order tracking, returns, cancellations** â€” under hard business rules
- ðŸ™‹ **Human escalation** â€” sensitive chats handed to the seller with a clean summary

### ðŸ“² Seller Side â€” Telegram
- ðŸ“¥ **Any dataset, one upload** â€” JSON Â· JSONL Â· CSV/TSV Â· XLSX Â· XML Â· Shopify Â· messy pasted text (~70 field aliases)
- ðŸ’± **Currency brains** â€” detects `$` `USD` `à§³` `tk`, converts USDâ†’BDT, and **asks you** when the file doesn't say
- ðŸ“Š **Business copilot** â€” `/orders` `/sales` `/hotleads` `/insights`
- ðŸŒ **`/language`** â€” replies in Auto (per-customer), English, or Bangla
- â¸ï¸ **`/stop` & `/resume`** â€” pause the bot, take over personally, hand it back

### ðŸ›¡ï¸ Production Grade
- ðŸ”’ Oversell-proof **atomic stock reservation** Â· strict order state machine
- âš¡ Circuit breaker + send pacing â€” can never spam Meta's API
- ðŸ§¹ Every price as `4,200à§³` Â· zero raw links in chat Â· prompt-injection defenses
- âœ… **150+ automated checks** â€” imports, money, orders, memory, failure paths

---

## ðŸ—ï¸ How It Works

```mermaid
flowchart TD
    C[ðŸ’¬ Customer] --> M[Messenger Webhook]
    M --> A[ðŸ§  Meta Spark<br/>reasons Â· fast Â· low-effort]
    A --> T[ðŸ”§ Tool Layer<br/>checked Â· audited]
    T --> R[(ðŸ“š Catalog RAG<br/>Turso DB)]
    T --> O[ðŸ§¾ Orders<br/>stock-safe]
    T --> K[ðŸ“– Knowledge<br/>policies]
    O --> S[ðŸ“² Seller on Telegram<br/>alerts + full control]
    S --> T
```

> **Golden rule:** the AI never invents prices, stock, or orders. It asks a tool; the tool checks the database; only verified facts reach the customer.

---

## ðŸš€ Run It Yourself

```bash
pip install -r requirements.txt
cp .env.example .env   # add your tokens
python messenger.py
```

<details>
<summary><b>ðŸ”‘ Environment variables</b></summary>

| Variable | Purpose |
|---|---|
| `PAGE_ACCESS_TOKEN` Â· `PAGE_ID` | Facebook Page |
| `META_MODEL_API_KEY` | Meta Model API â€” chat + vision, one key |
| `LLAMA_MODEL_NAME` | default `muse-spark-1.2` (the fast one) |
| `TELEGRAM_BOT_TOKEN` Â· `TELEGRAM_CHAT_ID` | Seller bot + owner |
| `TURSO_DATABASE_URL` Â· `TURSO_AUTH_TOKEN` | Catalog database |
| `CATALOG_USD_BDT_RATE` | optional Â· default 120 |

</details>

## âœ… Tests

```bash
python tests/test_hackathon.py
```

150+ checks: dataset parsing in every format, currency conversion, oversell protection, order lifecycle, context memory & cleanup, image resolution, failure fallbacks. **No external calls needed.**

---

<div align="center">

### ðŸ† Built for the Meta Hackathon â€” the entire AI stack runs on Meta APIs.

**Talk â†’ Understand â†’ Search â†’ Reason â†’ Act â†’ Remember â†’ Analyze â†’ Report**

MIT License

</div>

