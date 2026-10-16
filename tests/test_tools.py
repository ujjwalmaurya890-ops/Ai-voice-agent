import pytest

from campusvoice import tools
from campusvoice.tools import StudyStore, execute_tool


@pytest.fixture
def store(tmp_path):
    return StudyStore(tmp_path / "s.json")


def test_declarations_build_against_real_sdk_types():
    built = tools.build_tools()
    names = [d.name for d in built[0].function_declarations]
    assert names == [d["name"] for d in tools.TOOL_DECLARATIONS]


def test_build_tools_default_is_synchronous():
    built = tools.build_tools()
    assert all(d.behavior is None for d in built[0].function_declarations)
    # build_tools() must not mutate the shared declaration templates.
    assert "behavior" not in tools.TOOL_DECLARATIONS[0]


def test_build_tools_async_only_marks_every_declaration_non_blocking():
    from google.genai import types

    built = tools.build_tools(async_only=True)
    assert all(
        d.behavior == types.Behavior.NON_BLOCKING for d in built[0].function_declarations
    )


def test_tasks_sorted_by_due_date_and_persisted(tmp_path):
    s = StudyStore(tmp_path / "s.json")
    s.add_task("later", "2026-12-01")
    s.add_task("no date")
    s.add_task("sooner", "2026-11-01", "CS101")
    assert [t["title"] for t in s.list_tasks()] == ["sooner", "later", "no date"]
    reloaded = StudyStore(tmp_path / "s.json")
    assert len(reloaded.list_tasks()) == 3


def test_complete_task_hides_it_by_default(store):
    t = store.add_task("read ch 4")
    store.complete_task(t["id"])
    assert store.list_tasks() == []
    assert len(store.list_tasks(include_completed=True)) == 1


def test_bad_inputs_become_error_dicts_not_exceptions(store):
    assert "error" in execute_tool(store, "add_task", {"title": "  "})
    assert "error" in execute_tool(store, "add_task", {"title": "x", "due_date": "tomorrow"})
    assert "error" in execute_tool(store, "complete_task", {"task_id": "abc"})
    assert "error" in execute_tool(store, "complete_task", {"task_id": 999})
    assert "error" in execute_tool(store, "nope", {})
    assert "error" in execute_tool(store, "search_notes", {"query": ""})


def test_notes_search_ranks_by_matches(store):
    store.save_note("Mitosis", "Cell division in somatic cells", "BIO")
    store.save_note("Osmosis", "Water moves across a membrane; cell stays")
    hits = execute_tool(store, "search_notes", {"query": "cell division"})["result"]
    assert hits[0]["title"] == "Mitosis"


def test_datetime_tool_shape(store):
    result = execute_tool(store, "get_current_datetime", None)["result"]
    assert set(result) == {"date", "weekday", "time", "timezone"}


def test_corrupt_file_is_set_aside_not_fatal(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    s = StudyStore(path)
    assert s.list_tasks() == []
    backups = list(tmp_path.glob("s.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not json"


def test_empty_file_is_treated_as_no_data(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("", encoding="utf-8")
    s = StudyStore(path)
    assert s.list_tasks() == []
    # Nothing to recover, so no backup should be created for an empty file.
    assert list(tmp_path.glob("s.corrupt-*.json")) == []


def test_non_dict_root_is_backed_up_and_replaced_with_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    s = StudyStore(path)
    assert s.list_tasks() == []
    backups = list(tmp_path.glob("s.corrupt-*.json"))
    assert len(backups) == 1


def test_malformed_task_entries_are_dropped_but_valid_ones_kept(tmp_path):
    import json

    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "next_id": 5,
                "tasks": [
                    {"id": 1, "title": "good task", "done": False, "due_date": None, "course": None},
                    {"id": 2, "title": "", "done": False},  # empty title: invalid
                    "not even a dict",
                    {"id": 3, "title": "missing done field"},  # missing "done": invalid
                    {"id": 4, "title": 42, "done": False},  # wrong type for title: invalid
                ],
                "notes": "should be a list",
            }
        ),
        encoding="utf-8",
    )
    s = StudyStore(path)
    tasks = s.list_tasks()
    assert [t["title"] for t in tasks] == ["good task"]
    # The original file is preserved as a recoverable backup, not silently lost.
    backups = list(tmp_path.glob("s.corrupt-*.json"))
    assert len(backups) == 1
    # A new task gets an id that cannot collide with any surviving record.
    new_task = s.add_task("another task")
    assert new_task["id"] == 5


def test_missing_fields_default_safely(tmp_path):
    import json

    path = tmp_path / "s.json"
    path.write_text(json.dumps({"tasks": [], "notes": []}), encoding="utf-8")  # no next_id
    s = StudyStore(path)
    task = s.add_task("first task ever")
    assert task["id"] == 1


def test_save_failure_is_reported_and_does_not_lose_existing_data(tmp_path, monkeypatch):
    s = StudyStore(tmp_path / "s.json")
    s.add_task("existing task")

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(tools.os, "replace", boom)
    result = execute_tool(s, "add_task", {"title": "this one should fail"})
    assert "error" in result
    # The failed task must not linger in memory, and the earlier task survives.
    titles = [t["title"] for t in s.list_tasks()]
    assert titles == ["existing task"]
