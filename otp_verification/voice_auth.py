import os
import re
from fastapi import APIRouter, Form, Response, Request  # 📍 Added Request import
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from twilio.base.exceptions import TwilioException
from dotenv import load_dotenv

# Local imports
from db.cache_management import cache_get, cache_set, cache_delete
from db.main_db import execute_query 

# Load environment configurations
load_dotenv()

auth_router = APIRouter(prefix="/voice", tags=["auth"])

# Read environmental secrets
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID")

# ═] SYSTEM ENVIRONMENT DIAGNOSTIC CHECK [═
print("\n" + "═"*60)
print("TWILIO SYSTEM BOOT INITIALIZATION REPORT:")
print(f"   -> TWILIO_ACCOUNT_SID:       { '✅ ACTIVE' if ACCOUNT_SID else '❌ MISSING' }")
print(f"   -> TWILIO_AUTH_TOKEN:        { '✅ ACTIVE' if AUTH_TOKEN else '❌ MISSING' }")
print(f"   -> TWILIO_VERIFY_SERVICE_SID: { '✅ ACTIVE' if VERIFY_SERVICE_SID else '❌ MISSING' }")
print("═"*60 + "\n")

# Safely declare the client interface wrapper
twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN) if all([ACCOUNT_SID, AUTH_TOKEN, VERIFY_SERVICE_SID]) else None

# 📍 Load and parse environment timeouts cleanly
ORDER_ID_TIMEOUT = int(os.getenv("ORDER_ID_TIMEOUT_SECONDS"))
OTP_TIMEOUT = int(os.getenv("OTP_TIMEOUT"))

# 📍 26-Alphabet Voice Input Cleaner Logic
def normalize_all_alphabets(raw_speech: str) -> str:
    if not raw_speech:
        return ""
    text = raw_speech.lower().strip()
    
    # 1. Convert spoken words to digits first while spelling is complete
    number_map = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "zero": "0"
    }
    for word, digit in number_map.items():
        text = text.replace(word, digit)
        
    # 2. Now handle spoken stuttering/elongations safely
    text = re.sub(r'(\w)\1+', r'\1', text)  # Collapse stutter/elongated characters (aeee -> ae)
    text = re.sub(r'\b([b-df-hj-np-tv-z])e\b', r'\1', text)  # Clean trailing phonetic vowels (pe -> p)
    
    return re.sub(r'[^a-z0-9]', '', text).upper()
# ════════════════════════════════════════════════════
# STEP 1 & 2: Initial Greet & Request Order ID (Voice)
# ════════════════════════════════════════════════════
@auth_router.post("/incoming")
async def handle_incoming_call(request: Request, CallSid: str = Form(...)): # 📍 Injected request object
    response = VoiceResponse()
    
    # 📍 FIXED: Added expire_seconds parameter to prevent unexpected keyword parameter crashes
    cache_set(f"auth_state:{CallSid}", {"status": "AWAITING_ORDER_ID"})
    
    # Track voice processing fallback retry counters
    attempts = int(request.query_params.get('retry', 0))
    
    if attempts < 3:
        # Setup voice capture pipeline
        gather = Gather(
            input="speech", 
            action=f"/voice/process-order?retry={attempts}", 
            method="POST", 
            timeout=ORDER_ID_TIMEOUT, # Time to wait after user stops speaking
            speech_model="phone_call",
            hints="ABC1234, A, B, C, 1, 2, 3, 4"
        )
        gather.say("Please say your order ID clearly, spelling out any letters.", voice="Polly.Joanna")
        response.append(gather)
        
        # If Gather times out because of complete silence, loop back
        response.redirect(f"/voice/incoming?retry={attempts + 1}")
    else:
        # 📍 Fixed fallback loop: Exit after 3 failed tries
        response.say("We haven't received a clear response. Goodbye.", voice="Polly.Joanna")
        response.hangup()
        
    return Response(content=str(response), media_type="application/xml")


