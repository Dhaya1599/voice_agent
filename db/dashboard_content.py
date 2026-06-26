from db.cache_management import cache_get
from db.main_db import execute_query
from db.order_context_verify import get_order_context
import re

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

def get_order_context_cached(order_id: int, call_sid: str = 'None'):
    """
    Wrapper function to satisfy the import requirement in llm.py.
    Passes the order_id directly to the core aggregation logic.
    """
    return get_order_context(order_id)

def get_cached_response(key, default=None):
    """
    Maps the streaming handler's request to the unified in-memory cache system.
    """
    return cache_get(key, default)

def get_dashboard_analytics():
    """ Calculate dashboard metrics by aggregating data from the message table """

    faq = """ SELECT primary_intent, COUNT(*) as intent_count FROM messages WHERE primary_intent IS NOT NULL GROUP BY primary_intent ORDER BY intent_count DESC LIMIT 5; """
    faq_query = execute_query(faq, fetch_mode='all') or []
    faqs = [{"intent": row[0], "count": row[1]} for row in faq_query]

    # ── FIXED SQL SYNTAX ERRORS HERE ──
    metrics_instruction = """ 
        SELECT 
            ROUND((COUNT(CASE WHEN csat_score >= 4 THEN 1 END) * 100.0) / NULLIF(COUNT(csat_score), 0), 1) as csat_rate,
            ROUND((COUNT(CASE WHEN human_handoff = TRUE THEN 1 END) * 100.0) / NULLIF(COUNT(*), 0), 1) as escalation_rate
        FROM messages;
    """
    metrics_row = execute_query(metrics_instruction, fetch_mode='one')
    
    csat_rate = metrics_row[0] if metrics_row and metrics_row[0] is not None else 0.0
    escalation_rate = metrics_row[1] if metrics_row and metrics_row[1] is not None else 0.0
    
    return {
        "faqs": faqs,
        "csatPercentage": csat_rate,
        "escalationPercentage": escalation_rate
    }

def parse_csat_from_transcript(transcript: str) -> int:
    """
    Analyzes a raw multi-line voice conversation transcript string 
    and assigns a customer satisfaction score from 1 to 5.
    """
    if not transcript or transcript == "No transcript available.":
        return None
        
    transcript_lower = transcript.lower()
    lines = transcript_lower.split("\n")
    
    # PASS 1: Look for explicit numerical ratings in the last few lines from the USER
    rating_patterns = [
        r"rating\s*(?:is|should\s*be)?\s*([1-5])",
        r"give\s*(?:it)?\s*(?:a)?\s*([1-5])\s*(?:star|out\s*of\s*5)?",
        r"\b([1-5])\s*stars?\b"
    ]
    
    for line in reversed(lines):
        if "user:" in line:
            for pattern in rating_patterns:
                match = re.search(pattern, line)
                if match:
                    return int(match.group(1))

    # PASS 2: Sentiment analysis fallback based on keyword presence ratios
    positive_keywords = ["thank you", "thanks", "great", "awesome", "solved", "perfect", "helpful", "good job"]
    negative_keywords = ["terrible", "bad", "horrible", "useless", "stupid", "annoying", "waste of time", "wrong"]
    
    pos_score = sum(1 for word in positive_keywords if word in transcript_lower)
    neg_score = sum(1 for word in negative_keywords if word in transcript_lower)
    
    if pos_score > neg_score:
        return 5 if (pos_score - neg_score) >= 2 else 4
    elif neg_score > pos_score:
        return 1 if (neg_score - pos_score) >= 2 else 2
        
    return 3