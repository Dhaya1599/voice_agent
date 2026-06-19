import sounddevice as sd
import scipy.io.wavfile as wav
import numpy as np
from groq import Groq
from dotenv import load_dotenv
import os
import tempfile

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

SAMPLE_RATE = 16000
DURATION = 5

def record_audio():
    """Record audio from microphone and return as wav bytes"""

    print("Listening.... (speak now)")

    recording = sd.rec(
        int(DURATION * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype=np.int16
    )

    sd.wait() #no interruptions till audio is completed
    print("Recording done")

    temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav.write(temp_file.name, SAMPLE_RATE, recording)

    return temp_file.name

def transcribe(audio_file_path):
    """Send audio file to Groq Whisper and get text back"""

    with open(audio_file_path, "rb") as audio_file:

        response = client.audio.transcriptions .create(
            model="Whisper-large-v3",
            file=audio_file,
            language="en"
        )

    return response.text

def listen():
    """Record and transcribe in one step"""
    audio_path = record_audio()
    text = transcribe(audio_path)

    os.unlink(audio_path)

    return text

if __name__ == "__main__":
    print("STT Test -- Speak after the prompt\n")

    while True:
        input("Press ENTER to start recording...")
        text = listen()
        print(f"You said:{text}\n")
