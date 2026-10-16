"""Gemini Live API session: audio in/out, interruption, tools, reconnection.

Layers (bottom to top):
  build_live_config  - the LiveConnectConfig sent when connecting
  MessageHandler     - reacts to each server message (audio, interrupt, tools)
  LiveRunner         - one asyncio loop: connect, stream, reconnect with resume
  VoiceSession       - thread-safe facade the GUI uses
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from google import genai
from google.genai import types

from . import config
from .audio import AudioIO, PlaybackBuffer
from .secure_key import redact
from .tools import MUTATING_TOOLS, StudyStore, build_tools, execute_tool

log = logging.getLogger(__name__)

# ---- events delivered to the UI ---------------------------------------
STATUS = "status"  # connecting | listening | reconnecting | stopped
USER_TEXT = "user_text"  # transcript fragment of what the student said
MODEL_TEXT = "model_text"  # transcript fragment of what the model said
INTERRUPTED = "interrupted"
TURN_COMPLETE = "turn_complete"
TOOL = "tool"
TOOLS_CHANGED = "tools_changed"
ERROR = "error"
# Extended-thinking models keep reasoning/tool-calling after turn_complete; the
# server tells us whether it is still busy via `interaction_status`.
# https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live-extended-thinking
THINKING = "thinking"  # interaction_status == IN_PROGRESS after turn_complete
IDLE = "idle"  # interaction_status == IDLE: the server is truly done
TOOL_TIMEOUT = "tool_timeout"
WARNING = "warning"  # non-fatal configuration notice, e.g. an unknown model id

# A tool call is allowed this long to finish before CampusVoice reports a
# timeout to the model instead of hanging the receive loop indefinitely.
TOOL_CALL_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class UiEvent:
    kind: str
    text: str = ""


Emit = Callable[[UiEvent], None]


def build_live_config(
    model: str, handle: Optional[str] = None, voice: str = config.VOICE_NAME
) -> types.LiveConnectConfig:
    """Connection settings.

    - Server VAD with START_OF_ACTIVITY_INTERRUPTS: speaking over the model
      cancels its reply (barge-in).
    - Context window compression + session resumption keep the conversation
      alive past the 15-minute audio limit and ~10-minute connection limit.
    - Capabilities (thinking, sync vs. async tools) are looked up per model
      instead of assumed, so an unsupported combination is never sent:
      https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live-extended-thinking
      documents thinking_level and NON_BLOCKING-only function calling as
      specific to that model; gemini-3.8-live must omit thinking_config.
    """
    caps = config.capabilities_for(model)
    kwargs: dict[str, Any] = dict(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=config.SYSTEM_PROMPT,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                prefix_padding_ms=config.VAD_PREFIX_PADDING_MS,
                silence_duration_ms=config.VAD_SILENCE_MS,
            ),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        ),
        session_resumption=types.SessionResumptionConfig(handle=handle),
        tools=build_tools(async_only=caps.async_tools_only),
    )
    if caps.thinking:
        kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=types.ThinkingLevel(config.THINKING_LEVEL.upper())
        )
    return types.LiveConnectConfig(**kwargs)


# ---- server message handling ------------------------------------------
class MessageHandler:
    def __init__(self, playback: PlaybackBuffer, store: StudyStore, emit: Emit) -> None:
        self._playback = playback
        self._store = store
        self._emit = emit
        self.resumption_handle: Optional[str] = None
        self.go_away_seen = False
        # Function-call ids are unique per call; remembering recently answered
        # ones stops a duplicate/replayed tool_call message from running a
        # mutating tool (e.g. add_task) twice. Bounded (deque + set) so
        # memory cannot grow without limit across a long session.
        self._answered_call_ids: "set[str]" = set()
        self._answered_call_order: "deque[str]" = deque(maxlen=500)

    async def handle(self, message: Any, session: Any) -> None:
        content = getattr(message, "server_content", None)
        if content is not None:
            self._handle_content(content)

        update = getattr(message, "session_resumption_update", None)
        if update is not None and update.resumable and update.new_handle:
            self.resumption_handle = update.new_handle

        if getattr(message, "go_away", None) is not None:
            self.go_away_seen = True
            self._emit(UiEvent(STATUS, "reconnecting"))

        tool_call = getattr(message, "tool_call", None)
        if tool_call is not None and tool_call.function_calls:
            await self._handle_tool_call(tool_call, session)

    def _handle_content(self, content: Any) -> None:
        # Barge-in: the server cancelled its reply. Drop queued speech NOW.
        if getattr(content, "interrupted", False):
            dropped = self._playback.clear()
            log.info("Interrupted; dropped %d bytes of queued audio", dropped)
            self._emit(UiEvent(INTERRUPTED))

        model_turn = getattr(content, "model_turn", None)
        if model_turn is not None and model_turn.parts:
            for part in model_turn.parts:  # one event can carry several parts
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    self._playback.write(inline.data)

        user_tx = getattr(content, "input_transcription", None)
        if user_tx is not None and user_tx.text:
            self._emit(UiEvent(USER_TEXT, user_tx.text))

        model_tx = getattr(content, "output_transcription", None)
        if model_tx is not None and model_tx.text:
            self._emit(UiEvent(MODEL_TEXT, model_tx.text))

        # interaction_status is "always sent alongside turn_complete" per the
        # extended-thinking docs. On models without async reasoning it is
        # None/absent and turn_complete alone means "idle"; on
        # gemini-3.8-live-extended-thinking, turn_complete can fire while the
        # server is still reasoning or running a NON_BLOCKING tool call, so we
        # surface IN_PROGRESS as a distinct "thinking" state instead of
        # telling the UI the conversation turn is fully done.
        status = getattr(content, "interaction_status", None)
        status_name = getattr(status, "name", status)
        if getattr(content, "turn_complete", False):
            if status_name == "IN_PROGRESS":
                self._emit(UiEvent(THINKING))
            else:
                self._emit(UiEvent(TURN_COMPLETE))
        elif status_name == "IDLE":
            self._emit(UiEvent(IDLE))

    async def _handle_tool_call(self, tool_call: Any, session: Any) -> None:
        responses = []
        changed = False
        for call in tool_call.function_calls:
            if call.id and call.id in self._answered_call_ids:
                log.warning("Ignoring duplicate tool call id=%s name=%s", call.id, call.name)
                continue
            try:
                outcome = await asyncio.wait_for(
                    asyncio.to_thread(execute_tool, self._store, call.name, call.args),
                    timeout=TOOL_CALL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                log.error("Tool '%s' timed out after %.0fs", call.name, TOOL_CALL_TIMEOUT_SECONDS)
                outcome = {"error": "The tool took too long and was cancelled."}
                self._emit(UiEvent(TOOL_TIMEOUT, call.name))
            if call.id:
                if len(self._answered_call_order) == self._answered_call_order.maxlen:
                    self._answered_call_ids.discard(self._answered_call_order[0])
                self._answered_call_order.append(call.id)
                self._answered_call_ids.add(call.id)
            self._emit(UiEvent(TOOL, call.name))
            if call.name in MUTATING_TOOLS and "error" not in outcome:
                changed = True
            responses.append(
                types.FunctionResponse(
                    id=call.id,
                    name=call.name,
                    response=outcome,
                    # Only applicable to NON_BLOCKING declarations (ignored
                    # otherwise): report results when the model is free so a
                    # background tool result never barges in on its own
                    # speech. https://ai.google.dev/gemini-api/docs/live-api/tools
                    scheduling=types.FunctionResponseScheduling.WHEN_IDLE,
                )
            )
        if responses:
            await session.send_tool_response(function_responses=responses)
        if changed:
            self._emit(UiEvent(TOOLS_CHANGED))


# ---- the streaming loop -----------------------------------------------
def _drain(queue: "asyncio.Queue[Any]") -> None:
    while not queue.empty():
        queue.get_nowait()


class LiveRunner:
    def __init__(
        self,
        client: Any,
        model: str,
        playback: PlaybackBuffer,
        mic_queue: "asyncio.Queue[bytes]",
        text_queue: "asyncio.Queue[str]",
        store: StudyStore,
        emit: Emit,
        *,
        api_key: Optional[str] = None,
        half_duplex: bool = config.HALF_DUPLEX,
        config_factory: Callable[..., Any] = build_live_config,
        max_failures: int = 4,
        min_stable_seconds: float = 3.0,
        backoff_base: float = 1.0,
    ) -> None:
        self._client = client
        self._model = model
        self._playback = playback
        self._mic_queue = mic_queue
        self._text_queue = text_queue
        self._emit = emit
        self._api_key = api_key
        self._half_duplex = half_duplex
        self._config_factory = config_factory
        self._max_failures = max_failures
        self._min_stable = min_stable_seconds
        self._backoff_base = backoff_base
        self._stop = asyncio.Event()
        self._stream_ended = True
        self.muted = False
        self.handler = MessageHandler(playback, store, emit)

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        failures = 0
        self._emit(UiEvent(STATUS, "connecting"))
        while not self._stop.is_set():
            live_config = self._config_factory(
                self._model, handle=self.handler.resumption_handle
            )
            started = loop.time()
            error: Optional[BaseException] = None
            self.handler.go_away_seen = False
            try:
                async with self._client.aio.live.connect(
                    model=self._model, config=live_config
                ) as session:
                    self._emit(UiEvent(STATUS, "listening"))
                    await self._run_connection(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network drop, bad key, bad model, ...
                error = exc
            if self._stop.is_set():
                break

            lived = loop.time() - started
            # A connection that lived a while (or got a server GoAway) is normal
            # churn; one that dies immediately counts toward giving up.
            healthy = lived >= self._min_stable or self.handler.go_away_seen
            failures = 0 if healthy else failures + 1
            if error is not None:
                message = redact(error, self._api_key)
                log.warning("Live connection ended: %s", message)
                self._emit(UiEvent(ERROR, message))
            if failures >= self._max_failures:
                self._emit(
                    UiEvent(
                        ERROR,
                        "Could not keep a connection. Check your API key, internet "
                        f"connection, and that model '{self._model}' is available.",
                    )
                )
                break
            self._emit(UiEvent(STATUS, "reconnecting"))
            # Never reconnect in a tight loop, even after a "healthy" drop.
            delay = min(self._backoff_base * 2 ** (failures - 1), 15.0) if failures else 0.05
            await self._sleep_or_stop(delay)
        self._emit(UiEvent(STATUS, "stopped"))

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run_connection(self, session: Any) -> None:
        _drain(self._mic_queue)  # discard audio captured while disconnected
        self._stream_ended = True
        stop_task = asyncio.create_task(self._stop.wait())
        workers = [
            asyncio.create_task(self._send_audio(session)),
            asyncio.create_task(self._send_text(session)),
            asyncio.create_task(self._receive(session)),
        ]
        done, pending = await asyncio.wait(
            [stop_task, *workers], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task is not stop_task and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]

    async def _send_audio(self, session: Any) -> None:
        mime = f"audio/pcm;rate={config.INPUT_SAMPLE_RATE}"
        while True:
            chunk = await self._mic_queue.get()
            if self.muted:
                # Tell the server the stream paused so it flushes cached audio.
                if not self._stream_ended:
                    await session.send_realtime_input(audio_stream_end=True)
                    self._stream_ended = True
                continue
            if self._half_duplex and self._playback.pending_bytes() > 0:
                continue  # echo guard: don't listen while the model is talking
            self._stream_ended = False
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=mime))

    async def _send_text(self, session: Any) -> None:
        while True:
            text = await self._text_queue.get()
            await session.send_realtime_input(text=text)

    async def _receive(self, session: Any) -> None:
        # receive() ends after each completed turn, so loop for the whole call.
        while True:
            async for message in session.receive():
                await self.handler.handle(message, session)


# ---- facade for the GUI thread ----------------------------------------
class VoiceSession:
    """Runs the asyncio loop on a background thread. All methods are thread-safe."""

    def __init__(self, api_key: str, store: StudyStore, emit: Emit, model: str = config.MODEL_ID) -> None:
        self._api_key = api_key
        self._store = store
        self._emit = emit
        self._model = model
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[LiveRunner] = None
        self._text_queue: Optional["asyncio.Queue[str]"] = None
        self._muted = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="campusvoice-live", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._ready.wait(timeout)
        if self._loop is not None and self._runner is not None:
            try:
                self._loop.call_soon_threadsafe(self._runner.request_stop)
            except RuntimeError:
                pass  # loop already finished
        if self._thread is not None:
            self._thread.join(timeout)

    def send_text(self, text: str) -> None:
        if self._loop is not None and self._text_queue is not None and text.strip():
            self._loop.call_soon_threadsafe(self._text_queue.put_nowait, text.strip())

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if self._runner is not None:
            self._runner.muted = muted

    # -- background thread --
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            self._emit(UiEvent(ERROR, redact(exc, self._api_key)))
            self._emit(UiEvent(STATUS, "stopped"))
        finally:
            self._ready.set()

    async def _main(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        if config.MODEL_ID_WARNING:
            self._emit(UiEvent(WARNING, config.MODEL_ID_WARNING))
        if config.capabilities_for(self._model).thinking and config.THINKING_LEVEL_WARNING:
            self._emit(UiEvent(WARNING, config.THINKING_LEVEL_WARNING))
        mic_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=100)
        self._text_queue = asyncio.Queue()
        playback = PlaybackBuffer()
        client = genai.Client(api_key=self._api_key)
        self._runner = LiveRunner(
            client, self._model, playback, mic_queue, self._text_queue,
            self._store, self._emit, api_key=self._api_key,
        )
        self._runner.muted = self._muted
        audio = AudioIO(loop, mic_queue, playback)
        try:
            audio.start()
        except Exception as exc:
            self._emit(UiEvent(ERROR, f"Could not open the microphone or speakers: {redact(exc, self._api_key)}"))
            self._emit(UiEvent(STATUS, "stopped"))
            self._ready.set()
            return
        self._ready.set()
        try:
            await self._runner.run()
        finally:
            audio.stop()
