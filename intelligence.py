"""Seller intelligence (spec 57-67). Deterministic, evidence-first.

Every number comes from real counters, orders, memory, or the catalog.
Insights are labeled as observations with their sample size, never as
facts the data cannot support. No LLM call is needed to compute them.
"""
from collections import Counter

from analytics import get_analytics
from customer_memory import get_memory
from sentiment import open_tickets


def seller_summary(repo=None) -> str:
    from database import get_product_repository
    repo = repo or get_product_repository()
    summary = repo.get_summary()
    analytics = get_analytics()
    memory = get_memory()
    lines = [
        "Business summary",
        analytics.daily_summary(),
        f"Catalog: {summary.get('total_products', 0)} products, "
        f"{summary.get('in_stock_count', 0)} in stock, {summary.get('low_stock_count', 0)} low stock, "
        f"{summary.get('out_of_stock_count', 0)} out of stock",
        f"Hot leads: {len(memory.hot_leads())} | Open support tickets: {len(open_tickets())}",
        f"Abandoned carts tracked: {len(memory.abandoned(older_than_sec=0))}",
    ]
    return "\n".join(lines)


def low_stock(repo=None, limit=10):
    from database import get_product_repository
    repo = repo or get_product_repository()
    rows = [p for p in repo.get_all_products() if 0 < int(p.get("stock", 0) or 0) <= 3]
    rows.sort(key=lambda p: int(p.get("stock", 0) or 0))
    return rows[:limit]


def demand_report(n=5) -> str:
    top = get_analytics().top_demand(n)
    if not top:
        return "No unmet demand recorded yet."
    return "Most requested but unavailable:\n" + "\n".join(f"- {q} (x{c})" for q, c in top)


def hidden_opportunities(repo=None) -> str:
    """Patterns a busy seller would miss, each with its evidence count."""
    findings = []
    demand = get_analytics().top_demand(5)
    for query, count in demand:
        if count >= 3:
            findings.append(f"{count} customers asked for '{query}' but it was unavailable. Consider stocking it.")
    low = low_stock(repo)
    for product in low[:3]:
        findings.append(f"'{product.get('name')}' has only {product.get('stock')} left. Restock soon if it sells.")
    abandoned = get_memory().abandoned(older_than_sec=0)
    if abandoned:
        findings.append(f"{len(abandoned)} customers left carts unfinished. A seller follow-up may recover sales.")
    tickets = open_tickets()
    if tickets:
        findings.append(f"{len(tickets)} support conversations need a human. Oldest: {tickets[0]['summary'][:80]}")
    if not findings:
        return "No hidden opportunities detected yet. Keep collecting conversations."
    return "Hidden opportunities:\n" + "\n".join(f"{i + 1}. {f}" for i, f in enumerate(findings))


def why_not_buying(product_name="") -> str:
    """Conversation-drop analysis from recorded demand and funnel data."""
    analytics = get_analytics()
    funnel_note = analytics.daily_summary()
    demand = dict(analytics.top_demand(20))
    target = (product_name or "").lower()
    hits = {q: c for q, c in demand.items() if target and target in q}
    lines = ["Why customers may not be buying (evidence from conversations):"]
    if hits:
        total = sum(hits.values())
        lines.append(f"- {total} conversations mentioned '{product_name}' without a catalog match or stock. "
                     "Check price visibility, stock, and product information.")
    else:
        lines.append("- No repeated unmet demand mentions this product. The drop-off is more likely price, "
                     "delivery, or missing details than availability.")
    lines.append(f"- Funnel today: {funnel_note.splitlines()[1] if len(funnel_note.splitlines()) > 1 else funnel_note}")
    lines.append("Treat this as an insight to verify, not a proven cause.")
    return "\n".join(lines)


def merchandiser_pick(repo=None) -> str:
    """Promote what is demanded, in stock, and not about to run out."""
    from database import get_product_repository
    repo = repo or get_product_repository()
    products = repo.get_all_products()
    if not products:
        return "Catalog is empty. Add products before promoting."
    demand_words = Counter()
    for query, count in get_analytics().top_demand(20):
        for word in query.split():
            demand_words[word] += count
    scored = []
    for product in products:
        stock = int(product.get("stock", 0) or 0)
        if stock <= 3:
            continue
        name = (product.get("name") or "").lower()
        score = sum(demand_words[w] for w in name.split() if w in demand_words)
        scored.append((score, stock, product))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    if not scored:
        return "No product has enough stock to promote safely today."
    best_score, _, best = scored[0]
    if best_score <= 0:
        return ("No clear demand signal today. Best-stocked candidate if you still want "
                f"to promote: {best.get('name')} ({best.get('price_formatted')}), "
                f"{best.get('stock')} in stock.")
    return (f"Promote today: {best.get('name')} ({best.get('price_formatted')}), "
            f"{best.get('stock')} in stock. It matches current customer demand and has safe inventory.")
