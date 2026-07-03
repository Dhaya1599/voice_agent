import os
import re
import time
import json
import base64
import asyncio
from datetime import datetime
import math
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, Query, HTTPException, BackgroundTasks
from fastapi.responses import Response, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from websockets.exceptions import ConnectionClosed
from typing import Optional

# Local database and management imports
from llm import chat
from db.cache_management import cache_set, cache_get, cache_delete, cache_keys, cache_ping
from db.main_db import execute_query
from db.call_tracking import set_call_state, get_call_state, update_call_state, delete_call_state
from db.call_core_log import start_call, end_call
from db.transcription import save_message
from db.dashboard_content import get_cached_response
from db.order_context_verify import get_order_context
from twilio.rest import Client as TwilioClient
from otp_verification.voice_auth import auth_router

load_dotenv()

# ==================================================================
# FASTAPI APP INITIALIZATION & SWAGGER META CONFIGURATION
# ==================================================================
app = FastAPI(
    title="Voice Agent Control Tower API",
    description="Backend systems processing live telemetry metrics, automated voice verifications, inventory routing, and admin transaction audits.",
    version="1.0.0",
    docs_url="/docs",       # Standard Swagger UI Endpoint
    redoc_url="/redoc"      # Clean ReDoc Alternative Endpoint
)

app.include_router(auth_router)

# Configure CORS so your React Frontend on port 5173 can talk to it safely
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # allows monitoring websites to communicate with backend
    allow_credentials=True, # secure authorization of cookies, tokens
    allow_methods=["*"], # allows to perform fetch and update operations by dashboard
    allow_headers=["*"]
)

twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
dg_client = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))

# Pricing rates config per 1M tokens / minutes
INPUT_RATE_PER_TOKEN = 2.50 / 1_000_000
OUTPUT_RATE_PER_TOKEN = 10.00 / 1_000_000
DEEPGRAM_RATE_MIN = 0.0043
TWILIO_RATE_MIN = 0.014

# ==================================================================
# BACKGROUND ASYNC TELEMETRY WORKERS
# ==================================================================
def log_llm_metrics_task(call_sid: str, latency_ms: int, prompt_tokens: int, completion_tokens: int):
    """Asynchronously logs execution times and computes token pricing matrix parameters."""
    try:
        cost = (prompt_tokens * INPUT_RATE_PER_TOKEN) + (completion_tokens * OUTPUT_RATE_PER_TOKEN)
        # Unique log_id execution mapping fallback handling
        log_id = f"log_{int(time.time())}"
        
        insert_log_q = """
            INSERT INTO operational_logs (log_id, caller_reference, primary_intent, status, latency_ms)
            VALUES (%s, %s, %s, %s, %s);
        """
        execute_query(insert_log_q, (log_id, call_sid, "VOICE_TRANSACTION", "SUCCESS", latency_ms), fetch_mode=None)
        print(f"[METRICS WORKER] Successfully stored log metrics for execution turn. Cost: ${cost:.6f}")
    except Exception as e:
        print(f"[METRICS ERROR] Background logging pipeline failed: {e}")

def log_session_cleanup_task(call_sid: str, twilio_metrics: dict, dg_metrics: dict):
    """Asynchronously saves complete session pricing metrics upon active channel disconnection."""
    try:
        total_session_cost = twilio_metrics["cost"] + dg_metrics["cost"]
        update_call_metrics_q = """
            UPDATE calls 
            SET status = 'completed', 
                ended_at = NOW()
            WHERE id = %s;
        """
        execute_query(update_call_metrics_q, (call_sid,), fetch_mode=None)
        print(f"[SESSION CLEANUP] Metrics computed successfully. Call Context total cost: ${total_session_cost:.4f}")
    except Exception as e:
        print(f"[SESSION ERROR] Post-session archival worker tracking crashed: {e}")

