import db.main_db import execute_query
from cache_management import invalidate_categories_cache

def add_product(product_name, category, price, stock_available=True):
    """Add a new product and immediately invalidate categories cache."""
    instruction = """
        INSERT INTO product_catalog (product_name, category, price, stock_available)
        VALUES (%s, %s, %s, %s)
    """
    execute_query(instruction, (product_name, category, price, stock_available))
    invalidate_categories_cache()
    print(f"Product '{product_name}' added and cache invalidated")

def update_product_availability(product_id, is_available):
    """Update stock availability and invalidate cache."""
    instruction = """
        UPDATE product_catalog
        SET stock_available = %s
        WHERE product_id = %s
    """
    execute_query(instruction, (is_available, product_id))
    invalidate_categories_cache()

def get_product_categories():
    """Fetches an organized overview of active categories and items."""
    instruction = """
        SELECT category, string_agg(product_name, ', ' ORDER BY product_name) AS products
        FROM product_catalog
        WHERE stock_available = true
        GROUP BY category
        ORDER BY category
    """
    rows = execute_query(instruction, fetch_mode='all')
    if not rows:
        return ""

    lines = ["CATEGORIES AND PRODUCTS WE CURRENTLY CARRY:"]
    for category, products in rows:
        lines.append(f"- {category}: {products}")
    return "\n".join(lines)