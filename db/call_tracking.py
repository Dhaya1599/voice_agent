import time
from db.cache_management import cache_set, cache_get, cache_delete 


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
        # convert the boolean-> int-> str without def
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
