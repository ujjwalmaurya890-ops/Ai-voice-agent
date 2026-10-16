"""Microphone capture and speaker playback.

PlaybackBuffer is a thread-safe byte FIFO whose clear() lets us stop speech
instantly when the user interrupts (barge-in). sounddevice is imported lazily
so the rest of the app and the tests work without audio hardware.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from . import config

log = logging.getLogger(__name__)


class PlaybackBuffer:
    def __init__(self, max_seconds: int = 60, rate: int = config.OUTPUT_SAMPLE_RATE) -> None:
        self._buf = bytearray()
        self._carry = b""
        self._lock = threading.Lock()
        self._max_bytes = max_seconds * rate * 2

    def write(self, data: bytes) -> None:
        """Append 16-bit PCM. Odd trailing bytes are carried to the next write."""
        if not data:
            return
        with self._lock:
            data = self._carry + data
            if len(data) % 2:
                self._carry, data = data[-1:], data[:-1]
            else:
                self._carry = b""
            self._buf += data
            overflow = len(self._buf) - self._max_bytes
            if overflow > 0:
                del self._buf[: overflow + (overflow % 2)]

    def read(self, nbytes: int) -> bytes:
        """Return exactly nbytes, padding with silence when the buffer runs dry."""
        with self._lock:
            chunk = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
        if len(chunk) < nbytes:
            chunk += b"\x00" * (nbytes - len(chunk))
        return chunk

    def clear(self) -> int:
        """Drop all queued audio. Returns the number of bytes discarded."""
        with self._lock:
            dropped = len(self._buf)
            self._buf.clear()
            self._carry = b""
        return dropped

    def pending_bytes(self) -> int:
        with self._lock:
            return len(self._buf)


def _sounddevice() -> Any:
    import sounddevice  # needs PortAudio; the Windows wheel bundles it

    return sounddevice


def list_devices() -> str:
    return str(_sounddevice().query_devices())


class AudioIO:
    """Owns the mic input stream and speaker output stream."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        mic_queue: "asyncio.Queue[bytes]",
        playback: PlaybackBuffer,
        input_device: Optional[int] = None,
        output_device: Optional[int] = None,
    ) -> None:
        self._loop = loop
        self._mic_queue = mic_queue
        self._playback = playback
        self._input_device = input_device
        self._output_device = output_device
        self._in: Any = None
        self._out: Any = None

    # Runs on the PortAudio thread: hand bytes to the asyncio loop safely.
    def _on_mic(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            log.warning("Mic stream status: %s", status)
        try:
            self._loop.call_soon_threadsafe(self._push, bytes(indata))
        except RuntimeError:
            pass  # loop already closed during shutdown

    def _push(self, data: bytes) -> None:
        if self._mic_queue.full():
            try:
                self._mic_queue.get_nowait()  # drop oldest, keep audio fresh
            except asyncio.QueueEmpty:
                pass
        self._mic_queue.put_nowait(data)

    # Also on the PortAudio thread.
    def _on_speaker(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        outdata[:] = self._playback.read(frames * 2)

    def start(self) -> None:
        sd = _sounddevice()
        self._in = sd.RawInputStream(
            samplerate=config.INPUT_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=config.MIC_BLOCK_FRAMES,
            device=self._input_device,
            callback=self._on_mic,
        )
        self._out = sd.RawOutputStream(
            samplerate=config.OUTPUT_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=config.SPEAKER_BLOCK_FRAMES,
            device=self._output_device,
            callback=self._on_speaker,
        )
        self._in.start()
        self._out.start()

    def stop(self) -> None:
        for stream in (self._in, self._out):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    log.exception("Error closing audio stream")
        self._in = self._out = None
        self._playback.clear()
