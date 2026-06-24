from db.main_db import execute_query
from datetime import datetime
 
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

from datetime import datetime
from db.main_db import execute_query
from db.cache_management import cache_get

from datetime import datetime
from db.main_db import execute_query
from db.cache_management import cache_get

def save_verified_order(call_sid, order_id):
    """
    Saves verified order context by cleanly aligning query placeholders
    with the target columns present in 'calls'.
    """
    # 1. Fetch the phone number from the session cache
    customer_phone = None
    session_data = cache_get(f"auth_state:{call_sid}")
    if session_data:
        customer_phone = session_data.get("customer_phone")
    
    if not customer_phone:
        customer_phone = "UNKNOWN"

    # 2. Insert into the parent 'calls' table with exactly two parameters matching two columns
    parent_instruction = """
        INSERT INTO calls (call_sid, caller_number)
        VALUES (%s, %s)
        ON CONFLICT (call_sid) DO NOTHING;
    """
    try:
        # ✅ Fixed: Tuple now contains exactly 2 parameters to match the 2 columns above
        execute_query(parent_instruction, (call_sid, customer_phone), fetch_mode=None)
        print(f"[{call_sid}] Parent dependency verified with phone: {customer_phone}")
    except Exception as parent_err:
        print(f"[{call_sid}] Parent insertion notice: {parent_err}")

    # 3. Write verification record matching your voice_code column setup
    child_instruction = """
        INSERT INTO call_verifications (call_sid, voice_code, verified_at)
        VALUES (%s, %s, %s);
    """
    try:
        return execute_query(child_instruction, (call_sid, str(order_id), datetime.now()), fetch_mode=None)
    except Exception as child_err:
        print(f"❌ [Database Error] Could not save verification context: {child_err}")
        raise child_err

def get_verified_order(call_sid: str):
    """Checks if a call session has already been security verified."""
    instruction = "SELECT voice_code FROM call_verifications WHERE call_sid = %s"
    row = execute_query(instruction, (call_sid,), fetch_mode='one')
    
    # Extract the string element from the record tuple if it exists
    if row and row[0]:
        return str(row[0])
    return None