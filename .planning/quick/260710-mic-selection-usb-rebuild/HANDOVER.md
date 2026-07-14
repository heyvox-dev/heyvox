# Handover — Rebuild the mic-selection / recovery mechanism for a stable USB device

**Written:** 2026-07-10 · **Status:** EXECUTED 2026-07-14 (DEF-208/209/210/211) · **Owner:** Franz

## Execution summary (2026-07-14)

All three rebuild goals shipped, plus one discovery:

1. **Transport-aware selection (DEF-208)** — `mic.get_device_transport`/`is_usb_transport`
   (CoreAudio transport type, live-verified G535 = `'usb '`); USB cooldown tiers
   `[15, 30, 60]` (cap 60s) vs BT-era `[120, 300, 600, 1800]`;
   `DeviceManager._usb_same_device_retry` re-opens the SAME USB device first in
   `reinit()`/`handle_io_error()` — no cooldown/demotion when it heals.
2. **-9986 storm self-restart (DEF-209)** — `pa_storm_detected` (≥6 failures, ≥2 devices,
   120s window, success resets) wired into all open paths; main loop checks every 5s
   early in the loop; execv restart with a 600s marker cooldown → banner instead of a
   restart loop.
3. **Wedge self-heal (DEF-210)** — `_start_wedge_supervisor` daemon thread; heartbeat
   older than 300s → force-restart (execv, `os._exit(1)` fallback for launchd relaunch).
   Documented limitation: a GIL-holding wedge starves the supervisor too (observed
   incident did not hold the GIL).
4. **Discovery (DEF-211)** — the DEF-104 manual-restart guard tests existed on main
   WITHOUT their implementation (CI red since 07-13); implementation rebuilt to spec
   (`pop_hotplug_restart_request` / `_restart_for_hotplug_candidate` /
   `_is_coreaudio_live_portaudio_miss`).

Open question #1 (transport detection reliability): answered, reliable at selection
time via the live HAL. Open question #2 (in-process Pa reset): sidestepped as planned —
supervised full-process restart. Tests: `tests/test_transport_policy.py` (19) + the two
restored DEF-104 guards. Details: DEFECT-LOG DEF-208..211.

## The core insight (Franz's, confirmed)

HeyVox's mic robustness machinery — device cooldowns, fallback-to-built-in,
BT-HFP (A2DP→HFP) retries, gain boost — was designed for **Bluetooth** headsets,
which have genuinely unstable connections. Franz deliberately moved to a **USB
Lightspeed** device (Logitech G535) precisely because it should be a *stable*
connection. On a stable USB device this machinery is at best dead weight and at
worst **actively harmful**: it thrashes on transient blips, escalates cooldowns,
demotes a fundamentally-healthy device to the worse built-in mic, and — as of
2026-07-10 — that thrashing culminated in a corrupted PortAudio context that only
a **full process restart** could clear.

## What happened 2026-07-09 evening → 2026-07-10 morning (the trigger, from `/tmp/heyvox.log`)

1. **20:56–21:14** — the G535 repeatedly produced zero audio / failed to open.
   Each failure hit the cooldown path and fell back to the built-in mic. The
   cooldown escalated across failures: `728s → 601s → 363s → 125s → 8s` then a
   hard `OSError(-9986, 'Internal PortAudio error')` → `zero audio, cooldown
   1800s (failure #6)`.
2. **07:59–08:00 (next morning)** — `[keepalive] could not open silent stream
   (attempt 1…12, PA context dropped for fresh retry): [Errno -9986] Internal
   PortAudio error`. Every stream open failed with **-9986**, for **every**
   device, **even after the in-process "PA context dropped for fresh retry"**
   (i.e. re-creating the PortAudio context in-process did NOT clear it).
3. **08:01** — `Previous instance died without clean shutdown (heartbeat stale
   by 38702s)` ≈ **10.75 h**. The daemon had effectively been wedged since
   ~21:16 and sat dead all night until Franz manually restarted it.
4. On the fresh instance, the manual menu switch worked instantly: `Mic switch
   requested from menu: G535 → Switched to: [1] G535 Wireless Gaming Headset
   (pinned)`.

## Why the manual switch was impossible without a restart (root cause)

The **PortAudio context was corrupted** — `-9986` (`paInternalError`) on *every*
`Pa_OpenStream`, for *every* device, and the in-process recovery (drop + recreate
the PA context) did **not** heal it. With no openable stream, the manual switch to
the G535 could not bind a device. Only a full process exit + fresh `Pa_Initialize`
at startup clears the corruption. So the restart wasn't about the G535 — it was
the only way to reset a wedged PortAudio library.

