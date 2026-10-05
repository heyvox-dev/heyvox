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
                        raised first; then the runtime RPC
                        `session.tabs.activate` selects the chat tab of the
                        session (tab id `agent-session:<ORCA_AGENT_SESSION_ID>`).
                        Orca ignores that call for a chat tab in a worktree
                        that is not active yet, hence the order. The CLI has
                        no chat-tab command, so the RPC goes through Orca's
                        own runtime client (`_rpc`).

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


def _rpc(method: str, params: dict, timeout: float = _CLI_TIMEOUT) -> Optional[dict]:
    """Call an Orca runtime RPC method via Orca's bundled runtime client.

    The CLI exposes no chat-tab command; Orca's own CLI runs on the same
    client (Electron in node mode), so this costs about the same as a CLI
    call (~150 ms). Returns the response `result`, None on any failure.
    """
    cli = _cli()
    if not cli:
        return None
    contents = os.path.realpath(cli).split("/Contents/")[0] + "/Contents"
    electron = os.path.join(contents, "MacOS", "Orca")
    client = os.path.join(
        contents, "Resources", "app.asar.unpacked", "out", "cli", "runtime-client.js",
    )
    if not (os.path.exists(electron) and os.path.exists(client)):
        return None
    script = (
        "const {RuntimeClient}=require(" + json.dumps(client) + ");"
        "new RuntimeClient().call(" + json.dumps(method) + "," + json.dumps(params) + ")"
        ".then(r=>{process.stdout.write(JSON.stringify(r));process.exit(0)},"
        "e=>{process.stderr.write(String(e&&e.message||e));process.exit(1)});"
    )
    try:
        r = subprocess.run(
            [electron, "-e", script], capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"},
        )
        data = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.debug("orca rpc %s failed: %s", method, e)
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
            session_id=os.environ.get("ORCA_AGENT_SESSION_ID", ""),
        )

    def activate(self, identity, profile, *, pid: Optional[int] = None) -> bool:
        try:
            if not self._activate(identity.workspace_id):
                return False
            if identity.session_id:
                self._activate_tab(identity.workspace_id, identity.session_id)
            return True
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

    def _activate_tab(self, worktree_id: str, session_id: str) -> bool:
        """Select the chat tab of `session_id` in the (already active) worktree.

        Best effort: the worktree is up either way, so a failure here only
        means Orca keeps showing whichever tab it last showed.
        """
        result = _rpc("session.tabs.activate", {
            "worktree": f"id:{worktree_id}",
            "tabId": f"agent-session:{session_id}",
        })
        if result is None:
            log.debug("orca: activating tab of session %r failed", session_id)
            return False
        return True
