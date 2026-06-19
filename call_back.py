import os
from twilio.rest import Client
from dotenv import load_dotenv

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
trail_number = os.getenv("TWILIO_PHONE_NUMBER")
my_number = os.getenv("HUMAN_AGENT_NUMBER"

client = Client(account_sid,auth_token)

def trigger_callback_outbound(ngrok_url):
    """
    Forces Twilio to dial your phone. 
    When you answer, Twilio hits the URL to know what to say to you.
    """
    call = client.call.create(
        to = my_number,
        from = trail_number,

        url = f"{ngrok_url}/voice/callback-speak"
    )
    print("Callback has been initiated")