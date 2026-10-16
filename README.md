# CampusVoice

A Windows desktop voice study partner built on the **Gemini Live API** (Google GenAI SDK, Python).
Talk to it, interrupt it mid-sentence, and let it keep your tasks and notes by voice.

## What it does

- Real-time two-way voice with **barge-in**: when you start talking, the server cancels the reply and the app flushes queued speech immediately.
- Study tools the model can call: add/list/complete tasks with deadlines, save/search notes, get today's date.
- Live transcript of both sides, a task list panel, mute, and a typed-message box.
- Sessions longer than 15 minutes: context window compression and session resumption are enabled, and the app reconnects with the last resume handle when the server sends GoAway.
- An optional high-reasoning mode (`gemini-3.8-live-extended-thinking`) that runs tool calls asynchronously and shows a "Thinking..." status while the server keeps reasoning after a turn.

## Model IDs (re-checked 2026-10-16 against ai.google.dev/gemini-api/docs/models and the per-model capability pages)

| Use | Model ID | Tool calling | Thinking |
|---|---|---|---|
| Default (low latency voice) | `gemini-3.8-live` | synchronous | not supported |
| Slower, more reasoning (set `CAMPUSVOICE_MODEL`) | `gemini-3.8-live-extended-thinking` | **asynchronous only** (`NON_BLOCKING`) | supported (`low`/`medium`/`high`) |
| Legacy, avoid | `gemini-3.1-flash-live-preview` | synchronous | not supported |

`campusvoice/config.py` keeps this as a small capability table (`MODEL_CAPABILITIES`) instead of hard-coding behaviour per model inline, and `build_live_config()`/`build_tools()` read from it. If you set `CAMPUSVOICE_MODEL` to something not in the table, CampusVoice still tries to connect (Google ships new Live models before this table can be updated) but logs/shows a clear warning and falls back to the conservative defaults (synchronous tools, no thinking) instead of guessing. If connecting ever fails with "model not found", re-check the docs page and edit `campusvoice/config.py`.

## Windows setup (about 10 minutes)

1. **Install Python 3.11 or newer** from https://www.python.org/downloads/windows/ . On the first installer screen tick **Add python.exe to PATH**. Keep the default options (this includes Tkinter, which the window needs).
2. **Get a Gemini API key** at https://aistudio.google.com/apikey . Live API usage is billed, so check the pricing page for your account.
3. **Unzip** this folder anywhere, e.g. `C:\Users\you\campusvoice-ai`. Open it in File Explorer.
4. **Double-click `setup.bat`.** It creates a `.venv` and installs the packages. It exits with a non-zero code (and prints why) if Python is too old or the install fails, so you can tell setup succeeded without reading the log closely.
5. **Double-click `test.bat`.** Expect `58 passed`. These tests need no key and no microphone.
6. **Give the app your key**, either way:
   - Open the app (step 8) and click **API key...**. The key goes into Windows Credential Manager, never a file. Or
   - In Command Prompt: `setx GEMINI_API_KEY "paste-key-here"`, then close and reopen the window. The environment variable wins over the saved key.
   - CampusVoice does not read a `.env` file at all (one less place a key could end up committed by accident). `.env` is still listed in `.gitignore` as a safety net if you add one yourself.
7. **Double-click `check.bat`.** It confirms the key is found (value hidden), prints the resolved model and its capabilities, lists audio devices, and does one real Live API call that should print `received N.Ns of audio`. This is the first step that touches Google and your sound hardware.
8. **Double-click `run.bat`.** Click **Start**, wait for "Listening", and talk.

If Windows blocks the mic: Settings > Privacy & security > Microphone > turn on **Let desktop apps access your microphone**.

**Use headphones.** With speakers, the mic hears the model and the server may treat that as you interrupting. If you must use speakers, set `CAMPUSVOICE_HALF_DUPLEX=1` (the app stops listening while it speaks, so barge-in is off).

## Try saying

- "Explain how a hash table handles collisions, then quiz me."
- "Add a task: finish the OS lab report, due next Friday."
- "What's on my list?" / "Mark task 2 done."
- "Save a note: TCP handshake is SYN, SYN-ACK, ACK."
- "Plan a 2 hour study block for my algorithms midterm."

## Settings (environment variables, all optional)

| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | API key (checked in that order, before Credential Manager) |
| `CAMPUSVOICE_MODEL` | Live model ID. Unknown values still connect, with a warning. |
| `CAMPUSVOICE_THINKING_LEVEL` | `low` (default), `medium`, or `high`. Only used when the model supports thinking. An unsupported value falls back to `low` with a warning instead of failing to connect. |
| `CAMPUSVOICE_VOICE` | Prebuilt voice name, default `Kore` |
| `CAMPUSVOICE_HALF_DUPLEX` | `1` = don't send mic audio while the model speaks |
| `CAMPUSVOICE_DATA_DIR` | Where tasks, notes and the log live. Default `%APPDATA%\CampusVoice` |

