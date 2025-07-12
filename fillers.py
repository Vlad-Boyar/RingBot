import requests
import pathlib
import os
import subprocess
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DEEPGRAM_API_KEY")
PHRASES = ["Hmm", "Well", "Let me think"]

ASSETS_DIR = pathlib.Path("assets")
ASSETS_DIR.mkdir(exist_ok=True)

for idx, phrase in enumerate(PHRASES):
    url = (
        "https://api.deepgram.com/v1/speak?"
        "model=aura-2-andromeda-en"
        "&encoding=mulaw"
        "&sample_rate=8000"
        "&channels=1"
    )

    response = requests.post(
        url,
        headers={
            "Authorization": f"Token {API_KEY}",
            "Content-Type": "application/json"
        },
        json={"text": phrase}
    )

    if response.status_code == 200:
        step0_path = ASSETS_DIR / f"filler_{idx+1}_{phrase.lower().replace(' ', '_')}_raw.mulaw"
        step1_path = ASSETS_DIR / f"filler_{idx+1}_{phrase.lower().replace(' ', '_')}_step1.mulaw"
        final_path = ASSETS_DIR / f"filler_{idx+1}_{phrase.lower().replace(' ', '_')}.mulaw"

        # Сохраняем ответ как step0
        with open(step0_path, "wb") as f:
            f.write(response.content)
        print(f"✅ Downloaded: {step0_path.name}")

        # === Первый прогон ===
        cmd1 = [
            "ffmpeg",
            "-f", "mulaw",
            "-ar", "8000",
            "-i", str(step0_path),
            "-af", "afade=t=in:ss=0:d=0.03,apad=pad_dur=0.05",
            "-c:a", "pcm_mulaw",
            "-ar", "8000",
            "-f", "mulaw",
            "-y",
            str(step1_path)
        ]
        subprocess.run(cmd1, check=True)
        print(f"✅ Pass 1: {step1_path.name}")

        # === Второй прогон ===
        cmd2 = [
            "ffmpeg",
            "-f", "mulaw",
            "-ar", "8000",
            "-i", str(step1_path),
            "-af", "afade=t=in:ss=0:d=0.03,apad=pad_dur=0.05",
            "-c:a", "pcm_mulaw",
            "-ar", "8000",
            "-f", "mulaw",
            "-y",
            str(final_path)
        ]
        subprocess.run(cmd2, check=True)
        print(f"✅ Pass 2: {final_path.name}")

        os.remove(step0_path)
        os.remove(step1_path)
        print(f"🗑️ Deleted temp: {step0_path.name}, {step1_path.name}")

    else:
        print(f"❌ Error {response.status_code}: {response.text}")

print("🎉 All fillers generated and fixed! Check assets/")
