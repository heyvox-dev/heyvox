"""Orca workspace provider (heyvox.adapters.base.WorkspaceProvider).

Orca (com.stablyai.orca) is driven entirely through its own CLI — no AX
walking and no sidecar database:

  * what is visible   — `orca worktree ps --json`, row with `isActive`
  * which workspace   — a row's `worktreeId` (stable, survives renames)
  * sidebar name      — `displayName`; a main checkout in "automatic" mode
                        shows under the repo's name instead (its displayName
                        is only the branch)
  * bringing it up    — `orca terminal create --worktree id:<id> --focus`
                        and closing that probe terminal again. `orca terminal
                        switch` is NOT used: it can only select an existing
                        shell terminal tab, which would hide the chat tab the
                        dictation composer lives in. Chat sessions are not
                        terminals and have no CLI handle, so the workspace is
                        raised and Orca shows whichever tab it last showed
                        there.

Every CLI call costs ~130 ms. detect_context() therefore pays for one call and
resolve() none (the context already IS the worktree id), which keeps the
100 ms resolve budget in input.target.capture_lock.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Optional

from heyvox.adapters.base import WorkspaceIdentity, WorkspaceInfo

log = logging.getLogger(__name__)

_DEFAULT_CLI = "/Applications/Orca.app/Contents/Resources/bin/orca"
_CLI_TIMEOUT = 3.0
_PROBE_TITLE = "heyvox-focus"
_VERIFY_BUDGET_SECS = 2.0


def _cli() -> Optional[str]:
    """Path of the Orca CLI, or None when it is not installed."""
    env = os.environ.get("ORCA_CLI_COMMAND", "")
    if env and os.path.exists(env):
        return env
    if os.path.exists(_DEFAULT_CLI):
        return _DEFAULT_CLI
    return shutil.which("orca")


def _run(args: list, cwd: Optional[str] = None, timeout: float = _CLI_TIMEOUT) -> Optional[dict]:
    """Run `orca <args> --json`; return the `result` dict, None on any failure."""
    cli = _cli()
    if not cli:
        return None
    try:
        r = subprocess.run(
            [cli, *args, "--json"], capture_output=True, text=True,
            timeout=timeout, cwd=cwd or None,
        )
        data = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.debug("orca %s failed: %s", args, e)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    result = data.get("result")
    return result if isinstance(result, dict) else None


def _rows() -> list:
    """Live, non-archived worktree rows from `orca worktree ps`."""
    result = _run(["worktree", "ps"])
    rows = (result or {}).get("worktrees") or []
    return [r for r in rows if isinstance(r, dict) and not r.get("isArchived")]


def _row_by_id(rows: list, worktree_id: str) -> Optional[dict]:
    for row in rows:
        if row.get("worktreeId") == worktree_id:
            return row
    return None


def _in_path(cwd: str, path: str) -> bool:
    path = path.rstrip("/")
    return bool(path) and (cwd == path or cwd.startswith(path + "/"))


def _row_by_cwd(rows: list, cwd: str) -> Optional[dict]:
    """Row whose path contains cwd; the longest path wins (nested worktrees)."""
    cwd = cwd.rstrip("/")
    best = None
    for row in rows:
        path = row.get("path") or ""
        if _in_path(cwd, path) and (best is None or len(path) > len(best.get("path", ""))):
            best = row
    return best


def _sidebar_name(row: dict) -> str:
    """The name Orca's sidebar shows for a row.

    A main checkout in "automatic" display mode is listed under the repo's
    name (its displayName is just the branch); everything else shows
    displayName. `ps` rows do not carry the mode, so the main checkout asks
    `worktree show` once.
    """
    display = row.get("displayName") or ""
    if row.get("isMainWorktree"):
        shown = _run(["worktree", "show", "--worktree", f"id:{row.get('worktreeId')}"])
        mode = ((shown or {}).get("worktree") or {}).get("displayNameMode")
        if mode in (None, "automatic") and row.get("repo"):
            return row["repo"]
    return display or row.get("repo") or ""


class OrcaWorkspaceProvider:
    """WorkspaceProvider for Orca (see heyvox.adapters.base)."""

    name = "orca"

    def detect_context(self, pid: int) -> str:
        for row in _rows():
            if row.get("isActive"):
                return row.get("worktreeId") or ""
        return ""

    def resolve(self, context, profile):
        # The context is already the stable worktree id (see module docstring).
        return WorkspaceIdentity(workspace_id=context) if context else None

    def resolve_by_name(self, name, profile):
        if not name:
            return None
        want = name.strip().casefold()
        rows = _rows()
        for row in rows:
            if (row.get("displayName") or "").casefold() == want:
                return WorkspaceIdentity(workspace_id=row["worktreeId"])
        for row in rows:  # main checkouts are listed under the repo name
            if row.get("isMainWorktree") and (row.get("repo") or "").casefold() == want:
                return WorkspaceIdentity(workspace_id=row["worktreeId"])
        return None

    def resolve_by_cwd(self, cwd, profile):
        if not cwd:
            return None
        row = _row_by_cwd(_rows(), cwd)
        return WorkspaceIdentity(workspace_id=row["worktreeId"]) if row else None

    def describe_cwd(self, cwd: str, profile=None) -> Optional[WorkspaceInfo]:
        """Name + id of the worktree containing `cwd` (None outside Orca).

        Gated on ORCA_AGENT_SESSION_ID so a Conductor session never touches
        the Orca CLI (which could otherwise try to start the app).
        """
        if not cwd or not os.environ.get("ORCA_AGENT_SESSION_ID"):
            return None
        row = _row_by_cwd(_rows(), cwd)
        if row is None:
            return None
        name = _sidebar_name(row)
        if not name:
            return None
        return WorkspaceInfo(
            provider=self.name, name=name, workspace_id=row["worktreeId"],
            project=row.get("repo") or "",
        )

    def activate(self, identity, profile, *, pid: Optional[int] = None) -> bool:
        try:
            return self._activate(identity.workspace_id)
        except Exception as e:  # never raises (protocol contract)
            log.debug("orca activate raised: %r", e)
            return False

    def _activate(self, worktree_id: str) -> bool:
        row = _row_by_id(_rows(), worktree_id)
        if row is None:
            return False
        if row.get("isActive"):
            return True
        created = _run([
            "terminal", "create", "--worktree", f"id:{worktree_id}",
            "--title", _PROBE_TITLE, "--focus",
        ])
        handle = ((created or {}).get("terminal") or {}).get("handle")
        if not handle:
            return False
        ok = False
        try:
            deadline = time.monotonic() + _VERIFY_BUDGET_SECS
            while time.monotonic() < deadline:
                row = _row_by_id(_rows(), worktree_id)
                if row is not None and row.get("isActive"):
                    ok = True
                    break
                time.sleep(0.1)
        finally:
            _run(["terminal", "close", "--terminal", handle])
        return ok
