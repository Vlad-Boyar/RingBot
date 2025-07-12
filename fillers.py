import requests
import pathlib
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DEEPGRAM_API_KEY")
PHRASES = ["Hmm", "Well", "Let me think"]

for idx, phrase in enumerate(PHRASES):
    response = requests.post(
        "https://api.deepgram.com/v1/speak?model=aura-2-andromeda-en&encoding=mulaw&sample_rate=8000",
        headers={
            "Authorization": f"Token {API_KEY}",
            "Content-Type": "application/json"
        },
        json={"text": phrase}
    )

    if response.status_code == 200:
        out_path = pathlib.Path(f"assets/filler_{idx+1}_{phrase.lower().replace(' ', '_')}.mulaw")
        out_path.parent.mkdir(exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(response.content)
        print(f"✅ Saved: {out_path}")
    else:
        print(f"❌ Error {response.status_code}: {response.text}")
