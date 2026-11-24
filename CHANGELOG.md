# Changelog

## 0.1.2 — 2026-11-23

Follow-up after real Windows Codex testing.

- Codex prompts are delivered on stdin (`codex exec ... -`): the npm `codex.cmd` shim silently dropped a long multi-line argument, so Codex started without the handoff task.
- Optional **Codex model** setting, passed as `codex -m`; the model can stay empty to use Codex's own configuration.
- Optional **Windows sandbox** setting (`windows.sandbox`) for Codex builds that deny the project directory under the default token; a run that hits `Access to the path ... is denied` now gets an explicit warning instead of looking like an ordinary missing task.
- Return state per project: a resumed session can update a file the app already copied back while the project copy still matches what the app wrote. If the project file was edited outside the app, the return still reports a conflict and keeps both sides.
- Public-release preparation: the default workspace root is now `~/.agent-continuity/sessions` on every platform (no author-specific drive path); `test_verify.py` checks the running app's health and version; docs contain no local absolute paths.
- Tests: 30 backend tests plus the PM regression scripts.

## 0.1.1 — 2026-11-21

Revision round after the project-owner review of v0.1.1.

- Per-project workspaces: `<workspace root>/<project id>`, persistent per project; the same project always resumes in the same Claude Desktop project directory.
- Claude starts with `--add-dir <project>` so it can read and edit the source project; the handoff reproduces the applied definition and current memory because added directories do not auto-load the project's `.claude/` settings.
- Startup is refused when the workspace equals the project or either contains the other.
- Workspace files are copied back (never moved) so Claude Desktop sessions keep their files; change detection uses SHA-256 instead of size/second mtime.
- Conflicts, symlinks, directory clashes, path escapes and copy failures are reported in the job result; a conflict never overwrites project content.
- Explicit "check and return" action per project for files created after resuming in Desktop; repeatable, conflict-safe.
- One continuation/return operation per project at a time; a second attempt is refused with a clear message.
- Job and handoff share one terminal state (`completed` / `partial` / `failed` / `in_progress`); exit 0 without `AC_DONE` is not complete, and return errors or conflicts downgrade a run to `partial`.
- UI shows workspace path, copied files, conflicts, return errors and the difference between a printed `AC_READ` marker and a real tool read.
- Default workspace root falls back to `~/.agent-continuity/sessions` when no E: drive exists; unsafe configuration errors before the run.
- Tests: 28 backend tests plus the PM regression scripts.

## 0.1.1 — 2026-11-20

- Claude continuations now run in a fixed session workspace (default `~/.agent-continuity/sessions`, configurable in Settings) instead of inside the project, so each session shows up as a normal Claude Code project in Claude Desktop and can be continued there.
- The handoff Markdown copy lives in the workspace (`.agent-continuity/handoffs/<id>.md`), keeping the source project's `git status` clean.
- Files a session creates in the workspace are moved back into the original project after the run; same-named files with different content are never overwritten and are reported instead.
- Job result and handoff detail show the workspace path, the "open folder" button and the list of files returned to the project.
- Codex continuations are unchanged and still run inside the project.
- Tests: 14 backend tests (adds workspace snapshot, artifact return, and no-overwrite cases).

## 0.1.0 — 2026-11-19

- Local web app for projects, versioned agent definitions, memory, handoffs and Git-based device sync.
- Real Claude Code and Codex continuation from the browser, with session IDs and `AC_READ` / `AC_DONE` evidence.
- Managed host blocks with apply/restore; project files outside the block are preserved.
- Memory revisions, revoke and explicit restore; definition rollback does not revive revoked memory.
- Per-device receipt files; sync conflicts preserve both sides and require a choice.
- Windows/macOS/Linux start scripts, JSON export, ZIP backup, uninstall of host integration.
- Tests: 12 backend tests covering storage, versions, memory, handoff, sync/conflict, backup/restore, persisted jobs, APIs.
