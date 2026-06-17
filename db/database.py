import os
import time
from datetime import datetime
from dotenv import load_dotenv

# Import database core execution functions from your main_db file
from db.main_db import execute_query, get_connection

import psycopg2
import psycopg2.extras

# Load environment variables
load_dotenv()

# Fetch Voice ID config safely from .env
VOICE_ID = os.getenv("BOT_AI_VOICE_ID")
if not VOICE_ID:
    print("⚠️ WARNING: 'VOICE_ID' is not set in your .env file.")

# ── SINGLE CLEAN IN-MEMORY CACHE (Unified here for llm.py and server.py) ──
_cache = {}

def cache_set(key, value, expiry=None):
    _cache[key] = value

def cache_get(key, default=None):
    return _cache.get(key, default)

def cache_delete(key):
    _cache.pop(key, None)

def cache_keys(prefix=""):
    return [k for k in _cache.keys() if k.startswith(prefix)]

def cache_ping():
    return True

# ── CACHE MANAGEMENT BUSINESS LOGIC ──

def invalidate_categories_cache():
    """Forces the next request to fetch fresh product data from the database."""
    try:
        cache_delete("global:product_categories")
        print("Product categories cache invalidated")
    except Exception as e:
        print(f"Could not invalidate categories cache: {e}")


# ── PRODUCT CATALOG MANAGEMENT ──

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

# ── CALL STATE TRACKING ──

def set_call_state(call_sid: str, is_speaking: bool, host: str = ""):
    """Initialize call state when stream opens."""
    cache_set(f"call:{call_sid}", {
        "is_speaking":      int(is_speaking),
        "host":             host,
        "resumed_at":       "0",
        "last_activity_at": str(time.time()),
        "call_started_at":  str(time.time()),
    })

def get_call_state(call_sid: str) -> dict:
    """Read full call state from cache. Returns empty dict if not found."""
    state = cache_get(f"call:{call_sid}", {})
    if not state:
        return {}
    return {
        "is_speaking":      bool(int(state.get("is_speaking", 0))),
        "host":             state.get("host", ""),
        "resumed_at":       float(state.get("resumed_at", 0)),
        "last_activity_at": float(state.get("last_activity_at", 0)),
        "call_started_at":  float(state.get("call_started_at", 0)),
    }

def update_call_state(call_sid: str, **kwargs):
    """Dynamically update one or more fields in the call state using a loop."""
    cache_key = f"call:{call_sid}"
    current_state = cache_get(cache_key, {})
    if not current_state:
        return

    transformations = {
        "is_speaking": lambda v: str(int(v)),
        "resumed_at": str,
        "last_activity_at": str,
        "host": str
    }

    for key, value in kwargs.items():
        if key in transformations:
            current_state[key] = transformations[key](value)

    cache_set(cache_key, current_state)

def delete_call_state(call_sid: str):
    """Remove all cache keys for a call when it ends using unified helpers."""
    cache_delete(f"call:{call_sid}")
    cache_delete(f"order_context:{call_sid}")
    cache_delete(f"responses:{call_sid}")
    print(f"[{call_sid}] Cache cleared")

# ── INTENT DETECTION ──

def detect_intent(text):
    """Scans text for strict keyword list matches."""
    text_lower = text.lower()
    
    order_keywords = ["order", "track", "where is my", "status", "delivery", "shipping", "package", "parcel"]
    policy_keywords = ["return", "refund", "exchange", "policy", "guarantee", "warranty"]
    store_keywords = ["store", "location", "hours", "open", "address", "close", "time", "where are you"]
    product_keywords = ["product", "catalog", "buy", "stock", "items", "do you have", "price", "cost", "available"]
    offer_keywords = ["offer", "discount", "deal", "coupon", "sale", "promo"]
    
    if any(keyword in text_lower for keyword in order_keywords):
        return "order_tracking"
    if any(keyword in text_lower for keyword in policy_keywords):
        return "policy_inquiry"
    if any(keyword in text_lower for keyword in store_keywords):
        return "store_info"
    if any(keyword in text_lower for keyword in offer_keywords):
        return "offers_inquiry"
    if any(keyword in text_lower for keyword in product_keywords):
        return "product_inquiry"
        
    return "general_conversation"

# ── CORE CALL METRICS LOGGING ──

