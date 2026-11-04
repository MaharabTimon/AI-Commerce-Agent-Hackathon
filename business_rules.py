"""Hard business rules (spec 47). Rules override AI preferences.

Values come from env so each business configures its own limits without
code changes. Every check returns (allowed, reason) and never raises.
"""
import os


def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


class BusinessRules:
    def __init__(self):
        self.max_discount_pct = _env_int("MAX_DISCOUNT_PCT", 10)
        self.refund_requires_human = _env_bool("REFUND_REQUIRES_HUMAN", True)
        self.ai_can_cancel_order = _env_bool("AI_CAN_CANCEL_ORDER", False)
        self.ai_can_modify_address = _env_bool("AI_CAN_MODIFY_ADDRESS", False)
        self.min_order_value_bdt = _env_int("MIN_ORDER_VALUE", 300)
        self.cod_enabled = _env_bool("COD_ENABLED", True)
        self.delivery_charge_dhaka = _env_int("DELIVERY_CHARGE_DHAKA", 60)
        self.delivery_charge_outside = _env_int("DELIVERY_CHARGE_OUTSIDE", 120)
        self.return_window_days = _env_int("RETURN_WINDOW_DAYS", 7)
        self.max_followups_per_week = _env_int("MAX_FOLLOWUPS_PER_WEEK", 2)

    def check_discount(self, pct) -> tuple:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            return False, "discount is not a number"
        if pct < 0:
            return False, "discount cannot be negative"
        if pct > self.max_discount_pct:
            return False, f"discount {pct:g}% exceeds max {self.max_discount_pct}%: needs seller approval"
        return True, ""

    def check_order_value(self, total_bdt) -> tuple:
        try:
            total = int(total_bdt)
        except (TypeError, ValueError):
            return False, "order total is not a number"
        if total < self.min_order_value_bdt:
            return False, f"order total BDT {total} is below minimum BDT {self.min_order_value_bdt}"
        return True, ""

    def check_refund(self) -> tuple:
        if self.refund_requires_human:
            return False, "refunds require human approval"
        return True, ""

    def check_cancel(self) -> tuple:
        if not self.ai_can_cancel_order:
            return False, "order cancellation requires seller approval"
        return True, ""

    def delivery_charge(self, address: str) -> int:
        text = (address or "").lower()
        return self.delivery_charge_dhaka if "dhaka" in text or "ঢাকা" in text else self.delivery_charge_outside

    def summary(self) -> str:
        return (
            f"Max discount {self.max_discount_pct}% | Min order BDT {self.min_order_value_bdt} | "
            f"COD {'on' if self.cod_enabled else 'off'} | Refund human approval: {self.refund_requires_human} | "
            f"AI cancel: {self.ai_can_cancel_order} | Return window {self.return_window_days}d | "
            f"Delivery Dhaka BDT {self.delivery_charge_dhaka}, outside BDT {self.delivery_charge_outside}")


_rules = None


def get_rules() -> BusinessRules:
    global _rules
    if _rules is None:
        _rules = BusinessRules()
    return _rules
