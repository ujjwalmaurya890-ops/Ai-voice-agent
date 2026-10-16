"""Tkinter desktop window. Tkinter ships with the python.org Windows installer."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Optional

from . import config
from .live_session import (
    ERROR, IDLE, INTERRUPTED, MODEL_TEXT, STATUS, THINKING, TOOL, TOOL_TIMEOUT,
    TOOLS_CHANGED, TURN_COMPLETE, USER_TEXT, UiEvent, VoiceSession, WARNING,
)
from .logging_setup import setup_logging
from .secure_key import SecureStorageError, delete_api_key, get_api_key, store_api_key
from .tools import StudyStore
from .transcript import TranscriptAssembler

STATUS_LABELS = {
    "connecting": "Connecting...",
    "listening": "Listening - just talk (interrupt any time)",
    "reconnecting": "Reconnecting...",
    "stopped": "Stopped",
}

# Bound on how long the UI waits for a graceful shutdown before forcing the
# window closed anyway. A slow network or audio driver must never be able to
# hang the application on exit. VoiceSession.stop(timeout=3.0) can itself take
# up to ~2x its timeout in the worst case (readiness wait + thread join), so
# this is kept comfortably above that instead of racing it on every close.
SHUTDOWN_FORCE_MS = 7000


class CampusVoiceApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"CampusVoice - voice study partner ({config.MODEL_ID})")
        self.geometry("980x640")
        self.minsize(760, 480)

        self.store = StudyStore()
        self.events: "queue.Queue[UiEvent]" = queue.Queue()
        self.session: Optional[VoiceSession] = None
        self.assembler = TranscriptAssembler()
        self.muted = tk.BooleanVar(value=False)
        # Shutdown state: set once WM_DELETE_WINDOW / Stop-then-close fires, so
        # a second click or a second window-close event cannot start a
        # duplicate shutdown sequence.
        self._closing = False
        self._force_close_after: Optional[str] = None

        self._build_widgets()
        self._refresh_tasks()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._poll_events)

    # ---- layout -------------------------------------------------------
    def _build_widgets(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.start_btn = ttk.Button(top, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(top, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(top, text="Mute mic", variable=self.muted, command=self._toggle_mute).pack(side="left", padx=12)
        ttk.Button(top, text="API key...", command=self._set_key).pack(side="left")
        self.status = ttk.Label(top, text="Stopped")
        self.status.pack(side="right")

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(body)
        self.log = tk.Text(left, wrap="word", state="disabled", font=("Segoe UI", 11))
        scroll = ttk.Scrollbar(left, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("you", foreground="#0b57d0", font=("Segoe UI", 11, "bold"))
        self.log.tag_configure("bot", foreground="#1e7e34", font=("Segoe UI", 11, "bold"))
        self.log.tag_configure("sys", foreground="#777777", font=("Segoe UI", 10, "italic"))
        self.log.tag_configure("err", foreground="#b3261e")
        self.log.tag_configure("warn", foreground="#8a6100")

        entry_row = ttk.Frame(left)
        entry_row.pack(fill="x", pady=(6, 0))
        self.entry = ttk.Entry(entry_row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self._send_text())
        ttk.Button(entry_row, text="Send", command=self._send_text).pack(side="left", padx=(6, 0))
        body.add(left, weight=3)

        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        ttk.Label(right, text="Open tasks", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.tasks = tk.Listbox(right, font=("Segoe UI", 10))
        self.tasks.pack(fill="both", expand=True, pady=(4, 0))
        body.add(right, weight=1)

    # ---- actions ------------------------------------------------------
    def _start(self) -> None:
        if self._closing:
            return  # window is on its way down; ignore stray clicks
        key, _source = get_api_key()
        if not key:
            self._set_key()
            key, _source = get_api_key()
            if not key:
                return
        self.session = VoiceSession(key, self.store, self.events.put)
        self.session.set_muted(self.muted.get())
        self.session.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._append("Session starting. Headphones are recommended so the model does not hear itself.\n", "sys")

    def _stop(self) -> None:
        if self.session is not None:
            self.stop_btn.configure(state="disabled")
            self.status.configure(text="Stopping...")
            # stop() joins the worker thread; keep that off the GUI thread so
            # a slow network/audio shutdown never freezes the window.
            threading.Thread(target=self.session.stop, daemon=True).start()

    def _toggle_mute(self) -> None:
        if self.session is not None:
            self.session.set_muted(self.muted.get())

    def _send_text(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        if self.session is None or not self.session.running:
            messagebox.showinfo("CampusVoice", "Press Start first.")
            return
        self.entry.delete(0, "end")
        self.assembler.end_turn()
        self._append(f"You: {text}\n", "you")
        self.session.send_text(text)

    def _set_key(self) -> None:
        value = simpledialog.askstring(
            "Gemini API key",
            "Paste your Gemini API key (from aistudio.google.com/apikey).\n"
            "It is stored in Windows Credential Manager, not in a file.\n"
            "Leave blank and press OK to remove a saved key.",
            show="*",
            parent=self,
        )
        if value is None:
            return
        if not value.strip():
            delete_api_key()
            messagebox.showinfo("CampusVoice", "Saved key removed.")
            return
        try:
            store_api_key(value)
        except (ValueError, SecureStorageError) as exc:
            messagebox.showerror("CampusVoice", str(exc))
            return
        messagebox.showinfo("CampusVoice", "API key saved.")

    # ---- event pump (GUI thread only) ---------------------------------
    def _poll_events(self) -> None:
        if not self.winfo_exists():
            return  # window already destroyed; nothing left to poll into
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        except tk.TclError:
            return  # widgets were torn down mid-poll during shutdown
        if self.winfo_exists() and (not self._closing or self.session is not None):
            self.after(50, self._poll_events)

    def _handle(self, event: UiEvent) -> None:
        if event.kind == USER_TEXT:
            self._fragment("You", event.text, "you")
        elif event.kind == MODEL_TEXT:
            self._fragment("CampusVoice", event.text, "bot")
        elif event.kind in (TURN_COMPLETE, INTERRUPTED, IDLE):
            self.assembler.end_turn()
            if event.kind == INTERRUPTED:
                self._append("\n[interrupted]\n", "sys")
            if event.kind == IDLE and not self._closing:
                self.status.configure(text=STATUS_LABELS.get("listening", "Listening"))
        elif event.kind == THINKING:
            if not self._closing:
                self.status.configure(text="Thinking...")
        elif event.kind == TOOL:
            self._append(f"\n[tool: {event.text}]\n", "sys")
        elif event.kind == TOOL_TIMEOUT:
            self._append(f"\n[tool '{event.text}' timed out]\n", "warn")
        elif event.kind == TOOLS_CHANGED:
            self._refresh_tasks()
        elif event.kind == WARNING:
            self._append(f"\n{event.text}\n", "warn")
        elif event.kind == ERROR:
            self._append(f"\n{event.text}\n", "err")
        elif event.kind == STATUS:
            if not self._closing:
                self.status.configure(text=STATUS_LABELS.get(event.text, event.text))
            if event.text == "stopped":
                self.session = None
                if self._closing:
                    self._finish_close()
                else:
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")

    def _fragment(self, speaker: str, text: str, tag: str) -> None:
        new_line, chunk = self.assembler.add_fragment(speaker, text)
        if new_line:
            self._append(f"\n{speaker}: ", tag)
        self._append(chunk)

    def _append(self, text: str, tag: Optional[str] = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.configure(state="disabled")
        self.log.see("end")

    def _refresh_tasks(self) -> None:
        self.tasks.delete(0, "end")
        for task in self.store.list_tasks():
            due = f" (due {task['due_date']})" if task["due_date"] else ""
            self.tasks.insert("end", f"#{task['id']} {task['title']}{due}")

    # ---- shutdown -------------------------------------------------------
    def _on_close(self) -> None:
        """Non-blocking shutdown: never call session.stop() on the GUI thread.

        Sequence: mark the app as closing (ignore further Start/Stop/close
        clicks), disable controls, ask the session to stop on a background
        thread, and destroy the window either when the session reports
        "stopped" (via the normal thread-safe event queue) or when a bounded
        timeout elapses, whichever happens first. This keeps a slow network
        or audio shutdown from freezing the UI, and is safe to call even if
        session creation failed partway through (self.session is None).
        """
        if self._closing:
            return  # duplicate close event (e.g. Alt+F4 while already closing)
        self._closing = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")
        self.status.configure(text="Closing...")

        if self.session is None or not self.session.running:
            self._finish_close()
            return

        threading.Thread(target=self.session.stop, kwargs={"timeout": 3.0}, daemon=True).start()
        # Safety net: if the "stopped" status event never arrives (e.g. the
        # worker thread is wedged), force the window closed anyway after a
        # bounded wait instead of hanging forever.
        self._force_close_after = self.after(SHUTDOWN_FORCE_MS, self._finish_close)

    def _finish_close(self) -> None:
        if self._force_close_after is not None:
            try:
                self.after_cancel(self._force_close_after)
            except Exception:
                pass
            self._force_close_after = None
        try:
            self.destroy()
        except Exception:
            pass  # already destroyed


def main() -> None:
    setup_logging()
    CampusVoiceApp().mainloop()
