"""Simulated Gemini Live sessions: no network, no audio hardware.

Messages are built from the real google-genai types, so a renamed or
misspelled SDK field fails here instead of on your machine.
"""
import asyncio
from contextlib import asynccontextmanager

from google.genai import types

from campusvoice import config
from campusvoice import live_session as ls
from campusvoice.audio import PlaybackBuffer
from campusvoice.tools import StudyStore


# ---- message builders -------------------------------------------------
def audio_msg(n=200):
    part = types.Part(inline_data=types.Blob(data=b"\x01\x02" * n, mime_type="audio/pcm;rate=24000"))
    return types.LiveServerMessage(server_content=types.LiveServerContent(model_turn=types.Content(parts=[part])))


def content_msg(**kw):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kw))


def tool_msg(name, args, call_id="c1"):
    call = types.FunctionCall(id=call_id, name=name, args=args)
    return types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[call]))


def resume_msg(handle):
    return types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle=handle, resumable=True)
    )


# ---- fakes ------------------------------------------------------------
class FakeSession:
    def __init__(self, script, then="block"):
        self.script, self.then = list(script), then
        self.sent, self.tool_responses = [], []

    async def send_realtime_input(self, **kwargs):
        self.sent.append(kwargs)

    async def send_tool_response(self, **kwargs):
        self.tool_responses.append(kwargs["function_responses"])

    async def receive(self):
        while self.script:
            await asyncio.sleep(0)
            yield self.script.pop(0)
        if self.then == "close":
            raise ConnectionError("socket closed")
        await asyncio.sleep(3600)
        yield  # pragma: no cover


class FakeClient:
    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.connect_calls = []
        client = self

        class Live:
            @asynccontextmanager
            async def connect(self, model, config):
                client.connect_calls.append((model, config))
                yield client.sessions.pop(0)

        self.aio = type("Aio", (), {"live": Live()})()


class Rig:
    def __init__(self, tmp_path, sessions, **runner_kw):
        self.events = []
        self.playback = PlaybackBuffer()
        self.mic = asyncio.Queue(maxsize=100)
        self.text = asyncio.Queue()
        self.store = StudyStore(tmp_path / "s.json")
        self.client = FakeClient(sessions)
        self.handles_seen = []

        def factory(model, handle=None, **kw):
            self.handles_seen.append(handle)
            return ls.build_live_config(model, handle=handle)

        runner_kw.setdefault("min_stable_seconds", 0.0)
        runner_kw.setdefault("backoff_base", 0.01)
        self.runner = ls.LiveRunner(
            self.client, config.MODEL_DEFAULT, self.playback, self.mic, self.text,
            self.store, self.events.append, config_factory=factory, **runner_kw,
        )

    def kinds(self):
        return [e.kind for e in self.events]

    async def until(self, predicate, timeout=2.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not predicate():
            assert loop.time() < end, f"timed out; events={self.events}"
            await asyncio.sleep(0.005)

    async def run_until(self, predicate):
        task = asyncio.create_task(self.runner.run())
        await self.until(predicate)
        self.runner.request_stop()
        await asyncio.wait_for(task, 2)


# ---- tests ------------------------------------------------------------
def test_interruption_clears_queued_speech(tmp_path):
    async def scenario():
        session = FakeSession([audio_msg(), content_msg(interrupted=True)])
        rig = Rig(tmp_path, [session])
        # Observe the buffer just before the interrupt message is processed.
        seen = {}
        original_clear = rig.playback.clear

        def spy():
            seen["queued_before_clear"] = rig.playback.pending_bytes()
            return original_clear()

        rig.playback.clear = spy
        await rig.run_until(lambda: ls.INTERRUPTED in rig.kinds())
        assert seen["queued_before_clear"] == 400
        assert rig.playback.pending_bytes() == 0

    asyncio.run(scenario())


def test_transcripts_and_turn_complete_reach_ui(tmp_path):
    async def scenario():
        session = FakeSession([
            content_msg(input_transcription=types.Transcription(text="what is a mutex")),
            content_msg(output_transcription=types.Transcription(text="A lock")),
            content_msg(turn_complete=True),
        ])
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: ls.TURN_COMPLETE in rig.kinds())
        pairs = [(e.kind, e.text) for e in rig.events]
        assert (ls.USER_TEXT, "what is a mutex") in pairs
        assert (ls.MODEL_TEXT, "A lock") in pairs
        assert rig.events[-1] == ls.UiEvent(ls.STATUS, "stopped")

    asyncio.run(scenario())


def test_tool_call_runs_and_is_answered(tmp_path):
    async def scenario():
        session = FakeSession([tool_msg("add_task", {"title": "Lab report", "due_date": "2026-11-02"})])
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: session.tool_responses)
        response = session.tool_responses[0][0]
        assert response.id == "c1" and response.name == "add_task"
        assert response.response["result"]["title"] == "Lab report"
        assert rig.store.list_tasks()[0]["due_date"] == "2026-11-02"
        assert ls.TOOLS_CHANGED in rig.kinds()

    asyncio.run(scenario())


