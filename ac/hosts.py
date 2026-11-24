"""Real Claude Code / Codex task execution for a handoff.

The web app does not pretend a Markdown file was loaded. A continuation is a
real CLI process started for the handoff. The process is asked to read the
handoff file and to print explicit AC_READ / AC_DONE markers; those markers are
recorded as read/used receipts, separately from the saved/delivered layers.

Claude continuations run inside a fixed per-project workspace
(``settings.hosts.workspace``, default ``~/.agent-continuity/sessions``)
instead of inside the project. That directory is a normal Claude Code project,
so every continuation session shows up in Claude Desktop under it and the user
can keep talking to the same session. Files the session creates in the
workspace are copied back into the original project when the run ends.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from .core import Store, _inside, read_json, run, utc_now, write_json


def detect_hosts() -> list[dict]:
    out = []
    for host, cmd in (("claude", "claude"), ("codex", "codex")):
        path = shutil.which(cmd) or shutil.which(cmd + ".cmd")
        if not path and host == "codex":
            npm_codex = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
            if npm_codex.exists():
                path = str(npm_codex)
        version = ""
        if path:
            proc = run([path, "--version"], timeout=20)
            version = (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr) else ""
        out.append({"host": host, "available": bool(path), "path": path or "", "version": version})
    return out


def _exclude_local(project: Path) -> None:
    """Keep the generated handoff snapshot out of the user's Git status."""
    try:
        probe = run(["git", "-C", str(project), "rev-parse", "--git-path", "info/exclude"], timeout=10)
        if probe.returncode != 0 or not probe.stdout.strip():
            return
        exclude = Path(probe.stdout.strip())
        if not exclude.is_absolute():
            exclude = (project / exclude).resolve()
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".agent-continuity/" not in text:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(text.rstrip() + "\n.agent-continuity/\n", encoding="utf-8")
    except Exception:
        pass


META_DIR = ".agent-continuity"


def default_workspace_root() -> Path:
    """A per-user default that exists on every platform."""
    return Path.home() / ".agent-continuity" / "sessions"


