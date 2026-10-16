"""Study tools the model can call: tasks, notes, and the current date.

Data lives in a local JSON file (default %APPDATA%\\CampusVoice\\study_data.json).
Writes are atomic so a crash cannot corrupt the file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from google.genai import types

from . import config

log = logging.getLogger(__name__)

MAX_TITLE = 200
MAX_NOTE = 4000

_TASK_FIELDS = {"id", "title", "due_date", "course", "done", "created"}
_NOTE_FIELDS = {"id", "title", "content", "course", "created"}


class ToolError(ValueError):
    """Raised for bad tool arguments; the message is sent back to the model."""


class StorageError(RuntimeError):
    """Raised when persistent data cannot be written to disk."""


def _is_valid_task(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), int)
        and isinstance(value.get("title"), str)
        and value["title"].strip() != ""
        and isinstance(value.get("done"), bool)
        and (value.get("due_date") is None or isinstance(value.get("due_date"), str))
        and (value.get("course") is None or isinstance(value.get("course"), str))
    )


def _is_valid_note(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), int)
        and isinstance(value.get("title"), str)
        and value["title"].strip() != ""
        and isinstance(value.get("content"), str)
        and (value.get("course") is None or isinstance(value.get("course"), str))
    )


class StudyStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else config.data_dir() / "study_data.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"next_id": 1, "tasks": [], "notes": []}
        self._load()

    # ---- persistence -------------------------------------------------
    def _backup_unreadable(self, reason: str) -> None:
        """Move the current file aside instead of silently discarding it.

        Uses a timestamped name so a second failure never overwrites the
        first recoverable copy.
        """
        if not self._path.exists():
            return
        backup = self._path.with_name(
            f"{self._path.stem}.corrupt-{int(time.time())}{self._path.suffix}"
        )
        try:
            os.replace(self._path, backup)
            log.warning("Study data %s; kept a copy at %s", reason, backup)
        except OSError:
            log.exception("Study data %s, and the corrupt copy could not be saved", reason)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            log.exception("Could not read %s; starting with empty study data", self._path)
            return
        if not raw.strip():
            return  # empty file: nothing to recover, nothing to back up
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            self._backup_unreadable("was not valid JSON")
            return

        if not isinstance(loaded, dict):
            self._backup_unreadable(f"had an unexpected top-level type ({type(loaded).__name__})")
            return

        cleaned: dict[str, Any] = {"next_id": 1, "tasks": [], "notes": []}
        dropped = 0

        raw_tasks = loaded.get("tasks", [])
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                if _is_valid_task(item):
                    cleaned["tasks"].append({k: item.get(k) for k in _TASK_FIELDS})
                else:
                    dropped += 1
        else:
            dropped += 1

        raw_notes = loaded.get("notes", [])
        if isinstance(raw_notes, list):
            for item in raw_notes:
                if _is_valid_note(item):
                    cleaned["notes"].append({k: item.get(k) for k in _NOTE_FIELDS})
                else:
                    dropped += 1
        else:
            dropped += 1

        existing_ids = [t["id"] for t in cleaned["tasks"]] + [n["id"] for n in cleaned["notes"]]
        next_id = loaded.get("next_id")
        if not isinstance(next_id, int) or (existing_ids and next_id <= max(existing_ids)):
            next_id = (max(existing_ids) + 1) if existing_ids else 1
        cleaned["next_id"] = next_id

        self._data = cleaned
        if dropped:
            # Partial corruption: keep every valid record, but preserve a
            # recoverable copy of the original file before we overwrite it.
            self._backup_unreadable(f"had {dropped} malformed record(s), which were dropped")
            try:
                self._save()
            except StorageError:
                log.exception("Could not persist the cleaned-up study data")

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError(f"Could not save study data: {exc}") from exc

    # ---- tasks -------------------------------------------------------
    def add_task(
        self, title: str, due_date: Optional[str] = None, course: Optional[str] = None
    ) -> dict[str, Any]:
        title = _clean(title, MAX_TITLE, "title")
        due = None
        if due_date:
            try:
                due = date.fromisoformat(due_date).isoformat()
            except ValueError as exc:
                raise ToolError("due_date must look like 2026-10-31.") from exc
        with self._lock:
            task = {
                "id": self._data["next_id"],
                "title": title,
                "due_date": due,
                "course": (course or "").strip()[:80] or None,
                "done": False,
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            self._data["next_id"] += 1
            self._data["tasks"].append(task)
            try:
                self._save()
            except StorageError:
                self._data["tasks"].pop()  # keep in-memory state consistent with disk
                self._data["next_id"] -= 1
                raise
            return dict(task)

    def list_tasks(self, include_completed: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            tasks = [
                dict(t) for t in self._data["tasks"] if include_completed or not t["done"]
            ]
        tasks.sort(key=lambda t: (t["due_date"] is None, t["due_date"] or "", t["id"]))
        return tasks

    def complete_task(self, task_id: int) -> dict[str, Any]:
        with self._lock:
            for task in self._data["tasks"]:
                if task["id"] == task_id:
                    was_done = task["done"]
                    task["done"] = True
                    try:
                        self._save()
                    except StorageError:
                        task["done"] = was_done
                        raise
                    return dict(task)
        raise ToolError(f"No task with id {task_id}.")

    # ---- notes -------------------------------------------------------
    def save_note(
        self, title: str, content: str, course: Optional[str] = None
    ) -> dict[str, Any]:
        title = _clean(title, MAX_TITLE, "title")
        content = _clean(content, MAX_NOTE, "content")
        with self._lock:
            note = {
                "id": self._data["next_id"],
                "title": title,
                "content": content,
                "course": (course or "").strip()[:80] or None,
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            self._data["next_id"] += 1
            self._data["notes"].append(note)
            try:
                self._save()
            except StorageError:
                self._data["notes"].pop()
                self._data["next_id"] -= 1
                raise
            return dict(note)

    def search_notes(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        words = [w for w in (query or "").lower().split() if w]
        if not words:
            raise ToolError("query must not be empty.")
        with self._lock:
            notes = [dict(n) for n in self._data["notes"]]
        scored = []
        for note in notes:
            haystack = f"{note['title']} {note['content']} {note['course'] or ''}".lower()
            score = sum(haystack.count(w) for w in words)
            if score:
                scored.append((score, note))
        scored.sort(key=lambda pair: (-pair[0], -pair[1]["id"]))
        return [n for _, n in scored[:limit]]


def _clean(value: Any, limit: int, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ToolError(f"{field} must not be empty.")
    return text[:limit]


# ---- declarations sent to the model ----------------------------------
TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "add_task",
        "description": "Add a study task or deadline to the student's list.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Short task title."},
                "due_date": {
                    "type": "STRING",
                    "description": "Due date as YYYY-MM-DD. Omit if none.",
                },
                "course": {"type": "STRING", "description": "Course name or code."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List the student's tasks, soonest deadline first.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "include_completed": {
                    "type": "BOOLEAN",
                    "description": "Also include finished tasks.",
                }
            },
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a task as done using its numeric id from list_tasks.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"task_id": {"type": "INTEGER", "description": "Task id."}},
            "required": ["task_id"],
        },
    },
    {
        "name": "save_note",
        "description": "Save a short study note or flashcard-style fact.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING"},
                "content": {"type": "STRING", "description": "The note text."},
                "course": {"type": "STRING"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "search_notes",
        "description": "Search saved notes by keywords.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"query": {"type": "STRING", "description": "Keywords."}},
            "required": ["query"],
        },
    },
    {
        "name": "get_current_datetime",
        "description": "Get today's local date, weekday and time.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
]

MUTATING_TOOLS = {"add_task", "complete_task", "save_note"}


def build_tools(async_only: bool = False) -> list[types.Tool]:
    """Function declarations sent to the model.

    gemini-3.8-live-extended-thinking only supports asynchronous
    (NON_BLOCKING) function calling; synchronous declarations are a hard
    error on that model
    (https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live-extended-thinking).
    Pass async_only=True for that model so every declaration carries
    behavior=NON_BLOCKING. Other Live models keep the default (BLOCKING)
    behaviour, which is fine for these fast, local, synchronous tools.
    """
    behavior = types.Behavior.NON_BLOCKING if async_only else None
    declarations = []
    for decl in TOOL_DECLARATIONS:
        kwargs = dict(decl)
        if behavior is not None:
            kwargs["behavior"] = behavior
        declarations.append(types.FunctionDeclaration(**kwargs))
    return [types.Tool(function_declarations=declarations)]


def execute_tool(store: StudyStore, name: str, args: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Run a tool call. Always returns a dict: {"result": ...} or {"error": ...}."""
    args = dict(args or {})
    try:
        if name == "add_task":
            return {"result": store.add_task(args.get("title", ""), args.get("due_date"), args.get("course"))}
        if name == "list_tasks":
            return {"result": store.list_tasks(bool(args.get("include_completed", False)))}
        if name == "complete_task":
            try:
                task_id = int(args.get("task_id"))  # type: ignore[arg-type]  # validated below
            except (TypeError, ValueError) as exc:
                raise ToolError("task_id must be a whole number.") from exc
            return {"result": store.complete_task(task_id)}
        if name == "save_note":
            return {"result": store.save_note(args.get("title", ""), args.get("content", ""), args.get("course"))}
        if name == "search_notes":
            return {"result": store.search_notes(args.get("query", ""))}
        if name == "get_current_datetime":
            now = datetime.now().astimezone()
            return {
                "result": {
                    "date": now.date().isoformat(),
                    "weekday": now.strftime("%A"),
                    "time": now.strftime("%H:%M"),
                    "timezone": str(now.tzinfo),
                }
            }
        return {"error": f"Unknown tool: {name}"}
    except ToolError as exc:
        return {"error": str(exc)}
    except StorageError as exc:
        log.error("Tool '%s' could not persist data: %s", name, exc)
        return {"error": "Could not save your data to disk. Check available space and permissions."}
    except Exception:  # never let a tool bug kill the voice session
        log.exception("Tool '%s' failed unexpectedly", name)
        return {"error": "The tool failed unexpectedly."}