def start_call(call_sid: str, caller_number: str):
    """Logs a new incoming call into the database."""
    instruction = """
        INSERT INTO calls (call_sid, caller_number, started_at, status)
        VALUES (%s, %s, %s, 'active')
        ON CONFLICT (call_sid) DO NOTHING
    """
    execute_query(instruction, (call_sid, caller_number, datetime.now()))
    print("Call recorded in database!")

def end_call(call_sid: str):
    """Updates an existing call log to change status to 'ended'."""
    instruction = """
        UPDATE calls 
        SET ended_at = %s, status = 'ended'
        WHERE call_sid = %s
    """
    execute_query(instruction, (datetime.now(), call_sid))

def save_recording(call_sid: str, recording_url: str, recording_sid: str):
    """Saves the audio link to a specific call record."""
    instruction = """
        UPDATE calls 
        SET recording_url = %s, recording_sid = %s
        WHERE call_sid = %s
    """
    execute_query(instruction, (recording_url, recording_sid, call_sid))

# ── TRANSCRIPTION & DIALOGUE STORAGE ──

def save_message(call_sid: str, role: str, content: str):
    """Appends or creates a running text dialogue log block."""
    fetch_instruction = "SELECT conversation FROM messages WHERE call_sid = %s"
    row = execute_query(fetch_instruction, (call_sid,), fetch_mode='one')

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_line = f"[{timestamp}] {role.upper()}: {content}"

    if row:
        updated_conversation = row[0] + "\n" + new_line
        update_instruction = """
            UPDATE messages SET conversation = %s, last_updated = %s
            WHERE call_sid = %s
        """
        execute_query(update_instruction, (updated_conversation, datetime.now(), call_sid))
    else:
        insert_instruction = """
            INSERT INTO messages (call_sid, conversation, last_updated)
            VALUES (%s, %s, %s)
        """
        execute_query(insert_instruction, (call_sid, new_line, datetime.now()))

def get_conversation_history(call_sid: str):
    """Fetches a transcript and parses it into a clean list of dictionaries."""
    instruction = "SELECT conversation FROM messages WHERE call_sid = %s"
    row = execute_query(instruction, (call_sid,), fetch_mode='one')
    if not row or not row[0]:
        return []

    history = []
    for line in row[0].split("\n"):
        try:
            parts = line.split("] ", 1)
            if len(parts) < 2:
                continue
            role_part, content = parts[1].split(": ", 1)
            history.append({"role": role_part.lower(), "content": content})
        except ValueError:
            continue
    return history

def get_all_calls():
    """Retrieves a summarized list of all tracked calls for a dashboard view."""
    instruction = """
        SELECT c.call_sid, c.caller_number, c.started_at, c.ended_at,
               c.status, c.recording_url, COUNT(m.call_sid) as message_count
        FROM calls c
        LEFT JOIN messages m ON c.call_sid = m.call_sid
        GROUP BY c.call_sid, c.caller_number, c.started_at, c.ended_at, c.status, c.recording_url
        ORDER BY c.started_at DESC
    """
    return execute_query(instruction, fetch_mode='all')

def get_call_transcript(call_sid: str):
    """Gets the raw, unparsed string transcript of a call."""
    instruction = "SELECT conversation FROM messages WHERE call_sid = %s"
    row = execute_query(instruction, (call_sid,), fetch_mode='one')
    return row[0] if row else "No transcript available."

# ── SYSTEM CATALOG INFORMATION LOOKUPS ──

def get_product_offers(product_name: str = None, category: str = None):
    """Queries active promotions filtered by name or product category."""
    if product_name:
        instruction = """
            SELECT p.product_name, p.category, p.price, o.offer_name, o.discount_value, o.end_date
            FROM promotions_offers o
            JOIN product_catalog p ON o.product_id = p.product_id
            WHERE o.end_date >= CURRENT_DATE AND LOWER(p.product_name) LIKE LOWER(%s)
            ORDER BY o.discount_value DESC
        """
        params = (f"%{product_name}%",)
    elif category:
        instruction = """
            SELECT p.product_name, p.category, p.price, o.offer_name, o.discount_value, o.end_date
            FROM promotions_offers o
            JOIN product_catalog p ON o.product_id = p.product_id
            WHERE o.end_date >= CURRENT_DATE AND LOWER(p.category) LIKE LOWER(%s)
            ORDER BY o.discount_value DESC
        """
        params = (f"%{category}%",)
    else:
        instruction = """
            SELECT p.product_name, p.category, p.price, o.offer_name, o.discount_value, o.end_date
            FROM promotions_offers o
            JOIN product_catalog p ON o.product_id = p.product_id
            WHERE o.end_date >= CURRENT_DATE
            ORDER BY o.discount_value DESC
        """
        params = ()

    rows = execute_query(instruction, params, fetch_mode='all')
    if not rows:
        return None

    lines = ["ACTIVE OFFERS:"]
    for row in rows:
        prod_name, cat, price, offer, discount, end_date = row
        lines.append(f"  - {prod_name} ({cat}): {offer} — {discount}% off | Valid until {end_date.strftime('%B %d, %Y')}")
    return "\n".join(lines)

