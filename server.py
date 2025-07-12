import asyncio
import base64
import json
import sys
import pathlib
import os
import time

from dotenv import load_dotenv
import websockets  # ✅ legacy API
import websockets.legacy.server as ws_server
import websockets.legacy.client as ws_client

load_dotenv()

CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"
PROMPT_PATH = pathlib.Path(__file__).parent / "prompt.txt"

def sts_connect():
    api_key = os.getenv('DEEPGRAM_API_KEY')
    if not api_key:
        raise ValueError("DEEPGRAM_API_KEY environment variable is not set")

    print(f"[{time.time():.3f}] ✅ API Key loaded, length: {len(api_key.strip())}")

    return ws_client.connect(   # ✅ именно legacy client!
        "wss://agent.deepgram.com/v1/agent/converse",
        extra_headers={"Authorization": f"Token {api_key.strip()}"}
    )

async def twilio_handler(twilio_ws):
    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    global last_user_chunk_ts
    global tts_start_ts
    last_user_chunk_ts = 0.0
    tts_start_ts = 0.0

    async with sts_connect() as sts_ws:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config_message = json.load(f)

        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            prompt_text = f.read().strip()
            config_message["agent"]["think"]["prompt"] = prompt_text

        await sts_ws.send(json.dumps(config_message))
        print(f"[{time.time():.3f}] ✅ Sent initial config to Deepgram")

        async def sts_sender():
            print(f"[{time.time():.3f}] 🔄 sts_sender started")
            while True:
                chunk = await audio_queue.get()
                print(f"[{time.time():.3f}] sts_sender → Deepgram | chunk size: {len(chunk)} bytes")
                await sts_ws.send(chunk)

        async def sts_receiver():
            print(f"[{time.time():.3f}] 🔄 sts_receiver started")
            streamsid = await streamsid_queue.get()
            print(f"[{time.time():.3f}] sts_receiver got streamsid: {streamsid}")

            should_clear = False
            first_tts_chunk = True

            async for message in sts_ws:
                if isinstance(message, str):
                    decoded = json.loads(message)
                    print(f"[{time.time():.3f}] 🗨 Control message: {decoded}")

                    if decoded['type'] == 'UserStartedSpeaking':
                        clear_message = {
                            "event": "clear",
                            "streamSid": streamsid
                        }
                        await twilio_ws.send(json.dumps(clear_message))
                        should_clear = True
                        first_tts_chunk = True
                        print(f"[{time.time():.3f}] 🚫 Barge-in: should_clear=True, first_tts_chunk=True")
                    continue

                if first_tts_chunk:
                    global tts_start_ts, last_user_chunk_ts
                    tts_start_ts = time.time()
                    latency_gap = tts_start_ts - last_user_chunk_ts
                    print(f"[{tts_start_ts:.3f}] 🎙️ First TTS chunk → should_clear=False | Latency GAP: {latency_gap:.3f} sec")
                    should_clear = False
                    first_tts_chunk = False

                if should_clear:
                    print(f"[{time.time():.3f}] 🔕 Dropped TTS chunk (barge-in active)")
                    continue

                media_message = {
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": base64.b64encode(message).decode("ascii")},
                }
                await twilio_ws.send(json.dumps(media_message))
                print(f"[{time.time():.3f}] 📤 Sent TTS chunk → Twilio")

        async def twilio_receiver():
            print(f"[{time.time():.3f}] 🔄 twilio_receiver started")
            BUFFER_SIZE = 5 * 160  # 0.1 сек аудио
            inbuffer = bytearray(b"")

            async for message in twilio_ws:
                try:
                    data = json.loads(message)
                    event = data.get("event")
                    if event == "start":
                        streamsid = data["start"]["streamSid"]
                        streamsid_queue.put_nowait(streamsid)
                        print(f"[{time.time():.3f}] 🟢 Twilio start event | streamSid={streamsid}")
                    elif event == "connected":
                        print(f"[{time.time():.3f}] Twilio connected event")
                        continue
                    elif event == "media":
                        media = data["media"]
                        chunk = base64.b64decode(media["payload"])
                        if media["track"] == "inbound":
                            inbuffer.extend(chunk)
                        print(f"[{time.time():.3f}] 🔊 Twilio inbound chunk | buffer size: {len(inbuffer)} bytes")
                    elif event == "stop":
                        print(f"[{time.time():.3f}] 🛑 Twilio stop event")
                        break

                    while len(inbuffer) >= BUFFER_SIZE:
                        chunk = inbuffer[:BUFFER_SIZE]
                        audio_queue.put_nowait(chunk)
                        global last_user_chunk_ts
                        last_user_chunk_ts = time.time()
                        print(f"[{last_user_chunk_ts:.3f}] 📥 Queued user chunk → Deepgram | size: {len(chunk)} bytes")
                        inbuffer = inbuffer[BUFFER_SIZE:]
                except Exception as e:
                    print(f"[{time.time():.3f}] ❌ twilio_receiver error: {e}")
                    break

        await asyncio.wait(
            [
                asyncio.create_task(sts_sender()),
                asyncio.create_task(sts_receiver()),
                asyncio.create_task(twilio_receiver()),
            ]
        )

        await twilio_ws.close()
        print(f"[{time.time():.3f}] ✅ twilio_handler closed Twilio WS connection")


async def router(websocket, path):
    print(f"Path: {path}")
    if path == "/twilio":
        print(f"[{time.time():.3f}] 🚦 Starting Twilio handler")
        await twilio_handler(websocket)
    else:
        print(f"[{time.time():.3f}] ❌ Unknown path: {path}")
        await websocket.close()

async def main():
    async with ws_server.serve(router, "localhost", 5000):
        print("Server started ws://localhost:5000")
        await asyncio.Future()

asyncio.run(main())