def workspace_root(store: Store) -> Path:
    """Configured root that holds one persistent workspace per project."""
    raw = ((store.settings.get("hosts") or {}).get("workspace")
           or os.environ.get("AGENT_CONTINUITY_WORKSPACE")
           or str(default_workspace_root()))
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (Path.home() / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _safe_dirname(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-.")
    return cleaned or "project"


def _check_separation(session_dir: Path, project: Path) -> None:
    """Refuse a workspace that is the project or contains/is contained by it."""
    session = session_dir.resolve()
    project = project.resolve()
    if session == project:
        raise ValueError("接续工作区不能与源项目相同；请在 设置 → 接续工作区 换一个目录。")
    if _inside(project, session):
        raise ValueError(f"接续工作区 {session} 位于源项目 {project} 内部，"
                         "会把会话文件写进项目；请换一个项目外的目录。")
    if _inside(session, project):
        raise ValueError(f"源项目 {project} 位于接续工作区 {session} 内部，"
                         "回传可能影响其他项目；请换一个更窄的工作区。")


def project_workspace(store: Store, pid: str, project: Path) -> Path:
    """Persistent per-project workspace; the same project always reuses it."""
    root = workspace_root(store)
    session_dir = (root / _safe_dirname(pid)).resolve()
    _check_separation(session_dir, project)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_tree(root: Path) -> dict[str, str]:
    """Content hashes of workspace files (size/mtime alone misses edits)."""
    state: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.split("/")[0] == META_DIR:
            continue
        try:
            state[rel] = _sha256(path)
        except OSError:
            continue
    return state


RETURN_STATE = "session-return.json"


def _load_return_state(store: Store, pid: str) -> dict[str, str]:
    """Digest of what this app last wrote into the project, per file.

    Lets a resumed session update a file it created earlier without being
    reported as a conflict, while a project file the user edited since our
    copy is still protected.
    """
    try:
        state = read_json(store.data_dir(pid) / RETURN_STATE, {})
        return {str(k): str(v) for k, v in state.items()} if isinstance(state, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_return_state(store: Store, pid: str, state: dict[str, str]) -> None:
    try:
        write_json(store.data_dir(pid) / RETURN_STATE, state)
    except OSError:
        pass


def _sync_back(session_dir: Path, project: Path,
               baseline: dict[str, str] | None = None,
               state: dict[str, str] | None = None) -> dict:
    """Copy workspace files into the project; never delete or overwrite work.

    - copy, not move, so the Claude Desktop session keeps its files
    - content hashes decide what changed
    - an existing file is replaced only when it still matches what this app
      wrote last time; anything else is reported, never replaced
    - symlinks, directories in the way and path escapes become errors
    """
    if state is None:
        state = {}
    copied: list[str] = []
    identical: list[str] = []
    updated: list[str] = []
    conflicts: list[str] = []
    errors: list[str] = []
    project_root = project.resolve()
    for path in sorted(session_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(session_dir).as_posix()
        if rel.split("/")[0] == META_DIR:
            continue
        try:
            digest = _sha256(path)
        except OSError as exc:
            errors.append(f"{rel}: 读取失败 {exc}")
            continue
        if baseline is not None and baseline.get(rel) == digest:
            continue
        target = project / rel
        try:
            if _inside(project_root, target.resolve()):
                pass
            else:
                errors.append(f"{rel}: 目标路径越出项目目录，未写入")
                continue
            if target.is_symlink():
                errors.append(f"{rel}: 项目里同名路径是符号链接，未写入")
                continue
            if target.exists():
                if target.is_dir():
                    errors.append(f"{rel}: 项目里同名路径是目录，未写入")
                    continue
                current = _sha256(target)
                if current == digest:
                    identical.append(rel)
                    state[rel] = digest
                    continue
                # The project file still holds exactly what this app wrote
                # last time, so the session's newer version is a safe update.
                if state.get(rel) and state[rel] == current:
                    shutil.copy2(path, target)
                    copied.append(rel)
                    updated.append(rel)
                    state[rel] = digest
                    continue
                conflicts.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(rel)
            state[rel] = digest
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
    return {"copied": copied, "identical": identical, "updated": updated,
            "conflicts": conflicts, "errors": errors,
            "synced_files": copied + identical, "kept_in_session": conflicts}


def _write_session_readme(session_dir: Path, project: Path, project_name: str, handoff: dict) -> None:
    meta = session_dir / META_DIR
    meta.mkdir(parents=True, exist_ok=True)
    text = f"""# Agent Continuity 接续工作区

这个目录是 Claude Code 的接续工作区，按项目固定：同一个项目每次都回到这里，
所以在 Claude Desktop 打开这个目录，就能看到历史接续会话并继续对话。

- 源项目：{project}
- 项目名：{project_name}
- 项目 ID：{handoff.get('project', '')}
- 最近交接：{handoff.get('id', '')} — {handoff.get('goal', '')}
- 交接快照：{META_DIR}/handoffs/{handoff.get('id', '')}.md

任务结束后，这里新建或修改的文件会被**复制**回源项目（不删除这里的副本）；
源项目已有同名但内容不同的文件不会被覆盖，会在应用里列为冲突。
"""
    (meta / "README.md").write_text(text, encoding="utf-8")


def _memory_block(memories: list[dict]) -> str:
    """Render the project's current memory so a new session actually has it."""
    if not memories:
        return "（这个项目还没有当前记忆。）"
    lines = []
    for m in memories:
        rev = m.get("current_revision") or ""
        lines.append(f"- [{rev}] {m.get('title', '')} ({m.get('scope', 'project')})")
        body = (m.get("text") or "").strip()
        if body:
            lines.extend("  " + line for line in body.splitlines())
    return "\n".join(lines)


def _handoff_snapshot(base: Path, handoff: dict, memories: list[dict] | None = None,
                      project_path: Path | None = None,
                      applied_definition: str = "") -> Path:
    path = base / META_DIR / "handoffs" / f"{handoff['id']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    revision = handoff.get("data_commit", "") or "unknown"
    dirty = handoff.get("dirty_files") or []
    project_line = f"\nProject: {project_path}" if project_path else ""
    memory_text = _memory_block(memories or [])
    text = f"""# Handoff {handoff['id']}

Revision: {revision}
Status: {handoff.get('status', '')}
Created: {handoff.get('created_at', '')}{project_line}

## Current goal
{handoff.get('goal', '')}

## Already done
{handoff.get('done', '')}

## Next step
{handoff.get('next', '')}

## Evidence and constraints
{handoff.get('evidence', '')}

## Project memory (current revisions)
These are this project's active facts and decisions, taken from the app at the
moment this handoff started. Revoked entries are not included. Treat them as
current unless the goal says otherwise.

{memory_text}

## Applied agent definition
{applied_definition or "（这个 Agent 在项目里没有已应用的定义。）"}

## Code state
- base commit: {handoff.get('base_commit', '') or 'not a Git repository'}
- uncommitted files:
{chr(10).join('- ' + line for line in dirty) if dirty else '- none'}

## Required markers
After you actually read this file, print exactly:
AC_READ {handoff['id']} {revision}

When the next step is complete and checked, print exactly:
AC_DONE {handoff['id']}
"""
    path.write_text(text, encoding="utf-8")
    _exclude_local(base)
    return path


def _resolve_command(command: str, host: str) -> str:
    path = shutil.which(command) or shutil.which(command + ".cmd")
    if not path and host == "codex":
        npm_codex = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
        if npm_codex.exists():
            path = str(npm_codex)
    return path or command


TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
                 ".toml", ".cfg", ".ini", ".sh", ".ps1", ".bat", ".csv", ".tex"}


def _crlf_state(project: Path) -> dict[str, bool]:
    state: dict[str, bool] = {}
    proc = run(["git", "-C", str(project), "ls-files"], timeout=20)
    if proc.returncode != 0:
        return state
    for rel in [line.strip() for line in proc.stdout.splitlines() if line.strip()][:500]:
        path = project / rel
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.exists():
            continue
        try:
            state[rel] = b"\r\n" in path.read_bytes()[:8192]
        except OSError:
            continue
    return state


def _command_hint(command: str, host: str) -> str:
    return (f'命令 "{command}" 不存在或无法启动。请到 设置 → {host}.command 修正为可用命令'
            f"（例如 claude / codex），或先安装对应 CLI，然后重试。")


def build_prompt(project_name: str, handoff: dict, handoff_path: Path,
                 project_path: Path | None = None, session_dir: Path | None = None,
                 memory_revision: str = "") -> str:
    hid = handoff["id"]
    revision = handoff.get("data_commit", "") or "unknown"
    location = ""
    if project_path is not None:
        location = f"""
The project you are continuing lives at this absolute path:
{project_path}

Your working directory for this session is:
{session_dir or project_path}

The project directory is added to this session with --add-dir, so you may read
and edit files there through that absolute path. Read the project's CLAUDE.md
and any AGENTS.md before editing. Project-level .claude/ settings and hooks are
not guaranteed to auto-load from an added directory; the applied agent
definition is reproduced in the handoff, and the files themselves are readable.
If you create files in the working directory they are copied into the project
when the run ends; prefer writing deliverables straight into the project.
"""
    memory_line = (f"The handoff embeds project memory as captured at revision {memory_revision}. "
                   if memory_revision else "The handoff embeds project memory as captured at handoff start. ")
    return f"""You are continuing work on the local project "{project_name}".
{location}
First read this handoff file completely:
{handoff_path}

It contains the current goal, what is already done, the next step, evidence, the
base commit, uncommitted files, and the project's current memory. {memory_line}Follow it; the facts in that "Project memory" section are supplied by Agent Continuity for this session. Do not discard uncommitted changes and do not claim work that you did not verify.

After you have actually read the handoff file, print exactly one line:
AC_READ {hid} {revision}

When the next step is complete and you have checked the result, print exactly:
AC_DONE {hid}

If you cannot complete it, print AC_BLOCKED {hid} <short reason> instead of AC_DONE.
"""


def _parse_claude(stdout: str) -> dict:
    session_id, result, error = "", "", ""
    transcript_parts: list[str] = []
    tool_reads: list[str] = []
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if isinstance(data, list):
        for event in data:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        transcript_parts.append(block["text"])
                    if block.get("type") == "tool_use":
                        tool_reads.append(json.dumps({"name": block.get("name"), "input": block.get("input")},
                                                     ensure_ascii=False))
        result_events = [e for e in data if isinstance(e, dict) and e.get("type") == "result"]
        data = result_events[-1] if result_events else {}
    if isinstance(data, dict):
        session_id = data.get("session_id", "") or ""
        result = data.get("result", "") or ""
        if result:
            transcript_parts.append(result)
        if data.get("is_error"):
            error = result or "Claude Code returned an error"
    return {"session_id": session_id, "result": result,
            "transcript": "\n".join(transcript_parts), "tool_reads": tool_reads, "error": error}


def _parse_codex(stdout: str, last_file: Path | None = None) -> dict:
    session_id, result, error = "", "", ""
    transcript_parts: list[str] = []
    commands: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            session_id = event.get("thread_id", "") or session_id
        elif event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                result = item["text"]
                transcript_parts.append(item["text"])
            if item.get("type") == "command_execution" and item.get("command"):
                # Keep full event: a tool *request* is not proof of a successful read.
                commands.append(json.dumps(item, ensure_ascii=False))
        elif event.get("type") == "turn.failed":
            error = json.dumps(event.get("error") or event, ensure_ascii=False)[:500]
    if last_file and last_file.exists():
        text = last_file.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            if not transcript_parts:
                transcript_parts.append(text)
            result = text
    return {"session_id": session_id, "result": result,
            "transcript": "\n".join(transcript_parts), "commands": commands, "error": error}


def _markers(text: str, hid: str, revision: str = "") -> dict:
    read_ok = bool(re.search(rf"AC_READ\s+{re.escape(hid)}\s+{re.escape(revision)}\b", text or ""))
    read_any = bool(re.search(rf"AC_READ\s+{re.escape(hid)}\b", text or ""))
    return {
        "read": read_ok if revision else read_any,
        "read_wrong_revision": read_any and not read_ok,
        "done": bool(re.search(rf"AC_DONE\s+{re.escape(hid)}\b", text or "")),
        "blocked": bool(re.search(rf"AC_BLOCKED\s+{re.escape(hid)}\b", text or "")),
    }


def run_handoff(store: Store, pid: str, hid: str, host: str, timeout: int | None = None,
                extra_prompt: str = "") -> dict:
    handoff = store.handoff_get(pid, hid)
    project = store.project_path(pid)
    data = store.data_dir(pid)
    project_name = store.project_entry(pid).get("name", pid)
    # Claude runs in this project's persistent workspace so the session shows up
    # in Claude Desktop and can be resumed; Codex keeps the project as its cwd.
    if host == "claude":
        workspace = project_workspace(store, pid, project)
    else:
        workspace = project
    handoff = {**handoff, "project": pid}
    # Capture only active, in-scope memories, with their actual revision ids.
    try:
        memories = [m for m in store.memory_list(pid, status="current")
                    if m.get("scope") in (None, "", "project")]
    except (AttributeError, TypeError):
        memories = []  # simulated store in adapter regression
    memory_revisions = [m.get("current_revision", "") for m in memories]
    applied_definition = ""
    try:
        for entry in store.definitions_list(pid, "claude"):
            if entry.get("applied"):
                doc = store.definition_get(pid, "claude", entry["applied"]["revision"])
                applied_definition = (f"Target: {entry['applied']['target']} "
                                      f"(revision {entry['applied']['revision']})\n\n"
                                      f"{doc.get('content', '')}")
                break
    except (AttributeError, KeyError, TypeError):
        applied_definition = ""
    handoff_path = _handoff_snapshot(workspace, handoff, memories=memories,
                                     project_path=project,
                                     applied_definition=applied_definition)
    _write_session_readme(workspace, project, project_name, handoff)
    prompt = build_prompt(project_name, handoff, handoff_path,
                          project_path=project, session_dir=workspace,
                          memory_revision=",".join(x for x in memory_revisions if x))
    if extra_prompt.strip():
        prompt += "\nAdditional user note:\n" + extra_prompt.strip() + "\n"

    started = utc_now()
    t0 = time.time()
    before_crlf = _crlf_state(project)
    baseline = _scan_tree(workspace)
    if timeout is None:
        try:
            timeout = int(store.settings.get("hosts", {}).get("timeout", 900))
        except (TypeError, ValueError):
            timeout = 900
    if host == "claude":
        command = _resolve_command(store.settings.get("claude", {}).get("command") or "claude", "claude")
        args = [command, "-p", prompt, "--output-format", "json",
                "--permission-mode", "acceptEdits",
                "--add-dir", str(project.resolve()),
                "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash(git *),Bash(python *),Bash(python3 *)"]
        model = store.settings.get("claude", {}).get("model")
        if model:
            args += ["--model", model]
        proc = run(args, cwd=workspace, env=store.claude_env(), timeout=timeout)
        parsed = _parse_claude(proc.stdout)
        raw = proc.stdout + "\n" + proc.stderr
    elif host == "codex":
        command = _resolve_command(store.settings.get("codex", {}).get("command") or "codex", "codex")
        last_file = data / "jobs" / f"{hid}-{int(time.time())}-last.txt"
        last_file.parent.mkdir(parents=True, exist_ok=True)
        args = [command, "exec", "-C", str(project), "-s", "workspace-write",
                "-c", "approval_policy=never", "--json", "-o", str(last_file)]
        codex_settings = store.settings.get("codex", {})
        model = codex_settings.get("model")
        if model:
            args += ["-m", str(model)]
        # Some Windows Codex builds cannot read the project under the default
        # unelevated sandbox token; the user can opt into the elevated one.
        sandbox = str(codex_settings.get("windows_sandbox") or "").strip()
        if sandbox:
            args += ["-c", f'windows.sandbox="{sandbox}"']
        # Deliver the prompt on stdin: the npm codex.cmd shim mangles a long
        # multi-line argument, and codex then sees no task at all.
        args.append("-")
        proc = run(args, cwd=project, env=store.codex_env(), timeout=timeout,
                   input_text=prompt)
        parsed = _parse_codex(proc.stdout, last_file)
        raw = proc.stdout + "\n" + proc.stderr
    else:
        raise ValueError(f"unsupported host: {host}")

    transcript = parsed.get("transcript") or parsed.get("result", "")
    markers = _markers(transcript, hid, handoff.get("data_commit", ""))
    # A tool call *request* is not proof of a read: for Codex require a
    # successful command event; for Claude require a Read tool_use.
    read_via_tool = False
    if host == "claude":
        read_via_tool = any(handoff_path.name in str(item) or str(handoff_path) in str(item)
                            for item in (parsed.get("tool_reads") or []))
    else:
        for raw_cmd in (parsed.get("commands") or []):
            try:
                item = json.loads(raw_cmd)
            except (ValueError, TypeError):
                continue
            ok_exit = str(item.get("exit_code", "0")) in ("0", "None", "")
            if ok_exit and item.get("status") != "failed" and (
                    str(item.get("exit_code", "")) == "0" or "exit_code" not in item):
                if handoff_path.name in str(item.get("command", "")):
                    read_via_tool = True
    blocked = markers["blocked"]
    failed = proc.returncode != 0 or bool(parsed.get("error"))
    ok = proc.returncode == 0 and bool(parsed.get("result")) and not parsed.get("error") and not blocked
    receipts = []
    if (markers["read"] or read_via_tool) and not failed:
        detail = (f"{host} printed AC_READ" if markers["read"]
                  else f"{host} successfully read handoff snapshot")
        receipts.append(store.receipt_add(pid, "read", "handoff", hid,
                                          handoff.get("data_commit", ""),
                                          session_id=parsed.get("session_id", ""),
                                          detail=detail))
    if ok and markers["done"]:
        receipts.append(store.receipt_add(pid, "used", "handoff", hid,
                                          handoff.get("data_commit", ""),
                                          session_id=parsed.get("session_id", ""),
                                          detail=f"{host} reported AC_DONE (self-report)"))
    after_crlf = _crlf_state(project)
    warnings = [f"{rel}: host changed CRLF to LF" for rel, was_crlf in before_crlf.items()
                if was_crlf and rel in after_crlf and not after_crlf[rel]]
    if host == "codex" and re.search(r"Access to the path .* is denied", raw):
        warnings.append("Codex 沙箱拒绝访问项目目录。这是本机 Codex 的 Windows 沙箱权限问题；"
                        "可先修复 Codex 沙箱（或在 设置 → Codex → Windows 沙箱 选择其他模式）后重试。")
    if host == "claude":
        return_state = _load_return_state(store, pid)
        synced = _sync_back(workspace, project, baseline, return_state)
        _save_return_state(store, pid, return_state)
    else:
        synced = {"copied": [], "identical": [], "updated": [], "conflicts": [],
                  "errors": [], "synced_files": [], "kept_in_session": []}
    if synced["kept_in_session"]:
        warnings.append("这些文件已在源项目存在且内容不同，未覆盖，留在工作区："
                        + ", ".join(synced["kept_in_session"]))
    if synced["errors"]:
        warnings.append("回传时出错：" + "; ".join(synced["errors"]))
    # Job and handoff share one terminal state. AC_DONE is a self-report, not
    # independent verification, and a run with return errors is not a clean pass.
    if failed or blocked:
        status = "failed"
    elif ok and markers["done"] and not synced["errors"] and not synced["kept_in_session"]:
        status = "completed"
    elif ok and markers["done"]:
        status = "partial"
    else:
        status = "in_progress"
    try:
        store.handoff_set_status(pid, hid, status)
    except Exception:
        pass
    if proc.returncode == 127:
        error = _command_hint(command, host)
    elif proc.returncode == 124:
        error = (parsed.get("error", "") or "") + " " + (proc.stderr or "").strip()
        error = (error.strip() or f"接续任务超过 {timeout} 秒，已终止进程树。")
        if "timeout" in error.lower():
            error += " 该任务拥有的进程树已终止，不会再写项目文件。"
    else:
        error = parsed.get("error", "") or (proc.stderr if proc.returncode != 0 else "")
    if blocked:
        error = (error + " " if error else "") + "宿主报告 AC_BLOCKED，任务未完成。"
    return {
        "ok": ok, "host": host, "session_id": parsed.get("session_id", ""),
        "result": parsed.get("result", ""), "error": error, "warnings": warnings,
        "status": status, "completion_evidence": "self-reported" if markers["done"] else "none",
        "markers": markers, "receipts": receipts, "exit_code": proc.returncode,
        "started_at": started, "duration_ms": int((time.time() - t0) * 1000),
        "raw_log": raw[-200000:], "session_dir": str(workspace),
        "synced_files": synced["synced_files"], "copied_files": synced["copied"],
        "updated_files": synced["updated"],
        "kept_in_session": synced["kept_in_session"], "sync_errors": synced["errors"],
        "tool_read": read_via_tool, "marker_read": bool(markers["read"]),
        "project_path": str(project), "project_id": pid,
    }


def sync_workspace_back(store: Store, pid: str) -> dict:
    """Explicit, repeatable check-and-copy of a project's workspace files.

    Used after resuming the session in Claude Desktop: files created there are
    copied into the project, conflicts are listed, nothing is overwritten.
    """
    project = store.project_path(pid)
    session_dir = project_workspace(store, pid, project)
    state = _load_return_state(store, pid)
    result = _sync_back(session_dir, project, baseline=None, state=state)
    _save_return_state(store, pid, state)
    result.update({"ok": not result["errors"], "session_dir": str(session_dir),
                   "project_path": str(project), "project_id": pid})
    return result