def get_return_policy(product_name: str = None):
    """Gets return window timelines for a specific inventory listing."""
    if product_name:
        instruction = """
            SELECT p.product_name, r.return_window_days, r.exchange_allowed, r.policy_description
            FROM return_refund_policies r
            JOIN product_catalog p ON r.product_id = p.product_id
            WHERE LOWER(p.product_name) LIKE LOWER(%s)
        """
        params = (f"%{product_name}%",)
    else:
        instruction = """
            SELECT p.product_name, r.return_window_days, r.exchange_allowed, r.policy_description
            FROM return_refund_policies r
            JOIN product_catalog p ON r.product_id = p.product_id
            LIMIT 1
        """
        params = ()

    row = execute_query(instruction, params, fetch_mode='one')
    if not row:
        return None

    prod_name, return_days, exchange, description = row
    return (
        f"RETURN POLICY for {prod_name}:\n"
        f"  Return window: {return_days} days\n"
        f"  Exchange allowed: {'Yes' if exchange else 'No'}\n"
        f"  Policy: {description}"
    )

def get_warranty(product_name: str = None):
    """Looks up technical support coverage durations."""
    if product_name:
        instruction = """
            SELECT p.product_name, w.warranty_period, w.coverage_details
            FROM warranty_information w
            JOIN product_catalog p ON w.product_id = p.product_id
            WHERE LOWER(p.product_name) LIKE LOWER(%s)
        """
        params = (f"%{product_name}%",)
    else:
        instruction = """
            SELECT p.product_name, w.warranty_period, w.coverage_details
            FROM warranty_information w
            JOIN product_catalog p ON w.product_id = p.product_id
            LIMIT 1
        """
        params = ()

    row = execute_query(instruction, params, fetch_mode='one')
    if not row:
        return None

    prod_name, period, coverage = row
    return f"WARRANTY for {prod_name}:\n  Period: {period}\n  Coverage: {coverage}"

def get_store_info(city: str = None):
    """Retrieves operational trading times formatted nicely for text speech."""
    if city:
        instruction = """
            SELECT store_name, city, opening_time, closing_time
            FROM store_locations WHERE LOWER(city) LIKE LOWER(%s) ORDER BY city
        """
        params = (f"%{city}%",)
    else:
        instruction = """
            SELECT store_name, city, opening_time, closing_time
            FROM store_locations ORDER BY city
        """
        params = ()

    rows = execute_query(instruction, params, fetch_mode='all')
    if not rows:
        return None

    lines = ["STORE LOCATIONS:"]
    for row in rows:
        store_name, city_name, opening, closing = row
        lines.append(f"  - {store_name}, {city_name}: Open {opening.strftime('%I:%M %p')} — {closing.strftime('%I:%M %p')}")
    return "\n".join(lines)

# ── COMPREHENSIVE ORDER CONTEXT AGGREGATION ──

