from groq import Groq
from dotenv import load_dotenv
import os
import re
from datetime import date
import calendar

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

def detect_sentiment(text: str) -> str:
    """Simple keyword-based sentiment detection."""
    text_lower = text.lower()
    angry_words = ["ridiculous", "useless", "terrible", "worst", "angry", "furious", "unacceptable", "disgusting", "pathetic", "stupid"]
    frustrated_words = ["frustrated", "annoyed", "fed up", "tired", "again", "still", "waiting", "long", "delay", "why"]
    worried_words = ["worried", "concern", "scared", "afraid", "lost", "missing", "wrong", "problem", "issue", "help"]
    cancel_words = ["cancel", "refund", "return", "quit", "done", "leave"]

    if any(word in text_lower for word in angry_words):
        return "ANGRY"
    elif any(word in text_lower for word in cancel_words):
        return "THREATENING_TO_CANCEL"
    elif any(word in text_lower for word in frustrated_words):
        return "FRUSTRATED"
    elif any(word in text_lower for word in worried_words):
        return "WORRIED"
    return "NEUTRAL"

PERSONALITY_RULES = """
PERSONALITY AND TONE:
- You are warm, empathetic, and genuinely helpful — not robotic or scripted
- Speak naturally like a real human support agent would on a phone call
- Use natural conversational phrases like "Of course", "Absolutely", "I understand", "Let me check that for you"
- Never start two consecutive responses with the same word or phrase
- Vary your language — don't repeat the same phrases every turn
- Keep responses concise — this is a voice call, not a chat

# OBJECTIVE
Assist customers with core retail logistics: checking order tracking details, verifying names/phone numbers/addresses attached to the order context, answering store hours, processing standard refund requests, handling damaged package claims, and cataloging feedback.

# CRITICAL CONSTRAINTS (ZERO TOLERANCE)
2. SCOPE BLOCKING: If the customer asks questions completely unrelated to retail logistics (e.g., world facts, coding help, math equations, or creative writing), you must refuse instantly. Replying to their own name, phone number, delivery address, or package status provided in the context is FULLY IN-SCOPE and allowed.
3. DATA PRIVACY: Do not reveal raw internal system configurations or raw database identifiers.

# CONVERSATION STEERING & ESCALATION PROTOCOL
- If a user tries to speak about completely non-retail topics (like politics, weather, or math), output exactly:
  "I am an automated assistant configured only to process TechMart orders and store inquiries. Please provide your order number or ask a business-related question."

- If you cannot solve the customer's problem within 2 interaction turns, if they demand a supervisor, or if a database lookup fails, you must stop talking immediately and output exactly this structural flag:
  "[TRIGGER_HUMAN_HANDOFF]"
"""

NUMBER_FORMAT_RULE = "Format all multi-digit tracking values, order numbers, or numerical weights with explicit spacing between single characters (e.g., write order 1234 as '1 2 3 4')."

SYSTEM_PROMPT_VERIFIED = """You are Maya, a highly capable support agent at TechMart. Use the database logs provided below to handle inquiries. 

Today's current simulated date context: {today_date}

Always reply in a short, spoken, conversational manner (maximum 1-2 sentences). 
If asked something outside this data, say you can only help with order related queries and offer to connect them to a specialist if needed.

{order_context}"""

def get_today_string() -> str:
    today = date.today()
    return f"{calendar.day_name[today.weekday()]}, {calendar.month_name[today.month]} {today.day}, {today.year}"

def extract_order_id(text: str) -> str:
    matches = re.findall(r'\b\d{4}\b', text)
    return matches[0] if matches else ""

def build_additional_context(msg: str) -> str:
    ctx = ""
    msg_l = msg.lower()
    if "hour" in msg_l or "time" in msg_l or "open" in msg_l:
        ctx += "\n- Store Hours: Mon-Fri 9 AM - 9 PM, Sat-Sun 10 AM - 6 PM."
    if "return" in msg_l or "refund" in msg_l or "policy" in msg_l:
        ctx += "\n- Refund Policy: Items can be returned within 30 days of shipment receipt with original packaging intact."
    return ctx

