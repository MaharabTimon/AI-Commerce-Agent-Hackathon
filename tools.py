"""Controlled tool layer (spec 44, 45, 83, 100).

AI reasons, tools act, database is the source of truth. The LLM never
touches the database directly: every action goes through a named tool
with a fixed schema, a permission level, optional confirmation, and an
audit entry. Unknown tools and bad arguments fail closed.
"""
import logging
import time
import uuid

from audit import get_audit_log
from business_rules import get_rules

logger = logging.getLogger(__name__)

ROLES = ("customer", "seller", "manager", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}


class ToolError(Exception):
    pass


class Tool:
    def __init__(self, name, fn, min_role="customer", confirm=False, required=()):
        self.name = name
        self.fn = fn
        self.min_role = min_role
        self.confirm = confirm
        self.required = tuple(required)


class ToolRegistry:
    def __init__(self):
        self._tools = {}
        self._pending = {}  # token -> (tool, actor, kwargs)

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def names(self):
        return sorted(self._tools)

    def call(self, name, actor="customer", role="customer", confirmed=False, **kwargs):
        """Execute a tool. Returns dict; never raises to the caller."""
        request_id = uuid.uuid4().hex[:8]
        audit = get_audit_log()
        tool = self._tools.get(name)
        if tool is None:
            audit.record(actor, name, "unknown tool", "denied", request_id)
            return {"ok": False, "error": f"unknown tool: {name}", "request_id": request_id}
        if ROLE_RANK.get(role, 0) < ROLE_RANK.get(tool.min_role, 0):
            audit.record(actor, name, f"role {role} below {tool.min_role}", "denied", request_id)
            return {"ok": False, "error": "permission denied", "request_id": request_id}
        missing = [k for k in tool.required if kwargs.get(k) in (None, "")]
        if missing:
            audit.record(actor, name, f"missing {missing}", "invalid", request_id)
            return {"ok": False, "error": f"missing required: {', '.join(missing)}", "request_id": request_id}
        # Customers may only act on their own cart/orders (no IDOR via a
        # caller-supplied user_id or a sequential order id).
        if ROLE_RANK.get(role, 0) <= 0:
            uid = kwargs.get("user_id")
            if uid is not None and str(uid) != str(actor):
                audit.record(actor, name, "foreign user_id", "denied", request_id)
                return {"ok": False, "error": "permission denied", "request_id": request_id}
            if name in ("get_order", "track_order", "cancel_order", "create_return_request"):
                try:
                    from orders import get_order_store
                    _o = get_order_store().get(str(kwargs.get("order_id")))
                    if _o is not None and str(_o.get("user_id")) != str(actor):
                        audit.record(actor, name, "foreign order", "denied", request_id)
                        return {"ok": False, "error": "permission denied", "request_id": request_id}
                except Exception:
                    pass
        if tool.confirm and not confirmed:
            token = uuid.uuid4().hex[:12]
            if len(self._pending) >= 100:
                self._pending.pop(next(iter(self._pending)))
            self._pending[token] = (tool, actor, kwargs, time.time())
            audit.record(actor, name, "awaiting confirmation", "pending", request_id)
            return {"ok": False, "needs_confirmation": True, "confirm_token": token,
                    "error": f"{name} requires explicit confirmation", "request_id": request_id}
        started = time.time()
        try:
            result = tool.fn(**kwargs)
            audit.record(actor, name, _summarize(kwargs), "ok", request_id)
            return {"ok": True, "result": result, "request_id": request_id,
                    "latency_ms": int((time.time() - started) * 1000)}
        except Exception as exc:
            logger.exception("tool %s failed", name)
            audit.record(actor, name, _summarize(kwargs), f"error: {exc}"[:40], request_id)
            return {"ok": False, "error": "tool failed, please try again", "request_id": request_id}

    def confirm(self, token, actor="customer", role="customer"):
        row = self._pending.pop(token, None)
        if row is None:
            return {"ok": False, "error": "confirmation expired or unknown"}
        tool, stored_actor, kwargs, ts = row
        if str(actor) != str(stored_actor):
            return {"ok": False, "error": "confirmation actor mismatch"}
        if time.time() - ts > 300:
            return {"ok": False, "error": "confirmation expired or unknown"}
        # Execute as the original actor, never with a caller-upgraded role.
        return self.call(tool.name, actor=stored_actor, role=role, confirmed=True, **kwargs)


def _summarize(kwargs) -> str:
    parts = []
    for k, v in list(kwargs.items())[:4]:
        text = str(v)
        parts.append(f"{k}={text[:40]}")
    return ", ".join(parts)


_registry = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def _search_products(query, top_k=4):
    from rag_service import get_rag
    return [{"id": p.get("id"), "name": p.get("name"), "price_bdt": p.get("price_bdt"),
             "price": p.get("price_formatted"), "in_stock": bool(p.get("in_stock", True)),
             "category": p.get("category")} for p in get_rag().search(str(query)[:200], top_k=int(top_k))]


def _get_product(product_id):
    from database import get_product_repository
    product = get_product_repository().get_by_id(int(product_id))
    if product is None:
        raise ToolError("product not found")
    return product


