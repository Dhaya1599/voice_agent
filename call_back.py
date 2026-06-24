import os
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()  # Ensure environment variables are loaded

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
trail_number = os.getenv("TWILIO_PHONE_NUMBER")
my_number = os.getenv("HUMAN_AGENT_NUMBER")

client = Client(account_sid, auth_token)

def trigger_callback_outbound(tunnel_url):
    """
    Forces Twilio to dial your phone. 
    When you answer, Twilio hits your ngrok tunnel URL to know what to say to you.
    """
    call = client.calls.create(
        to=my_number,
        from_=trail_number,      # Note: Twilio SDK uses 'from_' because 'from' is a reserved keyword in Python
        url=f"{tunnel_url}/voice/callback-speak"
    )
    print(f"Callback has been initiated. Call SID: {call.sid}")

# ════════════════════════════════════════════════════
# EXECUTION BLOCK FOR STANDALONE TERMINAL RUNS
# ════════════════════════════════════════════════════
if __name__ == "__main__":
    # Your updated ngrok base tunnel path
    CURRENT_TUNNEL = "https://waffle-handcart-craftwork.ngrok-free.dev"
    
    print(f"Connecting to Twilio and initiating outbound pipeline via {CURRENT_TUNNEL}...")
    trigger_callback_outbound(CURRENT_TUNNEL)