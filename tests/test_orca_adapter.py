"""Tests for heyvox.adapters.orca — the Orca CLI-backed WorkspaceProvider.

The CLI is never invoked: `heyvox.adapters.orca._run` is patched with a fake
that answers `worktree ps` / `worktree show` / `terminal create|close` from an
in-memory state, so the logic (not Orca) is under test.
"""

from __future__ import annotations

import pytest

from heyvox.adapters import get_workspace_provider
from heyvox.adapters import orca as orca_mod
from heyvox.adapters.base import WorkspaceIdentity
from heyvox.adapters.orca import OrcaWorkspaceProvider
from heyvox.herald.workspace_label import get_workspace_label, identify_workspace
from tests.test_workspace_label import _cfg

MAIN = "r1::/Users/x/Source/vox"
FEATURE = "r1::/Users/x/orca/workspaces/vox/orca-feature"
OTHER = "r2::/Users/x/Source/other"


def _row(wid, path, display, repo, *, main=False, active=False, archived=False):
    return {
        "worktreeId": wid, "path": path, "displayName": display, "repo": repo,
        "isMainWorktree": main, "isActive": active, "isArchived": archived,
    }


class FakeOrca:
    """Stand-in for orca_mod._run."""

    def __init__(self, rows, modes=None, *, raise_on_create=False):
        self.rows = rows
        self.modes = modes or {}
        self.calls: list[list] = []
        self.created: list[str] = []
        self.closed: list[str] = []
        self.raise_on_create = raise_on_create

    def __call__(self, args, cwd=None, timeout=3.0):
        self.calls.append(list(args))
        if args[:2] == ["worktree", "ps"]:
            return {"worktrees": [dict(r) for r in self.rows]}
        if args[:2] == ["worktree", "show"]:
            wid = args[3][len("id:"):]
            return {"worktree": {"displayNameMode": self.modes.get(wid, "automatic")}}
        if args[:2] == ["terminal", "create"]:
            if self.raise_on_create:
                return None
            wid = args[args.index("--worktree") + 1][len("id:"):]
            self.created.append(wid)
            for r in self.rows:  # --focus activates the workspace
                r["isActive"] = r["worktreeId"] == wid
            return {"terminal": {"handle": f"term_{len(self.created)}"}}
        if args[:2] == ["terminal", "close"]:
            self.closed.append(args[args.index("--terminal") + 1])
            return {"close": {}}
        raise AssertionError(f"unexpected CLI call {args}")


def _rows():
    return [
        _row(MAIN, "/Users/x/Source/vox", "main", "HeyVox", main=True, active=True),
        _row(FEATURE, "/Users/x/orca/workspaces/vox/orca-feature", "Orca Anbindung", "HeyVox"),
        _row(OTHER, "/Users/x/Source/other", "main", "Other", main=True),
        _row("r1::/old", "/Users/x/orca/workspaces/vox/old", "Old", "HeyVox", archived=True),
    ]


@pytest.fixture
def fake(monkeypatch):
    f = FakeOrca(_rows())
    monkeypatch.setattr(orca_mod, "_run", f)
    monkeypatch.setattr(orca_mod.time, "sleep", lambda s: None)
    return f


def test_registry_returns_orca_provider():
    assert isinstance(get_workspace_provider("orca"), OrcaWorkspaceProvider)


class TestDetectAndResolve:
    def test_detect_context_is_active_worktree_id(self, fake):
        assert OrcaWorkspaceProvider().detect_context(123) == MAIN

    def test_detect_context_empty_when_orca_unreachable(self, monkeypatch):
        monkeypatch.setattr(orca_mod, "_run", lambda *a, **k: None)
        assert OrcaWorkspaceProvider().detect_context(123) == ""

    def test_resolve_makes_no_cli_call(self, fake):
        ident = OrcaWorkspaceProvider().resolve(FEATURE, None)
        assert ident == WorkspaceIdentity(workspace_id=FEATURE)
        assert fake.calls == []

    def test_resolve_empty_context_is_none(self, fake):
        assert OrcaWorkspaceProvider().resolve("", None) is None

    def test_resolve_by_name_display_name_case_insensitive(self, fake):
        ident = OrcaWorkspaceProvider().resolve_by_name("orca anbindung", None)
        assert ident.workspace_id == FEATURE

    def test_resolve_by_name_main_checkout_by_repo_name(self, fake):
        ident = OrcaWorkspaceProvider().resolve_by_name("Other", None)
        assert ident.workspace_id == OTHER

    def test_resolve_by_name_ignores_archived(self, fake):
        assert OrcaWorkspaceProvider().resolve_by_name("Old", None) is None

    def test_resolve_by_cwd_subdirectory(self, fake):
        ident = OrcaWorkspaceProvider().resolve_by_cwd(
            "/Users/x/orca/workspaces/vox/orca-feature/heyvox/adapters", None)
        assert ident.workspace_id == FEATURE

    def test_resolve_by_cwd_path_boundary(self, fake):
        # "/Users/x/Source/vox-v2" must not match "/Users/x/Source/vox"
        assert OrcaWorkspaceProvider().resolve_by_cwd("/Users/x/Source/vox-v2", None) is None

    def test_resolve_by_cwd_longest_path_wins(self, monkeypatch):
        nested = _row("r1::nested", "/Users/x/Source/vox/.claude/wt", "Nested", "HeyVox")
        monkeypatch.setattr(orca_mod, "_run", FakeOrca(_rows() + [nested]))
        ident = OrcaWorkspaceProvider().resolve_by_cwd("/Users/x/Source/vox/.claude/wt/a", None)
        assert ident.workspace_id == "r1::nested"


