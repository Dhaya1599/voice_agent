from datetime import datetime 
from db.main_db import execute_query
from db.transcription import get_call_transcript
from db.dashboard_content import detect_intent
from db.dashboard_content import parse_csat_from_transcript

def start_call(call_sid: str, caller_number: str):
    """Logs a new incoming call into the database."""
    instruction = """
        INSERT INTO calls (call_sid, caller_number, started_at, status)
        VALUES (%s, %s, %s, 'active')
        ON CONFLICT (call_sid) DO NOTHING
    """
    execute_query(instruction, (call_sid, caller_number, datetime.now()))
    print("Call recorded in database!")

def end_call(call_sid: str):
    """Updates an existing call log to change status to 'ended'."""
    instruction = """
        UPDATE calls 
        SET ended_at = %s, status = 'ended'
        WHERE call_sid = %s
    """
    execute_query(instruction, (datetime.now(), call_sid))
    #fetching conversation from message table
    transcript = get_call_transcript(call_sid)

    #generalising the intent, hanoff and csat sore as neutral
    primary_intent = "general_conversation"
    human_handoff = False
    csat_score = None

    #now making sure transcript is not null doesnt not have get_call_transcripts's fallback message
    if transcript and transcript != "No transcript available. ":
        primary_intent = detect_intent(transcript)
        #defining the key words for the transfer
        handoff_keys = ["transfer","human","live agent","someone","representative","Not a robot","speak to someone","operator"]
        human_handoff = any(keyword in transcript.lower() for keyword in handoff_keys)
        #now we calculate the satisfaction score
        csat_score = parse_csat_from_transcript(transcript)
        word_count = len(transcript.split())
        
        # Base infrastructure cost rates
        TWILIO_VOICE_RATE = 0.013 / 60   # $0.013 per minute -> per second
        DEEPGRAM_STT_RATE = 0.0043 / 60  # $0.0043 per minute -> per second
        LLM_TOKEN_ESTIMATE_RATE = 0.00002 # Average price per word generated/processed
        
        # Estimate call duration assuming an average speech rate of 150 words per minute (2.5 words per second)
        estimated_duration_seconds = max(5.0, word_count / 2.5) 
        
        # Formula sum
        calculated_cost = (estimated_duration_seconds * TWILIO_VOICE_RATE) + \
                          (estimated_duration_seconds * DEEPGRAM_STT_RATE) + \
                          (word_count * LLM_TOKEN_ESTIMATE_RATE)
                          
        # Round cleanly to 4 decimal places (e.g., $0.0345)
        call_cost = round(calculated_cost, 4)
    else:
        # Fallback for empty/failed calls
        call_cost = 0.0100 

    # ── 2. UPDATE THE SQL QUERY TO INCLUDE COST ──
    message_instruction = """ 
        UPDATE messages 
        SET primary_intent = %s, human_handoff = %s, csat_score = %s, cost = %s 
        WHERE call_sid = %s 
    """
    
    # Execute with the new parameter
    execute_query(message_instruction, (primary_intent, human_handoff, csat_score, call_cost, call_sid))
    print("Realtime call logs and cost metrics are updated in admin side!!!")
    