## How the key is protected

- Never stored in the project, `.env`, or any plain file. Source is env var or Windows Credential Manager (`keyring`).
- Never printed. Error text and log lines pass through a scrubber (`secure_key.redact`) that removes the literal key value, any `AIza...`-shaped key, and `key=...` URL fragments.
- `check.bat` and the UI only say where a key was found, not its value.
- Tested against the exact key value, a repeated key appearing several times in one message, a generic (non-`AIza`) key format, and a key embedded in a URL with no explicit key value available to the redactor (see `tests/test_secure_key.py`).

## Project layout

```
campusvoice/
  config.py        model IDs + per-model capability table, audio constants, VAD settings, system prompt
  secure_key.py    key lookup/storage and redaction
  tools.py         tasks/notes store (validated, atomic, self-healing JSON) and the function declarations
  audio.py         mic capture, speaker playback, flushable buffer
  live_session.py  Live config, message handler, reconnect loop, thread facade
  transcript.py    groups transcript fragments into lines
  gui.py           Tkinter window, non-blocking shutdown state machine
  diagnostics.py   check.bat backend
tests/             58 pytest tests (fake Live server, real SDK types)
setup.bat run.bat test.bat check.bat
```

## What changed in this audit pass

This project had already been through one correctness pass (the model IDs,
SDK field names, and most of the Live API plumbing were accurate and backed
by 35 passing tests). This pass re-verified everything against the current
`ai.google.dev/gemini-api/docs/live-api*` pages and the *installed*
`google-genai==2.31.0` package's real types (not assumed field names), and
fixed the gaps that were found:

1. **Extended-thinking tool calling was a hard-error waiting to happen.**
   `gemini-3.8-live-extended-thinking` only supports `NON_BLOCKING` (async)
   function declarations; synchronous ones are documented to return a hard
   error on that model. `build_tools()` never set `behavior`, so switching to
   the extended-thinking model via `CAMPUSVOICE_MODEL` would have broken tool
   calls. Fixed: `build_tools(async_only=...)` now marks every declaration
   `NON_BLOCKING` for that model (and only that model), and the Live config
   looks this up from a per-model capability table instead of a single
   `if model == ...` check. (`tools.py`, `live_session.py`, `config.py`)