class TestDescribeCwd:
    def test_requires_orca_session_env(self, fake, monkeypatch):
        monkeypatch.delenv("ORCA_AGENT_SESSION_ID", raising=False)
        assert OrcaWorkspaceProvider().describe_cwd("/Users/x/Source/vox") is None
        assert fake.calls == []  # never touches the CLI outside Orca

    def test_main_checkout_automatic_shows_repo_name(self, fake, monkeypatch):
        monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "s")
        info = OrcaWorkspaceProvider().describe_cwd("/Users/x/Source/vox")
        assert (info.name, info.project, info.workspace_id) == ("HeyVox", "HeyVox", MAIN)
        assert info.provider == "orca"

    def test_main_checkout_with_fixed_name_keeps_display_name(self, monkeypatch):
        monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "s")
        rows = [_row(MAIN, "/Users/x/Source/vox", "Mein Name", "HeyVox", main=True)]
        monkeypatch.setattr(orca_mod, "_run", FakeOrca(rows, {MAIN: "fixed"}))
        assert OrcaWorkspaceProvider().describe_cwd("/Users/x/Source/vox").name == "Mein Name"

    def test_feature_worktree_uses_display_name_and_repo_as_project(self, fake, monkeypatch):
        monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "s")
        info = OrcaWorkspaceProvider().describe_cwd("/Users/x/orca/workspaces/vox/orca-feature")
        assert (info.name, info.project) == ("Orca Anbindung", "HeyVox")
        assert not any(c[:2] == ["worktree", "show"] for c in fake.calls)

    def test_unknown_cwd_is_none(self, fake, monkeypatch):
        monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "s")
        assert OrcaWorkspaceProvider().describe_cwd("/tmp/elsewhere") is None


