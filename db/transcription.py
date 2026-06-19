from db.main_db import execute_query 
import datetime

def save_recording(call_sid: str, recording_url: str, recording_sid: str):
    """Saves the audio link to a specific call record."""
    instruction = """
        UPDATE calls 
        SET recording_url = %s, recording_sid = %s
        WHERE call_sid = %s
    """
    execute_query(instruction, (recording_url, recording_sid, call_sid))

# ── TRANSCRIPTION & DIALOGUE STORAGE ──

def save_message(call_sid: str, role: str, content: str):
    """Appends or creates a running text dialogue log block."""
    fetch_instruction = "SELECT conversation FROM messages WHERE call_sid = %s"
    row = execute_query(fetch_instruction, (call_sid,), fetch_mode='one')

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_line = f"[{timestamp}] {role.upper()}: {content}"

    if row:
        updated_conversation = row[0] + "\n" + new_line
        update_instruction = """
            UPDATE messages SET conversation = %s, last_updated = %s
            WHERE call_sid = %s
        """
        execute_query(update_instruction, (updated_conversation, datetime.now(), call_sid))
    else:
        insert_instruction = """
            INSERT INTO messages (call_sid, conversation, last_updated)
            VALUES (%s, %s, %s)
        """
        execute_query(insert_instruction, (call_sid, new_line, datetime.now()))

def get_conversation_history(call_sid: str):
    """Fetches a transcript and parses it into a clean list of dictionaries."""
    instruction = "SELECT conversation FROM messages WHERE call_sid = %s"
    row = execute_query(instruction, (call_sid,), fetch_mode='one')
    if not row or not row[0]:
        return []

    history = []
    for line in row[0].split("\n"):
        try:
            parts = line.split("] ", 1)
            if len(parts) < 2:
                continue
            role_part, content = parts[1].split(": ", 1)
            history.append({"role": role_part.lower(), "content": content})
        except ValueError:
            continue
    return history

def get_all_calls():
    """Retrieves a summarized list of all tracked calls for a dashboard view."""
    instruction = """
        SELECT c.call_sid, c.caller_number, c.started_at, c.ended_at,
               c.status, c.recording_url, COUNT(m.call_sid) as message_count
        FROM calls c
        LEFT JOIN messages m ON c.call_sid = m.call_sid
        GROUP BY c.call_sid, c.caller_number, c.started_at, c.ended_at, c.status, c.recording_url
        ORDER BY c.started_at DESC
    """
    return execute_query(instruction, fetch_mode='all')

def get_call_transcript(call_sid: str):
    """Gets the raw, unparsed string transcript of a call."""
    instruction = "SELECT conversation FROM messages WHERE call_sid = %s"
    row = execute_query(instruction, (call_sid,), fetch_mode='one')
    return row[0] if row else "No transcript available."

