from contextlib import contextmanager
import json
import logging
import os
import re
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import requests

import config

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)


def is_serverless() -> bool:
    """
    Returns True if running inside a serverless environment (Vercel, AWS Lambda, etc.)
    where the application deployment directory is mounted read-only.
    """
    return bool(
        os.environ.get("VERCEL")
        or os.environ.get("AWS_LAMBDA")
        or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        or os.environ.get("AWS_EXECUTION_ENV")
        or os.environ.get("LAMBDA_TASK_ROOT")
    )


# Backwards compatibility alias
IS_SERVERLESS = is_serverless()


def _find_catalog_path(filename: str) -> str:
    """Finds bundled catalog file in BASE_DIR or PARENT_DIR."""
    p1 = os.path.join(BASE_DIR, filename)
    if os.path.exists(p1):
        return p1
    p2 = os.path.join(PARENT_DIR, filename)
    if os.path.exists(p2):
        return p2
    return p1


def get_writable_catalog_path(filename: str) -> str:
    """
    Returns a writable path in /tmp on serverless environments, or the local path.
    Guarantees that the parent directory exists and seeds from bundled catalog on first run.
    """
    if is_serverless():
        import tempfile
        tmp_target = os.path.join(tempfile.gettempdir(), filename)
        os.makedirs(os.path.dirname(tmp_target), exist_ok=True)
        if not os.path.exists(tmp_target):
            bundle_src = _find_catalog_path(filename)
            if os.path.exists(bundle_src):
                import shutil
                try:
                    shutil.copy2(bundle_src, tmp_target)
                    logger.info(f"Initialized writable catalog copy in /tmp: {tmp_target}")
                except Exception as e:
                    logger.warning(f"Failed to copy {filename} to /tmp: {e}")
                    if filename.endswith(".json"):
                        try:
                            with open(bundle_src, "r", encoding="utf-8") as src, open(tmp_target, "w", encoding="utf-8") as dst:
                                dst.write(src.read())
                        except Exception:
                            pass
        return tmp_target
    return _find_catalog_path(filename)


DEFAULT_JSON_PATH = get_writable_catalog_path("products.json")
DEFAULT_SQLITE_PATH = get_writable_catalog_path("products.db")


class BaseProductRepository(ABC):
    """
    Abstract Base Class defining the product repository interface.
    Allows seamlessly swapping between JSON files, SQLite, PostgreSQL, etc.
    """

    @abstractmethod
    def get_all_products(self) -> List[Dict]:
        """Returns all products in the catalog."""
        pass

    @abstractmethod
    def get_by_id(self, product_id: int) -> Optional[Dict]:
        """Returns product matching ID or None."""
        pass

    @abstractmethod
    def get_by_category(self, category: str) -> List[Dict]:
        """Returns products in a given category."""
        pass

    @abstractmethod
    def get_by_brand(self, brand: str) -> List[Dict]:
        """Returns products matching a brand."""
        pass

    @abstractmethod
    def get_categories(self) -> List[str]:
        """Returns distinct list of categories."""
        pass

    @abstractmethod
    def get_brands_by_category(self, category: str) -> List[str]:
        """Returns distinct brands in a given category."""
        pass

    @abstractmethod
    def search_lexical(self, query: str, limit: int = 10) -> List[Dict]:
        """Returns products matching lexical/text search."""
        pass

    @abstractmethod
    def add_product(self, product: Dict) -> Dict:
        """Adds a single product and returns the inserted product with ID."""
        pass

    @abstractmethod
    def update_product(self, product_id: int, updates: Dict) -> Optional[Dict]:
        """Updates specific fields of a product."""
        pass

    @abstractmethod
    def delete_product(self, product_id: int) -> bool:
        """Deletes a product by ID."""
        pass

    @abstractmethod
    def bulk_update(self, updates_list: List[Dict]) -> int:
        """Updates multiple products in one transaction."""
        pass

    @abstractmethod
    def replace_all(self, products: List[Dict]) -> int:
        """Replaces the entire product collection."""
        pass

    @abstractmethod
    def get_summary(self) -> Dict:
        """Returns catalog summary statistics."""
        pass

    def try_reserve_stock(self, product_id, qty) -> bool:
        """Atomically take qty units. False when stock is insufficient.

        Default is read-check-write; SQL backends override with a single
        conditional UPDATE so concurrent containers cannot oversell.
        """
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return False
        if qty < 1 or product_id is None:
            return False
        product = self.get_by_id(product_id)
        if product is None:
            return False
        stock = int(product.get("stock", 0) or 0)
        if stock < qty:
            return False
        self.update_product(product_id, {"stock": stock - qty})
        return True

    def release_stock(self, product_id, qty) -> None:
        """Return qty units to stock (best effort, never raises)."""
        try:
            product = self.get_by_id(product_id)
            if product is not None:
                self.update_product(product_id,
                                    {"stock": int(product.get("stock", 0) or 0) + int(qty)})
        except Exception:
            pass

    def get_catalog_fingerprint(self):
        """Cheap catalog-change marker, e.g. (count, max_updated).

        Lets long-lived in-memory indexes detect a catalog replaced by
        another process (serverless containers do not share memory).
        Returns None when the backend cannot provide one cheaply.
        """
        return None


