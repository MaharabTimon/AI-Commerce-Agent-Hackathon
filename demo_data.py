"""Safe hackathon demo mode (spec 96-98). Fake data only.

Run: python demo_data.py
Seeds a throwaway JSON catalog, one demo order, one hot lead, and one
escalation ticket, then prints the six judge scenarios with the exact
messages to send. Nothing here touches a real customer or payment.
"""

DEMO_PRODUCTS = [
    {"id": 1, "name": "Demo Elegant Wedding Dress", "brand": "DemoHouse", "category": "Dresses",
     "price_bdt": 2800, "price_formatted": "BDT 2,800", "stock": 8, "in_stock": True,
     "specifications": "Elegant, not flashy. Sizes S M L. Black and maroon."},
    {"id": 2, "name": "Demo Classic Black Dress", "brand": "DemoHouse", "category": "Dresses",
     "price_bdt": 2300, "price_formatted": "BDT 2,300", "stock": 5, "in_stock": True,
     "specifications": "Black, traditional cut. Sizes M L XL."},
    {"id": 3, "name": "Demo Red Saree Premium", "brand": "DemoHouse", "category": "Sarees",
     "price_bdt": 4500, "price_formatted": "BDT 4,500", "stock": 2, "in_stock": True,
     "specifications": "Red jamdani saree with blouse piece."},
    {"id": 4, "name": "Demo Pearl Earrings", "brand": "DemoHouse", "category": "Accessories",
     "price_bdt": 400, "price_formatted": "BDT 400", "stock": 20, "in_stock": True,
     "specifications": "Pearl drop earrings, gift box included."},
    {"id": 5, "name": "Demo Hidden Sample", "brand": "DemoHouse", "category": "Dresses",
     "price_bdt": 999, "price_formatted": "BDT 999", "stock": 9, "in_stock": True,
     "status": "HIDDEN", "specifications": "Must never be shown to customers."},
]

SCENARIOS = [
    ("1 Natural shopping", "Customer: 'I need something elegant for a wedding under 3000.' "
     "Expect a budget-filtered, occasion-aware answer in the customer's language."),
    ("2 Image search", "Customer sends a dress photo. Expect similar in-stock products, "
     "labeled as similar, never falsely claimed as an exact match."),
    ("3 Conversational checkout", "Customer picks a product, gives size, name, phone, address, "
     "sees a summary, confirms. Expect a CONFIRMED order and a Telegram ticket."),
    ("4 Seller intelligence", "Seller on Telegram: /sales, then 'Why aren't people buying the red saree?' "
     "Expect evidence-based insight with sample sizes."),
    ("5 Hot leads", "Seller: /hotleads. Expect ranked customers by lead score and stage."),
    ("6 Hidden opportunity", "Seller: /opportunities. Expect unmet demand, low-stock risk, "
     "abandoned carts, and open tickets with counts."),
]


def seed_demo():
    from database import JsonProductRepository
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_products.json")
    import json
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DEMO_PRODUCTS, fh, ensure_ascii=False, indent=1)
    repo = JsonProductRepository(path)
    from orders import OrderStore
    orders = OrderStore(repo=repo)
    order = orders.create("demo-customer", [{"id": 2, "name": "Demo Classic Black Dress", "qty": 1}],
                          2300, name="Demo Ayesha", phone="01712345678", address="Uttara, Dhaka")
    from customer_memory import CustomerMemory
    memory = CustomerMemory()
    memory.touch("demo-customer", sentiment_stage="READY_TO_BUY", score=94)
    return path, order, memory


if __name__ == "__main__":
    _path, _order, _memory = seed_demo()
    print(f"Demo catalog seeded: {_path}")
    print(f"Demo order created: {_order['id']} ({_order['state']})")
    print("\nJudge scenarios:")
    for title, text in SCENARIOS:
        print(f"\n{title}\n  {text}")
