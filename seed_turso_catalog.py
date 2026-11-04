import os
import sqlite3
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "").rstrip("/")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

if not TURSO_URL or not TURSO_TOKEN:
    raise ValueError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set in .env")

def execute_turso_batch(stmt_list):
    url = f"{TURSO_URL}/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json"
    }
    requests_payload = []
    for stmt in stmt_list:
        sql = stmt.get("sql")
        raw_args = stmt.get("args", [])
        formatted_args = []
        for a in raw_args:
            if a is None:
                formatted_args.append({"type": "null"})
            elif isinstance(a, int):
                formatted_args.append({"type": "integer", "value": str(a)})
            elif isinstance(a, float):
                formatted_args.append({"type": "float", "value": a})
            else:
                formatted_args.append({"type": "text", "value": str(a)})
        
        req = {"type": "execute", "stmt": {"sql": sql}}
        if formatted_args:
            req["stmt"]["args"] = formatted_args
        requests_payload.append(req)

    resp = requests.post(url, headers=headers, json={"requests": requests_payload}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    for res_item in data.get("results", []):
        if res_item.get("type") != "ok":
            error_msg = res_item.get("error", {}).get("message", "Unknown error")
            raise RuntimeError(f"Turso query error: {error_msg}")
    return data

def setup_schema():
    print("Setting up Turso schema...")
    stmts = [
        {"sql": "DROP TABLE IF EXISTS products_fts;"},
        {"sql": "DROP TABLE IF EXISTS products;"},
        {"sql": """
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                brand TEXT NOT NULL DEFAULT 'General',
                category TEXT NOT NULL DEFAULT 'Electronics',
                price_bdt INTEGER NOT NULL,
                price_formatted TEXT NOT NULL,
                in_stock INTEGER NOT NULL DEFAULT 1,
                specifications TEXT DEFAULT '',
                stock INTEGER DEFAULT 1,
                color TEXT DEFAULT '',
                size TEXT DEFAULT '',
                variant TEXT DEFAULT '',
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'ACTIVE',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );
        """},
        {"sql": "CREATE INDEX IF NOT EXISTS idx_prod_category ON products(category);"},
        {"sql": "CREATE INDEX IF NOT EXISTS idx_prod_brand ON products(brand);"},
        {"sql": "CREATE INDEX IF NOT EXISTS idx_prod_cat_brand ON products(category, brand);"},
        {"sql": """
            CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
                id UNINDEXED,
                name,
                brand,
                category,
                specifications,
                tokenize='porter unicode61'
            );
        """},
        {"sql": """
            CREATE TRIGGER IF NOT EXISTS trg_products_insert AFTER INSERT ON products
            BEGIN
                INSERT INTO products_fts(id, name, brand, category, specifications)
                VALUES (new.id, new.name, new.brand, new.category, new.specifications);
            END;
        """},
        {"sql": """
            CREATE TRIGGER IF NOT EXISTS trg_products_delete AFTER DELETE ON products
            BEGIN
                DELETE FROM products_fts WHERE id = old.id;
            END;
        """},
        {"sql": """
            CREATE TRIGGER IF NOT EXISTS trg_products_update AFTER UPDATE ON products
            BEGIN
                DELETE FROM products_fts WHERE id = old.id;
                INSERT INTO products_fts(id, name, brand, category, specifications)
                VALUES (new.id, new.name, new.brand, new.category, new.specifications);
            END;
        """}
    ]
    execute_turso_batch(stmts)
    print("Turso schema, indexes, FTS5 and triggers initialized successfully.")

def seed_catalog():
    # Load from products.db if available, else products.json
    db_file = os.path.join(os.path.dirname(__file__), "products.db")
    products = []
    if os.path.exists(db_file):
        print(f"Reading products from {db_file}...")
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM products ORDER BY id ASC")
        rows = c.fetchall()
        for r in rows:
            products.append(dict(r))
        conn.close()
    else:
        json_file = os.path.join(os.path.dirname(__file__), "products.json")
        print(f"Reading products from {json_file}...")
        with open(json_file, "r", encoding="utf-8") as f:
            products = json.load(f)

    print(f"Found {len(products)} products to seed into Turso.")
    
    insert_sql = """
        INSERT INTO products (
            id, name, brand, category, price_bdt, price_formatted, in_stock, specifications,
            stock, color, size, variant, description, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    batch_size = 50
    total = len(products)
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    for i in range(0, total, batch_size):
        chunk = products[i:i+batch_size]
        batch_stmts = []
        for p in chunk:
            p_id = p.get("id")
            name = str(p.get("name", "")).strip()
            brand = str(p.get("brand", "General")).strip()
            category = str(p.get("category", "Electronics")).strip()
            price_bdt = int(p.get("price_bdt", 0))
            price_formatted = str(p.get("price_formatted") or f"BDT {price_bdt:,}")
            stock = int(p.get("stock", 1))
            in_stock = int(1 if stock > 0 and p.get("in_stock", True) else 0)
            specifications = str(p.get("specifications", "") or "")
            color = str(p.get("color", "") or "")
            size = str(p.get("size", "") or "")
            variant = str(p.get("variant", "") or "")
            description = str(p.get("description", "") or "")
            status = str(p.get("status", "ACTIVE" if in_stock else "OUT_OF_STOCK"))
            created_at = str(p.get("created_at") or now_str)
            updated_at = str(p.get("updated_at") or now_str)

            batch_stmts.append({
                "sql": insert_sql,
                "args": [
                    p_id, name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                    stock, color, size, variant, description, status, created_at, updated_at
                ]
            })
        execute_turso_batch(batch_stmts)
        print(f"Seeded {min(i + batch_size, total)}/{total} products...")

    print("Catalog seeding complete!")

if __name__ == "__main__":
    setup_schema()
    seed_catalog()
