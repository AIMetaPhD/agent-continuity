"""Local-first storage, versioning, memory, handoff, sync and host integration.

The app deliberately uses plain files plus Git. There is no database and no
background service: every state change is a file write and (for project data)
a Git commit, so it can be inspected, backed up and synced with ordinary Git.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

APP_VERSION = "0.1.2"
BEGIN = "<!-- agent-continuity:begin -->"
END = "<!-- agent-continuity:end -->"
DEFAULT_HOME = Path.home() / ".agent-continuity"
HOST_AGENTS = {"claude": "claude", "codex": "codex"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def slugify(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-._")
    return (value or fallback)[:48]


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_json_checked(path: Path) -> tuple[Any, str]:
    """Read JSON and report why it is unreadable instead of silently dropping it.

    A memory file left with Git conflict markers must show up as broken, not
    vanish from the list.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "file missing"
    except OSError as exc:
        return None, f"read failed: {exc}"
    except ValueError as exc:
        return None, f"invalid JSON: {exc}"


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _as_text(value: Any) -> str:
    """TimeoutExpired.stdout/stderr can be bytes even for text-mode processes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the process and its descendants so nothing keeps writing files."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
        timeout: int = 60, input_text: str | None = None) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update({k: str(v) for k, v in env.items() if v is not None})
    kwargs: dict[str, Any] = dict(
        cwd=str(cwd) if cwd else None, env=merged,
        stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        note = f"timeout after {timeout}s; process tree terminated"
        return subprocess.CompletedProcess(cmd, 124, _as_text(out), _as_text(err) + "\n" + note)
    return subprocess.CompletedProcess(cmd, proc.returncode, _as_text(out), _as_text(err))


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(repo), *args], timeout=timeout)


def _unresolved_conflicts(repo: Path) -> list[str]:
    proc = _git(repo, "diff", "--name-only", "--diff-filter=U")
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _git_commit(repo: Path, message: str) -> None:
    # Never commit conflicted files: `git add -A` would stage the conflict
    # markers as if they were resolved content and propagate corruption.
    pending = _unresolved_conflicts(repo)
    if pending:
        raise ValueError("还有未解决的同步冲突：" + "、".join(pending)
                         + "。请先在 设备与同步 里选择保留哪一侧，再继续。")
    _git(repo, "add", "-A")
    status = _git(repo, "status", "--porcelain")
    if status.returncode == 0 and status.stdout.strip():
        _git(repo, "-c", "user.name=Agent Continuity",
             "-c", "user.email=local@agent-continuity.invalid",
             "commit", "-q", "-m", message)


def _safe_agent(agent: str) -> str:
    return slugify(agent or "agent", "agent").lower()


def _inside(base: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


class Store:
    """All product state for this device."""

    def __init__(self, home: str | Path | None = None):
        self.home = Path(home or os.environ.get("AGENT_CONTINUITY_HOME") or DEFAULT_HOME).expanduser().resolve()
        self.data_root = self.home / "data"
        self.backup_root = self.home / "backups"
        self.log_root = self.home / "logs"
        for p in (self.home, self.data_root, self.backup_root, self.log_root):
            p.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.device = read_json(self.home / "device.json", {})
        if not self.device.get("id"):
            self.device = {
                "id": f"dev-{uuid.uuid4().hex[:10]}",
                "name": socket.gethostname() or "this-device",
                "created_at": utc_now(),
            }
            write_json(self.home / "device.json", self.device)
        self.registry = read_json(self.home / "registry.json", {"version": 1, "projects": {}})
        self.registry.setdefault("version", 1)
        self.registry.setdefault("projects", {})
        self.settings = read_json(self.home / "settings.json", {})

    # ---------------------------------------------------------------- basics

    def save_registry(self) -> None:
        write_json(self.home / "registry.json", self.registry)

    def save_settings(self) -> None:
        write_json(self.home / "settings.json", self.settings)

    def project_entry(self, pid: str) -> dict:
        entry = self.registry["projects"].get(pid)
        if not entry:
            raise KeyError(f"unknown project: {pid}")
        return entry

    def project_path(self, pid: str) -> Path:
        entry = self.project_entry(pid)
        path = entry.get("devices", {}).get(self.device["id"]) or entry.get("path")
        if not path:
            raise KeyError(f"no local path for project {pid} on this device")
        return Path(path).expanduser().resolve()

    def data_dir(self, pid: str) -> Path:
        entry = self.project_entry(pid)
        return Path(entry["data_dir"]).resolve()

    def _project_meta(self, pid: str) -> dict:
        return read_json(self.data_dir(pid) / "project.json", {"id": pid})

    def _commit_data(self, pid: str, message: str) -> None:
        _git_commit(self.data_dir(pid), message)

    def _ensure_no_conflicts(self, pid: str) -> None:
        """Fail before writing anything while a merge is still unresolved."""
        pending = _unresolved_conflicts(self.data_dir(pid))
        if pending:
            raise ValueError("还有未解决的同步冲突：" + "、".join(pending)
                             + "。请先在 设备与同步 里选择保留哪一侧，再做修改。")

    # ------------------------------------------------------------- settings

    def settings_get(self) -> dict:
        settings = json.loads(json.dumps(self.settings))
        claude = settings.setdefault("claude", {})
        if claude.get("token"):
            claude["token"] = "********"
        return settings

    def settings_update(self, payload: dict) -> dict:
        with self.lock:
            for section in ("claude", "codex", "sync", "hosts", "ui"):
                if isinstance(payload.get(section), dict):
                    target = self.settings.setdefault(section, {})
                    for key, value in payload[section].items():
                        if section == "claude" and key == "token" and value == "********":
                            continue
                        target[key] = value
            self.save_settings()
            return self.settings_get()

    def claude_env(self) -> dict:
        claude = self.settings.get("claude", {})
        env = {}
        if claude.get("base_url"):
            env["ANTHROPIC_BASE_URL"] = claude["base_url"]
        if claude.get("token"):
            env["ANTHROPIC_AUTH_TOKEN"] = claude["token"]
        if claude.get("model"):
            env["ANTHROPIC_MODEL"] = claude["model"]
        return env

    def codex_env(self) -> dict:
        codex = self.settings.get("codex", {})
        env = {}
        if codex.get("codex_home"):
            env["CODEX_HOME"] = codex["codex_home"]
        return env

    # ---------------------------------------------------------------- setup

    def setup_checks(self) -> list[dict]:
        checks: list[dict] = []

        def add(cid, label, ok, detail, fix=""):
            checks.append({"id": cid, "label": label, "ok": bool(ok), "detail": detail, "fix": fix})

        add("python", "Python 3.11+", True, f"Python {sys.version.split()[0]}", "")
        git = shutil.which("git")
        gv = run(["git", "--version"]) if git else None
        add("git", "Git", bool(git and gv and gv.returncode == 0),
            (gv.stdout or "").strip() if gv else "not found",
            "安装 Git 并确保 git 在 PATH 中。")
        claude = shutil.which("claude")
        cv = run(["claude", "--version"]) if claude else None
        add("claude", "Claude Code CLI", bool(claude and cv and cv.returncode == 0),
            (cv.stdout or cv.stderr or "").strip() if cv else "not found",
            "安装 Claude Code；如果使用本机 MagPie，请在设置中填写网关地址。")
        codex = shutil.which("codex") or shutil.which("codex.cmd")
        if not codex:
            npm_codex = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
            if npm_codex.exists():
                codex = str(npm_codex)
        xv = run([codex, "--version"]) if codex else None
        add("codex", "Codex CLI", bool(codex and xv and xv.returncode == 0),
            (xv.stdout or xv.stderr or "").strip() if xv else "not found",
            "安装 Codex CLI 并完成登录；也可以在设置中填写 codex.cmd 的完整路径。")
        magpie_ok, magpie_detail = False, "not detected"
        try:
            with urllib.request.urlopen("http://127.0.0.1:3425/", timeout=2) as resp:
                body = resp.read(8192).decode("utf-8", "replace")
                magpie_ok = resp.status == 200 and ("magpie" in body.lower() or '"apis"' in body)
                magpie_detail = "MagPie at http://127.0.0.1:3425" if magpie_ok else body[:120]
        except Exception as exc:
            magpie_detail = f"not available ({exc.__class__.__name__})"
        add("magpie", "本机 MagPie 网关（可选）", magpie_ok, magpie_detail,
            "如果使用 MagPie，请先启动它；也可以改用 Claude Code 自己的登录。")
        try:
            probe = self.home / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            add("home", "数据目录可写", True, str(self.home), "")
        except Exception as exc:
            add("home", "数据目录可写", False, str(exc), "检查目录权限或设置 AGENT_CONTINUITY_HOME。")
        return checks

    # ------------------------------------------------------------- projects

    def list_projects(self) -> list[dict]:
        out = []
        for pid, entry in sorted(self.registry["projects"].items(), key=lambda kv: kv[1].get("created_at", "")):
            try:
                out.append(self.project_summary(pid))
            except Exception:
                out.append({"id": pid, "name": entry.get("name", pid), "error": "project data missing"})
        return out

    def project_summary(self, pid: str) -> dict:
        entry = self.project_entry(pid)
        path = entry.get("devices", {}).get(self.device["id"]) or entry.get("path") or ""
        data = self.data_dir(pid)
        memories = self._load_memories(pid)
        handoffs = self._load_handoffs(pid)
        definitions = []
        defs_root = data / "definitions"
        if defs_root.exists():
            for agent_dir in sorted(p for p in defs_root.iterdir() if p.is_dir()):
                idx = read_json(agent_dir / "index.json", {})
                if idx.get("revisions"):
                    definitions.append({
                        "agent": idx.get("agent", agent_dir.name),
                        "active_revision": idx.get("active_revision"),
                        "revision_count": len(idx.get("revisions", [])),
                        "applied": idx.get("applied"),
                    })
        return {
            "id": pid,
            "name": entry.get("name") or self._project_meta(pid).get("name") or pid,
            "path": path,
            "data_dir": str(data),
            "remote": entry.get("remote", ""),
            "created_at": entry.get("created_at"),
            "devices": entry.get("devices", {}),
            "device_count": len(entry.get("devices", {})),
            "memory_current": sum(1 for m in memories if m.get("status") == "current"),
            "memory_total": len(memories),
            "handoff_latest": handoffs[0] if handoffs else None,
            "definitions": definitions,
            "last_commit": self._last_commit(pid),
        }

    def create_project(self, name: str, path: str) -> dict:
        name = (name or "").strip() or "Untitled project"
        project_path = Path(path).expanduser().resolve()
        if not project_path.exists() or not project_path.is_dir():
            raise ValueError("项目目录不存在或不是目录")
        with self.lock:
            pid = f"{slugify(name, 'project')}-{uuid.uuid4().hex[:6]}"
            data = self.data_root / pid
            if data.exists():
                raise ValueError("项目数据目录已存在")
            data.mkdir(parents=True)
            write_json(data / "project.json", {"id": pid, "name": name, "created_at": utc_now()})
            _git(data, "init", "-q", "-b", "main")
            _git(data, "config", "user.name", "Agent Continuity")
            _git(data, "config", "user.email", "local@agent-continuity.invalid")
            _git_commit(data, "Initialize project data")
            entry = {
                "id": pid,
                "name": name,
                "created_at": utc_now(),
                "data_dir": str(data),
                "remote": "",
                "devices": {self.device["id"]: str(project_path)},
                "path": str(project_path),
            }
            self.registry["projects"][pid] = entry
            self.save_registry()
            self.receipt_add(pid, "saved", "project", pid, "1", detail="project registered")
            return self.project_summary(pid)

    def join_project(self, remote: str, path: str) -> dict:
        remote = (remote or "").strip()
        if not remote:
            raise ValueError("需要数据仓库的 Git 地址")
        project_path = Path(path).expanduser().resolve()
        if not project_path.exists() or not project_path.is_dir():
            raise ValueError("本机项目目录不存在或不是目录")
        with self.lock:
            tmp = self.data_root / f".join-{uuid.uuid4().hex[:8]}"
            proc = run(["git", "clone", "--quiet", remote, str(tmp)], timeout=120)
            if proc.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ValueError(f"克隆失败：{(proc.stderr or proc.stdout).strip()[:300]}")
            meta = read_json(tmp / "project.json", {})
            pid = meta.get("id")
            if not pid:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ValueError("数据仓库缺少 project.json/id")
            data = self.data_root / pid
            if data.exists():
                shutil.rmtree(tmp, ignore_errors=True)
                raise ValueError("本机已经有这个项目的数据")
            tmp.rename(data)
            # A joined device must be able to commit without touching the user's
            # global Git identity, exactly like a newly created project.
            _git(data, "config", "user.name", "Agent Continuity")
            _git(data, "config", "user.email", "local@agent-continuity.invalid")
            entry = {
                "id": pid,
                "name": meta.get("name", pid),
                "created_at": meta.get("created_at", utc_now()),
                "data_dir": str(data),
                "remote": remote,
                "devices": {self.device["id"]: str(project_path)},
                "path": str(project_path),
            }
            self.registry["projects"][pid] = entry
            self.save_registry()
            self.receipt_add(pid, "delivered", "project", pid, "1", detail="joined from remote")
            return self.project_summary(pid)

    def remove_project(self, pid: str) -> None:
        with self.lock:
            self.project_entry(pid)
            self.registry["projects"].pop(pid, None)
            self.save_registry()

    def update_device_path(self, pid: str, path: str) -> dict:
        project_path = Path(path).expanduser().resolve()
        if not project_path.exists() or not project_path.is_dir():
            raise ValueError("目录不存在或不是目录")
        with self.lock:
            entry = self.project_entry(pid)
            entry.setdefault("devices", {})[self.device["id"]] = str(project_path)
            entry["path"] = str(project_path)
            self.save_registry()
            return self.project_summary(pid)

    def _last_commit(self, pid: str) -> dict | None:
        proc = _git(self.data_dir(pid), "log", "-1", "--format=%H|%s|%cI")
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        sha, subject, at = (proc.stdout.strip().split("|", 2) + ["", "", ""])[:3]
        return {"sha": sha, "subject": subject, "at": at}

    # ----------------------------------------------------------- definitions

    def _def_dir(self, pid: str, agent: str) -> Path:
        return self.data_dir(pid) / "definitions" / _safe_agent(agent)

    def _def_index(self, pid: str, agent: str) -> dict:
        return read_json(self._def_dir(pid, agent) / "index.json",
                         {"agent": _safe_agent(agent), "active_revision": None, "revisions": [], "applied": None})

    def _save_def_index(self, pid: str, agent: str, index: dict) -> None:
        write_json(self._def_dir(pid, agent) / "index.json", index)

    def definitions_list(self, pid: str, agent: str | None = None) -> list[dict]:
        root = self.data_dir(pid) / "definitions"
        agents = [agent] if agent else ([p.name for p in root.iterdir() if p.is_dir()] if root.exists() else [])
        out = []
        for a in agents:
            idx = self._def_index(pid, a)
            out.append({
                "agent": idx.get("agent", a),
                "active_revision": idx.get("active_revision"),
                "applied": idx.get("applied"),
                "revisions": idx.get("revisions", []),
            })
        return out

    def definition_get(self, pid: str, agent: str, rev: str) -> dict:
        idx = self._def_index(pid, agent)
        meta = next((r for r in idx.get("revisions", []) if r["rev"] == rev), None)
        if not meta:
            raise KeyError("revision not found")
        path = self._def_dir(pid, agent) / f"{rev}.md"
        return {**meta, "agent": idx.get("agent", agent), "content": path.read_text(encoding="utf-8"),
                "active": idx.get("active_revision") == rev, "applied": idx.get("applied")}

    def definition_create(self, pid: str, agent: str, content: str, name: str = "", source: str = "web") -> dict:
        agent = _safe_agent(agent)
        with self.lock:
            idx = self._def_index(pid, agent)
            n = len(idx.get("revisions", [])) + 1
            rev = f"r{n}"
            path = self._def_dir(pid, agent) / f"{rev}.md"
            atomic_write(path, content)
            meta = {"rev": rev, "name": name or f"{agent} {rev}", "source": source,
                    "created_at": utc_now(), "sha256": sha256_text(content)}
            idx.setdefault("revisions", []).append(meta)
            if not idx.get("active_revision"):
                idx["active_revision"] = rev
            self._save_def_index(pid, agent, idx)
            self._commit_data(pid, f"Definition {agent} {rev}")
            self.receipt_add(pid, "saved", "definition", f"{agent}:{rev}", rev, detail="definition saved")
            return self.definition_get(pid, agent, rev)

    def definition_activate(self, pid: str, agent: str, rev: str) -> dict:
        with self.lock:
            idx = self._def_index(pid, agent)
            if not any(r["rev"] == rev for r in idx.get("revisions", [])):
                raise KeyError("revision not found")
            idx["active_revision"] = rev
            self._save_def_index(pid, agent, idx)
            self._commit_data(pid, f"Activate {agent} {rev}")
            self.receipt_add(pid, "saved", "definition", f"{agent}:{rev}", rev, detail="definition activated")
            return self.definition_get(pid, agent, rev)

    def definition_diff(self, pid: str, agent: str, rev_a: str, rev_b: str) -> str:
        a = self.definition_get(pid, agent, rev_a)["content"].splitlines(keepends=True)
        b = self.definition_get(pid, agent, rev_b)["content"].splitlines(keepends=True)
        return "".join(difflib.unified_diff(a, b, fromfile=f"{agent}/{rev_a}", tofile=f"{agent}/{rev_b}"))

    def _target_path(self, pid: str, target: str) -> Path:
        if target not in ("CLAUDE.md", "AGENTS.md"):
            raise ValueError("target must be CLAUDE.md or AGENTS.md")
        base = self.project_path(pid)
        path = (base / target).resolve()
        if not _inside(base, path):
            raise ValueError("target escapes project directory")
        return path

    def _replace_block(self, text: str, content: str) -> str:
        block = f"{BEGIN}\n{content.rstrip()}\n{END}"
        if BEGIN in text and END in text:
            start = text.index(BEGIN)
            end = text.index(END) + len(END)
            return text[:start] + block + text[end:]
        return (text.rstrip() + "\n\n" + block + "\n") if text.strip() else block + "\n"

    def _remove_block(self, text: str) -> str:
        if BEGIN not in text or END not in text:
            return text
        start = text.index(BEGIN)
        end = text.index(END) + len(END)
        out = (text[:start].rstrip("\n") + "\n" + text[end:].lstrip("\n")).strip("\n")
        return out + "\n" if out else ""

    def definition_apply(self, pid: str, agent: str, rev: str, target: str) -> dict:
        with self.lock:
            self._ensure_no_conflicts(pid)
            doc = self.definition_get(pid, agent, rev)
            path = self._target_path(pid, target)
            old_text = path.read_text(encoding="utf-8") if path.exists() else ""
            backup_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{target}"
            backup = self.data_dir(pid) / "backups" / backup_name
            atomic_write(backup, old_text)
            new_text = self._replace_block(old_text, doc["content"])
            atomic_write(path, new_text)
            idx = self._def_index(pid, agent)
            idx["applied"] = {
                "target": target,
                "revision": rev,
                "previous_content_hash": sha256_text(old_text),
                "applied_content_hash": sha256_text(new_text),
                "backup": str(backup.relative_to(self.data_dir(pid))),
                "applied_at": utc_now(),
            }
            self._save_def_index(pid, agent, idx)
            self._commit_data(pid, f"Apply {agent} {rev} to {target}")
            self.receipt_add(pid, "saved", "definition", f"{agent}:{rev}", rev,
                             detail=f"applied to {target}")
            return {"target": str(path), "applied": idx["applied"]}

    def definition_restore(self, pid: str, agent: str) -> dict:
        """Remove this app's managed block, keeping every edit outside it.

        Always removing the block (instead of restoring a snapshot taken during
        an earlier apply) is what makes repeated applies followed by an
        uninstall leave no stale managed rules behind.
        """
        with self.lock:
            idx = self._def_index(pid, agent)
            applied = idx.get("applied")
            if not applied:
                raise ValueError("this definition has not been applied")
            path = self._target_path(pid, applied["target"])
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            restored = self._remove_block(current)
            atomic_write(path, restored)
            idx["applied"] = None
            self._save_def_index(pid, agent, idx)
            self._commit_data(pid, f"Restore {agent} integration")
            self.receipt_add(pid, "saved", "definition", f"{agent}:{applied.get('revision')}",
                             applied.get("revision", ""), detail="integration restored")
            return {"target": str(path), "restored": True}

    # --------------------------------------------------------------- memory

    def _memories_dir(self, pid: str) -> Path:
        return self.data_dir(pid) / "memories"

    def _load_memories(self, pid: str) -> list[dict]:
        root = self._memories_dir(pid)
        if not root.exists():
            return []
        out = []
        for path in sorted(root.glob("*.json")):
            doc, error = read_json_checked(path)
            if doc:
                out.append(doc)
            elif error:
                # Show it as broken instead of silently dropping the record.
                out.append({
                    "id": path.stem, "title": f"{path.stem}（无法读取）",
                    "scope": "project", "status": "unreadable", "current_revision": "",
                    "created_at": "", "updated_at": "", "revisions": [],
                    "read_error": error,
                })
        return out

    def _save_memory(self, pid: str, doc: dict) -> None:
        write_json(self._memories_dir(pid) / f"{doc['id']}.json", doc)

    def memory_list(self, pid: str, query: str = "", scope: str = "", status: str = "") -> list[dict]:
        memories = self._load_memories(pid)
        q = (query or "").lower()
        out = []
        for m in memories:
            text = self._memory_text(m)
            if q and q not in (m.get("title", "") + " " + text).lower():
                continue
            if scope and m.get("scope") != scope:
                continue
            if status and m.get("status") != status:
                continue
            out.append({**m, "text": text})
        out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return out

    def _memory_text(self, doc: dict) -> str:
        revs = doc.get("revisions", [])
        current = doc.get("current_revision")
        for rev in revs:
            if rev.get("rev") == current:
                return rev.get("text", "")
        return revs[-1].get("text", "") if revs else ""

    def memory_get(self, pid: str, mid: str) -> dict:
        path = self._memories_dir(pid) / f"{mid}.json"
        doc, error = read_json_checked(path)
        if not doc:
            if path.exists():
                raise ValueError(f"记忆文件无法解析（{error}）；请先处理同步冲突或从备份恢复。")
            raise KeyError("memory not found")
        return {**doc, "text": self._memory_text(doc)}

    def memory_create(self, pid: str, title: str, text: str, scope: str = "project",
                      source: str = "web") -> dict:
        with self.lock:
            self._ensure_no_conflicts(pid)
            mid = f"m-{uuid.uuid4().hex[:10]}"
            doc = {
                "id": mid, "title": (title or "").strip() or text.strip()[:48] or "Memory",
                "scope": scope or "project", "status": "current", "current_revision": "r1",
                "created_at": utc_now(), "updated_at": utc_now(),
                "revisions": [{"rev": "r1", "text": text, "source": source,
                               "created_at": utc_now(), "parent": None}],
            }
            self._save_memory(pid, doc)
            self._commit_data(pid, f"Memory {mid} created")
            self.receipt_add(pid, "saved", "memory", mid, "r1", detail="memory created")
            return {**doc, "text": text}

    def memory_update(self, pid: str, mid: str, text: str | None = None, title: str | None = None,
                      status: str | None = None, source: str = "web") -> dict:
        with self.lock:
            self._ensure_no_conflicts(pid)
            doc = self.memory_get(pid, mid)
            doc.pop("text", None)
            if title is not None:
                doc["title"] = title.strip() or doc["title"]
            if text is not None:
                revs = doc.setdefault("revisions", [])
                n = len(revs) + 1
                rev = f"r{n}"
                revs.append({"rev": rev, "text": text, "source": source,
                             "created_at": utc_now(), "parent": doc.get("current_revision")})
                doc["current_revision"] = rev
                # Editing content never reactivates a revoked memory: only an
                # explicit status="current" does that, below.
            if status in ("current", "revoked"):
                if status == "revoked":
                    doc["status"] = "revoked"
                elif doc.get("status") == "revoked":
                    revs = doc.setdefault("revisions", [])
                    n = len(revs) + 1
                    rev = f"r{n}"
                    revs.append({"rev": rev, "text": self._memory_text(doc), "source": "explicit restore",
                                 "created_at": utc_now(), "parent": doc.get("current_revision")})
                    doc["current_revision"] = rev
                    doc["status"] = "current"
            doc["updated_at"] = utc_now()
            self._save_memory(pid, doc)
            self._commit_data(pid, f"Memory {mid} updated")
            self.receipt_add(pid, "saved", "memory", mid, doc.get("current_revision", ""),
                             detail=f"status={doc.get('status')}")
            return {**doc, "text": self._memory_text(doc)}

    # -------------------------------------------------------------- handoff

    def _handoffs_dir(self, pid: str) -> Path:
        return self.data_dir(pid) / "handoffs"

    def _load_handoffs(self, pid: str) -> list[dict]:
        root = self._handoffs_dir(pid)
        if not root.exists():
            return []
        out = []
        for path in root.glob("*.json"):
            doc = read_json(path, None)
            if doc:
                out.append(doc)
        out.sort(key=lambda h: h.get("created_at", ""), reverse=True)
        return out

    def handoff_list(self, pid: str) -> list[dict]:
        return self._load_handoffs(pid)

    def handoff_get(self, pid: str, hid: str) -> dict:
        doc = read_json(self._handoffs_dir(pid) / f"{hid}.json", None)
        if not doc:
            raise KeyError("handoff not found")
        jobs = []
        job_dir = self.data_dir(pid) / "jobs"
        if job_dir.exists():
            for path in sorted(job_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                job = read_json(path, None)
                if job and job.get("handoff") == hid:
                    result = job.get("result") or {}
                    jobs.append({
                        "id": job.get("id"), "status": job.get("status"), "host": job.get("host"),
                        "started_at": job.get("started_at"), "error": job.get("error", ""),
                        "session_id": result.get("session_id", ""),
                        "markers": result.get("markers"),
                        "result": result.get("result", ""),
                        "warnings": result.get("warnings") or [],
                        "duration_ms": result.get("duration_ms"),
                        "session_dir": result.get("session_dir", ""),
                        "synced_files": result.get("synced_files") or [],
                        "kept_in_session": result.get("kept_in_session") or [],
                        "sync_errors": result.get("sync_errors") or [],
                        "tool_read": result.get("tool_read"),
                    })
        doc["jobs"] = jobs
        return doc

    def _git_status(self, path: Path) -> dict:
        if not path.exists():
            return {"is_repo": False, "dirty_files": [], "head": ""}
        head = _git(path, "rev-parse", "HEAD")
        status = _git(path, "status", "--porcelain")
        return {
            "is_repo": head.returncode == 0,
            "head": head.stdout.strip() if head.returncode == 0 else "",
            "dirty_files": [line for line in status.stdout.splitlines() if line.strip()] if status.returncode == 0 else [],
        }

    def handoff_create(self, pid: str, goal: str, done: str, next_step: str, evidence: str = "",
                       agent: str = "claude") -> dict:
        with self.lock:
            self._ensure_no_conflicts(pid)
            project = self.project_path(pid)
            status = self._git_status(project)
            data_commit = self._last_commit(pid)
            hid = f"h-{uuid.uuid4().hex[:10]}"
            doc = {
                "id": hid, "status": "ready", "goal": goal, "done": done,
                "next": next_step, "evidence": evidence, "agent": agent,
                "base_commit": status.get("head", ""), "dirty_files": status.get("dirty_files", []),
                "data_commit": (data_commit or {}).get("sha", ""),
                "created_at": utc_now(), "updated_at": utc_now(), "tasks": [],
            }
            write_json(self._handoffs_dir(pid) / f"{hid}.json", doc)
            self._commit_data(pid, f"Handoff {hid} created")
            self.receipt_add(pid, "saved", "handoff", hid, doc["data_commit"], detail="handoff ready")
            return doc

    def handoff_set_status(self, pid: str, hid: str, status: str) -> dict:
        if status not in ("draft", "ready", "in_progress", "completed", "failed", "stale"):
            raise ValueError("invalid status")
        with self.lock:
            doc = self.handoff_get(pid, hid)
            doc["status"] = status
            doc["updated_at"] = utc_now()
            write_json(self._handoffs_dir(pid) / f"{hid}.json", doc)
            self._commit_data(pid, f"Handoff {hid} -> {status}")
            self.receipt_add(pid, "saved", "handoff", hid, doc.get("data_commit", ""),
                             detail=f"status={status}")
            return doc

    # -------------------------------------------------------------- receipts

    def receipt_add(self, pid: str, layer: str, record_type: str, record_id: str, revision: str,
                    session_id: str = "", detail: str = "") -> dict:
        receipt = {
            "id": f"rc-{uuid.uuid4().hex[:10]}", "layer": layer, "record_type": record_type,
            "record_id": record_id, "revision": revision, "device_id": self.device["id"],
            "session_id": session_id, "at": utc_now(), "detail": detail,
        }
        # One append-only file per device: two devices never conflict merely by
        # recording that they saw something. Receipts are merged by reading all
        # device files, not by rewriting a shared file.
        path = self.data_dir(pid) / "receipts" / f"{self.device['id']}.jsonl"
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(receipt, ensure_ascii=False) + "\n")
            self._commit_data(pid, f"Receipt {layer} {record_id}")
        return receipt

    def receipt_list(self, pid: str, record_id: str | None = None) -> list[dict]:
        root = self.data_dir(pid) / "receipts"
        if not root.exists():
            return []
        out = []
        for path in sorted(root.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if record_id and item.get("record_id") != record_id:
                    continue
                out.append(item)
        out.sort(key=lambda item: item.get("at", ""))
        return out

    # ------------------------------------------------------------------ sync

    def sync_status(self, pid: str) -> dict:
        data = self.data_dir(pid)
        entry = self.project_entry(pid)
        remote = entry.get("remote", "")
        status = _git(data, "status", "--porcelain")
        log = _git(data, "log", "-1", "--format=%H|%s|%cI")
        ahead = behind = 0
        if remote:
            counts = _git(data, "rev-list", "--left-right", "--count", "origin/main...HEAD")
            if counts.returncode == 0 and counts.stdout.strip():
                left, right = counts.stdout.split()
                behind, ahead = int(left), int(right)
        return {
            "remote": remote,
            "branch": "main",
            "dirty": [line for line in status.stdout.splitlines() if line.strip()] if status.returncode == 0 else [],
            "last_commit": log.stdout.strip() if log.returncode == 0 else "",
            "ahead": ahead, "behind": behind,
            "conflicts": self.sync_conflicts(pid),
        }

    def sync_set_remote(self, pid: str, url: str) -> dict:
        url = (url or "").strip()
        if not url:
            raise ValueError("remote URL is empty")
        data = self.data_dir(pid)
        existing = _git(data, "remote")
        if "origin" in existing.stdout.split():
            proc = _git(data, "remote", "set-url", "origin", url)
        else:
            proc = _git(data, "remote", "add", "origin", url)
        if proc.returncode != 0:
            raise ValueError((proc.stderr or proc.stdout).strip())
        self.project_entry(pid)["remote"] = url
        self.save_registry()
        return self.sync_status(pid)

    def sync_commit(self, pid: str, message: str = "Sync from Agent Continuity") -> dict:
        with self.lock:
            self._commit_data(pid, message)
            return self.sync_status(pid)

    def sync_push(self, pid: str) -> dict:
        data = self.data_dir(pid)
        if not self.project_entry(pid).get("remote"):
            raise ValueError("尚未设置 Git remote")
        self._ensure_no_conflicts(pid)
        self._commit_data(pid, "Sync before push")
        proc = _git(data, "push", "-u", "origin", "main", timeout=120)
        if proc.returncode != 0:
            raise ValueError((proc.stderr or proc.stdout).strip()[:500])
        return self.sync_status(pid)

    def sync_pull(self, pid: str) -> dict:
        data = self.data_dir(pid)
        if not self.project_entry(pid).get("remote"):
            raise ValueError("尚未设置 Git remote")
        pending = _unresolved_conflicts(data)
        if pending:
            return {"ok": False, "conflicts": self.sync_conflicts(pid),
                    "message": "还有未解决的同步冲突：" + "、".join(pending)
                               + "。请先选择保留哪一侧。"}
        self._commit_data(pid, "Local changes before pull")
        proc = _git(data, "pull", "--no-rebase", "origin", "main", timeout=120)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip()[:500]
            if "identity unknown" in message or "unable to auto-detect email" in message:
                message += "（这台设备的 Git 缺少提交身份；应用通常会自动写入仓库局部身份，若仍失败请检查该数据仓库的 .git/config。）"
            return {"ok": False, "conflicts": self.sync_conflicts(pid), "message": message}
        self._record_delivered(pid)
        return {"ok": True, "status": self.sync_status(pid)}

    def _record_delivered(self, pid: str) -> None:
        for m in self._load_memories(pid):
            self.receipt_add(pid, "delivered", "memory", m["id"], m.get("current_revision", ""),
                             detail="data repo pulled")
        for h in self._load_handoffs(pid):
            self.receipt_add(pid, "delivered", "handoff", h["id"], h.get("data_commit", ""),
                             detail="data repo pulled")
        for d in self.definitions_list(pid):
            rev = d.get("active_revision")
            if rev:
                self.receipt_add(pid, "delivered", "definition", f"{d['agent']}:{rev}", rev,
                                 detail="data repo pulled")

    def sync_conflicts(self, pid: str) -> list[dict]:
        data = self.data_dir(pid)
        names = _git(data, "diff", "--name-only", "--diff-filter=U")
        if names.returncode != 0:
            return []
        out = []
        for rel in [n for n in names.stdout.splitlines() if n.strip()]:
            ours = _git(data, "show", f":2:{rel}")
            theirs = _git(data, "show", f":3:{rel}")
            out.append({"path": rel, "local": ours.stdout if ours.returncode == 0 else "",
                        "remote": theirs.stdout if theirs.returncode == 0 else ""})
        return out

    def sync_resolve(self, pid: str, rel: str, choice: str) -> dict:
        if choice not in ("local", "remote"):
            raise ValueError("choice must be local or remote")
        data = self.data_dir(pid)
        if not _inside(data, (data / rel).resolve()):
            raise ValueError("path escapes data repo")
        stage = ":2:" if choice == "local" else ":3:"
        content = _git(data, "show", f"{stage}{rel}")
        if content.returncode != 0:
            raise ValueError((content.stderr or content.stdout).strip())
        if rel.endswith(".json"):
            try:
                json.loads(content.stdout)
            except ValueError as exc:
                raise ValueError(f"选中的版本不是有效 JSON（{exc}）；没有做任何改动。") from exc
        atomic_write(data / rel, content.stdout)
        _git(data, "add", rel)
        _git(data, "-c", "user.name=Agent Continuity", "-c", "user.email=local@agent-continuity.invalid",
             "commit", "-q", "-m", f"Resolve conflict in {rel} ({choice})")
        return {"path": rel, "choice": choice, "conflicts": self.sync_conflicts(pid)}

    # ------------------------------------------------------------- integrate

    def integrate(self, pid: str, host: str, target: str) -> dict:
        agent = HOST_AGENTS.get(host, _safe_agent(host))
        defs = self.definitions_list(pid, agent)
        if not defs or not defs[0].get("active_revision"):
            raise ValueError(f"{host} 还没有可激活的定义版本")
        return self.definition_apply(pid, agent, defs[0]["active_revision"], target)

    def unintegrate(self, pid: str) -> list[dict]:
        results = []
        for d in self.definitions_list(pid):
            if d.get("applied"):
                results.append(self.definition_restore(pid, d["agent"]))
        return results

    # ---------------------------------------------------------- export/backup

    def export_data(self, pid: str) -> dict:
        data = self.data_dir(pid)
        definitions = {}
        root = data / "definitions"
        if root.exists():
            for agent_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                idx = read_json(agent_dir / "index.json", {})
                definitions[idx.get("agent", agent_dir.name)] = {
                    "index": idx,
                    "revisions": {p.stem: p.read_text(encoding="utf-8")
                                  for p in sorted(agent_dir.glob("r*.md"))},
                }
        return {
            "app": "agent-continuity", "version": APP_VERSION, "exported_at": utc_now(),
            "project": self._project_meta(pid),
            "definitions": definitions,
            "memories": self._load_memories(pid),
            "handoffs": self._load_handoffs(pid),
            "receipts": self.receipt_list(pid),
        }

    def backup(self, pid: str) -> Path:
        data = self.data_dir(pid)
        dest = self.backup_root / f"{pid}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in data.rglob("*"):
                if path.is_file() and ".git" not in path.parts and "backups" not in path.parts:
                    zf.write(path, path.relative_to(data))
        return dest

    def backups(self, pid: str) -> list[dict]:
        out = []
        for path in sorted(self.backup_root.glob(f"{pid}-*.zip"), reverse=True):
            out.append({"name": path.name, "size": path.stat().st_size,
                        "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
        return out

    def restore_backup(self, pid: str, name: str) -> dict:
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError("invalid backup name")
        path = self.backup_root / name
        if not path.exists() or path.suffix.lower() != ".zip":
            raise KeyError("backup not found")
        data = self.data_dir(pid)
        temp = self.backup_root / f".restore-{uuid.uuid4().hex[:8]}"
        temp.mkdir(parents=True)
        try:
            with zipfile.ZipFile(path) as zf:
                for member in zf.infolist():
                    target = (temp / member.filename).resolve()
                    if not _inside(temp, target):
                        raise ValueError("backup contains an unsafe path")
                zf.extractall(temp)
            meta = read_json(temp / "project.json", {})
            if meta.get("id") != pid:
                raise ValueError("backup belongs to a different project")
            for item in list(data.iterdir()):
                if item.name == ".git":
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            for item in temp.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(temp)
                    dest = data / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
            self._commit_data(pid, f"Restore backup {name}")
            self.receipt_add(pid, "saved", "project", pid, "restore", detail=f"restored {name}")
            return {"restored": name}
        finally:
            shutil.rmtree(temp, ignore_errors=True)
