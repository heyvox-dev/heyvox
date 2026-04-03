/**
 * HeyVox Chrome Companion — Background Service Worker
 *
 * Maintains a WebSocket connection to the local HeyVox bridge server
 * (ws://127.0.0.1:9285) and relays tab media state + commands.
 *
 * Requirement: CHROME-01
 */

const WS_URL = "ws://127.0.0.1:9285";
const RECONNECT_DELAY_MS = 3000;

/** @type {WebSocket | null} */
let ws = null;

/** @type {Map<number, {state: string, url: string, title: string}>} */
const tabStates = new Map();

// ---------------------------------------------------------------------------
// WebSocket connection
// ---------------------------------------------------------------------------

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log("[HeyVox] Connected to bridge");
    // Send current tab states on reconnect
    for (const [tabId, info] of tabStates) {
      ws.send(JSON.stringify({ type: "tab_state", tabId, ...info }));
    }
  };

  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    handleBridgeCommand(msg);
  };

  ws.onclose = () => {
    ws = null;
    setTimeout(connect, RECONNECT_DELAY_MS);
  };

  ws.onerror = () => {
    // onclose will fire after this, triggering reconnect
  };
}

// ---------------------------------------------------------------------------
// Bridge command handling
// ---------------------------------------------------------------------------

function handleBridgeCommand(msg) {
  const { type, tabId } = msg;

  if (type === "pause" || type === "play") {
    if (tabId != null) {
      // Target specific tab
      chrome.tabs.sendMessage(tabId, { type }, () => {
        if (chrome.runtime.lastError) {
          // Tab may not have content script
        }
      });
    } else {
      // Broadcast to all tabs with media
      for (const [tid, info] of tabStates) {
        if (type === "pause" && info.state === "playing") {
          chrome.tabs.sendMessage(tid, { type }, () => {
            if (chrome.runtime.lastError) {}
          });
        } else if (type === "play" && info.state === "paused") {
          chrome.tabs.sendMessage(tid, { type }, () => {
            if (chrome.runtime.lastError) {}
          });
        }
      }
    }
  } else if (type === "query") {
    // Respond with all known tab states
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "tab_states",
        tabs: Object.fromEntries(tabStates),
      }));
    }
  }
}

// ---------------------------------------------------------------------------
// Content script messages
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.type === "tab_state" && sender.tab) {
    const tabId = sender.tab.id;
    if (msg.state === "none") {
      tabStates.delete(tabId);
    } else {
      tabStates.set(tabId, {
        state: msg.state,
        url: msg.url,
        title: msg.title,
      });
    }

    // Forward to bridge
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "tab_state", tabId, ...msg }));
    }
  }
});

// Clean up when tabs close
chrome.tabs.onRemoved.addListener((tabId) => {
  tabStates.delete(tabId);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "tab_closed", tabId }));
  }
});

// Start connection
connect();
