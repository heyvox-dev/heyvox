# Security Policy

HeyVox is a local-first macOS voice layer. It processes audio, runs speech-to-text
and text-to-speech, and controls media entirely on your Mac. This document
explains how to report a vulnerability and what is (and isn't) in scope.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security problem.**

Report privately via either:

1. **GitHub private vulnerability reporting** — the "Report a vulnerability"
   button under the repository's **Security** tab (Security Advisories). This is
   preferred.
2. **Email** — <hello@heyvox.dev>. If you want to encrypt, say so in a first
   message and we'll arrange a key.

Please include: affected version (`heyvox --version`), macOS version and chip
(Apple Silicon / Intel), a description of the issue, and the smallest steps that
reproduce it. A proof-of-concept helps a lot.

We'll acknowledge your report as soon as we reasonably can and keep you updated
on the fix. This is a small open-source project maintained on a best-effort
basis — we don't offer a paid bounty, but we're happy to credit you in the
release notes and advisory unless you prefer to stay anonymous.

## Supported versions

Security fixes land on `main` and ship in the next release. Only the latest
released version is supported; please upgrade before reporting.

| Version | Supported |
| ------- | --------- |
| latest (`main` / most recent release) | ✅ |
| older releases | ❌ |

## Scope and threat model

HeyVox's design assumes a **single-user Mac** — the machine's user is trusted.
The surfaces we care about, roughly in priority order:

1. **A malicious webpage** talking to a localhost port HeyVox opens.
2. **Attacker-influenced spoken/dictated input** (anyone within microphone range)
   reaching a shell, `AppleScript`, or other interpreter.
3. **A different local OS user, or another local process**, reaching HeyVox's IPC
   (Unix sockets, any TCP/HTTP listener) on a shared machine.
4. **A malicious or compromised dependency or model download.**

Design choices that follow from this:

- Local IPC uses per-user `$TMPDIR` Unix sockets (mode `0600`), not open TCP.
- The optional MCP HTTP server binds loopback only; it has **no per-user auth**,
  which is an accepted trade-off for the single-user target — on a shared Mac,
  prefer the default `stdio` transport (no open port) or firewall the port.
- Dictated/spoken text is never interpolated into shell or AppleScript source
  (it is pasted via the clipboard / typed via accessibility APIs).
- Model downloads are pinned to specific revisions.

### Generally out of scope

- Attacks requiring an already-compromised same-user account (a process running
  as you can already use macOS Accessibility APIs directly).
- The optional, off-by-default network features working as documented: anonymous
  telemetry and `learn-vocab` (see [docs/privacy.html](docs/privacy.html)). Report
  a *deviation* from their documented behavior, not their existence.
- Denial of service that only affects the attacker's own session.

If you're unsure whether something is in scope, report it anyway — we'd rather
hear about it.