def get_order_context(order_id: int, call_sid: str = None):
    """Assembles an integrated string tracking blueprint of a specific order id."""
    order_query = """
        SELECT o.order_id, o.order_status, o.total_amount, o.created_at,
               c.name, c.phone, c.email, c.address
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        WHERE o.order_id = %s
    """
    order_row = execute_query(order_query, (order_id,), fetch_mode='one')
    if not order_row:
        return None

    order_id_db, order_status, total_amount, created_at, cust_name, cust_phone, cust_email, cust_address = order_row

    items_query = """
        SELECT i.item_name, oi.quantity, oi.price_at_purchase, i.is_available
        FROM order_items oi
        JOIN inventory i ON oi.item_id = i.item_id
        WHERE oi.order_id = %s
    """
    items = execute_query(items_query, (order_id,), fetch_mode='all') or []

    details_query = """
        SELECT p.product_name, p.category, w.warranty_period, r.return_window_days
        FROM order_items oi
        JOIN inventory i ON oi.item_id = i.item_id
        LEFT JOIN product_catalog p ON i.product_id = p.product_id
        LEFT JOIN warranty_information w ON p.product_id = w.product_id
        LEFT JOIN return_refund_policies r ON p.product_id = r.product_id
        WHERE oi.order_id = %s
    """
    product_details = execute_query(details_query, (order_id,), fetch_mode='all') or []

    payment_query = """
        SELECT payment_method, payment_status, amount, paid_at
        FROM payments WHERE order_id = %s ORDER BY paid_at DESC LIMIT 1
    """
    payment = execute_query(payment_query, (order_id,), fetch_mode='one')

    delivery_query = """
        SELECT delivery_status, delivery_address, delivered_at, expected_delivery_date
        FROM deliveries WHERE order_id = %s ORDER BY delivery_id DESC LIMIT 1
    """
    delivery = execute_query(delivery_query, (order_id,), fetch_mode='one')

    lines = [
        f"CUSTOMER NAME: {cust_name}",
        f"CUSTOMER EMAIL: {cust_email}",
        f"CUSTOMER ADDRESS: {cust_address}",
        "",
        f"Order Status: {order_status}",
        f"Order Date: {created_at.strftime('%B %d, %Y') if created_at else 'N/A'}",
        f"Total Amount: ${total_amount}",
        "",
        "ITEMS ORDERED:"
    ]
    
    for item_name, qty, price, is_available in items:
        availability = "In Stock" if is_available else "Out of Stock"
        lines.append(f"  - {item_name} x{qty} @ ${price} each ({availability})")
    
    if product_details:
        lines.append("\nPRODUCT DETAILS:")
        for prod_name, category, warranty, return_days in product_details:
            if prod_name:
                lines.append(f"  - {prod_name} ({category}): Warranty: {warranty or 'N/A'} | Returns: {return_days or 'N/A'} days")

    lines.append("")
    if payment:
        pay_method, pay_status, pay_amount, paid_at = payment
        paid_str = paid_at.strftime('%B %d, %Y') if paid_at else 'Pending'
        lines.append(f"PAYMENT: {pay_method} | Status: {pay_status} | Amount: ${pay_amount} | Date: {paid_str}")
    else:
        lines.append("PAYMENT: No payment record found")

    lines.append("")
    if delivery:
        del_status, del_address, delivered_at, expected_date = delivery
        delivered_str = delivered_at.strftime('%B %d, %Y') if delivered_at else 'Not yet delivered'
        expected_str = expected_date.strftime('%B %d, %Y') if expected_date else 'Not available'
        lines.append(f"DELIVERY: Status: {del_status} | Address: {del_address} | Expected Delivery: {expected_str} | Delivered: {delivered_str}")
    else:
        lines.append("DELIVERY: No delivery record found")

    return "\n".join(lines)

# ── CUSTOMER VOICE ORDER VERIFICATION ──

def save_verified_order(call_sid: str, voice_code: str):
    """Saves or updates a customer's pass-code verification state using EXCLUDED to prevent duplication."""
    instruction = """
        INSERT INTO call_verifications (call_sid, voice_code, verified_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (call_sid) DO UPDATE
        SET voice_code = EXCLUDED.voice_code, verified_at = EXCLUDED.verified_at
    """
    execute_query(instruction, (call_sid, voice_code, datetime.now()))

def get_verified_order(call_sid: str):
    """Checks if a call session has already been security verified."""
    instruction = """
        SELECT voice_code FROM call_verifications
        WHERE call_sid = %s AND verified_at IS NOT NULL
    """
    row = execute_query(instruction, (call_sid,), fetch_mode='one')
    return row[0] if row else None

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
# ── EXPLICIT WRAPPER FOR LLM APP IMPORT ──

def get_order_context_cached(order_id: int, call_sid: str = 'None'):
    """
    Wrapper function to satisfy the import requirement in llm.py.
    Passes the order_id directly to the core aggregation logic.
    """
    return get_order_context(order_id)
# ── EXPLICIT WRAPPER FOR TRANSCRIPT CACHING ──

def get_cached_response(key, default=None):
    """
    Maps the streaming handler's request to the unified in-memory cache system.
    """
    return cache_get(key, default)