class JsonProductRepository(BaseProductRepository):
    """
    JSON file backed repository.
    Reads products.json and caches products in memory.
    """

    def __init__(self, file_path: Optional[str] = None):
        if is_serverless():
            if not file_path or not os.path.isabs(file_path):
                fname = os.path.basename(file_path) if file_path else "products.json"
                file_path = get_writable_catalog_path(fname)
        else:
            if not file_path:
                file_path = DEFAULT_JSON_PATH or _find_catalog_path("products.json")

        self.file_path = file_path
        self._products: List[Dict] = []
        self._load()

    def get_catalog_fingerprint(self):
        try:
            st = os.stat(self.file_path)
            return (st.st_size, st.st_mtime, len(self._products))
        except OSError:
            return None

    def _load(self):
        if not os.path.exists(self.file_path):
            bundled_path = _find_catalog_path(os.path.basename(self.file_path))
            if os.path.exists(bundled_path):
                try:
                    with open(bundled_path, "r", encoding="utf-8") as f:
                        self._products = json.load(f)
                    self._save()
                    logger.info(f"Loaded {len(self._products)} items from bundled catalog {bundled_path} into {self.file_path}")
                    return
                except Exception as e:
                    logger.warning(f"Error loading bundled catalog: {e}")
            logger.warning(f"JSON catalog not found at {self.file_path}")
            self._products = []
            return

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                self._products = json.load(f)
            logger.info(f"JsonProductRepository loaded {len(self._products)} items from {self.file_path}")
        except Exception as e:
            logger.error(f"Failed to load JSON catalog from {self.file_path}: {e}")
            bundled_path = _find_catalog_path(os.path.basename(self.file_path))
            if os.path.exists(bundled_path):
                try:
                    with open(bundled_path, "r", encoding="utf-8") as f:
                        self._products = json.load(f)
                except Exception:
                    self._products = []
            else:
                self._products = []

    def _save(self):
        dir_name = os.path.dirname(os.path.abspath(self.file_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        tmp_path = f"{self.file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._products, f, indent=2, ensure_ascii=False)
        if os.path.exists(self.file_path):
            os.replace(tmp_path, self.file_path)
        else:
            os.rename(tmp_path, self.file_path)

    @staticmethod
    def _normalize_prod(p: Dict) -> Dict:
        item = dict(p)
        in_stock = bool(item.get("in_stock", True))
        raw_stock = item.get("stock")
        if raw_stock is None:
            item["stock"] = 1 if in_stock else 0
        else:
            item["stock"] = int(raw_stock)
        item["in_stock"] = bool(item["stock"] > 0 and in_stock)
        item.setdefault("color", "")
        item.setdefault("size", "")
        item.setdefault("variant", "")
        item.setdefault("description", "")
        item.setdefault("status", "ACTIVE" if item["in_stock"] else "OUT_OF_STOCK")
        return item

    def get_all_products(self) -> List[Dict]:
        return [self._normalize_prod(p) for p in self._products]

    def get_by_id(self, product_id: int) -> Optional[Dict]:
        for p in self._products:
            if p.get("id") == product_id:
                return self._normalize_prod(p)
        return None

    def get_by_category(self, category: str) -> List[Dict]:
        c_lower = category.lower()
        return [self._normalize_prod(p) for p in self._products if p.get("category", "").lower() == c_lower]

    def get_by_brand(self, brand: str) -> List[Dict]:
        b_lower = brand.lower()
        return [self._normalize_prod(p) for p in self._products if b_lower in p.get("brand", "").lower()]

    def get_categories(self) -> List[str]:
        cats = {p.get("category", "") for p in self._products if p.get("category")}
        return sorted(list(cats))

    def get_brands_by_category(self, category: str) -> List[str]:
        c_lower = category.lower()
        brands = {
            p.get("brand", "")
            for p in self._products
            if p.get("category", "").lower() == c_lower and p.get("brand")
        }
        return sorted(list(brands))

    def search_lexical(self, query: str, limit: int = 10) -> List[Dict]:
        tokens = [t.lower() for t in query.split() if len(t) > 1]
        if not tokens:
            return [dict(p) for p in self._products[:limit]]

        matches = []
        for p in self._products:
            blob = f"{p.get('name', '')} {p.get('brand', '')} {p.get('category', '')} {p.get('specifications', '')}".lower()
            match_count = sum(1 for t in tokens if t in blob)
            if match_count > 0:
                matches.append((match_count, p))

        matches.sort(key=lambda x: x[0], reverse=True)
        return [dict(p) for _, p in matches[:limit]]

    def add_product(self, product: Dict) -> Dict:
        max_id = max((p.get("id", 0) for p in self._products), default=0)
        new_prod = dict(product)
        new_prod["id"] = max_id + 1
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        new_prod.setdefault("created_at", now_str)
        new_prod.setdefault("updated_at", now_str)
        new_prod.setdefault("stock", 1)
        new_prod.setdefault("in_stock", True if new_prod["stock"] > 0 else False)
        new_prod.setdefault("status", "ACTIVE" if new_prod["in_stock"] else "OUT_OF_STOCK")
        if "price_bdt" in new_prod and "price_formatted" not in new_prod:
            new_prod["price_formatted"] = f"{int(new_prod['price_bdt']):,}৳"
        self._products.append(new_prod)
        self._save()
        return dict(new_prod)

    def update_product(self, product_id: int, updates: Dict) -> Optional[Dict]:
        for idx, p in enumerate(self._products):
            if p.get("id") == product_id:
                now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                updated_p = dict(p)
                updated_p.update(updates)
                updated_p["updated_at"] = now_str
                if "stock" in updates:
                    stock_val = int(updates["stock"])
                    updated_p["in_stock"] = stock_val > 0
                    if stock_val == 0:
                        updated_p["status"] = "OUT_OF_STOCK"
                if "price_bdt" in updates:
                    updated_p["price_formatted"] = f"{int(updates['price_bdt']):,}৳"
                self._products[idx] = updated_p
                self._save()
                return dict(updated_p)
        return None

    def delete_product(self, product_id: int) -> bool:
        initial_len = len(self._products)
        self._products = [p for p in self._products if p.get("id") != product_id]
        if len(self._products) < initial_len:
            self._save()
            return True
        return False

    def bulk_update(self, updates_list: List[Dict]) -> int:
        id_to_updates = {u.get("id"): u for u in updates_list if "id" in u}
        modified_count = 0
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        for idx, p in enumerate(self._products):
            p_id = p.get("id")
            if p_id in id_to_updates:
                up = id_to_updates[p_id]
                updated_p = dict(p)
                updated_p.update(up)
                updated_p["updated_at"] = now_str
                if "stock" in up:
                    stock_val = int(up["stock"])
                    updated_p["in_stock"] = stock_val > 0
                if "price_bdt" in up:
                    updated_p["price_formatted"] = f"{int(up['price_bdt']):,}৳"
                self._products[idx] = updated_p
                modified_count += 1
        if modified_count > 0:
            self._save()
        return modified_count

    def replace_all(self, products: List[Dict]) -> int:
        if not products:
            raise ValueError("replace_all refused: empty product list would wipe the catalog")
        if len(products) > MAX_CATALOG_ROWS:
            raise ValueError(f"replace_all refused: more than {MAX_CATALOG_ROWS} rows")
        cleaned = [validate_product(p) for p in products]
        self._products = cleaned
        self._save()
        return len(self._products)

    def get_summary(self) -> Dict:
        total = len(self._products)
        in_stock = sum(1 for p in self._products if p.get("in_stock", True) and p.get("stock", 1) > 0)
        out_of_stock = total - in_stock
        cats = len({p.get("category") for p in self._products if p.get("category")})
        brands = len({p.get("brand") for p in self._products if p.get("brand")})
        total_val = sum(int(p.get("price_bdt", 0)) * max(1, int(p.get("stock", 1))) for p in self._products)
        low_stock = sum(1 for p in self._products if 0 < int(p.get("stock", 1)) <= 3)
        return {
            "total_products": total,
            "in_stock_count": in_stock,
            "out_of_stock_count": out_of_stock,
            "total_categories": cats,
            "total_brands": brands,
            "total_catalog_value": total_val,
            "low_stock_count": low_stock
        }


class SQLiteProductRepository(BaseProductRepository):
    """
    SQLite backed repository with FTS5 full-text search, automatic schema migration,
    and thread-safe connection handling.
    Guarantees persistence to writable /tmp path in serverless environments.
    """

    def __init__(self, db_path: Optional[str] = None):
        if is_serverless():
            if not db_path or not os.path.isabs(db_path):
                fname = os.path.basename(db_path) if db_path else "products.db"
                db_path = get_writable_catalog_path(fname)
        else:
            if not db_path:
                db_path = DEFAULT_SQLITE_PATH or _find_catalog_path("products.db")

        self.db_path = db_path
        dir_name = os.path.dirname(os.path.abspath(self.db_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        self._init_db()

    def get_catalog_fingerprint(self):
        try:
            st = os.stat(self.db_path)
            return (st.st_size, st.st_mtime)
        except OSError:
            return None

    def try_reserve_stock(self, product_id, qty) -> bool:
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return False
        if qty < 1 or product_id is None:
            return False
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE products SET stock = stock - ?, "
                "in_stock = CASE WHEN stock - ? > 0 THEN 1 ELSE 0 END "
                "WHERE id = ? AND stock >= ?",
                (qty, qty, int(product_id), qty))
            conn.commit()
            return cur.rowcount > 0

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
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

            # Non-destructive migrations for extended catalog fields
            new_columns = [
                ("stock", "INTEGER DEFAULT 1"),
                ("color", "TEXT DEFAULT ''"),
                ("size", "TEXT DEFAULT ''"),
                ("variant", "TEXT DEFAULT ''"),
                ("description", "TEXT DEFAULT ''"),
                ("status", "TEXT DEFAULT 'ACTIVE'"),
                ("created_at", "TEXT DEFAULT ''"),
                ("updated_at", "TEXT DEFAULT ''")
            ]
            for col_name, col_def in new_columns:
                try:
                    conn.execute(f"ALTER TABLE products ADD COLUMN {col_name} {col_def};")
                except sqlite3.OperationalError:
                    pass

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

            # Triggers to keep FTS in sync
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

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict:
        keys = row.keys()
        in_stock = bool(row["in_stock"])
        raw_stock = row["stock"] if "stock" in keys else None
        if raw_stock is None:
            stock_val = 1 if in_stock else 0
        else:
            stock_val = int(raw_stock)
        return {
            "id": row["id"],
            "name": row["name"],
            "brand": row["brand"],
            "category": row["category"],
            "price_bdt": row["price_bdt"],
            "price_formatted": f"{row['price_bdt']:,}৳",
            "in_stock": bool(in_stock and stock_val > 0),
            "stock": stock_val,
            "color": row["color"] if ("color" in keys and row["color"] is not None) else "",
            "size": row["size"] if ("size" in keys and row["size"] is not None) else "",
            "variant": row["variant"] if ("variant" in keys and row["variant"] is not None) else "",
            "description": row["description"] if ("description" in keys and row["description"] is not None) else "",
            "status": row["status"] if ("status" in keys and row["status"] is not None) else ("ACTIVE" if (in_stock and stock_val > 0) else "OUT_OF_STOCK"),
            "specifications": row["specifications"] or "",
            "created_at": row["created_at"] if ("created_at" in keys and row["created_at"] is not None) else "",
            "updated_at": row["updated_at"] if ("updated_at" in keys and row["updated_at"] is not None) else ""
        }

    def get_all_products(self) -> List[Dict]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM products ORDER BY id ASC")
            return [self._row_to_dict(r) for r in cursor.fetchall()]

    def get_by_id(self, product_id: int) -> Optional[Dict]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,))
            row = cursor.fetchone()
            return self._row_to_dict(row) if row else None

    def get_by_category(self, category: str) -> List[Dict]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM products WHERE LOWER(category) = LOWER(?) ORDER BY id ASC",
                (category,)
            )
            return [self._row_to_dict(r) for r in cursor.fetchall()]

    def get_by_brand(self, brand: str) -> List[Dict]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM products WHERE LOWER(brand) LIKE LOWER(?) ORDER BY id ASC",
                (f"%{brand}%",)
            )
            return [self._row_to_dict(r) for r in cursor.fetchall()]

    def get_categories(self) -> List[str]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT DISTINCT category FROM products ORDER BY category ASC")
            return [r["category"] for r in cursor.fetchall() if r["category"]]

    def get_brands_by_category(self, category: str) -> List[str]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT DISTINCT brand FROM products WHERE LOWER(category) = LOWER(?) ORDER BY brand ASC",
                (category,)
            )
            return [r["brand"] for r in cursor.fetchall() if r["brand"]]

    def search_lexical(self, query: str, limit: int = 10) -> List[Dict]:
        import re
        tokens = re.findall(r"\w+", query)
        if not tokens:
            return self.get_all_products()[:limit]

        fts_query = " OR ".join(f'"{t}"*' for t in tokens)
        with self._get_connection() as conn:
            try:
                cursor = conn.execute("""
                    SELECT p.*
                    FROM products p
                    JOIN products_fts f ON p.id = f.rowid
                    WHERE products_fts MATCH ?
                    ORDER BY bm25(products_fts)
                    LIMIT ?
                """, (fts_query, int(limit)))
                rows = cursor.fetchall()
                return [self._row_to_dict(r) for r in rows]
            except sqlite3.OperationalError as e:
                logger.warning(f"FTS5 query error: {e}, falling back to LIKE")
                like_clause = " OR ".join(["LOWER(name) LIKE ? OR LOWER(brand) LIKE ?"] * len(tokens))
                params = []
                for t in tokens:
                    params.extend([f"%{t.lower()}%", f"%{t.lower()}%"])
                params.append(int(limit))
                cursor = conn.execute(
                    f"SELECT * FROM products WHERE {like_clause} LIMIT ?",
                    params
                )
                return [self._row_to_dict(r) for r in cursor.fetchall()]

    def add_product(self, product: Dict) -> Dict:
        name = str(product.get("name", "")).strip()
        brand = str(product.get("brand", "")).strip() or "General"
        category = str(product.get("category", "")).strip() or "Electronics"
        price_bdt = int(product.get("price_bdt", 0))
        price_formatted = product.get("price_formatted") or f"{price_bdt:,}৳"
        stock = int(product.get("stock", 1))
        in_stock = int(1 if stock > 0 and product.get("in_stock", True) else 0)
        specifications = str(product.get("specifications", "") or "")
        color = str(product.get("color", "") or "")
        size = str(product.get("size", "") or "")
        variant = str(product.get("variant", "") or "")
        description = str(product.get("description", "") or "")
        status = str(product.get("status", "ACTIVE" if in_stock else "OUT_OF_STOCK"))
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        created_at = product.get("created_at") or now_str
        updated_at = product.get("updated_at") or now_str

        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO products (
                    name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                    stock, color, size, variant, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                stock, color, size, variant, description, status, created_at, updated_at
            ))
            new_id = cursor.lastrowid
            conn.commit()
            return self.get_by_id(new_id)

    def update_product(self, product_id: int, updates: Dict) -> Optional[Dict]:
        existing = self.get_by_id(product_id)
        if not existing:
            return None

        allowed_fields = [
            "name", "brand", "category", "price_bdt", "price_formatted",
            "in_stock", "specifications", "stock", "color", "size",
            "variant", "description", "status"
        ]
        clauses = []
        params = []
        for k, v in updates.items():
            if k in allowed_fields:
                clauses.append(f"{k} = ?")
                if k == "in_stock":
                    params.append(1 if v else 0)
                else:
                    params.append(v)

        if not clauses:
            return existing

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        clauses.append("updated_at = ?")
        params.append(now_str)
        params.append(product_id)

        with self._get_connection() as conn:
            conn.execute(
                f"UPDATE products SET {', '.join(clauses)} WHERE id = ?",
                params
            )
            conn.commit()

        return self.get_by_id(product_id)

    def delete_product(self, product_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
            conn.commit()
            return cursor.rowcount > 0

    def bulk_update(self, updates_list: List[Dict]) -> int:
        modified_count = 0
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            for item in updates_list:
                p_id = item.get("id")
                if not p_id:
                    continue
                allowed_fields = [
                    "name", "brand", "category", "price_bdt", "price_formatted",
                    "in_stock", "specifications", "stock", "color", "size",
                    "variant", "description", "status"
                ]
                clauses = []
                params = []
                for k, v in item.items():
                    if k in allowed_fields:
                        clauses.append(f"{k} = ?")
                        params.append((1 if v else 0) if k == "in_stock" else v)
                if clauses:
                    clauses.append("updated_at = ?")
                    params.append(now_str)
                    params.append(p_id)
                    cur = conn.execute(f"UPDATE products SET {', '.join(clauses)} WHERE id = ?", params)
                    modified_count += cur.rowcount
            conn.commit()
        return modified_count

    def replace_all(self, products: List[Dict]) -> int:
        if not products:
            raise ValueError("replace_all refuses an empty catalog")
        if len(products) > 5000:
            raise ValueError("replace_all refuses more than 5000 products")
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute("DELETE FROM products")
            conn.execute("DELETE FROM products_fts")
            count = 0
            for product in products:
                name = str(product.get("name", "")).strip()
                brand = str(product.get("brand", "")).strip() or "General"
                category = str(product.get("category", "")).strip() or "Electronics"
                price_bdt = int(product.get("price_bdt", 0))
                price_formatted = product.get("price_formatted") or f"{price_bdt:,}৳"
                stock = int(product.get("stock", 1))
                in_stock = int(1 if stock > 0 and product.get("in_stock", True) else 0)
                specifications = str(product.get("specifications", "") or "")
                color = str(product.get("color", "") or "")
                size = str(product.get("size", "") or "")
                variant = str(product.get("variant", "") or "")
                description = str(product.get("description", "") or "")
                status = str(product.get("status", "ACTIVE" if in_stock else "OUT_OF_STOCK"))
                created_at = product.get("created_at") or now_str
                updated_at = product.get("updated_at") or now_str

                conn.execute("""
                    INSERT INTO products (
                        name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                        stock, color, size, variant, description, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                    stock, color, size, variant, description, status, created_at, updated_at
                ))
                count += 1
            conn.commit()
            return count

    def get_summary(self) -> Dict:
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM products")
            total = c.fetchone()[0] or 0

            c.execute("SELECT COUNT(*) FROM products WHERE in_stock = 1 AND (stock > 0 OR stock IS NULL)")
            in_stock = c.fetchone()[0] or 0

            out_of_stock = total - in_stock

            c.execute("SELECT COUNT(DISTINCT category) FROM products WHERE category IS NOT NULL AND category != ''")
            categories_count = c.fetchone()[0] or 0

            c.execute("SELECT COUNT(DISTINCT brand) FROM products WHERE brand IS NOT NULL AND brand != ''")
            brands_count = c.fetchone()[0] or 0

            c.execute("SELECT SUM(price_bdt * (CASE WHEN stock > 0 THEN stock ELSE 1 END)) FROM products")
            total_val = c.fetchone()[0] or 0

            c.execute("SELECT COUNT(*) FROM products WHERE stock > 0 AND stock <= 3")
            low_stock = c.fetchone()[0] or 0

            return {
                "total_products": total,
                "in_stock_count": in_stock,
                "out_of_stock_count": out_of_stock,
                "total_categories": categories_count,
                "total_brands": brands_count,
                "total_catalog_value": int(total_val),
                "low_stock_count": low_stock
            }


class TursoProductRepository(BaseProductRepository):
    """
    Turso Cloud SQLite backed repository with FTS5 full-text search.
    Executes transactions and queries via Turso HTTP pipeline API (/v2/pipeline)
    using zero native binary dependencies (pure Python requests).
    Includes an in-memory cache for fast read operations and instant updates on mutations.
    """

    def __init__(self, db_url: Optional[str] = None, auth_token: Optional[str] = None):
        raw_url = db_url or os.environ.get("TURSO_DATABASE_URL") or getattr(config, "TURSO_DATABASE_URL", "")
        self.db_url = raw_url.strip().rstrip("/")
        if self.db_url and not self.db_url.startswith("http://") and not self.db_url.startswith("https://"):
            self.db_url = f"https://{self.db_url}"
        self.auth_token = (auth_token or os.environ.get("TURSO_AUTH_TOKEN") or getattr(config, "TURSO_AUTH_TOKEN", "")).strip()

        if not self.db_url or not self.auth_token:
            raise ValueError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured.")

        self._session = requests.Session()
        self._cached_products: Optional[List[Dict]] = None
        self._cache_timestamp: float = 0.0
        self._cache_ttl: float = 300.0  # 5 minutes, invalidated immediately on any write
        self._lock = threading.RLock()
        self._init_db()

    def _execute(self, stmt_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._lock:
            url = f"{self.db_url}/v2/pipeline"
            headers = {
                "Authorization": f"Bearer {self.auth_token}",
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
                    elif isinstance(a, bool):
                        formatted_args.append({"type": "integer", "value": "1" if a else "0"})
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

            resp = self._session.post(url, headers=headers, json={"requests": requests_payload}, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            parsed_results = []
            for res_item in data.get("results", []):
                if res_item.get("type") == "ok":
                    exec_res = res_item.get("response", {}).get("result", {})
                    cols = [c["name"] for c in exec_res.get("cols", [])]
                    rows = []
                    for r in exec_res.get("rows", []):
                        row_dict = {}
                        for col_name, val_obj in zip(cols, r):
                            v_type = val_obj.get("type")
                            val = val_obj.get("value")
                            if v_type == "null":
                                row_dict[col_name] = None
                            elif v_type == "integer":
                                row_dict[col_name] = int(val) if val is not None else 0
                            elif v_type == "float":
                                row_dict[col_name] = float(val) if val is not None else 0.0
                            else:
                                row_dict[col_name] = val
                        rows.append(row_dict)
                    parsed_results.append({
                        "rows": rows,
                        "affected_row_count": exec_res.get("affected_row_count", 0),
                        "last_insert_rowid": exec_res.get("last_insert_rowid")
                    })
                else:
                    error_msg = res_item.get("error", {}).get("message", "Unknown error")
                    raise RuntimeError(f"Turso query error: {error_msg}")
            return parsed_results

    def _init_db(self):
        stmts = [
            {"sql": """
                CREATE TABLE IF NOT EXISTS products (
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
                    INSERT INTO products_fts(rowid, name, brand, category, specifications)
                    VALUES (new.id, new.name, new.brand, new.category, new.specifications);
                END;
            """},
            {"sql": """
                CREATE TRIGGER IF NOT EXISTS trg_products_delete AFTER DELETE ON products
                BEGIN
                    DELETE FROM products_fts WHERE rowid = old.id;
                END;
            """},
            {"sql": """
                CREATE TRIGGER IF NOT EXISTS trg_products_update AFTER UPDATE ON products
                BEGIN
                    DELETE FROM products_fts WHERE rowid = old.id;
                    INSERT INTO products_fts(rowid, name, brand, category, specifications)
                    VALUES (new.id, new.name, new.brand, new.category, new.specifications);
                END;
            """}
        ]
        try:
            self._execute(stmts)
        except Exception as e:
            logger.warning(f"Turso _init_db warning (may already be initialized): {e}")

    def invalidate_cache(self):
        with self._lock:
            self._cached_products = None
            self._cache_timestamp = 0.0

    def get_catalog_fingerprint(self):
        """(count, max_updated_at) direct from Turso, bypassing the 5-min cache."""
        try:
            res = self._execute([{"sql": "SELECT COUNT(*), MAX(updated_at), SUM(price_bdt), SUM(stock) FROM products"}])
            rows = res[0]["rows"]
            if not rows:
                return None
            return tuple(rows[0].values())
        except Exception:
            return None
        global _catalog_active_cache
        _catalog_active_cache = None

    def _get_valid_cache(self) -> Optional[List[Dict]]:
        with self._lock:
            if self._cached_products is not None and (time.time() - self._cache_timestamp) < self._cache_ttl:
                return self._cached_products
            return None

    @staticmethod
    def _row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        in_stock = bool(row.get("in_stock", 1))
        raw_stock = row.get("stock")
        if raw_stock is None:
            stock_val = 1 if in_stock else 0
        else:
            stock_val = int(raw_stock)
        price_bdt = int(row.get("price_bdt") or 0)
        price_formatted = row.get("price_formatted") or f"{price_bdt:,}৳"
        return {
            "id": int(row["id"]) if row.get("id") is not None else None,
            "name": str(row.get("name") or ""),
            "brand": str(row.get("brand") or "General"),
            "category": str(row.get("category") or "Electronics"),
            "price_bdt": price_bdt,
            "price_formatted": f"{price_bdt:,}৳",
            "in_stock": bool(in_stock and stock_val > 0),
            "stock": stock_val,
            "color": str(row.get("color") or "") if row.get("color") is not None else "",
            "size": str(row.get("size") or "") if row.get("size") is not None else "",
            "variant": str(row.get("variant") or "") if row.get("variant") is not None else "",
            "description": str(row.get("description") or "") if row.get("description") is not None else "",
            "status": str(row.get("status") or ("ACTIVE" if (in_stock and stock_val > 0) else "OUT_OF_STOCK")),
            "specifications": str(row.get("specifications") or ""),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or "")
        }

    def get_all_products(self) -> List[Dict]:
        cached = self._get_valid_cache()
        if cached is not None:
            return [dict(p) for p in cached]

        res = self._execute([{"sql": "SELECT * FROM products ORDER BY id ASC"}])
        raw_rows = res[0]["rows"]
        parsed = [self._row_to_dict(r) for r in raw_rows]

        with self._lock:
            self._cached_products = parsed
            self._cache_timestamp = time.time()
        return [dict(p) for p in parsed]

    def get_by_id(self, product_id: int) -> Optional[Dict]:
        if product_id is None:
            return None
        cached = self._get_valid_cache()
        if cached is not None:
            for p in cached:
                if p.get("id") == int(product_id):
                    return dict(p)
        res = self._execute([{"sql": "SELECT * FROM products WHERE id = ?", "args": [int(product_id)]}])
        rows = res[0]["rows"]
        return self._row_to_dict(rows[0]) if rows else None

    def get_by_category(self, category: str) -> List[Dict]:
        c_lower = category.lower().strip()
        cached = self._get_valid_cache()
        if cached is not None:
            return [dict(p) for p in cached if p.get("category", "").lower() == c_lower]
        res = self._execute([{"sql": "SELECT * FROM products WHERE LOWER(category) = LOWER(?) ORDER BY id ASC", "args": [category]}])
        return [self._row_to_dict(r) for r in res[0]["rows"]]

    def get_by_brand(self, brand: str) -> List[Dict]:
        b_lower = brand.lower().strip()
        cached = self._get_valid_cache()
        if cached is not None:
            return [dict(p) for p in cached if b_lower in p.get("brand", "").lower()]
        res = self._execute([{"sql": "SELECT * FROM products WHERE LOWER(brand) LIKE LOWER(?) ORDER BY id ASC", "args": [f"%{brand}%"]}])
        return [self._row_to_dict(r) for r in res[0]["rows"]]

    def get_categories(self) -> List[str]:
        cached = self._get_valid_cache()
        if cached is not None:
            cats = {p.get("category", "") for p in cached if p.get("category")}
            return sorted(list(cats))
        res = self._execute([{"sql": "SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category != '' ORDER BY category ASC"}])
        return [r["category"] for r in res[0]["rows"] if r.get("category")]

    def get_brands_by_category(self, category: str) -> List[str]:
        c_lower = category.lower().strip()
        cached = self._get_valid_cache()
        if cached is not None:
            brands = {p.get("brand", "") for p in cached if p.get("category", "").lower() == c_lower and p.get("brand")}
            return sorted(list(brands))
        res = self._execute([{"sql": "SELECT DISTINCT brand FROM products WHERE LOWER(category) = LOWER(?) AND brand IS NOT NULL AND brand != '' ORDER BY brand ASC", "args": [category]}])
        return [r["brand"] for r in res[0]["rows"] if r.get("brand")]

    def search_lexical(self, query: str, limit: int = 10) -> List[Dict]:
        tokens = re.findall(r"\w+", query)
        if not tokens:
            return self.get_all_products()[:limit]

        fts_query = " OR ".join(f'"{t}"*' for t in tokens)
        try:
            res = self._execute([{
                "sql": """
                    SELECT p.*
                    FROM products p
                    JOIN products_fts f ON p.id = f.rowid
                    WHERE products_fts MATCH ?
                    ORDER BY bm25(products_fts)
                    LIMIT ?
                """,
                "args": [fts_query, int(limit)]
            }])
            return [self._row_to_dict(r) for r in res[0]["rows"]]
        except Exception as e:
            logger.warning(f"Turso FTS5 search failed: {e}, falling back to LIKE query")
            like_clause = " OR ".join(["LOWER(name) LIKE ? OR LOWER(brand) LIKE ?"] * len(tokens))
            params = []
            for t in tokens:
                params.extend([f"%{t.lower()}%", f"%{t.lower()}%"])
            params.append(int(limit))
            res = self._execute([{
                "sql": f"SELECT * FROM products WHERE {like_clause} LIMIT ?",
                "args": params
            }])
            return [self._row_to_dict(r) for r in res[0]["rows"]]

    def add_product(self, product: Dict) -> Dict:
        name = str(product.get("name", "")).strip()
        brand = str(product.get("brand", "")).strip() or "General"
        category = str(product.get("category", "")).strip() or "Electronics"
        price_bdt = int(product.get("price_bdt", 0))
        price_formatted = product.get("price_formatted") or f"{price_bdt:,}৳"
        stock = int(product.get("stock", 1))
        in_stock = int(1 if stock > 0 and product.get("in_stock", True) else 0)
        specifications = str(product.get("specifications", "") or "")
        color = str(product.get("color", "") or "")
        size = str(product.get("size", "") or "")
        variant = str(product.get("variant", "") or "")
        description = str(product.get("description", "") or "")
        status = str(product.get("status", "ACTIVE" if in_stock else "OUT_OF_STOCK"))
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        created_at = product.get("created_at") or now_str
        updated_at = product.get("updated_at") or now_str

        res = self._execute([{
            "sql": """
                INSERT INTO products (
                    name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                    stock, color, size, variant, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            "args": [
                name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                stock, color, size, variant, description, status, created_at, updated_at
            ]
        }])
        raw_id = res[0].get("last_insert_rowid")
        new_id = int(raw_id) if (raw_id is not None and str(raw_id).isdigit()) else None
        self.invalidate_cache()
        if new_id:
            created = self.get_by_id(new_id)
            if created:
                return created
        return {
            "id": new_id or 1, "name": name, "brand": brand, "category": category,
            "price_bdt": price_bdt, "price_formatted": f"{price_bdt:,}৳", "in_stock": bool(in_stock),
            "stock": stock, "color": color, "size": size, "variant": variant,
            "description": description, "status": status, "specifications": specifications,
            "created_at": created_at, "updated_at": updated_at
        }

    def update_product(self, product_id: int, updates: Dict) -> Optional[Dict]:
        existing = self.get_by_id(product_id)
        if not existing:
            return None

        updates_copy = dict(updates)
        if "price_bdt" in updates_copy and "price_formatted" not in updates_copy:
            updates_copy["price_formatted"] = f"{int(updates_copy['price_bdt']):,}৳"
        if "stock" in updates_copy and "in_stock" not in updates_copy:
            updates_copy["in_stock"] = int(updates_copy["stock"]) > 0
        if "stock" in updates_copy and int(updates_copy["stock"]) == 0:
            updates_copy["status"] = "OUT_OF_STOCK"

        allowed_fields = [
            "name", "brand", "category", "price_bdt", "price_formatted",
            "in_stock", "specifications", "stock", "color", "size",
            "variant", "description", "status"
        ]
        clauses = []
        params = []
        for k, v in updates_copy.items():
            if k in allowed_fields:
                clauses.append(f"{k} = ?")
                if k == "in_stock":
                    params.append(1 if v else 0)
                else:
                    params.append(v)

        if not clauses:
            return existing

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        clauses.append("updated_at = ?")
        params.append(now_str)
        params.append(int(product_id))

        self._execute([{
            "sql": f"UPDATE products SET {', '.join(clauses)} WHERE id = ?",
            "args": params
        }])
        self.invalidate_cache()
        return self.get_by_id(product_id)

    def delete_product(self, product_id: int) -> bool:
        res = self._execute([{
            "sql": "DELETE FROM products WHERE id = ?",
            "args": [int(product_id)]
        }])
        self.invalidate_cache()
        return res[0]["affected_row_count"] > 0

    def bulk_update(self, updates_list: List[Dict]) -> int:
        modified_count = 0
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        batch_stmts = []
        for item in updates_list:
            p_id = item.get("id")
            if not p_id:
                continue
            item_copy = dict(item)
            if "price_bdt" in item_copy and "price_formatted" not in item_copy:
                item_copy["price_formatted"] = f"{int(item_copy['price_bdt']):,}৳"
            if "stock" in item_copy and "in_stock" not in item_copy:
                item_copy["in_stock"] = int(item_copy["stock"]) > 0

            allowed_fields = [
                "name", "brand", "category", "price_bdt", "price_formatted",
                "in_stock", "specifications", "stock", "color", "size",
                "variant", "description", "status"
            ]
            clauses = []
            params = []
            for k, v in item_copy.items():
                if k in allowed_fields:
                    clauses.append(f"{k} = ?")
                    params.append((1 if v else 0) if k == "in_stock" else v)
            if clauses:
                clauses.append("updated_at = ?")
                params.append(now_str)
                params.append(int(p_id))
                batch_stmts.append({
                    "sql": f"UPDATE products SET {', '.join(clauses)} WHERE id = ?",
                    "args": params
                })
        if batch_stmts:
            res_list = self._execute(batch_stmts)
            modified_count = sum(r["affected_row_count"] for r in res_list)
            self.invalidate_cache()
        return modified_count

    def replace_all(self, products: List[Dict]) -> int:
        if not products:
            raise ValueError("replace_all refuses an empty catalog")
        if len(products) > 5000:
            raise ValueError("replace_all refuses more than 5000 products")
        with self._lock:
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            insert_sql = """
                INSERT INTO products (
                    id, name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                    stock, color, size, variant, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            # If total products <= 500, execute DELETE + all INSERTs in a SINGLE atomic pipeline batch
            if len(products) <= 500:
                batch_stmts = [
                    {"sql": "DELETE FROM products;"},
                    {"sql": "DELETE FROM products_fts;"}
                ]
                for idx, product in enumerate(products, start=1):
                    p_id = product.get("id") or idx
                    name = str(product.get("name", "")).strip()
                    brand = str(product.get("brand", "")).strip() or "General"
                    category = str(product.get("category", "")).strip() or "Electronics"
                    price_bdt = int(product.get("price_bdt", 0))
                    price_formatted = product.get("price_formatted") or f"{price_bdt:,}৳"
                    stock = int(product.get("stock", 1))
                    in_stock = int(1 if stock > 0 and product.get("in_stock", True) else 0)
                    specifications = str(product.get("specifications", "") or "")
                    color = str(product.get("color", "") or "")
                    size = str(product.get("size", "") or "")
                    variant = str(product.get("variant", "") or "")
                    description = str(product.get("description", "") or "")
                    status = str(product.get("status", "ACTIVE" if in_stock else "OUT_OF_STOCK"))
                    created_at = product.get("created_at") or now_str
                    updated_at = product.get("updated_at") or now_str

                    batch_stmts.append({
                        "sql": insert_sql,
                        "args": [
                            p_id, name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                            stock, color, size, variant, description, status, created_at, updated_at
                        ]
                    })
                self._execute(batch_stmts)
                self.invalidate_cache()
                return len(products)
            else:
                batch_size = 250
                for i in range(0, len(products), batch_size):
                    chunk = products[i:i + batch_size]
                    batch_stmts = []
                    if i == 0:
                        batch_stmts.append({"sql": "DELETE FROM products;"})
                        batch_stmts.append({"sql": "DELETE FROM products_fts;"})
                    for idx_offset, product in enumerate(chunk):
                        p_id = product.get("id") or (i + idx_offset + 1)
                        name = str(product.get("name", "")).strip()
                        brand = str(product.get("brand", "")).strip() or "General"
                        category = str(product.get("category", "")).strip() or "Electronics"
                        price_bdt = int(product.get("price_bdt", 0))
                        price_formatted = product.get("price_formatted") or f"{price_bdt:,}৳"
                        stock = int(product.get("stock", 1))
                        in_stock = int(1 if stock > 0 and product.get("in_stock", True) else 0)
                        specifications = str(product.get("specifications", "") or "")
                        color = str(product.get("color", "") or "")
                        size = str(product.get("size", "") or "")
                        variant = str(product.get("variant", "") or "")
                        description = str(product.get("description", "") or "")
                        status = str(product.get("status", "ACTIVE" if in_stock else "OUT_OF_STOCK"))
                        created_at = product.get("created_at") or now_str
                        updated_at = product.get("updated_at") or now_str

                        batch_stmts.append({
                            "sql": insert_sql,
                            "args": [
                                p_id, name, brand, category, price_bdt, price_formatted, in_stock, specifications,
                                stock, color, size, variant, description, status, created_at, updated_at
                            ]
                        })
                    self._execute(batch_stmts)
                self.invalidate_cache()
                return len(products)

    def try_reserve_stock(self, product_id, qty) -> bool:
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return False
        if qty < 1 or product_id is None:
            return False
        try:
            res = self._execute([{
                "sql": "UPDATE products SET stock = stock - ?, "
                       "in_stock = CASE WHEN stock - ? > 0 THEN 1 ELSE 0 END "
                       "WHERE id = ? AND stock >= ?",
                "args": [qty, qty, int(product_id), qty]}])
            ok = bool(res and res[0].get("affected_row_count", 0) > 0)
            if ok:
                self.invalidate_cache()
            return ok
        except Exception:
            return False

    def get_summary(self) -> Dict:
        res = self._execute([{
            "sql": """
                SELECT
                    COUNT(*) as total,
                    COUNT(CASE WHEN in_stock = 1 AND (stock > 0 OR stock IS NULL) THEN 1 END) as in_stock,
                    COUNT(DISTINCT category) as categories_count,
                    COUNT(DISTINCT brand) as brands_count,
                    SUM(price_bdt * (CASE WHEN stock > 0 THEN stock ELSE 1 END)) as total_val,
                    COUNT(CASE WHEN stock > 0 AND stock <= 3 THEN 1 END) as low_stock
                FROM products;
            """
        }])
        row = res[0]["rows"][0] if res and res[0]["rows"] else {}
        total = int(row.get("total") or 0)
        in_stock = int(row.get("in_stock") or 0)
        out_of_stock = total - in_stock
        categories_count = int(row.get("categories_count") or 0)
        brands_count = int(row.get("brands_count") or 0)
        total_val = int(row.get("total_val") or 0)
        low_stock = int(row.get("low_stock") or 0)

        return {
            "total_products": total,
            "in_stock_count": in_stock,
            "out_of_stock_count": out_of_stock,
            "total_categories": categories_count,
            "total_brands": brands_count,
            "total_catalog_value": total_val,
            "low_stock_count": low_stock
        }


_repository_instance: Optional[BaseProductRepository] = None


def get_product_repository(backend: Optional[str] = None) -> BaseProductRepository:
    """
    Factory function returning the configured product repository singleton.
    Priority order:
    1. If backend specified or TURSO_DATABASE_URL + TURSO_AUTH_TOKEN configured, use TursoProductRepository.
    2. If SQLite database exists, use SQLiteProductRepository.
    3. Fall back to JsonProductRepository.
    """
    global _repository_instance
    if _repository_instance is not None:
        return _repository_instance

    chosen_backend = backend or os.environ.get("CATALOG_BACKEND", "").lower().strip()
    turso_url = (os.environ.get("TURSO_DATABASE_URL") or getattr(config, "TURSO_DATABASE_URL", "")).strip()
    turso_token = (os.environ.get("TURSO_AUTH_TOKEN") or getattr(config, "TURSO_AUTH_TOKEN", "")).strip()

    if chosen_backend == "turso" or (chosen_backend not in ("sqlite", "json") and turso_url and turso_token):
        try:
            repo = TursoProductRepository()
            _repository_instance = repo
            logger.info("Initialized TursoProductRepository as active cloud SQLite catalog backend.")
            return _repository_instance
        except Exception as e:
            logger.warning(f"Failed to initialize TursoProductRepository: {e}, falling back to local storage...")

    if chosen_backend == "sqlite":
        _repository_instance = SQLiteProductRepository()
    elif chosen_backend == "json":
        _repository_instance = JsonProductRepository()
    else:
        sqlite_file = get_writable_catalog_path("products.db")
        bundled_sqlite = _find_catalog_path("products.db")
        if (os.path.exists(sqlite_file) and os.path.getsize(sqlite_file) > 0) or (bundled_sqlite and os.path.exists(bundled_sqlite) and os.path.getsize(bundled_sqlite) > 0):
            try:
                repo = SQLiteProductRepository(sqlite_file)
                if repo.get_all_products():
                    _repository_instance = repo
                    return _repository_instance
            except Exception as e:
                logger.warning(f"Failed to initialize SQLite repo: {e}, using JSON fallback")
        _repository_instance = JsonProductRepository()

    return _repository_instance


def reset_product_repository():
    """Resets the singleton repository instance (useful for testing)."""
    global _repository_instance
    _repository_instance = None


_catalog_active_cache: Optional[bool] = None
_catalog_active_cache_ts: float = 0.0


MAX_CATALOG_ROWS = 5000


def validate_product(product: Dict) -> Dict:
    """Validate and normalize one product dict. Raises ValueError."""
    if not isinstance(product, dict):
        raise ValueError("product must be a dict")
    name = str(product.get("name") or "").strip()
    if not name:
        raise ValueError("product name is required")
    out = dict(product)
    out["name"] = name[:200]
    try:
        price = int(float(out.get("price_bdt") or out.get("price") or 0))
    except (TypeError, ValueError):
        raise ValueError("price_bdt must be a number")
    if not (0 <= price <= 10_000_000):
        raise ValueError("price_bdt out of range 0..10000000")
    out["price_bdt"] = price
    try:
        stock = int(out.get("stock", 1))
    except (TypeError, ValueError):
        raise ValueError("stock must be an integer")
    out["stock"] = max(0, min(100000, stock))
    out["in_stock"] = bool(out["stock"] > 0 and out.get("in_stock", True))
    return out


def snapshot_catalog(repo=None) -> Optional[str]:
    """Copy current catalog to a timestamped JSON snapshot. Returns path."""
    import datetime
    repo = repo or get_product_repository()
    try:
        rows = repo.get_all_products()
    except Exception:
        logger.exception("snapshot: catalog read failed")
        return None
    if not rows:
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = get_writable_catalog_path(f"products_backup_{stamp}.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        logger.info("Catalog snapshot saved: %s (%d rows)", path, len(rows))
        return path
    except Exception:
        logger.exception("snapshot write failed")
        return None


def export_catalog_csv(repo=None) -> str:
    """Render the live catalog as CSV text (Telegram export, Phase 3)."""
    import csv
    import io
    repo = repo or get_product_repository()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "name", "brand", "category", "price_bdt", "stock", "in_stock", "specifications"])
    for p in repo.get_all_products():
        writer.writerow([p.get("id", ""), p.get("name", ""), p.get("brand", ""),
                         p.get("category", ""), p.get("price_bdt", ""), p.get("stock", ""),
                         p.get("in_stock", ""), str(p.get("specifications", ""))[:500]])
    return buf.getvalue()


def has_active_catalog(force_refresh: bool = False) -> bool:
    """
    Checks if a valid database is connected and contains at least 1 product.
    Caches result for 10s to eliminate WAN latency on every incoming webhook event.
    Returns False if DB is disconnected, credentials missing, or catalog is empty.
    """
    global _catalog_active_cache, _catalog_active_cache_ts
    now = time.time()
    if not force_refresh and _catalog_active_cache is not None and (now - _catalog_active_cache_ts) < 10.0:
        return _catalog_active_cache

    try:
        repo = get_product_repository()
        summary = repo.get_summary()
        result = bool(summary.get("total_products", 0) > 0)
        _catalog_active_cache = result
        _catalog_active_cache_ts = now
        return result
    except Exception as e:
        logger.warning(f"Catalog availability check failed: {e}")
        return False