def test_tool_error_is_returned_to_model_not_raised(tmp_path):
    async def scenario():
        session = FakeSession([tool_msg("complete_task", {"task_id": 42})])
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: session.tool_responses)
        assert "error" in session.tool_responses[0][0].response
        assert ls.TOOLS_CHANGED not in rig.kinds()

    asyncio.run(scenario())


def test_reconnect_resumes_with_last_handle(tmp_path):
    async def scenario():
        first = FakeSession([resume_msg("HANDLE-1"), types.LiveServerMessage(go_away=types.LiveServerGoAway())], then="close")
        second = FakeSession([])
        rig = Rig(tmp_path, [first, second])
        await rig.run_until(lambda: len(rig.client.connect_calls) == 2)
        assert rig.handles_seen == [None, "HANDLE-1"]
        second_cfg = rig.client.connect_calls[1][1]
        assert second_cfg.session_resumption.handle == "HANDLE-1"

    asyncio.run(scenario())


def test_gives_up_after_repeated_instant_failures_with_redacted_error(tmp_path):
    async def scenario():
        key = "AIza" + "Z" * 35

        class Boom(FakeSession):
            async def receive(self):
                raise ConnectionError(f"wss://x?key={key} refused")
                yield  # pragma: no cover

        rig = Rig(tmp_path, [Boom([]) for _ in range(5)], api_key=key, max_failures=3, min_stable_seconds=10.0)
        await asyncio.wait_for(rig.runner.run(), 3)
        errors = [e.text for e in rig.events if e.kind == ls.ERROR]
        assert errors and all(key not in t for t in errors)
        assert "Could not keep a connection" in errors[-1]
        assert len(rig.client.connect_calls) == 3
        assert rig.events[-1] == ls.UiEvent(ls.STATUS, "stopped")

    asyncio.run(scenario())


def test_mic_audio_is_streamed_as_16khz_pcm(tmp_path):
    async def scenario():
        session = FakeSession([])
        rig = Rig(tmp_path, [session])
        task = asyncio.create_task(rig.runner.run())
        await rig.until(lambda: ls.STATUS in rig.kinds())
        await asyncio.sleep(0.02)  # let _run_connection drain the queue first
        rig.mic.put_nowait(b"\x00\x01" * 400)
        await rig.until(lambda: session.sent)
        blob = session.sent[0]["audio"]
        assert blob.mime_type == "audio/pcm;rate=16000" and len(blob.data) == 800
        rig.runner.request_stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_mute_stops_audio_and_flushes_stream_once(tmp_path):
    async def scenario():
        session = FakeSession([])
        rig = Rig(tmp_path, [session])
        task = asyncio.create_task(rig.runner.run())
        await rig.until(lambda: ls.STATUS in rig.kinds())
        await asyncio.sleep(0.02)
        rig.mic.put_nowait(b"\x00\x01" * 10)  # sent normally
        await rig.until(lambda: len(session.sent) == 1)
        rig.runner.muted = True
        for _ in range(3):
            rig.mic.put_nowait(b"\x00\x01" * 10)
        await rig.until(lambda: rig.mic.empty())
        await asyncio.sleep(0.02)
        assert session.sent[1:] == [{"audio_stream_end": True}]
        rig.runner.request_stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_half_duplex_skips_mic_while_model_speaks(tmp_path):
    async def scenario():
        session = FakeSession([])
        rig = Rig(tmp_path, [session], half_duplex=True)
        task = asyncio.create_task(rig.runner.run())
        await rig.until(lambda: ls.STATUS in rig.kinds())
        await asyncio.sleep(0.02)
        rig.playback.write(b"\x01\x02" * 100)  # model "is talking"
        rig.mic.put_nowait(b"\x00\x01" * 10)
        await rig.until(lambda: rig.mic.empty())
        await asyncio.sleep(0.02)
        assert session.sent == []
        rig.runner.request_stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_typed_text_is_sent_as_realtime_text(tmp_path):
    async def scenario():
        session = FakeSession([])
        rig = Rig(tmp_path, [session])
        task = asyncio.create_task(rig.runner.run())
        await rig.until(lambda: ls.STATUS in rig.kinds())
        rig.text.put_nowait("quiz me on the OSI model")
        await rig.until(lambda: session.sent)
        assert session.sent[0] == {"text": "quiz me on the OSI model"}
        rig.runner.request_stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_live_config_matches_documented_model_rules():
    default = ls.build_live_config(config.MODEL_DEFAULT)
    extended = ls.build_live_config(config.MODEL_EXTENDED)
    assert default.thinking_config is None  # gemini-3.8-live must omit thinkingLevel
    assert extended.thinking_config.thinking_level is not None
    for cfg in (default, extended):
        assert cfg.realtime_input_config.activity_handling == types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
        assert cfg.input_audio_transcription is not None and cfg.output_audio_transcription is not None
        assert cfg.context_window_compression.sliding_window is not None
        assert cfg.response_modalities == [types.Modality.AUDIO]
        assert cfg.tools and cfg.system_instruction


