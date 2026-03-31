# HeyVox Chrome Companion

Connects browser media (YouTube, Spotify Web, etc.) to HeyVox for voice-controlled pause/play.

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

## How it works

```
Chrome tab (content.js)  →  background.js  →  WebSocket  →  bridge.py  →  HeyVox
          ← pause/play ←                  ←             ←
```

- **Content script**: detects `<video>`/`<audio>`, reports play/pause state, executes commands
- **Background worker**: relays state + commands over WebSocket
- **Bridge server**: Python async WebSocket server, tracks per-tab state

## Permissions

- `activeTab` only — no browsing history, no cookies, no network access beyond localhost WebSocket