# ════════════════════════════════════════════════════
# STEP 3 & 4: Lookup Phone & Send OTP via Twilio Verify
# ════════════════════════════════════════════════════
@auth_router.post("/process-order")
async def process_order(request: Request, CallSid: str = Form(None), SpeechResult: str = Form(None)):
    response = VoiceResponse()
    attempts = int(request.query_params.get('retry', 0))
    
    # Redirect back to incoming if voice capture didn't catch anything
    if not SpeechResult or not SpeechResult.strip():
        response.say("We did not catch your input.", voice="Polly.Joanna")
        response.redirect(f"/voice/incoming?retry={attempts + 1}")
        return Response(content=str(response), media_type="application/xml")

    # Clean the voice transcript into a hard alphanumeric order string
    order_id = normalize_all_alphabets(SpeechResult)
    print(f"[{CallSid}] Voice Input: '{SpeechResult}' -> Normalized Order ID: {order_id}")
    
    # Secure validation check against relational database records
    instruction = """ 
        SELECT c.phone 
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        WHERE o.order_id = %s 
    """
    row = execute_query(instruction, (order_id,), fetch_mode='one')
    
    if not row or not row[0]:
        response.say("The order ID provided could not be recognized. Let's try again.", voice="Polly.Joanna")
        response.redirect(f"/voice/incoming?retry={attempts + 1}")
        return Response(content=str(response), media_type="application/xml")
        
    customer_phone = row[0]
    print(f"[{CallSid}] Linked phone number found: {customer_phone}")

    if not twilio_client:
        response.say("Authentication service configuration configuration mismatch error.", voice="Polly.Joanna")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    try:
        # Dispatch SMS validation token code string
        twilio_client.verify.v2.services(VERIFY_SERVICE_SID) \
            .verifications \
            .create(to=customer_phone, channel='sms')
            
        # Move session status state down the cache table
        cache_set(f"auth_state:{CallSid}", {
            "status": "AWAITING_OTP",
            "customer_phone": customer_phone,
            "order_id": order_id
        })
        
        # Prompt for the numeric 6-digit SMS text code via DTMF keypad input collection
        gather = Gather(num_digits=6, action="/voice/verify-otp", method="POST", timeout=OTP_TIMEOUT)
        gather.say("A secure pass code has been texted to your mobile number. Please key in that six-digit code now.", voice="Polly.Joanna")
        response.append(gather)
        
        response.say("We did not receive your entry sequence. Disconnecting session. Goodbye.", voice="Polly.Joanna")
        response.hangup()
        
    except TwilioException as e:
        print(f"💥 Twilio Outbound Dispatch Error: {str(e)}")
        response.say("Authentication service unavailable right now. Please try again shortly.", voice="Polly.Joanna")
        response.hangup()

    return Response(content=str(response), media_type="application/xml")


# ════════════════════════════════════════════════════
# STEP 6 & 7: Validate OTP or Drop Session Context
# ════════════════════════════════════════════════════
@auth_router.post("/verify-otp")
async def verify_otp(CallSid: str = Form(None), Digits: str = Form(None)):
    response = VoiceResponse()
    
    session_data = cache_get(f"auth_state:{CallSid}")
    if not session_data or session_data.get("status") != "AWAITING_OTP":
        response.say("Session timed out or authorization context broken. Goodbye.", voice="Polly.Joanna")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    if not Digits:
        response.say("No digits received. Call connection dropped.", voice="Polly.Joanna")
        response.hangup()
        cache_delete(f"auth_state:{CallSid}")
        return Response(content=str(response), media_type="application/xml")

    entered_code = Digits.strip()
    customer_phone = session_data.get("customer_phone")
    
    try:
        check = twilio_client.verify.v2.services(VERIFY_SERVICE_SID) \
            .verification_checks \
            .create(to=customer_phone, code=entered_code)
            
        if check.status == "approved":
            print(f"[{CallSid}] Access authorization granted.")
            cache_set(f"auth_state:{CallSid}", {
                "status": "VERIFIED", 
                "order_id": session_data.get("order_id")
            })
            
            response.say("Identity verified successfully! Please hold while we connect you to your virtual assistant.", voice="Polly.Joanna")
            response.redirect("/incoming-call")
        else:
            response.say("Verification failed. The security pass entered is incorrect. Disconnecting call.", voice="Polly.Joanna")
            response.hangup()
            cache_delete(f"auth_state:{CallSid}")
            
    except TwilioException as e:
        print(f"❌ Verification Error Instance: {e}")
        response.say("Invalid entry structure pattern. Connection dropped.", voice="Polly.Joanna")
        response.hangup()
        cache_delete(f"auth_state:{CallSid}")

    return Response(content=str(response), media_type="application/xml")