# ---- extended-thinking / capability-aware config -----------------------
def test_extended_model_gets_async_only_tools_and_thinking_level():
    cfg = ls.build_live_config(config.MODEL_EXTENDED)
    behaviors = {d.behavior for d in cfg.tools[0].function_declarations}
    assert behaviors == {types.Behavior.NON_BLOCKING}
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW


def test_default_model_gets_synchronous_tools_and_no_thinking_config():
    cfg = ls.build_live_config(config.MODEL_DEFAULT)
    behaviors = {d.behavior for d in cfg.tools[0].function_declarations}
    assert behaviors == {None}
    assert cfg.thinking_config is None


def test_unknown_model_id_falls_back_to_safe_capabilities():
    # A model CampusVoice has never heard of must not be assumed to support
    # thinking or async-only tools; it gets the conservative defaults.
    cfg = ls.build_live_config("some-future-model-id")
    behaviors = {d.behavior for d in cfg.tools[0].function_declarations}
    assert behaviors == {None}
    assert cfg.thinking_config is None


def test_resolve_model_warns_on_unknown_id():
    model, warning = config.resolve_model("totally-made-up-model")
    assert model == "totally-made-up-model"
    assert warning is not None and "totally-made-up-model" in warning

    model, warning = config.resolve_model(config.MODEL_DEFAULT)
    assert warning is None


def test_resolve_thinking_level_rejects_unsupported_values():
    level, warning = config.resolve_thinking_level("minimal")
    assert level == "low" and warning is not None
    level, warning = config.resolve_thinking_level("high")
    assert level == "high" and warning is None


# ---- interaction_status (extended-thinking async reasoning protocol) ---
def test_turn_complete_with_in_progress_status_emits_thinking_not_done(tmp_path):
    async def scenario():
        msg = content_msg(turn_complete=True, interaction_status=types.InteractionStatus.IN_PROGRESS)
        session = FakeSession([msg])
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: ls.THINKING in rig.kinds())
        assert ls.TURN_COMPLETE not in rig.kinds()

    asyncio.run(scenario())


def test_idle_status_without_turn_complete_emits_idle(tmp_path):
    async def scenario():
        msg = content_msg(interaction_status=types.InteractionStatus.IDLE)
        session = FakeSession([msg])
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: ls.IDLE in rig.kinds())

    asyncio.run(scenario())


def test_turn_complete_without_status_still_means_done(tmp_path):
    # Non-extended-thinking models never send interaction_status; turn_complete
    # alone must keep meaning "done", exactly as before this fix.
    async def scenario():
        session = FakeSession([content_msg(turn_complete=True)])
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: ls.TURN_COMPLETE in rig.kinds())
        assert ls.THINKING not in rig.kinds()

    asyncio.run(scenario())


# ---- tool-call safety: dedup, timeout, scheduling -----------------------
def test_duplicate_tool_call_id_runs_only_once(tmp_path):
    async def scenario():
        msg = tool_msg("add_task", {"title": "dup"}, call_id="dup-1")
        session = FakeSession([msg, msg])  # same call id delivered twice
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: len(session.tool_responses) >= 1)
        await asyncio.sleep(0.05)
        assert len(rig.store.list_tasks()) == 1  # not duplicated

    asyncio.run(scenario())


def test_tool_response_uses_when_idle_scheduling(tmp_path):
    async def scenario():
        session = FakeSession([tool_msg("get_current_datetime", {})])
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: session.tool_responses)
        response = session.tool_responses[0][0]
        assert response.scheduling == types.FunctionResponseScheduling.WHEN_IDLE

    asyncio.run(scenario())


def test_slow_tool_times_out_instead_of_hanging_the_session(tmp_path, monkeypatch):
    import campusvoice.live_session as ls_module

    async def scenario():
        monkeypatch.setattr(ls_module, "TOOL_CALL_TIMEOUT_SECONDS", 0.05)

        def slow_execute(*_a, **_kw):
            import time

            time.sleep(0.3)
            return {"result": "too slow"}

        monkeypatch.setattr(ls_module, "execute_tool", slow_execute)
        session = FakeSession([tool_msg("get_current_datetime", {})])
        rig = Rig(tmp_path, [session])
        await rig.run_until(lambda: session.tool_responses)
        response = session.tool_responses[0][0]
        assert "error" in response.response
        assert ls.TOOL_TIMEOUT in rig.kinds()

    asyncio.run(scenario())
