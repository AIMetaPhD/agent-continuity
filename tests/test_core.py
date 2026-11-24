import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ac.core import BEGIN, END, Store, sha256_text, write_json
from ac.hosts import (_check_separation, _handoff_snapshot, _markers, _parse_claude,
                      _parse_codex, _scan_tree, _sync_back, default_workspace_root,
                      project_workspace, run_handoff, sync_workspace_back)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def make_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text("demo\n", encoding="utf-8")
    return project


def test_create_project_and_path(tmp_path):
    store = Store(tmp_path / "home")
    project = make_project(tmp_path)
    summary = store.create_project("Demo project", str(project))
    assert summary["id"]
    assert store.project_path(summary["id"]) == project.resolve()
    assert (store.data_dir(summary["id"]) / "project.json").exists()
    assert store._last_commit(summary["id"]) is not None


def test_definition_apply_and_restore_preserves_outside_edits(tmp_path):
    store = Store(tmp_path / "home")
    project = make_project(tmp_path)
    claude = project / "CLAUDE.md"
    claude.write_text("USER CONTENT\n", encoding="utf-8")
    pid = store.create_project("Demo", str(project))["id"]
    doc = store.definition_create(pid, "claude", "# Managed\nUse units.\n", name="v1")
    result = store.definition_apply(pid, "claude", doc["rev"], "CLAUDE.md")
    text = claude.read_text(encoding="utf-8")
    assert BEGIN in text and END in text and "Use units." in text and "USER CONTENT" in text
    assert result["applied"]["revision"] == doc["rev"]
    claude.write_text(text.replace("USER CONTENT", "USER EDITED"), encoding="utf-8")
    store.definition_restore(pid, "claude")
    text = claude.read_text(encoding="utf-8")
    assert BEGIN not in text and "USER EDITED" in text


def test_definition_restore_exact_when_unchanged(tmp_path):
    store = Store(tmp_path / "home")
    project = make_project(tmp_path)
    claude = project / "CLAUDE.md"
    claude.write_text("ORIGINAL\n", encoding="utf-8")
    pid = store.create_project("Demo", str(project))["id"]
    doc = store.definition_create(pid, "claude", "managed\n")
    store.definition_apply(pid, "claude", doc["rev"], "CLAUDE.md")
    store.definition_restore(pid, "claude")
    assert claude.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_memory_revision_revoke_and_explicit_restore(tmp_path):
    store = Store(tmp_path / "home")
    pid = store.create_project("Demo", str(make_project(tmp_path)))["id"]
    mem = store.memory_create(pid, "timeout", "5000", source="test")
    updated = store.memory_update(pid, mem["id"], text="8000", source="test")
    assert updated["current_revision"] == "r2"
    revoked = store.memory_update(pid, mem["id"], status="revoked")
    assert revoked["status"] == "revoked"
    restored = store.memory_update(pid, mem["id"], status="current")
    assert restored["status"] == "current"
    assert restored["current_revision"] == "r3"
    assert len(restored["revisions"]) == 3
    assert "8000" in restored["text"]


def test_definition_rollback_does_not_revive_memory(tmp_path):
    store = Store(tmp_path / "home")
    project = make_project(tmp_path)
    pid = store.create_project("Demo", str(project))["id"]
    mem = store.memory_create(pid, "backend", "native_backend", source="test")
    store.memory_update(pid, mem["id"], status="revoked")
    doc = store.definition_create(pid, "claude", "managed\n")
    store.definition_apply(pid, "claude", doc["rev"], "CLAUDE.md")
    store.definition_restore(pid, "claude")
    assert store.memory_get(pid, mem["id"])["status"] == "revoked"


def test_handoff_captures_dirty_files(tmp_path):
    project = make_project(tmp_path)
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.name", "Test")
    git(project, "config", "user.email", "test@example.invalid")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")
    (project / "README.md").write_text("changed\n", encoding="utf-8")
    store = Store(tmp_path / "home")
    pid = store.create_project("Demo", str(project))["id"]
    handoff = store.handoff_create(pid, "goal", "done", "next")
    assert handoff["dirty_files"]
    assert handoff["base_commit"]
    assert handoff["status"] == "ready"