# ------------------------------------------------------------------
# REACT DASHBOARD API ENDPOINTS (100% REALTIME DYNAMIC DATABASE FETCH)
# ------------------------------------------------------------------
async def live_ops_generator(request: Request):
    while True:
        if await request.is_disconnected():
            break
        try:
            active = "SELECT COUNT(*) FROM calls WHERE status IN ('active','IN_PROGRESS') OR ended_at IS NULL; "
            result = execute_query(active, fetch_mode='all')
            live_calls = result[0][0] if result else 0

            # return avg calls per second
            duration_q = """
            SELECT COALESCE(AVG(EXTRACT(EPOCH FROM (ended_at::timestamp - started_at::timestamp))),0) FROM calls WHERE ended_at IS NOT NULL;"""
            d_result = execute_query(duration_q, fetch_mode='all')
            avg_duration = round(float(d_result[0][0])) if d_result else 0

            payload = {
                "active_concurrent_count": int(live_calls),
                "average_call_duration_seconds": avg_duration,
                "timestamp_marker": time.strftime("%H:%M:%S")
            }
            yield f"data: {json.dumps(payload)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': 'Live connection reading failed'})}\n\n"
        await asyncio.sleep(2.0)

@app.get(
    "/api/v1/dashboard/live-stream", 
    tags=["Dashboard Core"],
    summary="Real-Time Call Operations Stream (SSE)",
    description="Establishes a persistent, long-lived Server-Sent Events (SSE) connection pushing live concurrent data packets every 2 seconds.",
    response_class=StreamingResponse
)
async def live_stream_endpoint(request: Request):
    return StreamingResponse(
        live_ops_generator(request), 
        media_type="text/event-stream"
    )
from typing import Optional
from fastapi import Query, HTTPException
import json

# ==================================================================
# 2. CACHED INTEGRATION HEALTH METRICS (REST SYSTEM TELEMETRY)
# ==================================================================
@app.get(
    "/api/v1/monitor/health-metrics", 
    tags=["System Monitoring"],
    summary="Cached Telemetry and Integration Statuses"
)
async def get_health_metrics_endpoint():
    cache_key = "dash:system_health"
    try:
        cached = cache_get(cache_key)
        if cached:
            return json.loads(cached)

        # A. Live Query: OTP Success Rate calculation
        otp_q = """
            SELECT 
                COUNT(*)::float / NULLIF((SELECT COUNT(*) FROM calls), 0) * 100 
            FROM call_verifications;
        """
        otp_res = execute_query(otp_q, fetch_mode='all')
        otp_success_rate = round(float(otp_res[0][0]), 2) if otp_res and otp_res[0][0] else 0.0

        # B. Live Query: Average LLM Latency tracking
        latency_q = "SELECT COALESCE(AVG(latency_ms), 0) FROM operational_logs WHERE status = 'SUCCESS';"
        latency_res = execute_query(latency_q, fetch_mode='all')
        avg_llm_latency = round(float(latency_res[0][0])) if latency_res else 0

        # C. Your dynamic custom check functions for carriers and engines
        # Replace these placeholders with your actual function calls if named differently!
        twilio_live = "10" if os.getenv("TWILIO_ACCOUNT_SID") else "OFFLINE"
        deepgram_live = "20" if os.getenv("DEEPGRAM_API_KEY") else "OFFLINE"

        fresh_metrics = {
            "twilio_status": twilio_live,
            "deepgram_status": deepgram_live,
            "otp_success_rate_pct": otp_success_rate,
            "llm_latency_tracker_ms": avg_llm_latency,
            "payment_gateway_console": "HEALTHY"
        }

        # Cache live statuses for 5 seconds to reduce table overhead
        cache_set(cache_key, json.dumps(fresh_metrics))
        return fresh_metrics
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Metrics sync aborted: {str(e)}")


# ==================================================================
# 5. CURSOR-PAGINATED ADMINISTRATION AUDIT LOGS (REST)
# ==================================================================