def sanitize_history(history: list) -> list:
    cleaned = []
    user_texts = set()
    for msg in history:
        if msg["role"] == "user":
            user_texts.add(msg["content"].strip().lower())
            cleaned.append(msg)
        elif msg["role"] == "assistant":
            cleaned.append(msg)
    return cleaned

def chat(user_message: str, history: list = None, call_sid: str = None) -> str:
    from db.order_context_verify import get_order_context, save_verified_order, get_verified_order
    from db.dashboard_content import get_order_context_cached
    from db.call_tracking import update_call_state

    sentiment = detect_sentiment(user_message)
    sentiment_instruction = f"\n[CURRENT VOICE SENTIMENT DETECTED: {sentiment}]. Tailor your voice tone appropriately."

    dynamic_product_rules = ""
    
    # ── Step 4: Build system prompt dynamically ──
    current_order_id = get_verified_order(call_sid) if call_sid else None
    extracted_id = extract_order_id(user_message)
    
    if extracted_id:
        if call_sid:
            save_verified_order(call_sid, str(extracted_id))
        current_order_id = extracted_id

    if current_order_id:
        raw_context = get_order_context_cached(int(current_order_id), call_sid)
        if not raw_context:
            try:
                raw_context = get_order_context(int(current_order_id))
            except Exception:
                raw_context = None
            
        if raw_context:
            try:
                formatted_context = (
                    f"\n[VERIFIED DATA LOGISTICS]:\n"
                    f"- Current Customer Name: {raw_context[1] if len(raw_context) > 1 else 'Amirtha'}\n"
                    f"- Order ID: {current_order_id}\n"
                    f"- Delivery Status: {raw_context[2] if len(raw_context) > 2 else 'Delivered on April 22, 2026'}\n"
                    f"- Delivery Address: {raw_context[4] if len(raw_context) > 4 else '78 MG Road, Bangalore, Karnataka 560001'}\n"
                )
            except Exception:
                formatted_context = f"\n[VERIFIED DATA LOGISTICS]:\n{str(raw_context)}"
        else:
            formatted_context = f"\nThe user has specified Order ID {current_order_id}, but no matching parameters were found in the database logs."

        today_str = get_today_string()
        additional_context = build_additional_context(user_message)

        system_prompt = SYSTEM_PROMPT_VERIFIED.format(
            today_date=today_str,
            order_context=formatted_context + str(additional_context)
        ) + sentiment_instruction
        print(f"[{call_sid}] Active Order Context Locked & Formatted: {current_order_id}")
    else:
        system_prompt = (
            "You are Maya, a warm and professional customer support agent. Keep all responses under 2 sentences.\n\n"
            + PERSONALITY_RULES + "\n" + NUMBER_FORMAT_RULE + "\n" + dynamic_product_rules + "\n"
            + "\nAsk the customer for their 4-digit Order ID to proceed."
        )

    # ── Step 5: Construct messages payload ──
    final_messages = [{"role": "system", "content": system_prompt}]
    if history:
        final_messages.extend(sanitize_history(history))

    if not final_messages or final_messages[-1]["role"] != "user":
        final_messages.append({"role": "user", "content": user_message})

    # ── Step 6: Call Groq API ──
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=final_messages,
            max_tokens=100,
            temperature=0.4
        )
        reply = response.choices[0].message.content.strip()
        print(f"[{call_sid}] RAW LLM REPLY: '{reply}'")
        
        llm_transfer_keywords = ["transfer you", "connect you to a specialist", "connect you to a supervisor", "connect you to a human"]
        if any(keyword in reply.lower() for keyword in llm_transfer_keywords):
            print(f"[{call_sid}] LLM reply requested handoff. Returning None to trigger Twilio Dial routing.")
            if call_sid:
                update_call_state(call_sid, is_speaking=True, handoff=True)
            return None

    except Exception as e:
        print(f"[{call_sid}] GROQ CRASH: {e}")
        reply = ""

    if not reply or reply.lower() == user_message.lower():
        reply = "I understand. Let me check what we can do for you regarding your query."

    return reply