def test_sync_push_pull_and_conflict(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    store_a = Store(tmp_path / "home-a")
    project_a = make_project(tmp_path / "a")
    pid = store_a.create_project("Sync demo", str(project_a))["id"]
    store_a.sync_set_remote(pid, str(remote))
    store_a.sync_push(pid)

    store_b = Store(tmp_path / "home-b")
    project_b = make_project(tmp_path / "b")
    joined = store_b.join_project(str(remote), str(project_b))
    assert joined["id"] == pid

    mem = store_a.memory_create(pid, "fact", "A", source="test")
    store_a.sync_push(pid)
    pulled = store_b.sync_pull(pid)
    assert pulled["ok"] is True
    assert store_b.memory_get(pid, mem["id"])["text"] == "A"
    assert any(r["layer"] == "delivered" for r in store_b.receipt_list(pid, mem["id"]))

    store_a.memory_update(pid, mem["id"], text="A2", source="test")
    store_a.sync_push(pid)
    store_b.memory_update(pid, mem["id"], text="B2", source="test")
    conflict = store_b.sync_pull(pid)
    assert conflict["ok"] is False
    assert conflict["conflicts"]
    rel = conflict["conflicts"][0]["path"]
    store_b.sync_resolve(pid, rel, "remote")
    assert store_b.sync_conflicts(pid) == []
    assert store_b.memory_get(pid, mem["id"])["text"] == "A2"


def test_backup_and_restore(tmp_path):
    store = Store(tmp_path / "home")
    pid = store.create_project("Demo", str(make_project(tmp_path)))["id"]
    mem = store.memory_create(pid, "fact", "before", source="test")
    backup = store.backup(pid)
    assert backup.exists()
    store.memory_update(pid, mem["id"], text="after", source="test")
    store.restore_backup(pid, backup.name)
    assert store.memory_get(pid, mem["id"])["text"] == "before"


def test_handoff_snapshot_is_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handoff = {"id": "h-test", "data_commit": "abc", "goal": "g", "done": "d",
               "next": "n", "evidence": "e", "base_commit": "", "dirty_files": [],
               "status": "ready", "created_at": "now"}
    path = _handoff_snapshot(workspace, handoff)
    assert path.exists()
    assert workspace.resolve() in path.resolve().parents
    assert path.parent.name == "handoffs"
    text = path.read_text(encoding="utf-8")
    assert "AC_READ h-test abc" in text and "AC_DONE h-test" in text


def test_session_files_are_copied_back_and_stay_in_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    project = make_project(tmp_path)
    workspace.mkdir()
    baseline = _scan_tree(workspace)
    (workspace / "game.html").write_text("<h1>new</h1>", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "a.txt").write_text("x", encoding="utf-8")
    meta = workspace / ".agent-continuity" / "handoffs"
    meta.mkdir(parents=True)
    (meta / "h.md").write_text("snapshot", encoding="utf-8")
    result = _sync_back(workspace, project, baseline)
    assert sorted(result["copied"]) == ["game.html", "nested/a.txt"]
    assert (project / "game.html").read_text(encoding="utf-8") == "<h1>new</h1>"
    assert (project / "nested" / "a.txt").exists()
    # The session keeps its files: Desktop resume must still see them.
    assert (workspace / "game.html").exists()
    assert (meta / "h.md").exists()
    assert result["errors"] == []


def test_session_files_do_not_overwrite_project_edits(tmp_path):
    workspace = tmp_path / "workspace"
    project = make_project(tmp_path)
    workspace.mkdir()
    (project / "keep.txt").write_text("user version", encoding="utf-8")
    baseline = _scan_tree(workspace)
    (workspace / "keep.txt").write_text("session version", encoding="utf-8")
    result = _sync_back(workspace, project, baseline)
    assert result["copied"] == [] and result["kept_in_session"] == ["keep.txt"]
    assert (project / "keep.txt").read_text(encoding="utf-8") == "user version"
    assert (workspace / "keep.txt").exists()


def test_same_size_quick_edit_is_detected(tmp_path):
    """Size + second-resolution mtime would miss this edit; hashes do not."""
    workspace = tmp_path / "workspace"
    project = make_project(tmp_path)
    workspace.mkdir()
    (workspace / "data.txt").write_text("AAAA", encoding="utf-8")
    baseline = _scan_tree(workspace)
    (workspace / "data.txt").write_text("BBBB", encoding="utf-8")
    result = _sync_back(workspace, project, baseline)
    assert result["copied"] == ["data.txt"]
    assert (project / "data.txt").read_text(encoding="utf-8") == "BBBB"


def test_workspace_equal_to_project_is_refused(tmp_path):
    project = make_project(tmp_path)
    with pytest.raises(ValueError):
        _check_separation(project, project)
    assert (project / "README.md").exists()


def test_configured_workspace_matching_project_is_refused(tmp_path):
    store = Store(tmp_path / "home")
    project = make_project(tmp_path)
    pid = store.create_project("Guard", str(project))["id"]
    # Point the root so that root/<pid> is the project itself.
    store.settings["hosts"] = {"workspace": str(project.parent)}
    import ac.hosts as hosts_module
    original = hosts_module._safe_dirname
    hosts_module._safe_dirname = lambda value: project.name
    try:
        with pytest.raises(ValueError):
            project_workspace(store, pid, project)
    finally:
        hosts_module._safe_dirname = original
    assert (project / "README.md").exists()


def test_workspace_inside_project_is_refused(tmp_path):
    store = Store(tmp_path / "home")
    project = make_project(tmp_path)
    store.settings["hosts"] = {"workspace": str(project / "sessions")}
    pid = store.create_project("Guard", str(project))["id"]
    with pytest.raises(ValueError):
        project_workspace(store, pid, project)


def test_projects_get_separate_workspaces_and_do_not_mix(tmp_path):
    store = Store(tmp_path / "home")
    store.settings["hosts"] = {"workspace": str(tmp_path / "ws-root")}
    project_a = make_project(tmp_path / "a")
    project_b = make_project(tmp_path / "b")
    pid_a = store.create_project("Alpha", str(project_a))["id"]
    pid_b = store.create_project("Beta", str(project_b))["id"]
    ws_a = project_workspace(store, pid_a, project_a)
    ws_b = project_workspace(store, pid_b, project_b)
    assert ws_a != ws_b
    (ws_a / "only-a.txt").write_text("A", encoding="utf-8")
    result = _sync_back(ws_b, project_b, None)
    assert result["copied"] == []
    assert not (project_b / "only-a.txt").exists()
    assert "only-a.txt" not in _scan_tree(ws_b)


def test_explicit_sync_picks_up_later_workspace_changes(tmp_path):
    store = Store(tmp_path / "home")
    store.settings["hosts"] = {"workspace": str(tmp_path / "ws-root")}
    project = make_project(tmp_path / "p")
    pid = store.create_project("Later", str(project))["id"]
    ws = project_workspace(store, pid, project)
    first = sync_workspace_back(store, pid)
    assert first["copied"] == []
    (ws / "added-in-desktop.txt").write_text("hello", encoding="utf-8")
    second = sync_workspace_back(store, pid)
    assert second["copied"] == ["added-in-desktop.txt"]
    third = sync_workspace_back(store, pid)
    assert third["copied"] == [] and third["identical"] == ["added-in-desktop.txt"]
    assert (project / "added-in-desktop.txt").read_text(encoding="utf-8") == "hello"


def test_returned_file_can_be_updated_by_resumed_session(tmp_path):
    store = Store(tmp_path / "home")
    store.settings["hosts"] = {"workspace": str(tmp_path / "ws-root")}
    project = make_project(tmp_path / "p")
    pid = store.create_project("Resume update", str(project))["id"]
    ws = project_workspace(store, pid, project)
    (ws / "note.txt").write_text("v1", encoding="utf-8")
    first = sync_workspace_back(store, pid)
    assert first["copied"] == ["note.txt"]
    (ws / "note.txt").write_text("v2 from desktop", encoding="utf-8")
    second = sync_workspace_back(store, pid)
    assert second["updated"] == ["note.txt"]
    assert (project / "note.txt").read_text(encoding="utf-8") == "v2 from desktop"
    # An edit made in the project outside the app still wins.
    (project / "note.txt").write_text("user edit", encoding="utf-8")
    (ws / "note.txt").write_text("v3 from desktop", encoding="utf-8")
    third = sync_workspace_back(store, pid)
    assert third["conflicts"] == ["note.txt"]
    assert (project / "note.txt").read_text(encoding="utf-8") == "user edit"


def test_symlinked_workspace_file_is_not_followed(tmp_path):
    workspace = tmp_path / "workspace"
    project = make_project(tmp_path)
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (workspace / "link.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    result = _sync_back(workspace, project, None)
    assert not (project / "link.txt").exists()


def test_marker_and_parsers():
    marks = _markers("AC_READ h-1 abc\nAC_DONE h-1", "h-1")
    assert marks["read"] is True and marks["done"] is True and marks["blocked"] is False
    assert _markers("nothing", "h-1")["read"] is False
    claude = _parse_claude(json.dumps({"session_id": "s1", "result": "ok"}))
    assert claude["session_id"] == "s1" and claude["result"] == "ok"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "AC_READ h-1 abc"}]}},
        {"type": "result", "session_id": "s2", "result": "AC_DONE h-1"},
    ]
    parsed = _parse_claude(json.dumps(events))
    assert parsed["session_id"] == "s2"
    assert _markers(parsed["transcript"], "h-1")["read"] is True
    assert _markers(parsed["transcript"], "h-1")["done"] is True
    tool_events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/h-1.md"}}]}}]
    tool_parsed = _parse_claude(json.dumps(tool_events))
    assert "h-1.md" in " ".join(tool_parsed["tool_reads"])
    codex = _parse_codex("\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                                                       "command": "cat /tmp/h-1.md"}}),
    ]))
    assert codex["session_id"] == "t1" and codex["result"] == "done"
    assert any("h-1.md" in c for c in codex["commands"])


