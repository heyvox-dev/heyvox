"""heyvox.reporting — bug-report bundle, redaction, and GitHub issue routing.

Two entry points:

* ``text_report.run_bugreport()`` — short text summary for clipboard / inline paste.
  Replaces the missing ``heyvox.doctor.run_bugreport`` the CLI used to call.

* ``bundle.build_bundle(opts)`` — full zip with logs, redacted config,
  system info, mic diagnostics, and defect counters. Used by the menu's
  "Report Issue…" dialog and ``heyvox bugreport --bundle``.

GitHub Issue URL routing lives in :mod:`heyvox.reporting.issue`.
"""

GITHUB_REPO = "heyvox-dev/heyvox"
