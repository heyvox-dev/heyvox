# HeyVox Chrome Companion (WebSocket Bridge)

> **Note**: This is the WebSocket-based media bridge. For more reliable browser media control,
> see the **Hush** native messaging extension at `heyvox/hush/extension/`.

Connects browser media (YouTube, Spotify Web, etc.) to HeyVox for voice-controlled pause/play via WebSocket.

## Install

1. Open Chrome → `chrome://extensions/`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select this `chrome-extension/` folder
5. Done — the extension auto-connects to the HeyVox bridge

## Start the bridge

```bash
# Option A: via heyvox CLI
heyvox chrome-bridge

# Option B: standalone entry point
heyvox-chrome-bridge
```

The bridge runs on `ws://127.0.0.1:9285` (localhost only). The extension reconnects automatically if the bridge restarts.

## Hush vs Chrome Companion

| | Hush (recommended) | Chrome Companion |
|---|---|---|
| Protocol | Native Messaging (Chrome → Python host) | WebSocket |
| Reliability | High (Chrome manages lifecycle) | Medium (separate process) |
| Setup | `install.sh` + load extension | Load extension + start bridge |
| Path | `heyvox/hush/extension/` | `chrome-extension/` |

## Permissions

- `activeTab` only — no browsing history, no cookies, no network access beyond localhost WebSocket
