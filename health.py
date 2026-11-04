"""Service health snapshot (spec 80). Fast, cached, no secrets exposed."""
import time

import config

_cache = {"at": 0.0, "data": None}
TTL = 30


def health_snapshot() -> dict:
    now = time.time()
    if _cache["data"] and now - _cache["at"] < TTL:
        return dict(_cache["data"])
    data = {
        "messenger": "configured" if config.PAGE_ACCESS_TOKEN else "missing_token",
        "llama": "configured" if config.META_MODEL_API_KEY else "missing_key",
        "telegram": "configured" if config.TELEGRAM_BOT_TOKEN else "missing_token",
        "database": "unknown",
        "catalog_products": 0,
    }
    try:
        from database import get_product_repository
        summary = get_product_repository().get_summary()
        data["catalog_products"] = int(summary.get("total_products", 0))
        data["database"] = "ok" if data["catalog_products"] > 0 else "empty"
    except Exception as exc:
        data["database"] = f"error: {type(exc).__name__}"
    try:
        import safety
        data["circuit_breaker"] = "tripped" if safety.breaker.is_tripped() else "ok"
    except Exception:
        data["circuit_breaker"] = "unknown"
    _cache.update(at=now, data=data)
    return dict(data)
