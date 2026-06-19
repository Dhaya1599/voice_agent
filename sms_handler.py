import os
import time
from fastapi import APIRouter, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

# Import your database core operations and LLM engine configurations
# Adjust these imports depending on your exact project structure
from db.cache_management import cache_get, cache_set
from llm import client, NUMBER_FORMAT_RULE,PERSONALITY_RULES, PRODUCT_QUERY_RULES,SYSTEM_PROMPT_VERIFIED  # assuming you have a configured client in llm.py
import uuid

load_dotenv()

# We use an APIRouter so this can be easily plugged directly into your main FastAPI server app
sms_router = APIRouter(prefix="/voice", tags=["sms"])

# Target an incredibly concise token window to restrict sentence length over texts
SMS_SYSTEM_PROMPT = (
    f"{PERSONALITY_RULES}\n\n"
    f"{PRODUCT_QUERY_RULES}\n\n"
    "CRITICAL SMS RULES:\n"
    "- You are conversing via SMS text message, NOT a voice call.\n"
    "- Keep every single response under 2 short sentences max. Be punchy and direct.\n"
    "- REVIEW FLOW: Once you have answered the customer's question or helped them out, "
    "you MUST ask them to reply with a quick 1-to-5 star rating or review score. "
    "For example: 'Glad I could help! Would you mind reply with a rating from 1 to 5 for my service today?'\n"
    "- If the user replies with a number or review text, say a sweet 'Thank you!' and stop asking."
    "- Do not space out digit sequences; display IDs naturally (e.g., Order ID: 1001).\n"
    "- Never use markdown formatting like bolding (**) or bullet points."
)


def get_short_llm_response(user_phone: str, user_message: str) -> str:
    """
    Manages text session state and fetches a highly compressed response from the LLM.
    """
    cache_key = f"sms_history:{user_phone}"
    
    # Fetch existing text message history context or initialize a new sequence
    session_data = cache_get(cache_key, default={"messages": []})
    
    # If the history is empty, initialize it with our specific SMS system guidelines
    if not session_data["messages"]:
        session_data["messages"].append({"role": "system", "content": SMS_SYSTEM_PROMPT})
        
    # Append the user's incoming raw text message
    session_data["messages"].append({"role": "user", "content": user_message})
    
    # Enforce a rolling history window of the last 6 messages to protect token limits and budget
    if len(session_data["messages"]) > 7:  # System prompt + 6 chat messages
        session_data["messages"] = [session_data["messages"][0]] + session_data["messages"][-6:]
        
    try:
        # Request completion from your LLM pipeline
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant", # or your chosen model like Llama 3 via Groq
            messages=session_data["messages"],
            max_tokens=65,       # Hard physical constraint to guarantee brief replies
            temperature=0.4      # Lower temperature for more factual, precise answers
        )
        
        reply = completion.choices[0].message.content.strip()
        
        # Append the assistant's reply back into memory history tracking
        session_data["messages"].append({"role": "assistant", "content": reply})
        cache_set(cache_key, session_data)
        
        return reply
        
    except Exception as e:
        print(f" [SMS LLM Exception Error]: {e}")
        return "I'm having trouble connecting to my systems right now. Please try again shortly."


@sms_router.post("/sms")
async def handle_incoming_sms(Body: str = Form(...), From: str = Form(...), MessageSid: str = Form(...)):
    user_text = Body.strip()
    caller_phone = From.strip()
    
    # 1. Catching User Ratings Natively
    import re
    if re.match(r"^[1-5]$", user_text):
        score = int(user_text)
        
        # SQL Update: Match the last active SMS session for this user phone and assign the review score
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE operational_logs 
            SET csat_score = %s, primary_intent = 'review_feedback'
            WHERE id = (
                SELECT id FROM operational_logs 
                WHERE caller_reference = %s AND routing_channel = 'sms'
                ORDER BY created_at DESC LIMIT 1
            )
        """, (score, caller_phone))
        conn.commit()
        cursor.close()
        conn.close()

        twilio_twiml = MessagingResponse()
        twilio_twiml.message("Thank you so much for your feedback! It helps us keep TechMart running flawlessly.")
        return Response(content=str(twilio_twiml), media_type="application/xml")

    # 2. Process Regular Text message through LLM Pipeline
    reply_content = get_short_llm_response(user_phone=caller_phone, user_message=user_text)
    
    # 3. Log This Active SMS Session right into the DB
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO operational_logs (session_id, caller_reference, primary_intent, routing_channel, telemetry_cost)
            VALUES (%s, %s, %s, %s, %s)
        """, (MessageSid, caller_phone, "general_inquiry", "sms", 0.01)) # SMS cost flat rate
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error writing SMS log row to database: {e}")

    twilio_twiml = MessagingResponse()
    twilio_twiml.message(reply_content)
    return Response(content=str(twilio_twiml), media_type="application/xml")