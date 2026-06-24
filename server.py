from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
import json
import base64
import asyncio
import os
import re
import time
import random
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv
from llm import chat
from websockets.exceptions import ConnectionClosed

# Database core and handler utility imports
from db.cache_management import cache_set, cache_get, cache_delete, cache_ping
from db.main_db import execute_query
from db.call_tracking import set_call_state, get_call_state, update_call_state, delete_call_state
from db.call_core_log import start_call, end_call
from db.transcription import save_message
from db.dashboard_content import get_cached_response
from db.order_context_verify import get_verified_order, save_verified_order

from twilio.rest import Client as TwilioClient
from sms_handler import sms_router
from call_back import trigger_callback_outbound

load_dotenv()

app = FastAPI()
app.include_router(sms_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# ════════════════════════════════════════════════════
# Dead call monitor & Spoken order ID helpers
# ════════════════════════════════════════════════════

async def monitor_dead_calls():
    return

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(monitor_dead_calls())

def spoken_to_order_id(text: str) -> str:
    text = text.lower().strip()
    multiplier_map = {
        'double': 2, 'twice': 2, 'triple': 3, 'thrice': 3, 'quadruple': 4, 'quad': 4, 'quintuple': 5
    }
    for word, times in multiplier_map.items():
        pattern = rf'\b{word}\s+(zero|one|two|three|four|five|six|seven|eight|nine|oh)\b'
        text = re.sub(pattern, lambda m: ' '.join([m.group(1)] * times), text)

    word_to_digit = {
        'zero': '0', 'oh': '0', 'one': '1', 'won': '1', 'two': '2', 'to': '2', 'too': '2',
        'three': '3', 'four': '4', 'for': '4', 'fore': '4', 'five': '5', 'six': '6',
        'seven': '7', 'eight': '8', 'ate': '8', 'nine': '9'
    }
    for word, digit in sorted(word_to_digit.items(), key=lambda x: -len(x[0])):
        text = re.sub(r'\b' + word + r'\b', digit, text)
    return re.sub(r'[^0-9]', '', text)

def get_play_block(text, host):
    safe_text = text.replace("'", "").replace('"', "")
    return f'<Say voice="Polly.Joanna">{safe_text}</Say>'

def build_transfer_twiml(host, call_sid):
    # UNTOUCHED: Kept exactly as requested to ensure it works perfectly
    transfer_number = os.getenv("FORWARDING_CENTER_NUMBER") or "+18885550199" 
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Please hold while we connect you to a support desk coordinator.</Say>
    <Dial>{transfer_number}</Dial>
</Response>"""

def build_response_twiml(text, host, call_sid, verified=False):
    play_block = get_play_block(text, host)
    if verified:
        gather = f"""<Gather input="speech" action="https://{host}/handle-speech?call_sid={call_sid}" method="POST" speechTimeout="auto" timeout="15" language="en-IN" speechModel="phone_call">{play_block}</Gather>"""
    else:
        gather = f"""<Gather input="dtmf" action="https://{host}/handle-order-id?call_sid={call_sid}" method="POST" timeout="10" finishOnKey="#">{play_block}</Gather>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?><Response>{gather}</Response>"""

# ════════════════════════════════════════════════════
# Core transcript processor
# ════════════════════════════════════════════════════

async def process_transcript(call_sid: str, transcript: str, host: str, websocket: WebSocket):
    print(f"[{call_sid}] Deepgram STT: '{transcript}'")
    update_call_state(call_sid, is_speaking=True, last_activity_at=time.time())

    clean_transcript = transcript.strip().lower().replace(".", "").replace(",", "")
    
    # 1. IMMEDIATE TRANSFERENCE OVERRIDE (For user phrases)
    user_wants_human = any(phrase in clean_transcript for phrase in [
        "connect to human", "talk to a human", "speak to a person", "connect with a person",
        "human agent", "representative", "supervisor", "transfer me", "human", "person"
    ])

    if user_wants_human:
        print(f"[{call_sid}] User keyword match detected. Routing directly to human agent...")
        twiml = build_transfer_twiml(host, call_sid)
        try:
            twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
            twilio_client.calls(call_sid).update(twiml=twiml)
        except Exception as e:
            print(f"[{call_sid}] Transfer failed: {e}")
        return

    # Handle goodbye/exit conditions
    goodbye_words = ["goodbye", "bye", "thank you", "thanks", "that's all"]
    if any(word in clean_transcript for word in goodbye_words):
        print(f"[{call_sid}] Goodbye detected. Rerouting call session to automated survey.")
        try:
            twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
            twilio_client.calls(call_sid).update(url=f"https://{host}/voice/trigger-survey", method="POST")
        except Exception as e:
            print(f"[{call_sid}] Twilio REST survey redirection failed: {e}")
            end_call(call_sid)
        return

    # Check cache matching
    cache_key = f"response_cache:{call_sid}:{transcript}"
    response_text = get_cached_response(cache_key)

    if response_text:
        save_message(call_sid, "user", transcript)
        save_message(call_sid, "assistant", response_text)
    else:
        try:
            # Query LLM pipeline 
            response_text = chat(transcript, call_sid=call_sid)
        except Exception as e:
            print(f"[{call_sid}] LLM error encountered: {e}")
            response_text = None
        
        if response_text:
            cache_set(cache_key, response_text)

    # 2. INTENT HANDOFF MATCH: If LLM returned None (because of a hard trigger block detection)
    if not response_text:
        print(f"[{call_sid}] LLM execution returned empty handoff context. Redirecting to human line...")
        twiml = build_transfer_twiml(host, call_sid)
        try:
            twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
            twilio_client.calls(call_sid).update(twiml=twiml)
        except Exception as e:
            print(f"[{call_sid}] Active phone transfer loop failed: {e}")
        return

    # 3. Stream regular answers back if no escalation criteria was triggered
    try:
        twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        play_block = get_play_block(response_text, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start><Stream url="wss://{host}/media-stream"><Parameter name="call_sid" value="{call_sid}"/></Stream></Start>
    {play_block}
    <Pause length="120"/>
</Response>"""
        twilio_client.calls(call_sid).update(twiml=twiml)
    except Exception as e:
        print(f"[{call_sid}] TwiML injection failed: {e}")

    word_count = len(response_text.split())
    duration = max(2.0, (word_count / 150) * 60)
    await asyncio.sleep(duration)

    update_call_state(call_sid, is_speaking=False, resumed_at=time.time(), last_activity_at=time.time())

# ════════════════════════════════════════════════════
# Call routes
# ════════════════════════════════════════════════════

@app.post("/incoming-call")
async def incoming_call(request: Request):
    host = request.headers.get("host")
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "unknown")
    caller_number = form_data.get("From", "unknown")
    
    start_call(call_sid, caller_number)

    greeting = "Hello! Welcome to customer support, This is maya at your service. Please say or type your 4-digit Order ID clearly."
    play_block = get_play_block(greeting, host)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="1"/>
    <Gather input="speech dtmf" action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt=1" method="POST" speechTimeout="auto" timeout="15" language="en-IN" finishOnKey="#">
        {play_block}
    </Gather>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

