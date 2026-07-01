"""heyvox.telemetry — opt-in anonymous telemetry.

Three modules:

* :mod:`heyvox.telemetry.consent` — enabled/disabled state, anonymous ID.
* :mod:`heyvox.telemetry.events`  — counter scraping, event construction.
* :mod:`heyvox.telemetry.sender`  — batched HTTPS POST with disk-backed
  retry queue. Tolerates server outage; keeps queued events on disk.

The user controls everything via:
* Setup wizard explicit consent step (opt-in only).
* HUD menu → Settings → Telemetry submenu.
* ``heyvox telemetry status|enable|disable|preview`` CLI.

See ``docs/telemetry.md`` for the field list and rationale.
"""
