import datetime 
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
    message_instruction = """ UPDATE messages SET primary_intent = %s, human_handoff = %s, csat_score = %s WHERE call_sid = %s """
    execute_query(message_instruction,(primary_intent,human_handoff,csat_score, call_sid))
    print("Realtime call logs are updated in admin side!!!")