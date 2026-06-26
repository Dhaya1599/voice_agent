import os
import re
import time
import json
import base64
import asyncio
import httpx
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from websockets.exceptions import ConnectionClosed

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

app = FastAPI()
app.include_router(auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
dg_client = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))

async def text_to_speech_mulaw(text: str) -> bytes:
    """
    Converts LLM text output into 8kHz Mu-law audio bytes using Deepgram's Aura TTS API,
    matching Twilio's required live telephone media stream format perfectly.
    """
    url = "https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding=mulaw&sample_rate=8000"
    headers = {
        "Authorization": f"Token {os.getenv('DEEPGRAM_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {"text": text}
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload, timeout=15.0)
        if response.status_code == 200:
            return response.content
        else:
            print(f"❌ Deepgram TTS API Error: {response.status_code} - {response.text}")
            return b""

@app.post("/incoming-call")
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
async def media_stream(websocket: WebSocket):
    """
    Handles real-time bidirectional audio streaming between Twilio, Deepgram (STT),
    Llama 3.1 8B (LLM), and Outbound Audio Playback (via base64 frames over WebSocket).
    """
    await websocket.accept()
    print("🚀 [WebSocket] Twilio media stream connection accepted.")

    call_sid = None
    stream_sid = None
    dg_connection = None
    loop = asyncio.get_running_loop()

    def on_transcript_received(self, result, **kwargs):
        sentence = result.channel.alternatives[0].transcript
        if len(sentence.strip()) > 0:
            asyncio.run_coroutine_threadsafe(
                process_transcript(sentence, call_sid, websocket, stream_sid), loop
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
        print(f"Deepgram STT Live Connection Started: {started}")

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
        print("🔒 [WebSocket] Streaming lifecycle closed.")


async def process_transcript(transcript: str, call_sid: str, websocket: WebSocket, stream_sid: str):
    if not transcript.strip():
        return

    print(f"[{call_sid}] Deepgram STT: '{transcript}'")
    save_message(call_sid, "user", transcript)

    # Invoke Llama 3.1 8B Chat Engine
    response_text = chat(transcript, call_sid=call_sid)

    # 1. Human Handoff Routing Trigger
    # If the user asks for an agent, our LLM passes None or triggers keywords. Let's explicitly look for handoff demands.
    if response_text is None or "[TRIGGER_HUMAN_HANDOFF]" in response_text or any(k in transcript.lower() for k in ["agent", "human", "specialist", "supervisor"]):
        print(f"[{call_sid}] Transfer requested. Executing live telephone line hot-swap intercept...")
        update_call_state(call_sid, is_speaking=True, handoff=True)

        handoff_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Please hold while I transfer your call to a retail logistics specialist.</Say>
    <Dial>+198807675348</Dial>
</Response>"""
        try:
            # Updating the active call to route to a hardcoded human agent line is safe here because we intend to exit the stream
            twilio_client.calls(call_sid).update(twiml=handoff_twiml)
        except Exception as twilio_err:
            print(f"[{call_sid}] Twilio live call agent transfer failed: {twilio_err}")
        return

    # 2. Asynchronous Conversational Streaming Response
    if response_text:
        print(f"[{call_sid}] Llama-8B Response: {response_text}")
        save_message(call_sid, "assistant", response_text)

        # Generate audio payload on the fly without breaking the socket connection
        audio_data = await text_to_speech_mulaw(response_text)
        if audio_data:
            base64_audio = base64.b64encode(audio_data).decode("utf-8")
            
            # Formulate standard Twilio outbound media message block
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64_audio
                }
            }
            # Stream the voice frames right back down the established pipeline
            await websocket.send_json(media_message)


def normalize_phonetic_input(raw_speech: str) -> str:
    text = raw_speech.lower().strip()
    text = re.sub(r'(\w)\1+', r'\1', text)
    text = re.sub(r'\b([b-df-hj-np-tv-z])e\b', r'\1', text)

    number_map = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                  "six": "6", "seven": "7", "eight": "8", "nine": "9", "zero": "0"}
    for word, digit in number_map.items():
        text = text.replace(word, digit)
        
    return re.sub(r'[^a-z0-9\s]', '', text).upper()

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=5000, reload=False)