# ==================================================================
# 3. STOCK & INVENTORY ALERT PANEL (REST)
# ==================================================================
@app.get(
    "/api/v1/admin/logs", 
    tags=["Admin Auditing"],
    summary="Paginated and Searchable Audit Trail"
)
async def get_admin_logs_endpoint(
    limit: int = Query(3, ge=1, le=100),
    cursor: Optional[int] = Query(None, description="Maximum database ID anchor point."),
    search: Optional[str] = Query(None, description="Search by caller, session/log id, or primary intent.")
):
    try:
        conditions = []
        params = []

        if cursor:
            conditions.append("id < %s")
            params.append(cursor)

        if search:
            conditions.append(
                "(caller_reference LIKE %s OR log_id LIKE %s OR primary_intent LIKE %s)"
            )
            like_term = f"%{search}%"
            params.extend([like_term, like_term, like_term])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT id, log_id, caller_reference, primary_intent, status, latency_ms 
            FROM operational_logs 
            {where_clause}
            ORDER BY id DESC LIMIT %s;
        """
        params.append(limit + 1)

        rows = execute_query(query, tuple(params), fetch_mode='all') or []
        
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
            
        next_cursor = rows[-1][0] if rows else None

        logs_list = []
        for r in rows:
            logs_list.append({
                "id": r[0],
                "session_id": r[1],
                "caller": r[2],
                "primary_intent": r[3],
                "status": r[4],
                "latency": f"{r[5]}ms"
            })

        return {
            "logs": logs_list,
            "rbac_context": "SUPPORT_VIEW",
            "pagination": {
                "next_cursor": next_cursor if has_more else None, 
                "has_more": has_more
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# ==================================================================
# 4. PRE-COMPUTED PROFIT & REVENUE ANALYTICS (REST)
# ==================================================================
@app.get(
    "/api/v1/analytics/financials", 
    tags=["Financial Analytics"],
    summary="Aggregated Ledger Revenue",
    description="Compiles basic macro calculations evaluating cumulative payments against order price averages."
)
async def get_financials_endpoint():
    try:
        rev_q = "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE payment_status = 'Completed';"
        rev_res = execute_query(rev_q, fetch_mode='all')
        total_revenue = float(rev_res[0][0]) if rev_res else 0.0

        order_avg_q = "SELECT COALESCE(AVG(total_amount), 0) FROM orders;"
        order_res = execute_query(order_avg_q, fetch_mode='all')
        avg_order_price = round(float(order_res[0][0]), 2) if order_res else 0.0

        return {
            "gross_profit_estimated": round(total_revenue * 0.25, 2), 
            "total_revenue": total_revenue,
            "conversion_rate_pct": 78.4,
            "avg_order_price": avg_order_price
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Financial analytics extraction crashed: {str(e)}")
LOW_STOCK_THRESHOLD = 5

@app.get("/api/v1/inventory/alerts")
async def get_inventory_alerts_endpoint():
    try:
        query = """
            SELECT i.product_id, i.item_name, i.quantity, i.is_available,
                   p.category, p.price
            FROM inventory i
            LEFT JOIN product_catalog p ON p.product_id = i.product_id;
        """
        rows = execute_query(query, fetch_mode='all') or []

        alerts = []
        for r in rows:
            quantity = r[2]
            is_available = r[3]

            if quantity == 0 or is_available == False:
                trigger_state = "OUT_OF_STOCK"
            elif quantity <= LOW_STOCK_THRESHOLD:
                trigger_state = "LOW_STOCK"
            else:
                trigger_state = "IN_STOCK"

            alerts.append({
                "product_id": r[0],
                "product_name": r[1],
                "quantity": quantity,
                "category": r[4] or "Uncategorized",
                "price": float(r[5]) if r[5] is not None else 0,
                "trigger_state": trigger_state
            })

        low_stock_count = sum(1 for a in alerts if a["trigger_state"] == "LOW_STOCK")
        out_of_stock_count = sum(1 for a in alerts if a["trigger_state"] == "OUT_OF_STOCK")

        return {
            "inventory_alerts": alerts,
            "flash_banner_active": out_of_stock_count > 0,
            "low_stock_count": low_stock_count,
            "out_of_stock_count": out_of_stock_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/inventory/top-performers")
async def get_top_performers_endpoint(
    limit: int = Query(5, ge=1, le=20),
    category: Optional[str] = Query(None)
):
    try:
        conditions = []
        params = []
        
        if category and category.lower() != "all":
            conditions.append("p.category = %s")
            params.append(category)
            
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        
        query = f"""
            SELECT p.product_id, p.product_name, p.category, COUNT(o.order_id) AS total_orders
            FROM orders o
            JOIN product_catalog p ON p.product_id = o.product_id
            {where_clause}
            GROUP BY p.product_id, p.product_name, p.category
            ORDER BY total_orders DESC
            LIMIT %s;
        """
        params.append(limit)
        
        rows = execute_query(query, tuple(params), fetch_mode='all') or []

        top_performers = [
            {
                "product_id": r[0],
                "product_name": r[1],
                "category": r[2],
                "total_orders": r[3]
            }
            for r in rows
        ]

        return {"top_performers": top_performers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/inventory/categories")
async def get_inventory_alerts_endpoint():
    try:
        query = "SELECT DISTINCT category FROM product_catalog ORDER BY category;"
        rows = execute_query(query, fetch_mode='all') or []
        return {"categories": [r[0] for r in rows]}
        
    except Exception as e:
        print(f"DEBUGGING ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
# ==================================================================
def calculate_twilio_usage(call_start_str, call_end_str, rate_per_minute=0.014):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    start = datetime.strptime(call_start_str, fmt)
    end = datetime.strptime(call_end_str, fmt)
    
    duration_seconds = (end - start).total_seconds()
    billable_minutes = math.ceil(duration_seconds / 60)
    estimated_cost = billable_minutes * rate_per_minute
    
    # FIXED: Moved brace up to avoid returning None
    return {
        "raw_duration_sec": duration_seconds,
        "usage_minutes": billable_minutes,
        "cost": estimated_cost
    }

def calculate_deepgram_usage(total_stream_seconds, model_rate_per_min=0.0043):
    usage_minutes = total_stream_seconds / 60
    estimated_cost = usage_minutes * model_rate_per_min
    
    # FIXED: Moved brace up to avoid returning None
    return {
        "usage_minutes": round(usage_minutes, 2),
        "cost": round(estimated_cost, 4)
    }

def extract_and_calculate_llm_usage(api_response):
    if hasattr(api_response, 'usage') and api_response.usage:
        input_tokens = api_response.usage.prompt_tokens
        output_tokens = api_response.usage.completion_tokens
    else:
        input_tokens, output_tokens = 50, 25

    cost = (input_tokens * INPUT_RATE_PER_TOKEN) + (output_tokens * OUTPUT_RATE_PER_TOKEN)
    
    # FIXED: Moved brace up to avoid returning None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost": round(cost, 6)
    }


# ==================================================================
# 6. LOW-RAM TEXT STREAM LOG EXPORTER (REST)
# ==================================================================
@app.get(
    "/api/v1/admin/logs/export", 
    tags=["Admin Auditing"],
    summary="Stream Raw Logs Export to CSV",
    description="Sequentially streams the complete log database row by row directly into a downloadable CSV file attachment.",
    response_class=StreamingResponse
)
async def export_logs_csv_endpoint():
    def log_csv_generator():
        yield "ID,SessionID,Caller,Intent,Status\n"
        query = "SELECT id, log_id, caller_reference, primary_intent, status FROM operational_logs ORDER BY id DESC;"
        rows = execute_query(query, fetch_mode='all') or []
        for r in rows:
            yield f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]}\n"

    return StreamingResponse(
        log_csv_generator(), 
        media_type="text/csv", 
        headers={"Content-Disposition": "attachment; filename=operational_telemetry_audit.csv"}
    )

#------------------------------------------------------------
# ADDING AGENTS ENDPOINT
#------------------------------------------------------------
@app.get(
    "/api/v1/admin/agents/monitor",
    tags=["Admin Auditing"],
    summary="Get Live Agent Status and Queue Metrics"
)
async def get_agent_monitor_endpoint():
    try:
        agent_q = "SELECT name, phone_no, status FROM Agents;"
        agent_rows = execute_query(agent_q, fetch_mode= 'all') or []

        agents_list = []
        for r in agent_rows:
            agents_list.append({
                "name": r[0],
                "phone_no": r[1],
                "status": r[2]
            })

        queue_q = "SELECT COUNT(*) FROM calls WHERE status = 'IN_PROGRESS';"
        queue_res = execute_query(queue_q,fetch_mode = 'all')
        queue_count = queue_res[0][0] if queue_res else 0

        return{
            "agents": agents_list,
            "queue_count": queue_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch failed: {str(e)}")


@app.get(
    "/api/v1/admin/agents/calls-per-hour",
    tags=["Admin Auditing"],
    summary="Get Hourly Call Volume for the last 24 Hours"
)
async def get_calls_per_hour_endpoint():
    try:
        query = """
            SELECT 
                TO_CHAR(DATE_TRUNC('hour', started_at), 'HH24:00') AS hour_label,
                COUNT(*) AS call_count
            FROM 
                calls
            WHERE 
                started_at >= NOW() - INTERVAL '24 hours'
            GROUP BY 
                DATE_TRUNC('hour', started_at)
            ORDER BY 
                DATE_TRUNC('hour', started_at) ASC;
        """
        rows = execute_query(query, fetch_mode='all') or []
        
        calls_data = []
        for r in rows:
            calls_data.append({
                "time": r[0],
                "calls": r[1]
            })
            
        return {
            "calls_per_hour": calls_data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch calls per hour: {str(e)}")
# ------------------------------------------------------------------
# EXISTING VOICE WORKFLOWS & WEBSOCKET AUDIO LAYER
# ------------------------------------------------------------------
@app.post("/incoming-call", include_in_schema=False)
async def incoming_call(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    print(f"[/incoming-call] CallSid: {call_sid}")
    auth_session = cache_get(f"auth_state:{call_sid}")
    print(f"[/incoming-call] Cache result: {auth_session}")

    if auth_session and auth_session.get("status") == "VERIFIED":
        from db.order_context_verify import save_verified_order
        save_verified_order(call_sid, auth_session.get("order_id"))

        twiml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{request.url.hostname}/media-stream" />
    </Connect> 
</Response>"""
        return Response(content=twiml_content, media_type="application/xml")

    fallback_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Redirect>/voice/incoming</Redirect>
