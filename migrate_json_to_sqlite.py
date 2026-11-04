import json
import logging
import os
import sqlite3

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, "products.json")
DB_PATH = os.path.join(BASE_DIR, "products.db")


def migrate():
    if not os.path.exists(JSON_PATH):
        logger.error(f"Cannot find source JSON file: {JSON_PATH}")
        return False

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        products = json.load(f)

    logger.info(f"Loaded {len(products)} products from {JSON_PATH}")

    # Remove existing db file if we want a clean rebuild
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            logger.info(f"Removed previous database file {DB_PATH}")
        except OSError as e:
            logger.warning(f"Could not remove old DB file: {e}")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # Create table schema
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                brand TEXT NOT NULL,
                category TEXT NOT NULL,
                price_bdt INTEGER NOT NULL,
                price_formatted TEXT NOT NULL,
                in_stock INTEGER NOT NULL DEFAULT 1,
                specifications TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_category ON products(category);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_brand ON products(brand);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_cat_brand ON products(category, brand);")

        # FTS5 Virtual Table for full-text search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
                id UNINDEXED,
                name,
                brand,
                category,
                specifications,
                tokenize='porter unicode61'
            );
        """)

        # Insert products
        insert_sql = """
            INSERT INTO products (id, name, brand, category, price_bdt, price_formatted, in_stock, specifications)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        fts_sql = """
            INSERT INTO products_fts (id, name, brand, category, specifications)
            VALUES (?, ?, ?, ?, ?);
        """

        rows = []
        fts_rows = []
        for p in products:
            p_id = p.get("id")
            name = p.get("name", "").strip()
            brand = p.get("brand", "").strip()
            category = p.get("category", "").strip()
            price_bdt = int(p.get("price_bdt", 0))
            price_formatted = p.get("price_formatted", "").strip()
            in_stock = 1 if p.get("in_stock", True) else 0
            specs = p.get("specifications", "").strip()

            rows.append((p_id, name, brand, category, price_bdt, price_formatted, in_stock, specs))
            fts_rows.append((p_id, name, brand, category, specs))

        conn.executemany(insert_sql, rows)
        conn.executemany(fts_sql, fts_rows)

        # Triggers to keep FTS in sync for future inserts/updates/deletes
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_products_insert AFTER INSERT ON products
            BEGIN
                INSERT INTO products_fts(id, name, brand, category, specifications)
                VALUES (new.id, new.name, new.brand, new.category, new.specifications);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_products_delete AFTER DELETE ON products
            BEGIN
                DELETE FROM products_fts WHERE id = old.id;
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_products_update AFTER UPDATE ON products
            BEGIN
                DELETE FROM products_fts WHERE id = old.id;
                INSERT INTO products_fts(id, name, brand, category, specifications)
                VALUES (new.id, new.name, new.brand, new.category, new.specifications);
            END;
        """)

        conn.commit()

        # Verification check
        cursor = conn.execute("SELECT COUNT(*) FROM products;")
        count = cursor.fetchone()[0]
        cursor_fts = conn.execute("SELECT COUNT(*) FROM products_fts;")
        fts_count = cursor_fts.fetchone()[0]

        logger.info(f"Migration completed successfully! Inserted {count} products (FTS index: {fts_count} entries).")
        return True
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
