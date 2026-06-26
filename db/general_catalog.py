from db.main_db import execute_query



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
