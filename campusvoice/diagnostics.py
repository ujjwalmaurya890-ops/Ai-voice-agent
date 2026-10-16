"""First-run checks:  python -m campusvoice.diagnostics --devices --key-status --live-check"""
from __future__ import annotations

import argparse
import asyncio
import sys

from google import genai

from . import audio, config
from .live_session import build_live_config
from .logging_setup import setup_logging
from .secure_key import get_api_key, redact


def key_status() -> bool:
    key, source = get_api_key()
    if key:
        print(f"API key: found via {source} (value hidden).")
        return True
    print("API key: NOT found. Set GEMINI_API_KEY or use 'API key...' in the app.")
    return False


def model_status() -> bool:
    """Report the resolved model id, its capabilities, and any config warnings."""
    caps = config.capabilities_for(config.MODEL_ID)
    print(f"Model: {config.MODEL_ID}")
    print(
        f"  audio in/out: {caps.audio_in}/{caps.audio_out}  "
        f"tool calling: {'async-only' if caps.async_tools_only else 'sync'}  "
        f"thinking: {caps.thinking}"
    )
    if caps.thinking:
        print(f"  thinking level: {config.THINKING_LEVEL}")
        if config.THINKING_LEVEL_WARNING:
            print(f"  WARNING: {config.THINKING_LEVEL_WARNING}")
    if config.MODEL_ID_WARNING:
        print(f"  WARNING: {config.MODEL_ID_WARNING}")
        return False
    return True


def devices() -> bool:
    try:
        print(audio.list_devices())
        return True
    except Exception as exc:
        print(f"Audio devices: could not query ({exc}).")
        return False


async def _live_check(key: str) -> bool:
    client = genai.Client(api_key=key)
    cfg = build_live_config(config.MODEL_ID)
    audio_bytes = 0
    transcript = ""
    async with client.aio.live.connect(model=config.MODEL_ID, config=cfg) as session:
        await session.send_realtime_input(text="Reply with one short sentence confirming you can hear me.")
        async def collect() -> None:
            nonlocal audio_bytes, transcript
            async for msg in session.receive():
                sc = msg.server_content
                if sc is None:
                    continue
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            audio_bytes += len(part.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    transcript += sc.output_transcription.text
        await asyncio.wait_for(collect(), timeout=30)
    seconds = audio_bytes / 2 / config.OUTPUT_SAMPLE_RATE
    print(f"Live check: received {seconds:.1f}s of audio from {config.MODEL_ID}.")
    if transcript:
        print(f"Model said: {transcript.strip()}")
    return audio_bytes > 0


def live_check() -> bool:
    key, _ = get_api_key()
    if not key:
        print("Live check skipped: no API key.")
        return False
    try:
        return asyncio.run(_live_check(key))
    except Exception as exc:
        print(f"Live check FAILED: {redact(exc, key)}")
        return False


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="CampusVoice diagnostics")
    parser.add_argument("--devices", action="store_true", help="list audio devices")
    parser.add_argument("--key-status", action="store_true", help="check API key is available")
    parser.add_argument("--model-status", action="store_true", help="show the resolved model and its capabilities")
    parser.add_argument("--live-check", action="store_true", help="connect to Gemini Live and get one spoken reply")
    args = parser.parse_args()
    if not (args.devices or args.key_status or args.model_status or args.live_check):
        parser.print_help()
        return
    ok = True
    if args.key_status:
        ok &= key_status()
    if args.model_status:
        ok &= model_status()
    if args.devices:
        ok &= devices()
    if args.live_check:
        ok &= live_check()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