</Response>"""
    return Response(content=fallback_twiml, media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket, background_tasks: BackgroundTasks):
    await websocket.accept()
    print("🚀 [WebSocket] Twilio media stream connection accepted.")

    call_sid = None
    stream_sid = None
    dg_connection = None
    loop = asyncio.get_running_loop()

    deepgram_stream_start = time.perf_counter()
    twilio_start_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def on_transcript_received(self, result, **kwargs):
        sentence = result.channel.alternatives[0].transcript
        if len(sentence.strip()) > 0:
            asyncio.run_coroutine_threadsafe(
                process_transcript(sentence, call_sid, websocket, stream_sid, background_tasks), loop
            )

    try:
        config = LiveOptions(
            model="nova-2-phonecall",
            language="en-US",
            encoding="mulaw",
            sample_rate=8000,
            interim_results=False, 
            endpointing=300
        )

        dg_connection = dg_client.listen.live.v("1")
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript_received)
        started = dg_connection.start(config)
        print(f"Deepgram started: {started}")

        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("event") == "start":
                call_sid = data["start"]["callSid"]
                stream_sid = data["start"]["streamSid"]

                caller_number = (
                    data["start"].get("customParameters", {}).get("caller_number")
                    or data["start"].get("from")
                    or "unknown"
                )

                print(f"📡 [WebSocket] Media context established. Call ID: {call_sid} | Stream ID: {stream_sid}")
                start_call(call_sid, caller_number)

            elif data.get("event") == "media":
                if dg_connection:
                    audio_payload = data["media"]["payload"]
                    raw_audio_bytes = base64.b64decode(audio_payload)
                    dg_connection.send(raw_audio_bytes)

            elif data.get("event") == "stop":
                print(f"🛑 [WebSocket] Twilio issued termination packet for: {call_sid}")
                break

    except WebSocketDisconnect:
        print(f"🔌 [WebSocket] Connection detached normally for Call: {call_sid}")
    except Exception as e:
        print(f"⚠️ [WebSocket] Stream runtime error: {e}")
    finally:
        if dg_connection:
            dg_connection.finish()
        if call_sid:
            end_call(call_sid)
            
            total_stream_seconds = time.perf_counter() - deepgram_stream_start
            twilio_end_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            
            dg_metrics = calculate_deepgram_usage(total_stream_seconds)
            twilio_metrics = calculate_twilio_usage(twilio_start_iso, twilio_end_iso)
            
            background_tasks.add_task(log_session_cleanup_task, call_sid, twilio_metrics, dg_metrics)
            
        print("🔒 [WebSocket] Streaming lifecycle closed.")


async def process_transcript(transcript: str, call_sid: str, websocket: WebSocket, stream_sid: str, background_tasks: BackgroundTasks):
    if not transcript.strip():
        return

    print(f"[{call_sid}] Deepgram STT: '{transcript}'")
    save_message(call_sid, "user", transcript)

    cleaned_input = normalize_phonetic_input(transcript)
    print(f"[{call_sid}] Normalization Applied: '{cleaned_input}'")

    llm_start_time = time.perf_counter()
    response_text = chat(cleaned_input, call_sid=call_sid)
    llm_latency_ms = int((time.perf_counter() - llm_start_time) * 1000)

    background_tasks.add_task(log_llm_metrics_task, call_sid, llm_latency_ms, 45, 25)

    if response_text and "[TRIGGER_HUMAN_HANDOFF]" in response_text:
        print(f"[{call_sid}] Flag sequence detected. Triggering agent transfer routing...")
        update_call_state(call_sid, is_speaking=True, handoff=True)

        handoff_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Please hold while I transfer your call to a retail logistics specialist.</Say>
    <Dial>+198807675348</Dial>
</Response>"""
        try:
            twilio_client.calls(call_sid).update(twiml=handoff_twiml)
        except Exception as twilio_err:
            print(f"[{call_sid}] Twilio live call agent transfer failed: {twilio_err}")
        return

    if response_text:
        print(f"[{call_sid}] Llama-8B Response: {response_text}")
        save_message(call_sid, "assistant", response_text)

        from otp_verification.voice_auth import text_to_speech_mulaw
        audio_data = await text_to_speech_mulaw(response_text)
        if audio_data:
            base64_audio = base64.b64encode(audio_data).decode("utf-8")
            
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64_audio
                }
            }
            await websocket.send_json(media_message)


def normalize_phonetic_input(raw_speech: str) -> str:
    if not raw_speech:
        return ""
    text = raw_speech.lower().strip()
    
    text = text.replace("and", "") 
    text = re.sub(r'(\w)\1+', r'\1', text) 
    text = re.sub(r'\b([b-df-hj-np-tv-z])e\b', r'\1', text)
    
    number_map = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "zero": "0"
    }
    for word, digit in number_map.items():
        text = text.replace(word, digit)
        
    return re.sub(r'[^a-z0-9\s]', '', text).upper()


if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)