@app.post("/call-status")
async def handle_call_status(request: Request):
    return {"status": "tracked"}

@app.post("/handle-order-id")
async def handle_order_id(request: Request, call_sid: str = ""):
    host = request.headers.get("host")
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")
    attempt = int(request.query_params.get("attempt", "1"))
    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    speech = form_data.get("SpeechResult", "").strip()

    if digits:
        return await process_order_id(digits, call_sid, host)

    if speech:
        order_id_str = spoken_to_order_id(speech)
        if not order_id_str or len(order_id_str) != 4:
            return await ask_again(call_sid, host, attempt, f"I heard {order_id_str or 'nothing'} which doesn't look like a valid 4 digit order ID")
        cache_set(f"pending_order:{call_sid}", order_id_str)
        spaced = ' '.join(list(order_id_str))
        play_block = get_play_block(f"I heard order ID {spaced}. Is that correct? Say yes to confirm or no to try again.", host)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}" method="POST" speechTimeout="auto" timeout="15" language="en-IN" speechModel="phone_call" hints="yes, no, correct, wrong">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}&amp;timeout=true</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")
    return await ask_again(call_sid, host, attempt, "didn't receive any input")

async def ask_again(call_sid: str, host: str, attempt: int, reason: str):
    if attempt >= 3:
        play_block = get_play_block("No problem. Please type your Order ID on the keypad and press the hash key.", host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Gather input="dtmf" action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={attempt}" method="POST" timeout="20" finishOnKey="#">{play_block}</Gather></Response>"""
    else:
        next_attempt = attempt + 1
        play_block = get_play_block(f"Sorry, I {reason}. Please say your Order ID again, digit by digit.", host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Gather input="speech dtmf" action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={next_attempt}" method="POST" speechTimeout="auto" timeout="15" language="en-IN" finishOnKey="#">{play_block}</Gather></Response>"""
    return Response(content=twiml, media_type="text/xml")

async def process_order_id(order_id_str: str, call_sid: str, host: str):
    from db.order_context_verify import get_order_context
    
    # 1. Check if the user is already fully OTP verified
    verified = get_verified_order(call_sid) is not None

    if verified:
        # If already OTP authenticated, talk to the LLM normally
        llm_reply = chat(order_id_str, call_sid=call_sid)
        set_call_state(call_sid, is_speaking=False, host=host)
        play_block = get_play_block(llm_reply, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start><Stream url="wss://{host}/media-stream"><Parameter name="call_sid" value="{call_sid}"/></Stream></Start>
    {play_block}
    <Pause length="120"/>
</Response>"""
        return Response(content=twiml, media_type="text/xml")
        
    else:
        # 2. Inbound Validation: Query the database directly to check if the order exists
        order_context = get_order_context(int(order_id_str)) if order_id_str.isdigit() else None
        
        if order_context:
            print(f"[{call_sid}] Valid Inbound Order ID {order_id_str} located in DB. Initiating OTP...")
            
            # Generate a 4-digit token
            otp_code = str(random.randint(1000, 9999))
            
            # Save the OTP temporarily to verify against on the next step
            save_verified_order(call_sid, otp_code)
            
            # Remember the valid order ID so we can assign it once OTP passes
            cache_set(f"authenticated_order_id:{call_sid}", order_id_str)
            
            human_number = os.getenv("HUMAN_AGENT_NUMBER")
            twilio_number = os.getenv("TWILIO_PHONE_NUMBER")
            
            try:
                if human_number and twilio_number:
                    twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
                    twilio_client.messages.create(
                        to=human_number,
                        from_=twilio_number,
                        body=f"Your secure verification token is: {otp_code}. Enter this code onto your phone keypad."
                    )
                    print(f"[{call_sid}] OTP text sent to {human_number}")
            except Exception as sms_err:
                print(f"[{call_sid}] Inbound verification SMS failed: {sms_err}")

            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="1"/>
    <Gather input="dtmf" action="https://{host}/voice/verify-otp?call_sid={call_sid}" method="POST" timeout="15" numDigits="4">
        <Say voice="Polly.Joanna">Order located. A security authentication code has been texted to your device. Please enter the four digit code on your keypad now.</Say>
    </Gather>
    <Redirect method="POST">https://{host}/handle-order-id?call_sid={call_sid}</Redirect>
</Response>"""
        else:
            print(f"[{call_sid}] Order ID {order_id_str} not found in database.")
            twiml = build_response_twiml("I'm sorry, that order ID was not found. Please double check the number and try entering it again.", host, call_sid, verified=False)
            
    return Response(content=twiml, media_type="text/xml")

@app.post("/confirm-order-id")
async def confirm_order_id(request: Request, call_sid: str = ""):
    host = request.headers.get("host")
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")
    attempt = int(request.query_params.get("attempt", "1"))
    if request.query_params.get("timeout", "false") == "true":
        order_id_str = cache_get(f"pending_order:{call_sid}") or "unknown"
        play_block = get_play_block(f"I didn't hear a response. Did you say order ID {' '.join(list(order_id_str))}? Please say yes or no.", host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Gather input="speech" action="https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}" method="POST" speechTimeout="auto" timeout="15" language="en-IN" speechModel="phone_call">{play_block}</Gather></Response>"""
        return Response(content=twiml, media_type="text/xml")

    form_data = await request.form()
    speech = form_data.get("SpeechResult", "").strip().lower()
    if any(word in speech for word in ["yes", "yeah", "yep", "correct"]):
        order_id_str = cache_get(f"pending_order:{call_sid}")
        cache_delete(f"pending_order:{call_sid}")
        return await process_order_id(order_id_str, call_sid, host) if order_id_str else await ask_again(call_sid, host, attempt, "something went wrong")
    
    cache_delete(f"pending_order:{call_sid}")
    return await ask_again(call_sid, host, attempt, "let's try again")

# ════════════════════════════════════════════════════
# CSAT Survey Webhook Routes
# ════════════════════════════════════════════════════

@app.post("/voice/trigger-survey")
async def trigger_survey(request: Request):
    host = request.headers.get("host")
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="https://{host}/voice/survey-callback" timeout="5" speechTimeout="auto" language="en-IN">
        <Say voice="Polly.Joanna">Thank you for talking to us. Please rate your overall call experience from one star to five stars.</Say>
    </Gather>
</Response>"""
    return Response(content=twiml_response, media_type="application/xml")

@app.post("/voice/survey-callback")
async def survey_callback(request: Request, SpeechResult: str = Form(None)):
    host = request.headers.get("host")
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "unknown")
    save_message(call_sid, "user", f"My rating is {SpeechResult}" if SpeechResult else "[No survey response provided]")
    end_call(call_sid)

    try:
        current_tunnel = f"https://{host}"
        print(f"[{call_sid}] Survey ended. Bootstrapping outbound callback via {current_tunnel}...")
        trigger_callback_outbound(current_tunnel)
    except Exception as cb_err:
        print(f"[{call_sid}] Non-blocking error running callback module: {cb_err}")

    twiml_goodbye = """<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Polly.Joanna">Thank you for your feedback. Goodbye.</Say><Hangup/></Response>"""
    return Response(content=twiml_goodbye, media_type="application/xml")

# ════════════════════════════════════════════════════
# Outbound Call Handling (Dynamic ID -> OTP Secure Verification)
# ════════════════════════════════════════════════════

@app.post("/voice/callback-speak")
async def callback_speak(request: Request):
    host = request.headers.get("host")
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "unknown")
    caller_number = form_data.get("From", "unknown")
    
    print(f"[{call_sid}] Outbound call answered by agent. Logging tracking reference...")

    try:
        start_call(call_sid, caller_number)
    except Exception as log_err:
        print(f"[{call_sid}] Call initialization note: {log_err}")

    greeting = "Hello agent! Callback connected. Please say or enter the specific 4-digit Order ID you wish to verify now."
    play_block = get_play_block(greeting, host)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="1"/>
    <Gather input="speech dtmf" action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt=1" method="POST" speechTimeout="auto" timeout="15" language="en-IN" finishOnKey="#">
        {play_block}
    </Gather>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


async def trigger_dynamic_otp_flow(call_sid: str, order_id: str, host: str):
    human_number = os.getenv("HUMAN_AGENT_NUMBER")
    twilio_number = os.getenv("TWILIO_PHONE_NUMBER")
    
    print(f"[{call_sid}] Dispatching custom security token payload for Order {order_id} to current line...")

    try:
        update_sql = """
            UPDATE customers 
            SET phone = %s 
            WHERE customer_id = (SELECT customer_id FROM orders WHERE order_id = %s LIMIT 1)
        """
        execute_query(update_sql, (human_number, int(order_id)))
    except Exception as db_err:
        print(f"[{call_sid}] Customer table context link failed: {db_err}")

    otp_code = str(random.randint(1000, 9999))
    save_verified_order(call_sid, otp_code)
    
    try:
        twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        twilio_client.messages.create(
            to=human_number,
            from_=twilio_number,
            body=f"Your dynamic secure verification token for Order {order_id} is: {otp_code}. Enter this code onto your phone keypad."
        )
    except Exception as sms_err:
        print(f"[{call_sid}] SMS transmission failed: {sms_err}")

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="1"/>
    <Gather input="dtmf" action="https://{host}/voice/verify-otp?call_sid={call_sid}" method="POST" timeout="15" numDigits="4">
        <Say voice="Polly.Joanna">An authentication code for your specified order has been texted to your device. Please enter the four digit code on your keypad now.</Say>
    </Gather>
    <Redirect method="POST">https://{host}/voice/callback-speak</Redirect>
</Response>"""
    return Response(content=twiml_response, media_type="text/xml")


@app.post("/voice/verify-otp")
async def verify_otp(request: Request, call_sid: str = ""):
    host = request.headers.get("host")
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")
        
    form_data = await request.form()
    digits_entered = form_data.get("Digits", "").strip()
    
    # Check the temporary stored OTP
    record = get_verified_order(call_sid)
    expected_otp = record[0] if record else None

    if expected_otp and digits_entered == expected_otp:
        print(f"[{call_sid}] OTP Verified! Upgrading call session to AI voice stream...")
        
        # Pull the actual order number we saved
        actual_order_id = cache_get(f"authenticated_order_id:{call_sid}") or "1234"
        
        # Save the real order ID into the verified slot so llm.py loads the proper context
        save_verified_order(call_sid, str(actual_order_id))
        cache_delete(f"authenticated_order_id:{call_sid}")
        
        set_call_state(call_sid, is_speaking=False, host=host)
        
        # Pass a greeting message to start the voice stream call smoothly
        llm_reply = chat("Hello! I see your order is verified. How can I help you today?", call_sid=call_sid)
        
        play_block = get_play_block(llm_reply, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start><Stream url="wss://{host}/media-stream"><Parameter name="call_sid" value="{call_sid}"/></Stream></Start>
    {play_block}
    <Pause length="120"/>
</Response>"""
        return Response(content=twiml, media_type="text/xml")
    
    print(f"[{call_sid}] OTP failed. Expected {expected_otp}, got {digits_entered}")
    failed_twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">That code doesn't match. Please enter your order ID again to request a new code.</Say>
    <Redirect method="POST">https://{host}/handle-order-id?call_sid={call_sid}</Redirect>
</Response>"""
    return Response(content=failed_twiml, media_type="text/xml")

# ════════════════════════════════════════════════════
# Deepgram WebSocket
# ════════════════════════════════════════════════════

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    stream_context = {"call_sid": "unknown", "host": "", "websocket": websocket}
    deepgram_client = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))
    deepgram_ws = None
    deepgram_connected = False

    try:
        async for message in websocket.iter_text():
            data = json.loads(message)
            if data.get("event") == "start":
                stream_context["call_sid"] = data["start"].get("callSid", "unknown")
                stream_sid = data["start"].get("streamSid", "unknown")
                cache_set(f"stream_sid:{stream_context['call_sid']}", stream_sid)
                state = get_call_state(stream_context["call_sid"])
                stream_context["host"] = state.get("host", "")
                await asyncio.sleep(0.5)
                break 

        async for message in websocket.iter_text():
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            
            if data.get("event") == "media":
                if deepgram_ws is None:
                    deepgram_ws = deepgram_client.listen.asyncwebsocket.v("1")

                    async def on_transcript(self, result, **kwargs):
                        try:
                            sentence = result.channel.alternatives[0].transcript
                            if not sentence or not result.is_final:
                                return
                            state = get_call_state(stream_context["call_sid"])
                            if state.get("is_speaking", False) or (time.time() - state.get("resumed_at", 0) < 0.5):
                                return
                            await process_transcript(stream_context["call_sid"], sentence, stream_context["host"], websocket=stream_context["websocket"])
                        except Exception as te:
                            print(f"[{stream_context['call_sid']}] Transcript handling error: {te}")

                    async def on_error(self, error, **kwargs):
                        pass

                    deepgram_ws.on(LiveTranscriptionEvents.Transcript, on_transcript)
                    deepgram_ws.on(LiveTranscriptionEvents.Error, on_error)

                    options = LiveOptions(model="nova-2", language="en-IN", encoding="mulaw", sample_rate=8000, endpointing=300, interim_results=False)
                    if await deepgram_ws.start(options) is False:
                        raise Exception("Deepgram connection rejected")
                    deepgram_connected = True

                audio_chunk = base64.b64decode(data["media"]["payload"])
                if deepgram_connected and deepgram_ws:
                    try:
                        await deepgram_ws.send(audio_chunk)
                    except (ConnectionClosed, Exception):
                        break
            elif data.get("event") == "stop":
                break
    except Exception:
        pass
    finally:
        if deepgram_connected and deepgram_ws:
            try: await deepgram_ws.finish()
            except Exception: pass
        if stream_context["call_sid"] != "unknown":
            delete_call_state(stream_context["call_sid"])
            cache_delete(f"stream_sid:{stream_context['call_sid']}")

if __name__ == "__main__":
    if cache_ping():
        uvicorn.run(app, host="0.0.0.0", port=5000)