def test_flask_api(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTINUITY_HOME", str(tmp_path / "home"))
    import importlib
    import app as appmod
    importlib.reload(appmod)
    appmod.STORE = Store(tmp_path / "home")
    client = appmod.app.test_client()
    project = make_project(tmp_path)
    created = client.post("/api/projects", json={"name": "API demo", "path": str(project)})
    assert created.status_code == 200
    pid = created.get_json()["project"]["id"]
    assert client.get("/api/projects").get_json()["projects"]
    assert client.post(f"/api/projects/{pid}/definitions",
                       json={"agent": "claude", "content": "hello"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/memories",
                       json={"title": "t", "text": "fact"}).status_code == 200
    assert client.post(f"/api/projects/{pid}/handoffs",
                       json={"goal": "g", "done": "", "next": "n"}).status_code == 200
    summary = client.get(f"/api/projects/{pid}").get_json()["project"]
    assert summary["memory_current"] == 1

def test_job_lookup_from_persisted_data(tmp_path, monkeypatch):
    store = Store(tmp_path / "home")
    pid = store.create_project("Demo", str(make_project(tmp_path)))["id"]
    job = {"id": "job-test", "status": "completed", "host": "claude", "handoff": "h-x"}
    (store.data_dir(pid) / "jobs").mkdir(parents=True, exist_ok=True)
    write_json(store.data_dir(pid) / "jobs" / "job-test.json", job)
    monkeypatch.setenv("AGENT_CONTINUITY_HOME", str(tmp_path / "home"))
    import importlib, app as appmod
    importlib.reload(appmod)
    appmod.STORE = Store(tmp_path / "home")
    res = appmod.app.test_client().get("/api/jobs/job-test")
    assert res.status_code == 200
    assert res.get_json()["job"]["status"] == "completed"


# --------------------------------------------------- continuation workspace

def _fake_cli(monkeypatch, payload: str, code: int = 0, captured: dict | None = None,
              on_call=None):
    """Fake only the host CLI; git and other subprocesses keep working."""
    import subprocess as sp
    import ac.hosts as hosts
    real_run = hosts.run
    monkeypatch.setattr(hosts, "_resolve_command", lambda command, host: command)

    def fake_run(cmd, cwd=None, env=None, timeout=60, input_text=None):
        name = Path(str(cmd[0])).name.lower()
        if not name.startswith("fake-"):
            return real_run(cmd, cwd=cwd, env=env, timeout=timeout, input_text=input_text)
        if captured is not None:
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["input_text"] = input_text
        if on_call is not None:
            on_call()
        return sp.CompletedProcess(cmd, code, payload, "" if code == 0 else "boom")
    monkeypatch.setattr(hosts, "run", fake_run)


def _claude_json(revision: str, extra: str = "", done: bool = True,
                 hid: str = "h-x") -> str:
    text = f"AC_READ {hid} {revision}\n"
    if done:
        text += f"AC_DONE {hid}\n"
    text += extra
    return json.dumps({"session_id": "session-1", "result": text})


def _setup_continuation(tmp_path):
    store = Store(tmp_path / "home")
    store.settings["hosts"] = {"workspace": str(tmp_path / "ws"), "timeout": 60}
    store.settings["claude"] = {"command": "fake-claude"}
    store.settings["codex"] = {"command": "fake-codex"}
    project = make_project(tmp_path / "proj")
    pid = store.create_project("Continuation", str(project))["id"]
    handoff = store.handoff_create(pid, "goal", "done", "next")
    return store, project, pid, handoff


def test_default_workspace_is_under_the_user_home():
    assert default_workspace_root() == Path.home() / ".agent-continuity" / "sessions"


def test_workspace_is_per_project_and_persistent(tmp_path):
    store, project, pid, _ = _setup_continuation(tmp_path)
    first = project_workspace(store, pid, project)
    second = project_workspace(store, pid, project)
    assert first == second
    assert first.parent == (tmp_path / "ws").resolve()


def test_claude_gets_add_dir_for_the_original_project(tmp_path, monkeypatch):
    store, project, pid, handoff = _setup_continuation(tmp_path)
    captured = {}
    _fake_cli(monkeypatch, _claude_json(handoff["data_commit"], hid=handoff["id"]), captured=captured)
    result = run_handoff(store, pid, handoff["id"], "claude")
    assert result["status"] == "completed"
    cmd = captured["cmd"]
    assert "--add-dir" in cmd and str(project.resolve()) in cmd
    # cwd is the project's own workspace, not the project.
    assert Path(captured["cwd"]).resolve() == project_workspace(store, pid, project)


def test_applied_definition_is_written_into_the_handoff_snapshot(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    handoff = {"id": "h-def", "data_commit": "rev1", "goal": "g", "done": "",
               "next": "n", "evidence": "", "base_commit": "", "dirty_files": [],
               "status": "ready", "created_at": "now"}
    path = _handoff_snapshot(workspace, handoff, applied_definition="Rule: report units in ms.")
    text = path.read_text(encoding="utf-8")
    assert "Applied agent definition" in text and "report units in ms" in text


def test_exit_zero_without_ac_done_is_not_completed(tmp_path, monkeypatch):
    store, project, pid, handoff = _setup_continuation(tmp_path)
    _fake_cli(monkeypatch, _claude_json(handoff["data_commit"], done=False, hid=handoff["id"]))
    result = run_handoff(store, pid, handoff["id"], "claude")
    assert result["status"] == "in_progress"
    assert not any(r["layer"] == "used" for r in result["receipts"])
    assert store.handoff_get(pid, handoff["id"])["status"] == "in_progress"


def test_cli_error_is_failed(tmp_path, monkeypatch):
    store, project, pid, handoff = _setup_continuation(tmp_path)
    _fake_cli(monkeypatch, "", code=1)
    result = run_handoff(store, pid, handoff["id"], "claude")
    assert result["status"] == "failed"
    assert store.handoff_get(pid, handoff["id"])["status"] == "failed"


def test_return_conflict_makes_status_partial(tmp_path, monkeypatch):
    store, project, pid, handoff = _setup_continuation(tmp_path)
    workspace = project_workspace(store, pid, project)
    (project / "shared.txt").write_text("project version", encoding="utf-8")

    def write_during_run():
        (workspace / "shared.txt").write_text("session version", encoding="utf-8")

    _fake_cli(monkeypatch, _claude_json(handoff["data_commit"], hid=handoff["id"]),
              on_call=write_during_run)
    result = run_handoff(store, pid, handoff["id"], "claude")
    assert result["status"] == "partial"
    assert result["kept_in_session"] == ["shared.txt"]
    assert (project / "shared.txt").read_text(encoding="utf-8") == "project version"


def test_codex_still_runs_in_the_project_directory(tmp_path, monkeypatch):
    store, project, pid, handoff = _setup_continuation(tmp_path)
    captured = {}
    events = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": f"AC_READ {handoff['id']} {handoff['data_commit']}\nAC_DONE {handoff['id']}"}}),
    ])
    _fake_cli(monkeypatch, events, captured=captured)
    result = run_handoff(store, pid, handoff["id"], "codex")
    assert Path(captured["cwd"]).resolve() == project.resolve()
    assert "-C" in captured["cmd"] and str(project.resolve()) in captured["cmd"]
    assert "-m" not in captured["cmd"]  # no override: Codex uses its own config
    assert captured["cmd"][-1] == "-"
    assert "First read this handoff file" in captured["input_text"]
    assert result["status"] == "completed"


def test_configured_codex_model_is_passed_through(tmp_path, monkeypatch):
    store, project, pid, handoff = _setup_continuation(tmp_path)
    store.settings.setdefault("codex", {})["model"] = "gpt-5.6-sol"
    store.settings["codex"]["windows_sandbox"] = "elevated"
    store.save_settings()
    captured = {}
    events = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": f"AC_READ {handoff['id']} {handoff['data_commit']}\nAC_DONE {handoff['id']}"}}),
    ])
    _fake_cli(monkeypatch, events, captured=captured)
    result = run_handoff(store, pid, handoff["id"], "codex")
    cmd = captured["cmd"]
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-5.6-sol"
    assert 'windows.sandbox="elevated"' in cmd
    # The prompt goes on stdin (the npm codex.cmd shim mangles argv prompts).
    assert cmd[-1] == "-" and "AC_READ" in (captured["input_text"] or "")
    assert result["status"] == "completed"


def test_project_lock_blocks_a_second_run(tmp_path):
    import app as appmod
    lock = appmod._project_lock("p-lock")
    assert lock.acquire(blocking=False)
    try:
        assert appmod._project_lock("p-lock").acquire(blocking=False) is False
        assert appmod._project_lock("p-other").acquire(blocking=False) is True
    finally:
        lock.release()
