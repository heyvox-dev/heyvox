/**
 * background.js — Hush Extension Service Worker
 *
 * Manages native messaging connection to com.hush.bridge and coordinates
 * media pause/resume across browser tabs via content script messaging.
 *
 * Native host message format (incoming):
 *   { action: "pause" }
 *   { action: "resume", rewindSecs?: 3, fadeInMs?: 1000 }
 *   { action: "status" }
 *   { action: "pause-tab", tabId: 123 }
 *   { action: "resume-tab", tabId: 123, rewindSecs?: 3, fadeInMs?: 1000 }
 *
 * Native host message format (outgoing):
 *   { state: "paused"|"playing"|"idle", tabs: [{id, title, url}], pausedCount: N }
 */

const NATIVE_HOST = 'com.hush.bridge';
const RECONNECT_DELAY_MS = 2000;
const RECONNECT_MAX_DELAY_MS = 60000;

/** @type {Map<number, {title: string, url: string, timestamp: number}>} */
const pausedTabs = new Map();

/** @type {chrome.runtime.Port | null} */
let nativePort = null;

/** @type {number} */
let reconnectAttempts = 0;

/** @type {ReturnType<typeof setTimeout> | null} */
let reconnectTimer = null;

// ---------------------------------------------------------------------------
// Native messaging
// ---------------------------------------------------------------------------

/**
 * Establishes (or re-establishes) the connection to the native host.
 */
function connectNativeHost() {
  if (nativePort !== null) return;

  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
    reconnectAttempts = 0;

    nativePort.onMessage.addListener(handleNativeMessage);

    nativePort.onDisconnect.addListener(() => {
      const error = chrome.runtime.lastError;
      console.warn('[Hush] Native port disconnected:', error?.message ?? 'unknown reason');
      nativePort = null;
      scheduleReconnect();
    });

    console.log('[Hush] Connected to native host:', NATIVE_HOST);
  } catch (err) {
    console.error('[Hush] Failed to connect to native host:', err);
    nativePort = null;
    scheduleReconnect();
  }
}

/**
 * Schedules a reconnect attempt with exponential backoff.
 */
function scheduleReconnect() {
  if (reconnectTimer !== null) return;

  // Never give up — MV3 can terminate us mid-backoff; the next lifecycle
  // event (onStartup/onInstalled/tabs.onUpdated) will re-enter module eval
  // and re-run connectNativeHost(). Cap backoff so we don't wait minutes.
  const delay = Math.min(
    RECONNECT_DELAY_MS * Math.pow(1.5, reconnectAttempts),
    RECONNECT_MAX_DELAY_MS
  );
  reconnectAttempts += 1;

  console.log(`[Hush] Reconnecting in ${Math.round(delay)}ms (attempt ${reconnectAttempts})`);

  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectNativeHost();
  }, delay);
}

/**
 * Force a reconnect now: clear any pending backoff and reset counters.
 */
function forceReconnect(reason) {
  console.log(`[Hush] Force reconnect (${reason})`);
  reconnectAttempts = 0;
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  connectNativeHost();
}

/**
 * Sends a message to the native host if the port is open.
 * @param {object} payload
 */
function sendToNative(payload) {
  if (nativePort === null) {
    console.warn('[Hush] Cannot send to native host — not connected');
    return;
  }
  try {
    nativePort.postMessage(payload);
  } catch (err) {
    console.error('[Hush] Error posting message to native host:', err);
  }
}

// ---------------------------------------------------------------------------
// Message routing
// ---------------------------------------------------------------------------

/**
 * Handles a message arriving from the native host.
 * @param {{ action: string, tabId?: number }} message
 */
async function handleNativeMessage(message) {
  console.log('[Hush] Native message received:', message);

  if (!message || typeof message.action !== 'string') {
    console.warn('[Hush] Received malformed native message:', message);
    return;
  }

  // Preserve the request ID added by the native host for response routing
  const requestId = message.id;
  let response;

  try {
    switch (message.action) {
      case 'pause':
        response = await pauseAllTabs();
        break;

      case 'resume':
        response = await resumeAllPausedTabs(message.rewindSecs, message.fadeInMs);
        break;

      case 'status':
        response = buildStatusResponse();
        break;

      case 'pause-tab':
        if (typeof message.tabId !== 'number') {
          response = { error: 'pause-tab requires a numeric tabId' };
        } else {
          response = await pauseSingleTab(message.tabId);
        }
        break;

      case 'resume-tab':
        if (typeof message.tabId !== 'number') {
          response = { error: 'resume-tab requires a numeric tabId' };
        } else {
          response = await resumeSingleTab(message.tabId, message.rewindSecs, message.fadeInMs);
        }
        break;

      case 'type-text': {
        // Insert text into the active tab's focused element
        const text = message.text;
        if (typeof text !== 'string') {
          response = { error: 'type-text requires a string text field' };
        } else {
          response = await typeTextInActiveTab(text);
        }
        break;
      }

      case 'press-enter': {
        // Press Enter in the active tab's focused element
        const count = typeof message.count === 'number' ? message.count : 1;
        response = await pressEnterInActiveTab(count);
        break;
      }

      default:
        response = { error: `Unknown action: ${message.action}` };
        console.warn('[Hush] Unknown action from native host:', message.action);
    }
  } catch (err) {
    console.error('[Hush] Error handling native message:', err);
    response = { error: String(err) };
  }

  // Attach the request ID so the native host can route the response
  if (requestId) {
    response.id = requestId;
  }

  sendToNative(response);
  updateBadge();
}

