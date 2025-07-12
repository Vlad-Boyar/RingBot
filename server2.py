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
last_user_chunk_ts = time.time()
bot_is_speaking = False  # 🚩 Новый флаг

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

async def vad_filler_loop(twilio_ws, streamsid):
    global last_user_chunk_ts, bot_is_speaking
    VAD_SILENCE_GAP = 1.5

    print(f"[{time.time():.3f}] ✅ VAD filler loop started")

    filler_sent = False  # 🚩 Флаг для текущей паузы

    while not twilio_ws.closed:
        await asyncio.sleep(0.2)
        gap = time.time() - last_user_chunk_ts
        print(f"[{time.time():.3f}] ⏱ GAP: {gap:.2f} sec | bot_is_speaking={bot_is_speaking} | filler_sent={filler_sent}")

        if gap > VAD_SILENCE_GAP and not bot_is_speaking:
            if not filler_sent:
                await play_filler_to_twilio(twilio_ws, streamsid)
                filler_sent = True
        else:
            # Если пользователь сказал что-то новое — сбрасываем флаг
            filler_sent = False

def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise ValueError("DEEPGRAM_API_KEY environment variable not set")
    return connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        extra_headers={"Authorization": f"Token {api_key.strip()}"}
    )

async def twilio_handler(twilio_ws):
    global last_user_chunk_ts, bot_is_speaking
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
            vad_task = None

            async for msg in twilio_ws:
                data = json.loads(msg)
                event = data.get("event")

                if event == "start":
                    streamsid = data["start"]["streamSid"]
                    streamsid_queue.put_nowait(streamsid)
                    print(f"[{time.time():.3f}] 🟢 Twilio START {streamsid}")
                    vad_task = asyncio.create_task(vad_filler_loop(twilio_ws, streamsid))

                elif event == "media":
                    chunk = base64.b64decode(data["media"]["payload"])
                    if data["media"]["track"] == "inbound":
                        inbuffer.extend(chunk)

                elif event == "stop":
                    print(f"[{time.time():.3f}] 🛑 Twilio STOP")
                    if vad_task:
                        vad_task.cancel()
                    break

                while len(inbuffer) >= BUFFER_SIZE:
                    chunk = inbuffer[:BUFFER_SIZE]
                    audio_queue.put_nowait(chunk)
                    last_user_chunk_ts = time.time()
                    print(f"[{time.time():.3f}] 🎙️ User chunk → Deepgram")
                    inbuffer = inbuffer[BUFFER_SIZE:]

        async def sts_sender():
            while True:
                chunk = await audio_queue.get()
                await sts_ws.send(chunk)

        async def sts_receiver():
            global bot_is_speaking
            streamsid = await streamsid_queue.get()

            while True:
                msg = await sts_ws.recv()
                if isinstance(msg, str):
                    decoded = json.loads(msg)
                    print(f"Deepgram: {decoded}")

                    if decoded.get("type") == "AgentAudioDone":
                        bot_is_speaking = False
                        print(f"[{time.time():.3f}] 🟢 TTS done → bot_is_speaking=False")
                    continue

                bot_is_speaking = True
                payload = base64.b64encode(msg).decode()
                media_message = {
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": payload},
                }
                await twilio_ws.send(json.dumps(media_message))
                print(f"[{time.time():.3f}] 🔈 Sent TTS chunk")

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

asyncio.run(main())