Compounding it: the daemon **hung for ~10.75 h** without self-restarting — the
heartbeat went stale but nothing acted on it until the user noticed.

## Mechanism inventory (keep / reconsider / remove for a USB device)

Code lives mainly in `heyvox/audio/mic.py` (selection + cooldown), `heyvox/audio/keepalive.py` (USB keep-alive), `heyvox/device_manager.py`, `heyvox/audio/device_handle.py`.

| Mechanism | Origin | Verdict for USB Lightspeed |
|---|---|---|
| Dead-mic watchdog (all-zero → 30s → reinit), `mic.py` | general | **KEEP** — any mic can die |
| Device cooldown + escalation (`_get_adaptive_cooldown`, `add_device_cooldown`, `mic.py:92/904`) | BT-era ("a failed device will fail again") | **RECONSIDER** — on USB a transient blip shouldn't earn a 1800s cooldown that demotes to built-in; this drove the thrashing |
| Fallback-to-built-in on cooldown (`find_best_mic`, `mic.py:575`) | BT-era | **RECONSIDER** — prefer RE-OPENing the same USB device over demoting |
| BT-HFP retry / A2DP→HFP probes | BT-only | **REMOVE for G535** — it has no BT mode; dead weight (already mostly inert but confirm it never runs for USB) |
| Gain boost (DEF-101) | G435-over-BT-HFP | already transport-scoped OFF for USB — **OK, leave** |
| USB keep-alive (`keepalive.py`, DEF-146-150) | USB | **KEEP but harden** — today it was the thing stuck in the -9986 loop; it needs a give-up-and-escalate path |

## Rebuild goals

1. **Transport-aware selection.** For USB/Lightspeed transport: prefer re-opening
   the SAME device on a transient failure; use a gentle/short cooldown (or none)
   and never escalate to 1800s; never silently demote to the built-in mic without
   surfacing it prominently. Reserve the aggressive cooldown/fallback for actual
   BT transports.
2. **PortAudio-corruption recovery that actually works.** In-process context
   recreation is PROVEN insufficient (today's -9986 storm survived it). When a
   `-9986` storm is detected (N consecutive opens fail across devices), the daemon
   should **self-restart** (clean exit → launchd relaunch) rather than loop
   forever. This is the only recovery observed to work.
3. **Wedge self-heal.** The daemon hung ~10.75 h with a stale heartbeat and no
   action. A supervisor/heartbeat check should force a self-restart when the
   audio subsystem is wedged (stale heartbeat OR sustained -9986), so the user
   never has to manually restart to recover a dead mic.

## Open questions / risks

1. How does the code detect "transport = USB vs BT"? (CoreAudio transport type — used elsewhere per memory; confirm it's available at selection time.) The whole rebuild hinges on this classification being reliable.
2. Is a mid-process `Pa_Terminate` + `Pa_Initialize` ever safe, or is a full process restart the only reliable reset? (Today suggests full restart.) A supervised self-restart sidesteps the question.
3. Did my (Claude's) ~6 daemon restarts on 2026-07-09 CAUSE the initial G535 instability (each restart re-opens the stream, which can trigger the Lightspeed zombie state)? Likely a contributor — so the evening's failure cascade is partly restart-churn-induced, not proof the G535 is inherently flaky. The rebuild should still handle the failure mode, but weight "G535 is unstable" evidence accordingly.
4. `-9986 paInternalError` root cause — is it the G535 specifically, USB power management, or PortAudio library state? Unclear; the recovery (self-restart) matters more than the cause.

## Key log signatures (for the next session to grep)

- Thrash: `skipping (cooldown, Ns remaining, failures=N)` + repeated `Switched to: [4] MacBook Pro Microphone`
- Corruption: `[Errno -9986] Internal PortAudio error`, `[keepalive] could not open silent stream`
- Wedge: `Previous instance died without clean shutdown (heartbeat stale by …s)`

Related memories: `bug_pyaudio_hotplug_cache` (DEF-104/189 stale cache), `bug_zombie_reinit_loop` (DEF-037), `project_g535_audio_robustness` (DEF-146-150), `bug_g535_silent_mic_gain_drop` (DEF-101/146), `reference_bluetooth_audio_headsets`.
