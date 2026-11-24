# Agent Continuity

A local-first workspace for versioned agent definitions, project memory and verifiable work handoffs across coding tools and devices.

Agent Continuity runs one small web app on each machine. Project data is plain files in a local Git repository; the project source stays where it is. You can use Claude Code, Codex, or both, and move work between Windows and Linux/macOS without copying chat context by hand.

## What it does

- **Projects**: register a real local directory; the project ID is separate from the device path.
- **Agent definitions**: import or write a definition, save revisions, diff, activate, apply to a managed block in `CLAUDE.md`/`AGENTS.md`, and restore the previous content.
- **Memory**: add/search/edit facts with revisions, source and status; revoke and explicitly restore; history is kept. Restoring a definition does not revive revoked memory.
- **Handoffs**: record goal, done, next step, evidence, base commit, uncommitted files and a memory snapshot; start a real Claude Code or Codex task from the web UI and see the session and result.
- **Devices and sync**: each device keeps a local data repository; use ordinary Git to push/pull. Conflicts show both sides and require a choice. The app never syncs an active SQLite/WAL database, an entire agent home, or credentials.
- **Evidence**: saved, delivered, read and used are separate layers. A read receipt records the session, revision and time; a missing marker is shown as unknown, not success.
- **Exit**: export readable JSON, back up the data repository, remove only the managed host blocks.

## Quick start

Requirements: Python 3.11+, Git. Claude Code and/or Codex CLI are optional for storage but required for real continuation.

### Windows

Double-click `scripts\start_windows.bat` (or run it from PowerShell). The first run creates `.venv` and installs Flask, then opens the browser.

### macOS

Double-click `scripts/start_macos.command`. If macOS blocks it, run:

```bash
chmod +x scripts/start_macos.command
./scripts/start_macos.command
```

### Linux

```bash
chmod +x scripts/start_linux.sh
./scripts/start_linux.sh
```

The app binds to `127.0.0.1` and prints the real URL. The default data home is `~/.agent-continuity`; set `AGENT_CONTINUITY_HOME` to change it.

A synthetic project is included in `examples/demo-project`; register it to try the flow without touching your own work.

## Five-step first use

1. Open the URL shown in the terminal.
2. Click **新建项目** (New project), enter a name and an existing local project directory.
3. Open **Agent 版本**, write a definition and save it; click **应用到项目** to write a managed block to `CLAUDE.md` or `AGENTS.md`.
4. Open **记忆**, add the facts the next agent needs. Current, historical and revoked entries are separate.
5. Open **接续**, fill goal / done / next / evidence, create the handoff, then click **Claude Code** or **Codex** to start a real task. The page shows the session ID, markers and output.

## Claude Code and Codex

- Claude Code: the app runs `claude -p ... --output-format json`. If you use a local gateway such as MagPie, set the base URL, token and model in **设置**; otherwise the CLI uses its existing login.
- Codex: the app runs `codex exec ... --json`. Set `CODEX_HOME` in **设置** if it is not already in the environment. An optional **model** is passed as `codex -m`, and an optional **Windows sandbox** overrides `windows.sandbox` (some Codex builds deny the project directory under the default token). The prompt is written to the CLI's stdin, because the npm `codex.cmd` shim can drop a long multi-line argument.
- A task is asked to read the handoff file and print `AC_READ <handoff-id> <revision>` after reading and `AC_DONE <handoff-id>` after completing. The app also checks the real CLI transcript for a Read/Bash tool access to the handoff snapshot; that becomes a read receipt with the session and time. `AC_DONE` is recorded as **done reported**, not as independently verified success; inspect the output and the project diff. If neither evidence is present, the UI shows unknown rather than success.

Claude continuations run inside a **per-project workspace** under a configurable root (default `~/.agent-continuity/sessions`, or `%USERPROFILE%\.agent-continuity\sessions` on Windows; change it in **Settings → workspace root**). Each project gets one fixed subdirectory named after its project id, and the same project always reuses it. That directory is an ordinary Claude Code project, so opening it in Claude Desktop lists every continuation session for that project and you can resume the conversation there.

The Claude CLI is started with `--add-dir <project>` so it can read and edit the source project by absolute path; the handoff also reproduces the applied agent definition and the project's current memory, because an added directory does not guarantee the project's own `.claude/` settings or hooks are loaded. Codex continuations still run inside the project directory.

The app refuses to start a continuation when the workspace equals the project or the two contain each other.

When a run ends, files the session created or changed in the workspace are **copied** (not moved) into the project, compared by SHA-256. A file this app already copied back can be updated by a later resumed session while the project copy still matches what the app wrote; if you edit that file in the project yourself, the next return reports it as a conflict instead of replacing your work. Any other existing file with different content is never overwritten either: it is listed as a conflict and stays in the workspace. Symlinks, directory clashes, path escapes and copy failures are reported in the job result instead of being dropped. After resuming a session in Claude Desktop, use **检查并回传 / Check and return** on the handoff or in Settings to copy new workspace files again; the action is repeatable and leaves conflicts alone. Only one continuation or return operation may run per project at a time.

## Data layout

```
~/.agent-continuity/
  device.json
  registry.json
  settings.json
  data/<project-id>/          # a Git repository per project
    project.json
    definitions/<agent>/...
    memories/*.json
    handoffs/*.json
    receipts/<device-id>.jsonl
    jobs/*.json
    backups/...
```

The project directory is only touched by the managed block in `CLAUDE.md`/`AGENTS.md`; the rest of your files are not copied or rewritten.

## Sync

Set a private Git remote in **设备与同步**, then use **提交本地 / 推送 / 拉取**. The app commits data changes as they happen. Pull conflicts are shown with both versions and a local/remote choice. If the source machine is offline, the target machine still reads its local copy.

## Credentials

A Claude gateway token entered in **Settings** is stored locally in `~/.agent-continuity/settings.json`. It is not included in JSON export or project backups, and it is never written into the project or the data Git repository. Use an existing CLI login instead if you do not want a token stored.

## Real limits

- macOS start script and behavior are provided but not verified on a physical Mac in this build; Windows and WSL2/Linux are the tested paths.
- A real handoff needs a working Claude Code or Codex CLI. Without one, storage and review still work, but continuation cannot run.
- The Claude Code leg may use a local gateway (for example MagPie). The gateway's backing model is whatever that gateway routes to; the app records the session but does not claim a specific model.
- This is not real-time collaborative editing. Do not sync an active SQLite/WAL database or credentials.
- The web UI is a single local user interface; it is not a multi-tenant server.
- Definition load uses the host's standard instructions file (`CLAUDE.md` / `AGENTS.md`). The app records the apply and starts a new CLI session in the project; it does not read a private host-internal load log, so a definition read is shown as applied rather than as a host-confirmed read.

## Development

```bash
python -m pytest -q
```

Run directly:

```bash
python app.py --port 8765 --open
```

## Dependencies and license

The app uses Flask (BSD-3-Clause) as its only runtime dependency. Agent Continuity itself is MIT; see `LICENSE`. No upstream source code was copied.