// ---------------------------------------------------------------------------
// Pause / resume logic
// ---------------------------------------------------------------------------

/**
 * Queries all tabs, pauses any tab with actively playing media, and records
 * them in pausedTabs.
 * @returns {Promise<object>} Status response
 */
async function pauseAllTabs() {
  const tabs = await chrome.tabs.query({});
  const nowPlaying = await findPlayingTabs(tabs);

  const paused = [];
  await Promise.allSettled(
    nowPlaying.map(async (tab) => {
      let method = 'content-script';
      const count = await sendToContentScript(tab.id, { action: 'pause-media' });
      if (count > 0) {
        // Content script successfully paused media elements
      } else if (tab.audible) {
        // Content script couldn't reach media (YouTube Shadow DOM, etc.)
        // Fall back to tab muting — silences audio without affecting playback state
        await chrome.tabs.update(tab.id, { muted: true });
        method = 'tab-mute';
      } else if (count === null) {
        // Couldn't confirm but attempted — treat as paused
      } else {
        // Nothing to pause in this tab
        return;
      }
      pausedTabs.set(tab.id, {
        title: tab.title ?? '',
        url: tab.url ?? '',
        method,
        timestamp: Date.now(),
      });
      paused.push(tab);
    })
  );

  return buildStatusResponse('paused');
}

/**
 * Resumes only the tabs that Hush previously paused.
 * @param {number} [rewindSecs=0] - seconds to rewind before playing
 * @param {number} [fadeInMs=0] - fade-in duration in milliseconds
 * @returns {Promise<object>} Status response
 */
async function resumeAllPausedTabs(rewindSecs = 0, fadeInMs = 0) {
  const entries = [...pausedTabs.entries()];

  await Promise.allSettled(
    entries.map(async ([tabId, info]) => {
      if (info.method === 'tab-mute') {
        // Unmute tabs that were muted as fallback
        await chrome.tabs.update(tabId, { muted: false }).catch(() => {});
      } else {
        await sendToContentScript(tabId, {
          action: 'resume-media',
          rewindSecs,
          fadeInMs,
        });
      }
    })
  );

  pausedTabs.clear();
  return buildStatusResponse('playing');
}

/**
 * Pauses a single tab by ID, recording it in pausedTabs.
 * @param {number} tabId
 * @returns {Promise<object>}
 */
async function pauseSingleTab(tabId) {
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return { error: `Tab ${tabId} not found` };
  }

  await sendToContentScript(tabId, { action: 'pause-media' });

  pausedTabs.set(tabId, {
    title: tab.title ?? '',
    url: tab.url ?? '',
    timestamp: Date.now(),
  });

  return buildStatusResponse();
}

/**
 * Resumes a single tab by ID, but only if Hush previously paused it.
 * @param {number} tabId
 * @param {number} [rewindSecs=0]
 * @param {number} [fadeInMs=0]
 * @returns {Promise<object>}
 */
async function resumeSingleTab(tabId, rewindSecs = 0, fadeInMs = 0) {
  const info = pausedTabs.get(tabId);
  if (!info) {
    return { error: `Tab ${tabId} was not paused by Hush` };
  }

  if (info.method === 'tab-mute') {
    await chrome.tabs.update(tabId, { muted: false }).catch(() => {});
  } else {
    await sendToContentScript(tabId, {
      action: 'resume-media',
      rewindSecs,
      fadeInMs,
    });
  }
  pausedTabs.delete(tabId);

  return buildStatusResponse();
}

// ---------------------------------------------------------------------------
// Text injection helpers
// ---------------------------------------------------------------------------

/**
 * Inserts text into the active tab's focused element via the content script.
 * @param {string} text
 * @returns {Promise<object>}
 */
async function typeTextInActiveTab(text) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) {
    return { error: 'No active tab found', ok: false };
  }
  if (!isScriptableUrl(tab.url)) {
    return { error: 'Active tab is not scriptable (chrome:// or similar)', ok: false };
  }
  const ok = await sendToContentScript(tab.id, { action: 'type-text', text });
  return { ok: !!ok, tabId: tab.id, title: tab.title ?? '' };
}

/**
 * Presses Enter in the active tab's focused element via the content script.
 * @param {number} count
 * @returns {Promise<object>}
 */
async function pressEnterInActiveTab(count) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) {
    return { error: 'No active tab found', ok: false };
  }
  if (!isScriptableUrl(tab.url)) {
    return { error: 'Active tab is not scriptable', ok: false };
  }
  const ok = await sendToContentScript(tab.id, { action: 'press-enter', count });
  return { ok: !!ok, tabId: tab.id };
}

