"""Tests for heyvox.herald.start_orchestrator()'s profile-selection logic.

The predicate that picks a workspace-switching profile out of
cfg.app_profiles changed from `profile.workspace_switch_cmd` (deleted) to
`profile.workspace_provider` when the Hammerspoon-based switch mechanism was
replaced by WorkspaceProvider.activate() — a behavioral change, not just a
rename: the old predicate required workspace_switch_cmd truthy just to
select a profile at all, so leaving it unported would have silently disabled
workspace switching entirely once that field was gone.
"""

from unittest.mock import MagicMock, patch

from heyvox.config import AppProfileConfig, HeyvoxConfig


def _cfg_with_profiles(*profiles) -> HeyvoxConfig:
    return HeyvoxConfig(app_profiles=list(profiles))


def test_start_orchestrator_threads_matching_profile_fields():
    from heyvox.herald import start_orchestrator

    profile = AppProfileConfig(
        name="Conductor",
        has_workspace_detection=True,
        workspace_provider="conductor",
        workspace_db="/tmp/fake.db",
    )
    cfg = _cfg_with_profiles(profile)

    with patch("heyvox.config.load_config", return_value=cfg), \
         patch("heyvox.herald.orchestrator.HeraldOrchestrator") as MockOrch, \
         patch("heyvox.herald.orchestrator.OrchestratorConfig") as MockOrchCfg:
        MockOrch.return_value = MagicMock()
        start_orchestrator()

    _args, kwargs = MockOrchCfg.call_args
    assert kwargs["workspace_provider"] == "conductor"
    assert kwargs["workspace_app_name"] == "Conductor"
    assert kwargs["workspace_db"] == "/tmp/fake.db"
    MockOrch.return_value.run.assert_called_once()


def test_start_orchestrator_no_matching_profile_leaves_fields_empty():
    """Both built-in profile names are overridden with non-matching versions
    here — otherwise HeyvoxConfig's merge_default_profiles validator would
    silently append the built-in Conductor profile (which DOES have
    workspace detection) and satisfy the loop despite the test's intent."""
    from heyvox.herald import start_orchestrator

    profiles = [
        AppProfileConfig(name="Conductor"),  # override: no workspace detection
        AppProfileConfig(name="Cursor"),
    ]
    cfg = _cfg_with_profiles(*profiles)

    with patch("heyvox.config.load_config", return_value=cfg), \
         patch("heyvox.herald.orchestrator.HeraldOrchestrator") as MockOrch, \
         patch("heyvox.herald.orchestrator.OrchestratorConfig") as MockOrchCfg:
        MockOrch.return_value = MagicMock()
        start_orchestrator()

    _args, kwargs = MockOrchCfg.call_args
    assert kwargs["workspace_provider"] == ""
    assert kwargs["workspace_app_name"] == ""
    assert kwargs["workspace_db"] == ""


def test_start_orchestrator_survives_load_config_failure():
    """load_config() raising must not prevent the orchestrator from starting
    — it just runs with the workspace-switching fields left at their
    defaults (matches the bare `except Exception: pass`)."""
    from heyvox.herald import start_orchestrator

    with patch("heyvox.config.load_config", side_effect=RuntimeError("boom")), \
         patch("heyvox.herald.orchestrator.HeraldOrchestrator") as MockOrch, \
         patch("heyvox.herald.orchestrator.OrchestratorConfig") as MockOrchCfg:
        MockOrch.return_value = MagicMock()
        start_orchestrator()

    _args, kwargs = MockOrchCfg.call_args
    assert kwargs["workspace_provider"] == ""
    MockOrch.return_value.run.assert_called_once()
