import os
import re
import time
import json
import base64
import asyncio
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions, Microphone
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
    allow_origins=["*"], #allows monitoring webistes to communicate with backend
    allow_credentials=True, #secure authorization of cookies, tokens
    allow_methods=["*"], #allows to perform fetch and update operations by dashboard
    allow_headers=["*"]
)

twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
dg_client = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))


@app.post("/incoming-call")
async def incoming_call(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    print(f"[/incoming-call] CallSid: {call_sid}")
    auth_session = cache_get(f"auth_state:{call_sid}")#fetch the sid related info from cache
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

    # Fallback if reached unauthenticated
    fallback_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Redirect>/voice/incoming</Redirect>
</Response>"""
    return Response(content=fallback_twiml, media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """
    Handles the real-time bidirectional audio routing loop between
    Twilio, Deepgram (STT), and the LLM engine.
    """
    await websocket.accept()
    print("🚀 [WebSocket] Twilio media stream connection accepted.")

    call_sid = None
    stream_sid = None
    dg_connection = None
    loop = asyncio.get_running_loop()

    def on_transcript_received(self, result, **kwargs):
        # from the deepgram created json result extract the channel in which 
        # it will puck the first option and transcript it into text
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
            interim_results=False, #only send the final trancription rather then bit
            #by bit transcription
            endpointing=300
        )

        dg_connection = dg_client.listen.live.v("1")
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript_received)
        started=dg_connection.start(config)
        print(f"Deepgram started: {started}")

        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("event") == "start":
                call_sid = data["start"]["callSid"]
                stream_sid = data["start"]["streamSid"]

                # Safely extract caller number with fallbacks
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


    cleaned_input = normalize_phonetic_input(transcript)
    print(f"[{call_sid}] Normalization Applied: '{cleaned_input}'")

    # Send the cleaned alpha-numeric ID (like AP36TF) to your DB or LLM logic
    response_text = chat(cleaned_input, call_sid=call_sid)
    # 1. Human handoff trigger
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
            print(f"[{call_sid}] Twilio live call stream intervention failed: {twilio_err}")
        return

    # 2. Speak Maya's response back into the call
    if response_text:
        print(f"[{call_sid}] Agent: {response_text}")
        save_message(call_sid, "assistant", response_text)

        speak_twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{response_text}</Say>
    <Connect>
        <Stream url="wss://{websocket.url.hostname}/media-stream" />
    </Connect>
</Response>"""
        try:
            twilio_client.calls(call_sid).update(twiml=speak_twiml)
        except Exception as e:
            print(f"⚠️ Failed to update live session stream back to call window: {e}")


def normalize_phonetic_input(raw_speech: str) -> str:
    text = raw_speech.lower().strip()
    # Dynamic regex cleaning for elongated sounds (e.g., 'aeee' -> 'a')
    
    text = re.sub(r'(\w)\1+', r'\1', text)
    
    # 3. Handle trailing phonetic filler vowels (e.g., 'peee' -> 'pe' -> 'p', 'teee' -> 't')
    # This strips trailing 'e's if they follow letters commonly elongated with an 'ee' sound.
    text = re.sub(r'\b([b-df-hj-np-tv-z])e\b', r'\1', text)

    number_map = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                  "six": "6", "seven": "7", "eight": "8", "nine": "9", "zero": "0"}
    for word, digit in number_map.items():
        text = text.replace(word, digit)
        
    return re.sub(r'[^a-z0-9]', '', text).upper()

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=5000, reload=False)
