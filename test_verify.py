#!/usr/bin/env python3
"""Standalone health check for a running Agent Continuity web app.

Usage:
    python test_verify.py [--base-url http://127.0.0.1:8765] [--expect-version 0.1.2]

Exit code 0 only when every check passes. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8765"
DEFAULT_VERSION = "0.1.2"


def fetch(base: str, path: str) -> tuple[int, str]:
    req = urllib.request.Request(base.rstrip("/") + path, headers={"User-Agent": "agent-continuity-verify"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def as_json(text: str) -> dict:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--expect-version", default=DEFAULT_VERSION)
    args = parser.parse_args()

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    status, body = fetch(args.base_url, "/api/health")
    health = as_json(body)
    check("GET /api/health returns 200", status == 200, f"HTTP {status}")
    check("health.ok is true", health.get("ok") is True, str(health.get("ok")))
    check("health.version matches", health.get("version") == args.expect_version,
          f"got {health.get('version')!r}, expected {args.expect_version!r}")

    status, body = fetch(args.base_url, "/api/setup")
    setup = as_json(body)
    check("GET /api/setup returns 200", status == 200, f"HTTP {status}")
    check("setup.version matches", setup.get("version") == args.expect_version,
          f"got {setup.get('version')!r}")
    check("setup exposes settings and workspace", bool(setup.get("settings")) and bool(setup.get("workspace")),
          f"workspace={setup.get('workspace')!r}")

    status, page = fetch(args.base_url, "/")
    check("GET / returns 200", status == 200, f"HTTP {status}")
    check("page loads the SPA", "Agent Continuity" in page and "/static/app.js" in page, "")

    status, app_js = fetch(args.base_url, "/static/app.js")
    check("GET /static/app.js returns 200", status == 200, f"HTTP {status}")
    check("app.js has version binding", "app-version" in app_js, "")
    check("app.js has check-and-return action", "session/sync" in app_js, "")

    status, body = fetch(args.base_url, "/api/projects")
    projects = as_json(body)
    check("GET /api/projects returns 200", status == 200, f"HTTP {status}")
    check("projects response is ok", projects.get("ok") is True, "")

    failed = [name for name, ok, _ in checks if not ok]
    for name, ok, detail in checks:
        suffix = f" ({detail})" if detail else ""
        print(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed against {args.base_url}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