2. **`turn_complete` no longer means "idle" on the extended-thinking model,
   and nothing read the field that tells you so.** The docs for that model
   say the server can keep reasoning or running an async tool call after
   `turn_complete`, and that the client must watch `interaction_status`
   (`IN_PROGRESS` / `IDLE`) instead. `MessageHandler` now reads that field
   (confirmed present on the installed SDK's `LiveServerContent`) and emits a
   distinct `THINKING` event instead of claiming the turn is done, with a new
   "Thinking..." status in the window. Models that never send
   `interaction_status` (the default/legacy models) are unaffected: the old
   turn-complete-means-done behaviour is unchanged for them. (`live_session.py`, `gui.py`)
3. **A slow tool call could stall the entire voice session.** Tool execution
   ran inline on the same asyncio loop as audio send/receive; local JSON I/O
   is fast today, but there was no bound if a disk hiccupped. Tool calls now
   run in a thread (`asyncio.to_thread`) with an 8-second timeout, so a stuck
   tool reports a timeout to the model instead of freezing mic/speaker
   streaming. (`live_session.py`)
4. **No duplicate-execution guard for tool calls.** A replayed `tool_call`
   message (e.g. from a reconnect edge case) would have run a mutating tool
   like `add_task` twice. Each handler now remembers the last 500 answered
   call ids and skips repeats. (`live_session.py`)
5. **Function responses didn't use the documented `scheduling` field.** Real
   `types.FunctionResponse` has a top-level `scheduling` field (not nested in
   `response`, as the public code samples informally show); CampusVoice now
   sets `WHEN_IDLE` explicitly so a background tool result never barges in on
   the model's own speech. (`live_session.py`)
6. **Blocking shutdown on the GUI thread.** `_on_close()` called
   `session.stop(timeout=3.0)` directly, which could freeze the window for up
   to several seconds on a slow network or audio driver. Shutdown is now a
   small state machine: the window marks itself "Closing...", disables
   controls, stops the session on a background thread, and destroys itself
   either when the normal thread-safe "stopped" event arrives or after a
   bounded safety-net timeout — never by blocking the Tk event loop. Repeated
   close events (Alt+F4 twice, or Stop then close) are ignored instead of
   starting a second shutdown. Closing before a session ever started, or
   while session startup failed partway through, is also handled. (`gui.py`)
7. **`StudyStore` trusted the JSON file's shape completely.** A valid-but-wrong
   root type, a `tasks`/`notes` value that wasn't a list, or individual
   records missing fields or holding the wrong types would have raised deep
   inside `list_tasks()`/`complete_task()` the first time the model asked for
   the task list. `_load()` now validates the root type and every record,
   drops only the malformed ones, keeps every valid task/note, recomputes
   `next_id` so recovered ids can't collide, and — critically — backs up the
   **original** file with a timestamped name before writing the cleaned
   version, so nothing is silently discarded. Save failures (e.g. disk full)
   now raise a specific `StorageError`, are reported back to the model as a
   clear error instead of a generic one, and roll back the in-memory change
   so the store doesn't drift from what's on disk. (`tools.py`)
8. **Unknown/invalid model and thinking-level configuration failed silently
   or late.** Setting `CAMPUSVOICE_MODEL` to a typo'd value, or
   `CAMPUSVOICE_THINKING_LEVEL` to `minimal` (documented as unsupported on
   the extended-thinking model), previously either crashed later or just did
   the wrong thing. `config.resolve_model()` / `resolve_thinking_level()`
   validate at startup and produce a plain-English warning shown in the app
   and `check.bat --model-status`, instead of the app finding out via a
   connection error. (`config.py`, `diagnostics.py`, `gui.py`)
9. **Minor cleanups**: removed an unused import left over from the previous
   pass (`diagnostics.py`), added explicit `exit /b %errorlevel%` to
   `run.bat`/`test.bat`/`check.bat` so double-clicking or scripting them
   reports the real exit code, and `.gitignore` now also excludes the
   timestamped corrupt-data backups and any stray `.zip`.

See `tests/test_live_session.py`, `tests/test_tools.py`, and
`tests/test_secure_key.py` for the regression tests added for every item
above.

## What was and wasn't verified

**Verified in this sandbox** (Linux, Python 3.11, `google-genai==2.31.0`, no
network/audio/Tk available): all 58 automated tests, `python -m py_compile`
on every module, `python -m pyflakes campusvoice tests` (clean), and
`python -m mypy campusvoice` (clean). Live API configs and messages are built
from the real SDK classes (not hand-rolled dicts), so a wrong field name,
removed field, or renamed enum fails a test here instead of surfacing on your
machine. The model IDs and their documented capabilities were re-fetched from
`ai.google.dev` while doing this pass (see the fetch dates above) rather than
assumed from the existing config.

**Not verified here, and you should treat these as open until you run them
yourself**:
- A real Windows audio device (mic/speakers) — `sounddevice`/PortAudio only
  install cleanly on Windows; this sandbox has no audio hardware.
- A real connection to Gemini — this sandbox has no API key and no outbound
  network access to `generativelanguage.googleapis.com`.
- The Tkinter window itself — this Linux sandbox has no `tkinter` module
  installed (confirmed: `import tkinter` fails) and no way to install it
  without package-manager/network access this sandbox doesn't have. `gui.py`
  passed `py_compile` and `pyflakes`, and its shutdown logic was designed and
  code-reviewed carefully (see item 6 above), but the actual Tk behaviour —
  does the window really stay responsive during shutdown, does "Thinking..."
  actually render — has not been exercised by an automated test or a live
  run. Please run `test.bat` then `run.bat` on Windows and report back if
  anything in the window looks wrong.
- Interruption/barge-in and reconnection are exercised against a scripted
  fake Live server in `tests/test_live_session.py`, which is a reasonable
  proxy for the SDK's message shapes and CampusVoice's reaction to them, but
  it cannot prove the real server's timing behaves the same way. Treat
  barge-in as "implemented and unit-tested", not "proven under real network
  conditions."

If something misbehaves on your machine, send the text from
`%APPDATA%\CampusVoice\campusvoice.log` (it's already redacted of key-like
strings).

## Troubleshooting

- **No sound / no mic**: run `check.bat`, read the device list, and set Windows default input/output devices. `AudioIO` accepts `input_device`/`output_device` indexes if you need to pick one explicitly.
- **It interrupts itself**: speaker echo. Use headphones.
- **"Could not keep a connection"**: wrong key, no internet, or the model ID is unavailable to your account. Run `check.bat` first; `--model-status` will flag an unrecognized model id before you waste a reconnect attempt on it.
- **Replies feel cut off after a pause**: raise `VAD_SILENCE_MS` in `config.py` (Google recommends 500-800 ms).
- **A tool call seems to hang**: after 8 seconds CampusVoice reports a timeout to the model instead of waiting forever; look for `[tool '<name>' timed out]` in the transcript panel.
- **"Could not save your data to disk"**: the JSON store couldn't write (disk full, permissions, or a locked file). The change you just made is not applied, but no existing task/note data is lost.

## Known limitations (not implemented in this pass)

The master task list this pass worked from also asked about document
ingestion (PDF/DOCX/TXT Q&A), a global push-to-talk hotkey, and broader UX
polish. None of those were added here: this pass focused on making the
existing Live API integration, shutdown, persistence, and tool-safety code
correct and tested first, per "prioritize a reliable core application over
an impressive but broken feature list." They remain open follow-ups; please
don't assume document ingestion or a global hotkey exist until they show up
in this README with their own tests.
