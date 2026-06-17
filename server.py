from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
import json
import base64
import asyncio
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv
from llm import chat
from db.database import (
    start_call,
    end_call,
    get_all_calls,
    get_call_transcript,
    save_recording,
    set_call_state,
    get_call_state,
    get_cached_response,
    get_order_context,
    update_call_state,
    delete_call_state,
    cache_set,
    cache_get,
    cache_delete,
    cache_keys,
    cache_ping
)
import os
import re
import time

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


# ════════════════════════════════════════════════════
# Dead call monitor
# ════════════════════════════════════════════════════

async def monitor_dead_calls():
    return

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(monitor_dead_calls())


# ════════════════════════════════════════════════════
# Spoken order ID converter
# ════════════════════════════════════════════════════

def spoken_to_order_id(text: str) -> str:
    text = text.lower().strip()

    multiplier_map = {
        'double': 2, 'twice': 2,
        'triple': 3, 'thrice': 3,
        'quadruple': 4, 'quad': 4,
        'quintuple': 5,
    }

    for word, times in multiplier_map.items():
        pattern = rf'\b{word}\s+(zero|one|two|three|four|five|six|seven|eight|nine|oh)\b'
        def expand(m, t=times):
            return ' '.join([m.group(1)] * t)
        text = re.sub(pattern, expand, text)

    word_to_digit = {
        'zero': '0', 'oh': '0',
        'one': '1', 'won': '1',
        'two': '2', 'to': '2', 'too': '2',
        'three': '3',
        'four': '4', 'for': '4', 'fore': '4',
        'five': '5',
        'six': '6',
        'seven': '7',
        'eight': '8', 'ate': '8',
        'nine': '9',
    }

    for word, digit in sorted(word_to_digit.items(), key=lambda x: -len(x[0])):
        text = re.sub(r'\b' + word + r'\b', digit, text)

    return re.sub(r'[^0-9]', '', text)


# ════════════════════════════════════════════════════
# TwiML helpers
# ════════════════════════════════════════════════════

def get_play_block(text, host):
    safe_text = text.replace("'", "").replace('"', "")
    return f'<Say voice="Polly.Joanna">{safe_text}</Say>'


def build_transfer_twiml(host, call_sid):
    transfer_number = os.getenv("HUMAN_AGENT_NUMBER")
    if transfer_number:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Please hold while we connect you to a human agent.</Say>
    <Dial>{transfer_number}</Dial>
</Response>"""
    else:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">We are sorry, all agents are unavailable. Please call back later.</Say>
    <Hangup/>
</Response>"""


def build_response_twiml(text, host, call_sid, verified=False):
    play_block = get_play_block(text, host)

    if verified:
        gather = f"""<Gather input="speech"
                action="https://{host}/handle-speech?call_sid={call_sid}"
                method="POST"
                speechTimeout="auto"
                timeout="15"
                language="en-IN"
                speechModel="phone_call">
            {play_block}
        </Gather>"""
    else:
        gather = f"""<Gather input="dtmf"
                action="https://{host}/handle-order-id?call_sid={call_sid}"
                method="POST"
                timeout="10"
                finishOnKey="#">
            {play_block}
        </Gather>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {gather}
</Response>"""


# ════════════════════════════════════════════════════
# Core transcript processor
# ════════════════════════════════════════════════════

