import asyncio
import base64
import json
import sys
import pathlib
import os
import time
import tempfile
import random
import glob
from dotenv import load_dotenv
import websockets
import subprocess
import websockets.legacy.server as ws_server
import websockets.legacy.client as ws_client

load_dotenv()

CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"
PROMPT_PATH = pathlib.Path(__file__).parent / "prompt.txt"

# === Глобальные ===
bot_is_speaking = False
bot_thinking = False

def sts_connect():
    api_key = os.getenv('DEEPGRAM_API_KEY')
    if not api_key:
        raise ValueError("DEEPGRAM_API_KEY environment variable is not set")

    print(f"[{time.time():.3f}] ✅ API Key loaded, length: {len(api_key.strip())}")

    return ws_client.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        extra_headers={"Authorization": f"Token {api_key.strip()}"}
    )

async def play_filler_to_twilio(twilio_ws, streamsid):
    filler_file = "assets/keyboard.mulaw"
    chunk_size = 160  # ~20ms при 8kHz

    try:
        with open(filler_file, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    f.seek(0)
                    continue

                payload = base64.b64encode(chunk).decode()
                media_message = {
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": payload},
                }
                await twilio_ws.send(json.dumps(media_message))
                await asyncio.sleep(0.02)  # имитация real-time
    except asyncio.CancelledError:
        print(f"[{time.time():.3f}] 🛑 Filler loop stopped cleanly (CancelledError)")
        raise

async def twilio_handler(twilio_ws):
    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    global last_user_chunk_ts
    global tts_start_ts
    global bot_is_speaking, bot_thinking

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
            global bot_is_speaking, bot_thinking

            filler_task = None
            filler_sent = False

            tts_buffer = bytearray()
            last_chunk_ts = time.time()
            IDLE_TIMEOUT = 0.1

            async def process_tts_buffer():
                nonlocal tts_buffer
                if not tts_buffer:
                    return

                with tempfile.NamedTemporaryFile(delete=False) as tmp_in:
                    tmp_in.write(tts_buffer)
                    tmp_in.flush()
                    tmp_input_path = tmp_in.name

                with tempfile.NamedTemporaryFile(delete=False) as tmp_out:
                    tmp_output_path = tmp_out.name

                print(f"[{time.time():.3f}] ⚡ Running ffmpeg atempo=1.3")
                subprocess.run([
                    "ffmpeg",
                    "-y",
                    "-f", "mulaw",
                    "-ar", "8000",
                    "-i", tmp_input_path,
                    "-filter:a", "atempo=1.3",
                    "-f", "mulaw",
                    tmp_output_path
                ], check=True)

                with open(tmp_output_path, "rb") as f:
                    sped_up_data = f.read()

                os.unlink(tmp_input_path)
                os.unlink(tmp_output_path)

                media_message = {
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": base64.b64encode(sped_up_data).decode("ascii")},
                }
                await twilio_ws.send(json.dumps(media_message))
                print(f"[{time.time():.3f}] 🚀 Sent sped-up chunk → Twilio")

                tts_buffer = bytearray()

            while True:
                try:
                    message = await asyncio.wait_for(sts_ws.recv(), timeout=IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    if tts_buffer:
                        print(f"[{time.time():.3f}] ⏰ Idle timeout — processing TTS buffer")
                        await process_tts_buffer()
                    continue

                if isinstance(message, str):
                    decoded = json.loads(message)
                    print(f"[{time.time():.3f}] 🗨 Control message: {decoded}")

                    if decoded.get("type") == "ConversationText":
                        if decoded.get("role") == "user":
                            bot_thinking = True
                            if (filler_task is None or filler_task.done()) and not filler_sent:
                                filler_task = asyncio.create_task(
                                    play_filler_to_twilio(twilio_ws, streamsid)
                                )
                                print(f"[{time.time():.3f}] 💬 Filler task STARTED id={id(filler_task)}")
                                filler_sent = True

                        elif decoded.get("role") == "assistant":
                            pass

                    if decoded.get("type") == "AgentAudioDone":
                        if filler_task and not filler_task.done():
                            await asyncio.sleep(0.1)
                            filler_task.cancel()
                            print(f"[{time.time():.3f}] ✂️ Filler CANCELLED by AgentAudioDone id={id(filler_task)}")
                        bot_is_speaking = False
                        bot_thinking = False
                        filler_sent = False
                        print(f"[{time.time():.3f}] 🟢 TTS done → bot_is_speaking=False, bot_thinking=False")

                    if decoded['type'] == 'UserStartedSpeaking':
                        if tts_buffer:
                            print(f"[{time.time():.3f}] 🚫 Barge-in — process remaining TTS buffer")
                            await process_tts_buffer()

                        clear_message = {
                            "event": "clear",
                            "streamSid": streamsid
                        }
                        await twilio_ws.send(json.dumps(clear_message))
                        should_clear = True
                        first_tts_chunk = True
                        print(f"[{time.time():.3f}] 🚫 Barge-in: should_clear=True, first_tts_chunk=True")
                    continue

                bot_is_speaking = True

                if first_tts_chunk:
                    if filler_task and not filler_task.done():
                        filler_task.cancel()
                        print(f"[{time.time():.3f}] ✂️ Filler CANCELLED by first TTS chunk id={id(filler_task)}")
                    else:
                        print(f"[{time.time():.3f}] ⚠️ No filler task to cancel on first TTS chunk")

                    global tts_start_ts, last_user_chunk_ts
                    tts_start_ts = time.time()
                    latency_gap = tts_start_ts - last_user_chunk_ts
                    print(f"[{tts_start_ts:.3f}] 🎙️ First TTS chunk → should_clear=False | Latency GAP: {latency_gap:.3f} sec")
                    should_clear = False
                    first_tts_chunk = False

                if should_clear:
                    print(f"[{time.time():.3f}] 🔕 Dropped TTS chunk (barge-in active)")
                    continue

                tts_buffer.extend(message)
                last_chunk_ts = time.time()
                print(f"[{time.time():.3f}] 📥 TTS chunk buffered | size: {len(tts_buffer)} bytes")

        async def twilio_receiver():
            print(f"[{time.time():.3f}] 🔄 twilio_receiver started")
            BUFFER_SIZE = 5 * 160
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
