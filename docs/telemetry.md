# HeyVox Telemetry

HeyVox can send anonymous usage signals so we can spot regressions before
users notice. Telemetry is **off by default** and only sends data once you
explicitly opt in.

## How to turn it on (or off)

Pick whichever is easiest:

- **HUD menu bar**: click the HeyVox icon → Settings → **Telemetry: Off ▸ Enable telemetry**.
- **CLI**: `heyvox telemetry enable` (and `heyvox telemetry disable` to turn off).
- **Setup wizard**: `heyvox setup` includes an explicit consent step.
- **Config file**: set `telemetry.enabled: true` in `~/.config/heyvox/config.yaml`.
- **Env override**: `HEYVOX_TELEMETRY=0` force-disables for one process, regardless of config.

## What's sent

Each upload batch contains one or more of these events:

### `heartbeat`

Sent once per upload cycle. Lets us count active installs.

```json
{
  "type": "heartbeat",
  "ts": 1735689600,
  "system": {
    "heyvox_version": "0.4.2",
    "macos_version": "26.5",
    "mac_model": "MacBookPro18,3",
    "python": "3.12.5",
    "machine_hash": "c22d49b0f5f44427"
  }
}
```

`machine_hash` is the first 16 hex characters of `sha256(hostname)`. It is
stable across restarts on the same machine but cannot be reversed back to
a hostname or username.

### `counter.delta`

Sent when one of the tracked counter tags fires in the main log since the
last successful upload. Numbers only — no log content, no surrounding text.

```json
{
  "type": "counter.delta",
  "ts": 1735689600,
  "tag": "USER_EFFORT",
  "delta": 3,
  "system": { ... same as heartbeat ... }
}
```

Tracked tags:

| Tag             | What it means                                                              |
| --------------- | -------------------------------------------------------------------------- |
| `WAKE_VAD_DROP` | Wake word fired but VAD killed the recording before any speech was captured |
| `NEAR_MISS`     | Wake-word score was just below threshold — hint at training drift          |
| `USER_EFFORT`   | User had to repeat "Hey Vox" before HeyVox listened                        |
| `MIC_ZOMBIE`    | Microphone returned all-zero samples; HeyVox had to recover the stream     |
| `KOKORO_RESTART`| Kokoro TTS daemon had to be restarted                                      |

## What is NOT sent

- Audio samples, microphone recordings, transcripts.
- File paths, directory names, workspace names, project names.
- Config file contents.
- Any text the user spoke or typed.
- API keys, tokens, secrets (HeyVox doesn't have any — there are no API keys in `config.yaml`).
- IP address is unavoidable (any HTTPS connection exposes it to the receiving server) but is not stored alongside event data.

## Your anonymous ID

A random UUID4 is generated the first time you enable telemetry and stored
at `~/.config/heyvox/telemetry/anon-id`.

- It is **only** used to deduplicate events from the same install.
- It is **not** linked to your name, email, IP, or any HeyVox account.
- You can reset it any time:
  - HUD menu → Settings → Telemetry → **Reset anonymous ID**, or
  - `heyvox telemetry reset-id`, or
  - Delete `~/.config/heyvox/telemetry/anon-id`.

## Where the data goes

Events are POSTed to the endpoint in `telemetry.endpoint`
(default: `https://heyvox.dev/telemetry/v1/events`).
You can point it elsewhere — to a self-hosted endpoint, to a mock for
testing, or to nothing at all if you keep telemetry disabled.

## Retry behaviour

If the server is unreachable, events are queued on disk at
`~/.config/heyvox/telemetry/queue/`. The sender retries on the next batch
cycle (default: hourly). A hard cap of 200 queued batch files prevents
disk growth if the server stays down for an extended period.

## Inspecting what would be sent

Run `heyvox telemetry preview` to see the exact JSON payload that the next
cycle would upload (and the anonymous ID at the top).