class TestActivate:
    def test_already_active_short_circuits(self, fake):
        assert OrcaWorkspaceProvider().activate(WorkspaceIdentity(MAIN), None) is True
        assert fake.created == []

    def test_activates_via_probe_terminal_and_closes_it(self, fake):
        assert OrcaWorkspaceProvider().activate(WorkspaceIdentity(FEATURE), None) is True
        assert fake.created == [FEATURE]
        assert fake.closed == ["term_1"]
        create = next(c for c in fake.calls if c[:2] == ["terminal", "create"])
        assert "--focus" in create
        assert not any(c[:2] == ["terminal", "switch"] for c in fake.calls)

    def test_unknown_worktree_returns_false_without_creating(self, fake):
        assert OrcaWorkspaceProvider().activate(WorkspaceIdentity("r9::nope"), None) is False
        assert fake.created == []

    def test_create_failure_returns_false(self, monkeypatch):
        f = FakeOrca(_rows(), raise_on_create=True)
        monkeypatch.setattr(orca_mod, "_run", f)
        assert OrcaWorkspaceProvider().activate(WorkspaceIdentity(FEATURE), None) is False

    def test_unverified_activation_returns_false_and_still_closes_probe(self, fake, monkeypatch):
        # create "succeeds" but the workspace never becomes active
        orig = fake.__call__

        def no_activate(args, cwd=None, timeout=3.0):
            out = orig(args, cwd, timeout)
            if args[:2] == ["terminal", "create"]:
                for r in fake.rows:
                    r["isActive"] = r["worktreeId"] == MAIN
            return out

        monkeypatch.setattr(orca_mod, "_run", no_activate)
        ticks = iter(range(0, 1000))
        monkeypatch.setattr(orca_mod.time, "monotonic", lambda: next(ticks) * 0.5)
        assert OrcaWorkspaceProvider().activate(WorkspaceIdentity(FEATURE), None) is False
        assert fake.closed == ["term_1"]

    def test_never_raises(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(orca_mod, "_run", boom)
        assert OrcaWorkspaceProvider().activate(WorkspaceIdentity(FEATURE), None) is False


class TestHeraldLabel:
    def test_identify_workspace_picks_orca_profile(self, fake, monkeypatch):
        monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "s")
        info = identify_workspace("/Users/x/orca/workspaces/vox/orca-feature", cfg=_cfg())
        assert info is not None and info.provider == "orca"

    def test_identify_workspace_none_outside_orca(self, fake, monkeypatch):
        monkeypatch.delenv("ORCA_AGENT_SESSION_ID", raising=False)
        assert identify_workspace("/Users/x/Source/vox", cfg=_cfg()) is None

    def test_label_prepends_project(self, fake, monkeypatch):
        monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "s")
        monkeypatch.delenv("HEYVOX_WORKSPACE_LABEL", raising=False)
        cfg = _cfg()
        info = identify_workspace("/Users/x/orca/workspaces/vox/orca-feature", cfg=cfg)
        assert get_workspace_label(info.name, cfg=cfg, info=info) == "HeyVox, Orca Anbindung"

    def test_label_main_checkout_not_duplicated(self, fake, monkeypatch):
        monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "s")
        monkeypatch.delenv("HEYVOX_WORKSPACE_LABEL", raising=False)
        cfg = _cfg()
        info = identify_workspace("/Users/x/Source/vox", cfg=cfg)
        assert get_workspace_label(info.name, cfg=cfg, info=info) == "HeyVox"

    def test_label_drops_issue_number_and_honours_project_labels(self, monkeypatch):
        monkeypatch.delenv("HEYVOX_WORKSPACE_LABEL", raising=False)
        from heyvox.adapters.base import WorkspaceInfo
        info = WorkspaceInfo("orca", "Plausibilität #312", "id", "AI Project Assistant")
        cfg = _cfg(project_labels={"AI Project Assistant": "Assistant"})
        assert get_workspace_label(info.name, cfg=cfg, info=info) == "Assistant, Plausibilität"

    def test_config_override_still_wins_over_provider_name(self, monkeypatch):
        monkeypatch.delenv("HEYVOX_WORKSPACE_LABEL", raising=False)
        from heyvox.adapters.base import WorkspaceInfo
        info = WorkspaceInfo("orca", "Orca Anbindung", "id", "HeyVox")
        cfg = _cfg(workspace_labels={"Orca Anbindung": "Orca"})
        assert get_workspace_label(info.name, cfg=cfg, info=info) == "Orca"

    def test_announce_disabled_wins(self, monkeypatch):
        from heyvox.adapters.base import WorkspaceInfo
        info = WorkspaceInfo("orca", "Orca Anbindung", "id", "HeyVox")
        assert get_workspace_label(info.name, cfg=_cfg(announce_workspace=False), info=info) == ""


class TestSessionTab:
    """Jump to a specific chat tab: worktree first, then session.tabs.activate."""

    @pytest.fixture
    def rpc(self, monkeypatch):
        calls = []

        def fake_rpc(method, params, timeout=3.0):
            calls.append((method, params))
            return {"ok": True}

        monkeypatch.setattr(orca_mod, "_rpc", fake_rpc)
        return calls

    def test_describe_cwd_carries_orca_session_id(self, fake, monkeypatch):
        monkeypatch.setenv("ORCA_AGENT_SESSION_ID", "claude_abc")
        info = OrcaWorkspaceProvider().describe_cwd("/Users/x/Source/vox")
        assert info.session_id == "claude_abc"

    def test_activate_selects_tab_after_raising_worktree(self, fake, rpc):
        ident = WorkspaceIdentity(FEATURE, session_id="claude_abc")
        assert OrcaWorkspaceProvider().activate(ident, None) is True
        assert fake.created == [FEATURE]
        assert rpc == [("session.tabs.activate", {
            "worktree": f"id:{FEATURE}", "tabId": "agent-session:claude_abc",
        })]

    def test_activate_selects_tab_in_already_active_worktree(self, fake, rpc):
        ident = WorkspaceIdentity(MAIN, session_id="claude_abc")
        assert OrcaWorkspaceProvider().activate(ident, None) is True
        assert fake.created == []
        assert [m for m, _ in rpc] == ["session.tabs.activate"]

    def test_no_session_no_rpc(self, fake, rpc):
        assert OrcaWorkspaceProvider().activate(WorkspaceIdentity(FEATURE), None) is True
        assert rpc == []

    def test_no_tab_when_worktree_fails(self, fake, rpc):
        ident = WorkspaceIdentity("r9::nope", session_id="claude_abc")
        assert OrcaWorkspaceProvider().activate(ident, None) is False
        assert rpc == []

    def test_tab_failure_keeps_worktree_success(self, fake, monkeypatch):
        monkeypatch.setattr(orca_mod, "_rpc", lambda *a, **k: None)
        ident = WorkspaceIdentity(FEATURE, session_id="claude_abc")
        assert OrcaWorkspaceProvider().activate(ident, None) is True