async def process_transcript(call_sid: str, transcript: str, host: str, websocket: WebSocket):
    from twilio.rest import Client as TwilioClient

    print(f"[{call_sid}] Deepgram STT: '{transcript}'")

    update_call_state(call_sid, is_speaking=True, last_activity_at=time.time())

    # Goodbye detection
    goodbye_words = ["goodbye", "bye", "thank you", "thanks", "that's all"]
    if any(word in transcript.lower() for word in goodbye_words):
        end_call(call_sid)
        return

    # Check cache
    cache_key = f"response_cache:{call_sid}:{transcript}"
    response_text = get_cached_response(cache_key)

    if response_text:
        from db.database import save_message
        save_message(call_sid, "user", transcript)
        save_message(call_sid, "assistant", response_text)
        print(f"[{call_sid}] Cache hit")
    else:
        print(f"[{call_sid}] Cache miss — sending to LLM")
        try:
            response_text = chat(transcript, call_sid=call_sid)
        except Exception as e:
            print(f"[{call_sid}] LLM error: {e}")
            import traceback
            traceback.print_exc()
            response_text = None

        if response_text:
            cache_set(cache_key, response_text)

    # LLM failed
    if not response_text:
        print(f"[{call_sid}] LLM failed — transferring to human agent")
        twiml = build_transfer_twiml(host, call_sid)
        try:
            twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
            twilio_client.calls(call_sid).update(twiml=twiml)
        except Exception as e:
            print(f"[{call_sid}] Transfer failed: {e}")
        return

    print(f"[{call_sid}] Agent: {response_text}")

    # ── Play response via Twilio REST API (most reliable method) ──
    try:
        twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        play_block = get_play_block(response_text, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="wss://{host}/media-stream">
            <Parameter name="call_sid" value="{call_sid}"/>
        </Stream>
    </Start>
    {play_block}
    <Pause length="120"/>
</Response>"""
        twilio_client.calls(call_sid).update(twiml=twiml)
        print(f"[{call_sid}] TwiML response injected via REST")
    except Exception as e:
        print(f"[{call_sid}] TwiML injection failed: {e}")

    # Wait estimated speech duration before re-enabling listening
    word_count = len(response_text.split())
    duration = max(2.0, (word_count / 150) * 60)
    await asyncio.sleep(duration)

    update_call_state(
        call_sid,
        is_speaking=False,
        resumed_at=time.time(),
        last_activity_at=time.time()
    )
    print(f"[{call_sid}] Listening resumed")


# ════════════════════════════════════════════════════
# Call routes
# ════════════════════════════════════════════════════

@app.post("/incoming-call")
async def incoming_call(request: Request):
    host = request.headers.get("host")
    form_data = await request.form()

    call_sid = form_data.get("CallSid", "unknown")
    caller_number = form_data.get("From", "unknown")

    print(f"Incoming call! SID: {call_sid} From: {caller_number}")
    start_call(call_sid, caller_number)

    greeting = "Hello! Welcome to customer support. Please say your Order ID clearly, digit by digit. For example, say one two three four for order 1234."
    play_block = get_play_block(greeting, host)

    try:
        from twilio.rest import Client as TwilioClient
        twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        twilio_client.calls(call_sid).recordings.create(
            recording_status_callback=f"https://{host}/recording-status",
            recording_status_callback_method="POST"
        )
        print(f"[{call_sid}] Background recording started")
    except Exception as e:
        print(f"[{call_sid}] Could not start recording: {e}")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="3"/>
    <Gather input="speech dtmf"
            action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt=1"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            finishOnKey="#"
            hints="zero, one, two, three, four, five, six, seven, eight, nine, double, triple, quadruple, oh">
        {play_block}
    </Gather>
</Response>"""
    return Response(content=twiml, media_type="text/xml")


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
        print(f"[{call_sid}] Order ID via keypad: {digits}")
        return await process_order_id(digits, call_sid, host)

    if speech:
        order_id_str = spoken_to_order_id(speech)

        if not order_id_str or len(order_id_str) != 4:
            return await ask_again(call_sid, host, attempt, f"I heard {order_id_str or 'nothing'} which doesnt look like a valid 4 digit order ID")

        print(f"[{call_sid}] Speech: '{speech}' -> Order ID: '{order_id_str}'")
        cache_set(f"pending_order:{call_sid}", order_id_str)

        spaced = ' '.join(list(order_id_str))
        confirm_text = f"I heard order ID {spaced}. Is that correct? Say yes to confirm or no to try again."
        play_block = get_play_block(confirm_text, host)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            speechModel="phone_call"
            hints="yes:5, no:5, correct:3, wrong:3, right:3, yeah:5, nope:5, yep:3">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}&amp;timeout=true</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    return await ask_again(call_sid, host, attempt, "didn't receive any input")