def _check_inventory(product_id):
    product = _get_product(product_id)
    stock = int(product.get("stock", 0) or 0)
    return {"product_id": product.get("id"), "name": product.get("name"), "stock": stock,
            "in_stock": stock > 0, "low_stock": 0 < stock <= 3}


def _get_cart(user_id):
    from cart import get_cart_store
    return get_cart_store().view(str(user_id))


def _add_to_cart(user_id, product_id, qty=1):
    product = _get_product(product_id)
    if not _check_inventory(product_id)["in_stock"]:
        raise ToolError("product is out of stock")
    from cart import get_cart_store
    stock = int(product.get("stock", 0) or 0)
    view = get_cart_store().add(str(user_id), product, qty=max(1, min(int(qty or 1), stock)))
    if view is None:
        raise ToolError("cart is full")
    return view


def _remove_from_cart(user_id, product_id):
    from cart import get_cart_store
    return get_cart_store().remove(str(user_id), product_id)


def _create_order(user_id, name="", phone="", address="", payment="cod"):
    if not str(name or "").strip() or not str(phone or "").strip() or not str(address or "").strip():
        raise ToolError("name, phone and address are required to place an order")
    if str(payment or "cod").strip().lower() == "cod" and not get_rules().cod_enabled:
        raise ToolError("cash on delivery is disabled for this store")
    from cart import get_cart_store
    from orders import get_order_store
    view = get_cart_store().view(str(user_id))
    if not view["items"]:
        raise ToolError("cart is empty")
    ok, reason = get_rules().check_order_value(view["total_bdt"])
    if not ok:
        raise ToolError(reason)
    order = get_order_store().create(str(user_id), view["items"], view["total_bdt"],
                                     name=name, phone=phone, address=address, payment=payment)
    get_cart_store().clear(str(user_id))
    try:
        import conversation_context as _ctx
        _ctx.clear(str(user_id))
    except Exception:
        pass
    try:
        from meta_llama_service import clear_user_history
        clear_user_history(str(user_id))
    except Exception:
        pass
    return order


def _get_order(order_id):
    from orders import get_order_store
    order = get_order_store().get(str(order_id))
    if order is None:
        raise ToolError("order not found")
    return order


def _cancel_order(order_id):
    # Business rule applies to every caller; no kwarg can bypass it.
    ok, reason = get_rules().check_cancel()
    if not ok:
        raise ToolError(reason + ": a seller will review it")
    from orders import get_order_store
    return get_order_store().transition(str(order_id), "CANCELLED")


def _create_return(order_id, reason=""):
    ok, why = get_rules().check_refund()
    from orders import get_order_store
    order = get_order_store().request_return(str(order_id), reason=str(reason)[:200])
    order["human_approval_required"] = not ok
    return order


def _search_knowledge(query):
    from knowledge import get_knowledge_base
    return get_knowledge_base().search(str(query)[:300])


def _create_ticket(user_id, summary, priority="MEDIUM"):
    from sentiment import create_ticket
    return create_ticket(str(user_id), str(summary)[:500], priority)


def _apply_discount(pct):
    ok, reason = get_rules().check_discount(pct)
    if not ok:
        raise ToolError(reason)
    return {"applied_pct": float(pct)}


def _update_price(product_id, new_price):
    from database import get_product_repository
    try:
        price = int(float(new_price))
    except (TypeError, ValueError):
        raise ToolError("new_price must be a number")
    if not (0 < price <= 10_000_000):
        raise ToolError("new_price out of range")
    updated = get_product_repository().update_product(int(product_id), {"price_bdt": price})
    if updated is None:
        raise ToolError("product not found")
    try:
        from rag_service import get_rag
        get_rag().load_and_index()
    except Exception:
        pass
    return updated


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("search_products", _search_products, required=("query",)))
    reg.register(Tool("get_product", _get_product, required=("product_id",)))
    reg.register(Tool("check_inventory", _check_inventory, required=("product_id",)))
    reg.register(Tool("get_cart", _get_cart, required=("user_id",)))
    reg.register(Tool("add_to_cart", _add_to_cart, required=("user_id", "product_id")))
    reg.register(Tool("remove_from_cart", _remove_from_cart, required=("user_id", "product_id")))
    reg.register(Tool("calculate_cart", _get_cart, required=("user_id",)))
    reg.register(Tool("create_order", _create_order, required=("user_id",), confirm=True))
    reg.register(Tool("get_order", _get_order, required=("order_id",)))
    reg.register(Tool("track_order", _get_order, required=("order_id",)))
    reg.register(Tool("cancel_order", _cancel_order, required=("order_id",), confirm=True))
    reg.register(Tool("create_return_request", _create_return, required=("order_id",), confirm=True))
    reg.register(Tool("search_knowledge", _search_knowledge, required=("query",)))
    reg.register(Tool("create_support_ticket", _create_ticket, required=("user_id", "summary")))
    reg.register(Tool("apply_discount", _apply_discount, min_role="seller", required=("pct",), confirm=True))
    reg.register(Tool("update_product_price", _update_price, min_role="seller",
                      required=("product_id", "new_price"), confirm=True))
    return reg
