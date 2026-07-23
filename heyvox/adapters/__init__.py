"""Agent adapters + workspace provider registry.

The registry maps the `workspace_provider` name an app profile declares to
its implementation. This mapping is the ONE sanctioned place an app name may
appear outside its own adapter module — adding support for another
workspace-managing app means adding an entry here plus a provider class in
its adapter module, never a branch in shared code paths.
"""

from typing import Optional


def get_workspace_provider(name: str) -> Optional[object]:
    """Return the WorkspaceProvider registered under ``name``, or None.

    Lazy imports keep adapter modules (and their app-specific dependencies)
    out of processes that never touch workspace resolution.
    """
    if not name:
        return None
    if name == "conductor":
        from heyvox.adapters.conductor import ConductorWorkspaceProvider
        return ConductorWorkspaceProvider()
    return None
