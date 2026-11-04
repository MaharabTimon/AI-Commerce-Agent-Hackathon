from typing import Any, Dict, List, Optional


def main_menu_keyboard() -> Dict[str, Any]:
    """Builds the primary Telegram Seller Control Panel inline keyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "📦 Products", "callback_data": "menu_products:1"},
                {"text": "➕ Add Product", "callback_data": "menu_add_product"}
            ],
            [
                {"text": "🔍 Search", "callback_data": "menu_search"},
                {"text": "📊 Stock", "callback_data": "menu_stock:1"}
            ],
            [
                {"text": "💰 Prices", "callback_data": "menu_prices:1"},
                {"text": "📈 Store Summary", "callback_data": "menu_summary"}
            ],
            [
                {"text": "📥 Import Dataset", "callback_data": "menu_import"},
                {"text": "📤 Export Dataset", "callback_data": "menu_export"}
            ],
            [
                {"text": "🔄 Replace Dataset", "callback_data": "menu_replace"},
                {"text": "❓ Help", "callback_data": "menu_help"}
            ],
            [
                {"text": "❌ Close Panel", "callback_data": "menu_close"}
            ]
        ]
    }


def products_list_keyboard(
    products: List[Dict],
    page: int,
    total_pages: int,
    action_prefix: str = "prod_view",
    nav_prefix: str = "menu_products"
) -> Dict[str, Any]:
    """Generates a paginated list of products with selection buttons."""
    buttons = []

    # Product item buttons
    for p in products:
        p_id = p.get("id")
        p_name = p.get("name", "Product")
        if len(p_name) > 28:
            p_name = p_name[:25] + "..."
        raw_p = p.get('price_bdt')
        try:
            p_val = int(raw_p) if raw_p is not None else 0
        except (ValueError, TypeError):
            p_val = 0
        price = p.get("price_formatted") or f"৳{p_val:,}"
        stock_badge = "✅" if p.get("in_stock", True) else "❌"
        buttons.append([
            {"text": f"{stock_badge} {p_name} ({price})", "callback_data": f"{action_prefix}:{p_id}"}
        ])

    # Pagination navigation row
    nav_row = []
    if page > 1:
        nav_row.append({"text": "⬅️ Prev", "callback_data": f"{nav_prefix}:{page - 1}"})
    nav_row.append({"text": f"📄 {page}/{max(1, total_pages)}", "callback_data": "noop"})
    if page < total_pages:
        nav_row.append({"text": "Next ➡️", "callback_data": f"{nav_prefix}:{page + 1}"})
    buttons.append(nav_row)

    # Action row
    buttons.append([
        {"text": "➕ Add Product", "callback_data": "menu_add_product"},
        {"text": "🔍 Search", "callback_data": "menu_search"}
    ])
    # Main menu back button
    buttons.append([
        {"text": "🏠 Main Menu", "callback_data": "menu_main"}
    ])

    return {"inline_keyboard": buttons}


def product_detail_keyboard(product_id: int, in_stock: bool) -> Dict[str, Any]:
    """Action menu for an individual product."""
    return {
        "inline_keyboard": [
            [
                {"text": "💰 Edit Price", "callback_data": f"prod_edit_price:{product_id}"},
                {"text": "📊 Edit Stock", "callback_data": f"prod_edit_stock:{product_id}"}
            ],
            [
                {"text": "🎨 Variant / Color", "callback_data": f"prod_edit_color:{product_id}"},
                {"text": "📝 Edit Specs", "callback_data": f"prod_edit_specs:{product_id}"}
            ],
            [
                {"text": "🗑 Delete Product", "callback_data": f"prod_delete_prompt:{product_id}"}
            ],
            [
                {"text": "⬅️ Back to Products", "callback_data": "menu_products:1"},
                {"text": "🏠 Main Menu", "callback_data": "menu_main"}
            ]
        ]
    }


def quick_stock_keyboard(product_id: int, current_stock: int) -> Dict[str, Any]:
    """Quick increment/decrement and exact stock buttons."""
    return {
        "inline_keyboard": [
            [
                {"text": "-5", "callback_data": f"stock_adj:{product_id}:-5"},
                {"text": "-1", "callback_data": f"stock_adj:{product_id}:-1"},
                {"text": "+1", "callback_data": f"stock_adj:{product_id}:1"},
                {"text": "+5", "callback_data": f"stock_adj:{product_id}:5"}
            ],
            [
                {"text": "🔴 Out of Stock (0)", "callback_data": f"stock_set_zero:{product_id}"},
                {"text": "🟢 In Stock (10)", "callback_data": f"stock_set_10:{product_id}"}
            ],
            [
                {"text": "✏️ Enter Exact Stock", "callback_data": f"stock_prompt_exact:{product_id}"}
            ],
            [
                {"text": "⬅️ Back to Product", "callback_data": f"prod_view:{product_id}"},
                {"text": "🏠 Main Menu", "callback_data": "menu_main"}
            ]
        ]
    }


def quick_price_keyboard(product_id: int) -> Dict[str, Any]:
    """Percentage adjustment and exact price buttons."""
    return {
        "inline_keyboard": [
            [
                {"text": "-10%", "callback_data": f"price_pct:{product_id}:-10"},
                {"text": "-5%", "callback_data": f"price_pct:{product_id}:-5"},
                {"text": "+5%", "callback_data": f"price_pct:{product_id}:5"},
                {"text": "+10%", "callback_data": f"price_pct:{product_id}:10"}
            ],
            [
                {"text": "✏️ Enter Exact Price", "callback_data": f"price_prompt_exact:{product_id}"}
            ],
            [
                {"text": "⬅️ Back to Product", "callback_data": f"prod_view:{product_id}"},
                {"text": "🏠 Main Menu", "callback_data": "menu_main"}
            ]
        ]
    }


def confirm_delete_keyboard(product_id: int) -> Dict[str, Any]:
    """Confirmation dialog keyboard for deleting a product."""
    return {
        "inline_keyboard": [
            [
                {"text": "❌ Cancel", "callback_data": f"prod_view:{product_id}"},
                {"text": "🗑 Yes, Delete", "callback_data": f"prod_delete_confirm:{product_id}"}
            ]
        ]
    }


def export_format_keyboard() -> Dict[str, Any]:
    """Formats available for catalog export."""
    return {
        "inline_keyboard": [
            [
                {"text": "📄 Raw Text (.txt)", "callback_data": "export_act:txt"},
                {"text": "📁 JSON (.json)", "callback_data": "export_act:json"}
            ],
            [
                {"text": "📊 CSV (.csv)", "callback_data": "export_act:csv"},
                {"text": "🏠 Main Menu", "callback_data": "menu_main"}
            ]
        ]
    }


def back_to_menu_keyboard() -> Dict[str, Any]:
    """Buttons to return to the control panel main menu or close."""
    return {
        "inline_keyboard": [
            [
                {"text": "🏠 Main Menu", "callback_data": "menu_main"},
                {"text": "❌ Close Panel", "callback_data": "menu_close"}
            ]
        ]
    }
