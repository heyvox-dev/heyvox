# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-10)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** v1.1 Architecture Hardening

## Current Position

Phase: Not started (defining requirements)
Plan: —
Status: Defining requirements
Last activity: 2026-04-10 — Milestone v1.1 started

Progress: [          ] 0%

## Performance Metrics

**Velocity (v1.0):**
- Total plans completed: 10
- Average duration: 3.5 min
- Total execution time: ~0.6 hours

## Accumulated Context

### Decisions

Full decision log in PROJECT.md Key Decisions table.

### Pending Todos

None.

### Blockers/Concerns

- Package name resolved: "heyvox" — repo at heyvox-dev/heyvox, domain heyvox.dev
- main.py is 2000 lines with 17+ globals — top refactoring priority
- Herald orchestrator.sh crosses shell/Python boundary 4 times per TTS request
- 25+ flag files in /tmp/ with race conditions between Python and bash processes

## Session Continuity

Last session: 2026-04-10
Stopped at: v1.1 milestone requirements definition
Resume file: None
