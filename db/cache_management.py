import os

from dotenv import load_dotenv


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


