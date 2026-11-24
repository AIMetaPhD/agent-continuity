#!/usr/bin/env python3
"""Agent Continuity local web app.

Run:  python app.py [--port 8765] [--open]
The server binds to 127.0.0.1 only. All data is local plain files + Git.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from ac import __version__
from ac.core import APP_VERSION, Store, read_json, write_json
from ac.hosts import (detect_hosts, project_workspace, run_handoff,
                      sync_workspace_back, workspace_root)

BASE = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
STORE = Store()
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
PROJECT_LOCKS: dict[str, threading.Lock] = {}
PROJECT_LOCKS_GUARD = threading.Lock()


def _project_lock(pid: str) -> threading.Lock:
    """One workspace writer per project: no two runs touch the same files."""
    with PROJECT_LOCKS_GUARD:
        return PROJECT_LOCKS.setdefault(pid, threading.Lock())


def err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def body() -> dict:
    return request.get_json(silent=True) or {}


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "app": "agent-continuity", "version": APP_VERSION,
                    "home": str(STORE.home), "device": STORE.device})


@app.get("/api/setup")
def setup():
    return jsonify({"ok": True, "version": APP_VERSION, "checks": STORE.setup_checks(),
                    "hosts": detect_hosts(),
                    "settings": STORE.settings_get(), "home": str(STORE.home),
                    "workspace": str(workspace_root(STORE)),
                    "device": STORE.device})


def _open_folder(path: Path) -> None:
    if hasattr(os, "startfile"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


@app.post("/api/projects/<pid>/session/open")
def session_open(pid: str):
    try:
        path = project_workspace(STORE, pid, STORE.project_path(pid))
        _open_folder(path)
        return jsonify({"ok": True, "path": str(path)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/session/sync")
def session_sync(pid: str):
    """Explicit check-and-copy after resuming a session in Claude Desktop."""
    lock = _project_lock(pid)
    if not lock.acquire(blocking=False):
        return err("这个项目已有接续或回传任务在运行，请等它结束后再试。", 409)
    try:
        return jsonify({"ok": True, "sync": sync_workspace_back(STORE, pid)})
    except Exception as exc:
        return err(str(exc))
    finally:
        lock.release()


@app.get("/api/settings")
def settings_get():
    return jsonify({"ok": True, "settings": STORE.settings_get()})


@app.put("/api/settings")
def settings_put():
    try:
        return jsonify({"ok": True, "settings": STORE.settings_update(body())})
    except Exception as exc:
        return err(str(exc))


@app.get("/api/projects")
def projects_list():
    return jsonify({"ok": True, "projects": STORE.list_projects(), "device": STORE.device})


@app.post("/api/projects")
def projects_create():
    data = body()
    try:
        project = STORE.create_project(data.get("name", ""), data.get("path", ""))
        return jsonify({"ok": True, "project": project})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/join")
def projects_join():
    data = body()
    try:
        project = STORE.join_project(data.get("remote", ""), data.get("path", ""))
        return jsonify({"ok": True, "project": project})
    except Exception as exc:
        return err(str(exc))


@app.get("/api/projects/<pid>")
def project_get(pid: str):
    try:
        return jsonify({"ok": True, "project": STORE.project_summary(pid)})
    except KeyError:
        return err("project not found", 404)
    except Exception as exc:
        return err(str(exc))


@app.delete("/api/projects/<pid>")
def project_delete(pid: str):
    try:
        STORE.remove_project(pid)
        return jsonify({"ok": True})
    except KeyError:
        return err("project not found", 404)
    except Exception as exc:
        return err(str(exc))


@app.put("/api/projects/<pid>/path")
def project_path(pid: str):
    try:
        return jsonify({"ok": True, "project": STORE.update_device_path(pid, body().get("path", ""))})
    except Exception as exc:
        return err(str(exc))


# ------------------------------------------------------------------ definitions


@app.get("/api/projects/<pid>/definitions")
def definitions_list(pid: str):
    try:
        return jsonify({"ok": True, "definitions": STORE.definitions_list(pid, request.args.get("agent"))})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/definitions")
def definitions_create(pid: str):
    data = body()
    try:
        doc = STORE.definition_create(pid, data.get("agent", "claude"), data.get("content", ""),
                                      data.get("name", ""), data.get("source", "web"))
        return jsonify({"ok": True, "definition": doc})
    except Exception as exc:
        return err(str(exc))


@app.get("/api/projects/<pid>/definitions/<agent>/<rev>")
def definitions_get(pid: str, agent: str, rev: str):
    try:
        return jsonify({"ok": True, "definition": STORE.definition_get(pid, agent, rev)})
    except KeyError:
        return err("revision not found", 404)
    except Exception as exc:
        return err(str(exc))


@app.get("/api/projects/<pid>/definitions/<agent>/diff")
def definitions_diff(pid: str, agent: str):
    try:
        a = request.args.get("a", "")
        b = request.args.get("b", "")
        return jsonify({"ok": True, "diff": STORE.definition_diff(pid, agent, a, b)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/definitions/<agent>/<rev>/activate")
def definitions_activate(pid: str, agent: str, rev: str):
    try:
        return jsonify({"ok": True, "definition": STORE.definition_activate(pid, agent, rev)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/definitions/<agent>/<rev>/apply")
def definitions_apply(pid: str, agent: str, rev: str):
    try:
        return jsonify({"ok": True, "result": STORE.definition_apply(pid, agent, rev,
                                                                   body().get("target", "CLAUDE.md"))})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/definitions/<agent>/restore")
def definitions_restore(pid: str, agent: str):
    try:
        return jsonify({"ok": True, "result": STORE.definition_restore(pid, agent)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/integrate")
def integrate(pid: str):
    data = body()
    try:
        return jsonify({"ok": True, "result": STORE.integrate(pid, data.get("host", "claude"),
                                                             data.get("target", "CLAUDE.md"))})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/unintegrate")
def unintegrate(pid: str):
    try:
        return jsonify({"ok": True, "results": STORE.unintegrate(pid)})
    except Exception as exc:
        return err(str(exc))


# ---------------------------------------------------------------------- memory


@app.get("/api/projects/<pid>/memories")
def memories_list(pid: str):
    try:
        return jsonify({"ok": True, "memories": STORE.memory_list(
            pid, request.args.get("q", ""), request.args.get("scope", ""), request.args.get("status", ""))})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/memories")
def memories_create(pid: str):
    data = body()
    try:
        doc = STORE.memory_create(pid, data.get("title", ""), data.get("text", ""),
                                  data.get("scope", "project"), data.get("source", "web"))
        return jsonify({"ok": True, "memory": doc})
    except Exception as exc:
        return err(str(exc))


@app.get("/api/projects/<pid>/memories/<mid>")
def memories_get(pid: str, mid: str):
    try:
        return jsonify({"ok": True, "memory": STORE.memory_get(pid, mid)})
    except KeyError:
        return err("memory not found", 404)
    except Exception as exc:
        return err(str(exc))


@app.patch("/api/projects/<pid>/memories/<mid>")
def memories_patch(pid: str, mid: str):
    data = body()
    try:
        doc = STORE.memory_update(pid, mid, data.get("text"), data.get("title"),
                                  data.get("status"), data.get("source", "web"))
        return jsonify({"ok": True, "memory": doc})
    except Exception as exc:
        return err(str(exc))


# --------------------------------------------------------------------- handoff


@app.get("/api/projects/<pid>/handoffs")
def handoffs_list(pid: str):
    try:
        return jsonify({"ok": True, "handoffs": STORE.handoff_list(pid)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/handoffs")
def handoffs_create(pid: str):
    data = body()
    try:
        doc = STORE.handoff_create(pid, data.get("goal", ""), data.get("done", ""),
                                   data.get("next", ""), data.get("evidence", ""),
                                   data.get("agent", "claude"))
        return jsonify({"ok": True, "handoff": doc})
    except Exception as exc:
        return err(str(exc))


@app.get("/api/projects/<pid>/handoffs/<hid>")
def handoffs_get(pid: str, hid: str):
    try:
        return jsonify({"ok": True, "handoff": STORE.handoff_get(pid, hid)})
    except KeyError:
        return err("handoff not found", 404)
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/handoffs/<hid>/status")
def handoffs_status(pid: str, hid: str):
    try:
        return jsonify({"ok": True, "handoff": STORE.handoff_set_status(pid, hid, body().get("status", ""))})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/handoffs/<hid>/continue")
def handoffs_continue(pid: str, hid: str):
    data = body()
    host = data.get("host", "claude")
    note = data.get("note", "")
    try:
        STORE.handoff_get(pid, hid)
    except KeyError:
        return err("handoff not found", 404)
    if host == "claude":
        # Refuse to start at all when the configured workspace is unsafe.
        try:
            project_workspace(STORE, pid, STORE.project_path(pid))
        except Exception as exc:
            return err(str(exc), 400)
    job_id = f"job-{uuid.uuid4().hex[:10]}"
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "status": "running", "host": host, "handoff": hid,
                        "project": pid, "started_at": time.time(), "result": None, "error": ""}
    thread = threading.Thread(target=_run_job, args=(job_id, pid, hid, host, note), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id})


def _run_job(job_id: str, pid: str, hid: str, host: str, note: str) -> None:
    lock = _project_lock(pid)
    if not lock.acquire(blocking=False):
        message = "这个项目已有接续任务在运行；请等它结束后再启动新的接续，避免两个任务同时写工作区。"
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "failed", "error": message, "result": None})
        _persist_job(pid, job_id)
        return
    try:
        result = run_handoff(STORE, pid, hid, host, extra_prompt=note)
        # job and handoff use the same terminal state computed by run_handoff.
        final_status = result.get("status") or ("completed" if result.get("ok") else "failed")
        with JOBS_LOCK:
            JOBS[job_id].update({"status": final_status,
                                 "result": result, "error": result.get("error", "")})
        _persist_job(pid, job_id)
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "failed", "error": str(exc), "trace": traceback.format_exc()})
        _persist_job(pid, job_id)
    finally:
        lock.release()


def _persist_job(pid: str, job_id: str) -> None:
    try:
        with JOBS_LOCK:
            job = dict(JOBS.get(job_id, {}))
        path = STORE.data_dir(pid) / "jobs" / f"{job_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, job)
    except Exception:
        pass


@app.get("/api/jobs/<job_id>")
def jobs_get(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            return jsonify({"ok": True, "job": job})
    for pid in list(STORE.registry.get("projects", {})):
        try:
            path = STORE.data_dir(pid) / "jobs" / f"{job_id}.json"
        except Exception:
            continue
        if path.exists():
            stored = read_json(path, None)
            if stored:
                return jsonify({"ok": True, "job": stored, "from": "persisted"})
    return err("job not found", 404)


# ------------------------------------------------------------------------ sync


@app.get("/api/projects/<pid>/sync")
def sync_get(pid: str):
    try:
        return jsonify({"ok": True, "sync": STORE.sync_status(pid)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/sync/remote")
def sync_remote(pid: str):
    try:
        return jsonify({"ok": True, "sync": STORE.sync_set_remote(pid, body().get("url", ""))})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/sync/commit")
def sync_commit(pid: str):
    try:
        return jsonify({"ok": True, "sync": STORE.sync_commit(pid, body().get("message", "Sync from Agent Continuity"))})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/sync/push")
def sync_push(pid: str):
    try:
        return jsonify({"ok": True, "sync": STORE.sync_push(pid)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/sync/pull")
def sync_pull(pid: str):
    try:
        return jsonify({"ok": True, "result": STORE.sync_pull(pid)})
    except Exception as exc:
        return err(str(exc))


@app.get("/api/projects/<pid>/sync/conflicts")
def sync_conflicts(pid: str):
    try:
        return jsonify({"ok": True, "conflicts": STORE.sync_conflicts(pid)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/sync/conflicts/resolve")
def sync_resolve(pid: str):
    data = body()
    try:
        return jsonify({"ok": True, **STORE.sync_resolve(pid, data.get("path", ""), data.get("choice", "local"))})
    except Exception as exc:
        return err(str(exc))


# -------------------------------------------------------------------- receipts


@app.get("/api/projects/<pid>/receipts")
def receipts_get(pid: str):
    try:
        return jsonify({"ok": True, "receipts": STORE.receipt_list(pid, request.args.get("record_id"))})
    except Exception as exc:
        return err(str(exc))


# --------------------------------------------------------------- export/backup


@app.get("/api/projects/<pid>/export")
def export_get(pid: str):
    try:
        return jsonify({"ok": True, "data": STORE.export_data(pid)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/backup")
def backup_post(pid: str):
    try:
        path = STORE.backup(pid)
        return jsonify({"ok": True, "path": str(path)})
    except Exception as exc:
        return err(str(exc))


@app.get("/api/projects/<pid>/backups")
def backups_get(pid: str):
    try:
        return jsonify({"ok": True, "backups": STORE.backups(pid)})
    except Exception as exc:
        return err(str(exc))


@app.post("/api/projects/<pid>/restore")
def restore_post(pid: str):
    try:
        return jsonify({"ok": True, "result": STORE.restore_backup(pid, body().get("name", ""))})
    except Exception as exc:
        return err(str(exc))


@app.errorhandler(Exception)
def handle_error(exc):
    if isinstance(exc, KeyError):
        return err(str(exc).strip("'"), 404)
    if isinstance(exc, ValueError):
        return err(str(exc), 400)
    return err(f"{exc.__class__.__name__}: {exc}", 500)


def free_port(start: int = 8765) -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Continuity local web app")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--open", action="store_true", help="open the browser")
    parser.add_argument("--home", default="", help="data home (default ~/.agent-continuity)")
    args = parser.parse_args()
    global STORE
    if args.home:
        STORE = Store(args.home)
    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"Agent Continuity {APP_VERSION}")
    print(f"Data home: {STORE.home}")
    print(f"Open: {url}")
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