// ---------------------------------------------------------------------------
// Tab querying helpers
// ---------------------------------------------------------------------------

/**
 * Filters a list of tabs to those that currently have playing media.
 * Uses Chrome's native tab.audible property first (reliable for YouTube
 * and other sites where content script DOM access fails), then falls
 * back to content script query for silent video.
 * @param {chrome.tabs.Tab[]} tabs
 * @returns {Promise<chrome.tabs.Tab[]>}
 */
async function findPlayingTabs(tabs) {
  // Primary: Chrome's built-in audible detection — works regardless of
  // Shadow DOM, cross-origin iframes, or content script availability.
  const audible = tabs.filter(
    (tab) => tab.audible && tab.id && isScriptableUrl(tab.url)
  );
  if (audible.length > 0) return audible;

  // Fallback: ask content scripts (catches silent video, e.g. muted autoplay)
  const results = await Promise.allSettled(
    tabs.map(async (tab) => {
      if (!tab.id || !isScriptableUrl(tab.url)) return null;
      const isPlaying = await sendToContentScript(tab.id, { action: 'query-media' });
      return isPlaying ? tab : null;
    })
  );

  return results
    .filter((r) => r.status === 'fulfilled' && r.value !== null)
    .map((r) => r.value);
}

/**
 * Returns true if the URL is one we can inject scripts into.
 * @param {string | undefined} url
 * @returns {boolean}
 */
function isScriptableUrl(url) {
  if (!url) return false;
  return url.startsWith('http://') || url.startsWith('https://');
}

// ---------------------------------------------------------------------------
// Content script messaging
// ---------------------------------------------------------------------------

/**
 * Sends a message to the content script in a tab and returns the response.
 * If the content script isn't loaded (e.g. after extension reload), injects
 * it on the fly and retries once.
 * @param {number} tabId
 * @param {object} message
 * @returns {Promise<any>}
 */
async function sendToContentScript(tabId, message) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, message);
    return response;
  } catch (err) {
    // Content script not ready — try injecting it
    try {
      await chrome.scripting.executeScript({
        target: { tabId, allFrames: true },
        files: ['content.js'],
      });
      // Retry after injection
      const response = await chrome.tabs.sendMessage(tabId, message);
      return response;
    } catch (retryErr) {
      // Tab not injectable (e.g. chrome:// page) — not fatal
      return null;
    }
  }
}

// ---------------------------------------------------------------------------
// Status and badge
// ---------------------------------------------------------------------------

/**
 * Builds a status response payload.
 * @param {'paused'|'playing'|'idle'} [overrideState]
 * @returns {object}
 */
function buildStatusResponse(overrideState) {
  const tabs = [...pausedTabs.entries()].map(([id, info]) => ({
    id,
    title: info.title,
    url: info.url,
  }));

  let state = overrideState;
  if (!state) {
    state = pausedTabs.size > 0 ? 'paused' : 'idle';
  }

  return {
    state,
    tabs,
    pausedCount: pausedTabs.size,
  };
}

/**
 * Updates the extension action badge to reflect the current pause count.
 */
function updateBadge() {
  const count = pausedTabs.size;

  if (count === 0) {
    chrome.action.setBadgeText({ text: '' });
    chrome.action.setBadgeBackgroundColor({ color: '#888888' });
  } else {
    chrome.action.setBadgeText({ text: String(count) });
    chrome.action.setBadgeBackgroundColor({ color: '#E53E3E' });
  }
}

// ---------------------------------------------------------------------------
// Popup / internal message listener
// ---------------------------------------------------------------------------

/**
 * Handles messages from the popup or other extension pages.
 */
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message.action !== 'string') return false;

  switch (message.action) {
    case 'get-status':
      sendResponse(buildStatusResponse());
      return false;

    case 'resume-all':
      resumeAllPausedTabs().then((resp) => {
        updateBadge();
        sendResponse(resp);
      });
      return true; // async response

    default:
      return false;
  }
});

// ---------------------------------------------------------------------------
// Tab cleanup — remove stale entries when a tab is closed or navigated
// ---------------------------------------------------------------------------

chrome.tabs.onRemoved.addListener((tabId) => {
  if (pausedTabs.has(tabId)) {
    pausedTabs.delete(tabId);
    updateBadge();
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  // If a tab navigated away, the media is gone — remove from tracking
  if (changeInfo.status === 'loading' && pausedTabs.has(tabId)) {
    pausedTabs.delete(tabId);
    updateBadge();
  }
});

// ---------------------------------------------------------------------------
// Lifecycle — MV3 service workers are ephemeral. Revive the native port
// whenever Chrome re-runs this module (startup, install/update) and on
// the tab events we already register for.
// ---------------------------------------------------------------------------

chrome.runtime.onStartup.addListener(() => forceReconnect('onStartup'));
chrome.runtime.onInstalled.addListener(() => forceReconnect('onInstalled'));

connectNativeHost();
console.log('[Hush] Service worker started');
