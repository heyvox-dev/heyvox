"""GitHub Issue URL builder + opener.

Constructs a pre-filled "new issue" URL pointing at the bug_report.yml
template in :data:`heyvox.reporting.GITHUB_REPO`. The user submits manually
in their browser, signed in with their own GitHub account.

We intentionally do not auto-create issues via the API — that would require
a GitHub token in HeyVox, an OAuth flow, or shelling out to ``gh``. The
pre-filled URL is the lowest-friction path and keeps full user control.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlencode

from heyvox.reporting import GITHUB_REPO


def build_issue_url(title: str, body: str, bundle_path: Path | None = None) -> str:
    """Build the GitHub Issue URL with the bug_report template pre-filled.

    ``title`` and ``body`` are URL-encoded. ``bundle_path``, if given, gets
    appended to the body as a hint so the user remembers to attach it.
    """
    if bundle_path is not None:
        body = (
            body
            + "\n\n---\n"
            + f"📎 Bundle: `{bundle_path}` — drag this `.zip` into the comment box.\n"
        )

    params = {
        "template": "bug_report.yml",
        "title": title[:200] if title else "[Bug] ",
        "labels": "bug,triage",
    }
    base = f"https://github.com/{GITHUB_REPO}/issues/new"
    qs = urlencode(params)

    # Body uses a separate ``body`` key. urlencode handles % escapes, but GH
    # specifically wants the ``body`` parameter for free text. Length cap is
    # generous; long bodies still work via the "drag the zip" hint.
    body_param = urlencode({"body": body[:8000]})
    return f"{base}?{qs}&{body_param}"


def open_in_browser(url: str) -> bool:
    """Open ``url`` in the system browser via ``open(1)``."""
    try:
        subprocess.run(["open", url], check=False, timeout=5)
        return True
    except Exception:
        return False


def reveal_in_finder(path: Path) -> bool:
    """Reveal ``path`` in Finder (selected) via ``open -R``."""
    try:
        subprocess.run(["open", "-R", str(path)], check=False, timeout=5)
        return True
    except Exception:
        return False
