---
status: partial
phase: 07-herald-python-port
source: [07-VERIFICATION.md]
started: 2026-04-11T16:35:00Z
updated: 2026-04-11T16:35:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. End-to-end TTS pipeline smoke test
expected: With HeyVox running, execute `echo '<tts>Hello from Python Herald</tts>' | python -m heyvox.herald.worker`. Kokoro daemon generates a WAV, orchestrator plays it via afplay, volume ducks to 3% and restores.
result: [pending]

## Summary

total: 1
passed: 0
issues: 0
pending: 1
skipped: 0
blocked: 0

## Gaps