async def ask_again(call_sid: str, host: str, attempt: int, reason: str):
    if attempt >= 3:
        text = "No problem. Please type your Order ID on the keypad and press the hash key."
        play_block = get_play_block(text, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="dtmf"
            action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={attempt}"
            method="POST"
            timeout="20"
            finishOnKey="#">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={attempt}</Redirect>
</Response>"""
    else:
        next_attempt = attempt + 1
        text = f"Sorry, I {reason}. Please say your Order ID again, digit by digit."
        play_block = get_play_block(text, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech dtmf"
            action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={next_attempt}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            finishOnKey="#"
            hints="zero:5, oh:5, one:5, two:5, three:5, four:5, five:5, six:5, seven:5, eight:5, nine:5, double:3, triple:3">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={next_attempt}</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")


async def process_order_id(order_id_str: str, call_sid: str, host: str):
    response_text = chat(order_id_str, call_sid=call_sid)
    print(f"[{call_sid}] Agent: {response_text}")

    from db.database import get_verified_order
    verified = get_verified_order(call_sid) is not None

    if verified:
        set_call_state(call_sid, is_speaking=False, host=host)
        play_block = get_play_block(response_text, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="wss://{host}/media-stream">
            <Parameter name="call_sid" value="{call_sid}"/>
        </Stream>
    </Start>
    {play_block}
    <Pause length="120"/>
</Response>"""
    else:
        twiml = build_response_twiml(response_text, host, call_sid, verified=False)

    return Response(content=twiml, media_type="text/xml")


@app.post("/confirm-order-id")
async def confirm_order_id(request: Request, call_sid: str = ""):
    host = request.headers.get("host")
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")

    attempt = int(request.query_params.get("attempt", "1"))
    timeout = request.query_params.get("timeout", "false")

    if timeout == "true":
        order_id_str = cache_get(f"pending_order:{call_sid}") or "unknown"
        spaced = ' '.join(list(order_id_str))
        confirm_text = f"I didnt hear a response. Did you say order ID {spaced}? Please say yes or no."
        play_block = get_play_block(confirm_text, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            speechModel="phone_call"
            hints="yes:5, no:5, yeah:5, nope:5">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}&amp;timeout=true</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    form_data = await request.form()
    speech = form_data.get("SpeechResult", "").strip().lower()

    yes_words = ["yes", "yeah", "yep", "correct", "right", "sure", "confirm", "affirmative"]
    no_words = ["no", "nope", "wrong", "incorrect", "negative", "retry", "again"]

    confirmed = any(word in speech for word in yes_words)
    denied = any(word in speech for word in no_words)

    if confirmed:
        order_id_str = cache_get(f"pending_order:{call_sid}")
        cache_delete(f"pending_order:{call_sid}")
        if order_id_str:
            print(f"[{call_sid}] Order ID confirmed: {order_id_str}")
            return await process_order_id(order_id_str, call_sid, host)
        else:
            return await ask_again(call_sid, host, attempt, "something went wrong")

    elif denied:
        cache_delete(f"pending_order:{call_sid}")
        print(f"[{call_sid}] Order ID rejected — attempt {attempt}")
        return await ask_again(call_sid, host, attempt, "lets try again")

    else:
        order_id_str = cache_get(f"pending_order:{call_sid}") or "unknown"
        spaced = ' '.join(list(order_id_str))
        confirm_text = f"Sorry, I didnt catch that. Did you say order ID {spaced}? Please say yes or no."
        play_block = get_play_block(confirm_text, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            hints="yes, no, correct, wrong, right, yeah, nope">
        {play_block}
    </Gather>
</Response>"""
        return Response(content=twiml, media_type="text/xml")


# ════════════════════════════════════════════════════
# Deepgram WebSocket
# ════════════════════════════════════════════════════

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    call_sid = "unknown"
    host = ""
    print(f"[unknown] Media stream connected — waiting for start event")

    async for message in websocket.iter_text():
        data = json.loads(message)
        if data.get("event") == "start":
            call_sid = data["start"].get("callSid", "unknown")
            stream_sid = data["start"].get("streamSid", "unknown")
            cache_set(f"stream_sid:{call_sid}", stream_sid)
            state = get_call_state(call_sid)
            host = state.get("host", "")
            print(f"[{call_sid}] Media stream identified with StreamSid: {stream_sid}")
            break

    deepgram_ws = None
    deepgram_connected = False

    try:
        deepgram_client = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))
        deepgram_ws = deepgram_client.listen.asyncwebsocket.v("1")

        async def on_transcript(self, result, **kwargs):
            try:
                sentence = result.channel.alternatives[0].transcript
                if not sentence or not result.is_final:
                    return

                state = get_call_state(call_sid)

                if state.get("is_speaking", False):
                    print(f"[{call_sid}] Muted — ignoring: '{sentence}'")
                    return

                resumed_at = state.get("resumed_at", 0)
                if time.time() - resumed_at < 0.5:
                    print(f"[{call_sid}] Cooldown — discarding: '{sentence}'")
                    return

                await process_transcript(call_sid, sentence, host, websocket=websocket)

            except Exception as e:
                print(f"[{call_sid}] Transcript error: {e}")

        async def on_error(self, error, **kwargs):
            print(f"[{call_sid}] Deepgram error: {error}")

        deepgram_ws.on(LiveTranscriptionEvents.Transcript, on_transcript)
        deepgram_ws.on(LiveTranscriptionEvents.Error, on_error)

        options = LiveOptions(
            model="nova-2",
            language="en-IN",
            encoding="mulaw",
            sample_rate=8000,
            endpointing=300,
            interim_results=False,
        )

        result = await deepgram_ws.start(options)
        if result is False:
            raise Exception("Deepgram connection rejected — check API key")

        deepgram_connected = True
        print(f"[{call_sid}] Deepgram live connection opened")

        async for message in websocket.iter_text():
            data = json.loads(message)
            if data.get("event") == "media":
                audio_chunk = base64.b64decode(data["media"]["payload"])
                await deepgram_ws.send(audio_chunk)
            elif data.get("event") == "mark":
                pass
            elif data.get("event") == "stop":
                print(f"[{call_sid}] Stream stopped")
                break

    except WebSocketDisconnect:
        print(f"[{call_sid}] WebSocket disconnected")

    except Exception as e:
        print(f"[{call_sid}] Deepgram failed: {e} — switching to Twilio STT")
        if host and call_sid != "unknown":
            try:
                from twilio.rest import Client as TwilioClient
                twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
                fallback_twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/handle-speech?call_sid={call_sid}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            speechModel="phone_call">
        <Say voice="Polly.Joanna">Sorry, please say your query.</Say>
    </Gather>
</Response>"""
                twilio_client.calls(call_sid).update(twiml=fallback_twiml)
                print(f"[{call_sid}] Switched to Twilio STT fallback")
            except Exception as fe:
                print(f"[{call_sid}] Fallback failed: {fe}")

    finally:
        if deepgram_connected and deepgram_ws:
            await deepgram_ws.finish()
        delete_call_state(call_sid)
        cache_delete(f"stream_sid:{call_sid}")
        print(f"[{call_sid}] Stream cleaned up")


# ════════════════════════════════════════════════════
# Twilio STT fallback
# ════════════════════════════════════════════════════

@app.post("/handle-speech")
async def handle_speech(
    request: Request,
    SpeechResult: str = Form(default=""),
    call_sid: str = ""
):
    host = request.headers.get("host")
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")

    final_transcript = SpeechResult.strip()
    print(f"[{call_sid}] Twilio STT fallback: '{final_transcript}'")

    if not final_transcript:
        sorry_text = "Sorry, I didnt catch that. Please say your query again."
        play_block = get_play_block(sorry_text, host)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/handle-speech?call_sid={call_sid}"
            method="POST"
            speechTimeout="auto"
            timeout="10"
            language="en-IN"
            speechModel="phone_call">
        {play_block}
    </Gather>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    goodbye_words = ["goodbye", "bye", "thank you", "thanks", "that's all"]
    if any(word in final_transcript.lower() for word in goodbye_words):
        end_call(call_sid)

    cache_key = f"response_cache:{call_sid}:{final_transcript}"
    response_text = get_cached_response(cache_key)

    if not response_text:
        response_text = chat(final_transcript, call_sid=call_sid)

    if response_text is None:
        twiml = build_transfer_twiml(host, call_sid)
        return Response(content=twiml, media_type="text/xml")

    print(f"[{call_sid}] Agent: {response_text}")
    twiml = build_response_twiml(response_text, host, call_sid, verified=True)
    return Response(content=twiml, media_type="text/xml")


# ════════════════════════════════════════════════════
# Recording routes
# ════════════════════════════════════════════════════

@app.post("/handle-recording")
async def handle_recording(request: Request, call_sid: str = ""):
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")
    form_data = await request.form()
    recording_url = form_data.get("RecordingUrl", "")
    recording_sid = form_data.get("RecordingSid", "")
    if recording_url:
        save_recording(call_sid, recording_url, recording_sid)
    return Response(content="OK", media_type="text/plain")


@app.post("/recording-status")
async def recording_status(request: Request):
    form_data = await request.form()
    status = form_data.get("RecordingStatus", "")
    recording_sid = form_data.get("RecordingSid", "")
    recording_url = form_data.get("RecordingUrl", "")
    call_sid = form_data.get("CallSid", "")
    print(f"Recording {recording_sid} status: {status}")
    if status == "completed" and recording_url and call_sid:
        save_recording(call_sid, recording_url, recording_sid)
    return Response(content="OK", media_type="text/plain")


# ════════════════════════════════════════════════════
# Admin routes
# ════════════════════════════════════════════════════

@app.get("/admin/calls")
async def view_calls():
    calls = get_all_calls()
    html = """
    <html><head><title>Call Centre Admin</title>
    <style>
        body { font-family: Arial; padding: 20px; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background: #4CAF50; color: white; }
        tr:nth-child(even) { background: #f2f2f2; }
        a { color: #4CAF50; }
    </style></head>
    <body><h1>Call Centre — All Calls</h1>
    <table><tr>
        <th>Call SID (click for transcript)</th>
        <th>From</th><th>Started</th><th>Ended</th>
        <th>Status</th><th>Recording</th><th>Messages</th>
    </tr>"""

    for call in calls:
        recording_link = f"<a href='{call[5]}' target='_blank'>▶️ Play</a>" if call[5] else "No recording"
        html += f"""<tr>
            <td><a href='/admin/transcript/{call[0]}'>{call[0]}</a></td>
            <td>{call[1]}</td><td>{call[2]}</td><td>{call[3] or 'Active'}</td>
            <td>{call[4]}</td><td>{recording_link}</td><td>{call[6]}</td>
        </tr>"""

    html += "</table></body></html>"
    return Response(content=html, media_type="text/html")


@app.get("/admin/transcript/{call_sid}")
async def view_transcript(call_sid: str):
    transcript = get_call_transcript(call_sid)
    html = f"""<html><head><title>Transcript</title>
    <style>
        body {{ font-family: Arial; padding: 20px; }}
        pre {{ background: #f5f5f5; padding: 20px; border-radius: 8px;
               white-space: pre-wrap; word-wrap: break-word; }}
    </style></head>
    <body><h1>Transcript: {call_sid}</h1>
    <a href='/admin/calls'>← Back to all calls</a><br><br>
    <pre>{transcript}</pre></body></html>"""
    return Response(content=html, media_type="text/html")


# ════════════════════════════════════════════════════
# Startup
# ════════════════════════════════════════════════════

if __name__ == "__main__":
    if cache_ping():
        print("In-memory cache initialized")
    print("Starting AI Call Centre Server...")
    print("Server running on http://localhost:5000")
    print("Admin panel: http://localhost:5000/admin/calls")
    uvicorn.run(app, host="0.0.0.0", port=5000)