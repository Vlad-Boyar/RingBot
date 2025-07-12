import asyncio
import base64
import json
import os
import pathlib
import time
from websockets.legacy.server import serve
from websockets.legacy.client import connect

CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"
PROMPT_PATH = pathlib.Path(__file__).parent / "prompt.txt"

# === Глобальные ===
bot_is_speaking = False
bot_thinking = False

async def play_filler_to_twilio(twilio_ws, streamsid):
    filler_file = "assets/filler.mulaw"
    if not os.path.isfile(filler_file):
        print("❌ Filler file not found!")
        return

    with open(filler_file, "rb") as f:
        data = f.read()

    payload = base64.b64encode(data).decode()
    media_message = {
        "event": "media",
        "streamSid": streamsid,
        "media": {"payload": payload},
    }
    await twilio_ws.send(json.dumps(media_message))
    print(f"[{time.time():.3f}] 🐑 Sent filler ({len(data)} bytes)")

def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise ValueError("DEEPGRAM_API_KEY environment variable not set")
    return connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        extra_headers={"Authorization": f"Token {api_key.strip()}"}
    )

async def twilio_handler(twilio_ws):
    global bot_is_speaking, bot_thinking
    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    async with sts_connect() as sts_ws:
        with open(CONFIG_PATH, "r") as f:
            config_message = json.load(f)
        with open(PROMPT_PATH, "r") as f:
            prompt_text = f.read().strip()
            config_message["agent"]["think"]["prompt"] = prompt_text

        await sts_ws.send(json.dumps(config_message))
        print(f"[{time.time():.3f}] ✅ Sent config to Deepgram")

        async def twilio_receiver():
            BUFFER_SIZE = 5 * 160
            inbuffer = bytearray()

            async for msg in twilio_ws:
                data = json.loads(msg)
                event = data.get("event")

                if event == "start":
                    streamsid = data["start"]["streamSid"]
                    streamsid_queue.put_nowait(streamsid)
                    print(f"[{time.time():.3f}] 🟢 Twilio START {streamsid}")

                elif event == "media":
                    chunk = base64.b64decode(data["media"]["payload"])
                    if data["media"]["track"] == "inbound":
                        inbuffer.extend(chunk)

                elif event == "stop":
                    print(f"[{time.time():.3f}] 🛑 Twilio STOP")
                    break

                while len(inbuffer) >= BUFFER_SIZE:
                    chunk = inbuffer[:BUFFER_SIZE]
                    audio_queue.put_nowait(chunk)
                    inbuffer = inbuffer[BUFFER_SIZE:]
                    print(f"[{time.time():.3f}] 🎙️ User chunk → Deepgram")

        async def sts_sender():
            while True:
                chunk = await audio_queue.get()
                await sts_ws.send(chunk)

        async def sts_receiver():
            global bot_is_speaking, bot_thinking
            streamsid = await streamsid_queue.get()
            filler_sent = False

            while True:
                msg = await sts_ws.recv()
                if isinstance(msg, str):
                    decoded = json.loads(msg)
                    print(f"Deepgram: {decoded}")

                    if decoded.get("type") == "ConversationText":
                        if decoded.get("role") == "user":
                            bot_thinking = True
                            if not bot_is_speaking and not filler_sent:
                                await play_filler_to_twilio(twilio_ws, streamsid)
                                filler_sent = True
                                print(f"[{time.time():.3f}] 💬 Filler sent after user speech")

                        elif decoded.get("role") == "assistant":
                            bot_thinking = True

                    if decoded.get("type") == "AgentAudioDone":
                        bot_is_speaking = False
                        bot_thinking = False
                        filler_sent = False
                        print(f"[{time.time():.3f}] 🟢 TTS done → bot_is_speaking=False, bot_thinking=False")
                    continue

                bot_is_speaking = True
                payload = base64.b64encode(msg).decode()
                media_message = {
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": payload},
                }
                await twilio_ws.send(json.dumps(media_message))
                print(f"[{time.time():.3f}] 🔈 Sent TTS chunk → bot_is_speaking=True")

        await asyncio.wait([
            asyncio.create_task(twilio_receiver()),
            asyncio.create_task(sts_sender()),
            asyncio.create_task(sts_receiver())
        ])

async def router(websocket, path):
    if path == "/twilio":
        await twilio_handler(websocket)
    else:
        await websocket.close()

async def main():
    async with serve(router, "localhost", 5000):
        print("✅ WS Server started ws://localhost:5000")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
