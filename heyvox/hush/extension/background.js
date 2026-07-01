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

/**
 * DEF-098 / DEF-110: persist pausedTabs to chrome.storage.local so the
 * in-memory Map survives MV3 service-worker death. We use storage.local
 * (not session) because session storage was observed to return empty
 * even within a single SW lifetime — local is rock-solid across
 * SW respawns, browser restarts, and extension crashes.
 *
 * Stale entries (older than STALE_AFTER_MS) are dropped on restore so
 * a paused-tab record from a previous browser session never leaks into
 * the current one.
 */
const STORAGE_KEY = 'hush:pausedTabs';
const STALE_AFTER_MS = 10 * 60 * 1000; // 10 minutes

/**
 * Write the current pausedTabs Map to chrome.storage.local.
 * Returns a promise so callers can await commit before responding.
 */
async function persistPausedTabs() {
  const entries = [...pausedTabs.entries()];
  try {
    await chrome.storage.local.set({ [STORAGE_KEY]: entries });
    console.log(`[Hush] persisted ${entries.length} paused tabs`);
  } catch (err) {
    console.warn('[Hush] persistPausedTabs failed:', err);
  }
}

/**
 * Restore pausedTabs from chrome.storage.local at SW startup.
 * Drops stale entries so the Map only contains tabs paused recently.
 */
async function restorePausedTabs() {
  try {
    const result = await chrome.storage.local.get(STORAGE_KEY);
    const entries = result[STORAGE_KEY];
    if (!Array.isArray(entries) || entries.length === 0) {
      console.log('[Hush] restore: storage was empty');
      return;
    }
    const now = Date.now();
    let restored = 0;
    let dropped = 0;
    for (const [id, info] of entries) {
      if (info?.timestamp && now - info.timestamp > STALE_AFTER_MS) {
        dropped += 1;
        continue;
      }
      pausedTabs.set(id, info);
      restored += 1;
    }
    console.log(`[Hush] restore: ${restored} active, ${dropped} stale dropped`);
    updateBadge();
  } catch (err) {
    console.warn('[Hush] restorePausedTabs failed:', err);
  }
}

// Kick off restoration immediately and capture the promise so message
// handlers can await it before reading pausedTabs. Without the await,
// a `resume` arriving right after SW wake-up sees an empty Map and
// returns pausedCount=0 even though the YouTube tab really is paused.
const restoreReady = restorePausedTabs();

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

  // Wait for session-storage restore before reading pausedTabs synchronously.
  await restoreReady;

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
  console.log(`[Hush] pauseAllTabs start, Map size before: ${pausedTabs.size}`);
  const tabs = await chrome.tabs.query({});
  const nowPlaying = await findPlayingTabs(tabs);

  const paused = [];
  // DEF-125: per-tab content-script diagnostic, keyed by tab.id, captured
  // here and merged into the final pauseDiag below so the native host log
  // sees (total, paused, skipped, host, frame) — explains tab-mute fallbacks
  // without requiring a page-DevTools console.
  const csDiagByTab = new Map();
  await Promise.allSettled(
    nowPlaying.map(async (tab) => {
      let method = 'content-script';
      const result = await sendToContentScript(tab.id, { action: 'pause-media' });
      // DEF-125: result is now {count, total, skipped, host, frame} (object)
      // for any responding content script. Legacy number return (or null on
      // unreachable tabs) falls through the typeof check.
      let count = 0;
      if (result && typeof result === 'object') {
        count = Number(result.count) || 0;
        csDiagByTab.set(tab.id, {
          cs_total: result.total,
          cs_paused: count,
          cs_skipped: result.skipped,
          cs_host: result.host,
          cs_frame: result.frame,
        });
      } else if (typeof result === 'number') {
        count = result;
      }
      if (count > 0) {
        // Content script successfully paused media elements
      } else if (tab.audible) {
        // Content script couldn't reach media (YouTube Shadow DOM, etc.)
        // Fall back to tab muting — silences audio without affecting playback state
        await chrome.tabs.update(tab.id, { muted: true });
        method = 'tab-mute';
      } else if (result === null) {
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

  await persistPausedTabs(); // DEF-098 / DEF-110
  const pauseDiag = paused.map((t) => ({
    tabId: t.id,
    method: pausedTabs.get(t.id)?.method ?? 'unknown',
    audible: t.audible === true,
    ...(csDiagByTab.get(t.id) ?? {}),
  }));
  console.log('[Hush] pause diagnostic:', JSON.stringify(pauseDiag));
  const resp = buildStatusResponse('paused');
  resp._pause = pauseDiag;
  return resp;
}

/**
 * Resumes only the tabs that Hush previously paused.
 * @param {number} [rewindSecs=0] - seconds to rewind before playing
 * @param {number} [fadeInMs=0] - fade-in duration in milliseconds
 * @returns {Promise<object>} Status response
 */
async function resumeAllPausedTabs(rewindSecs = 0, fadeInMs = 0) {
  const entries = [...pausedTabs.entries()];
  console.log(`[Hush] resumeAllPausedTabs start, Map size: ${pausedTabs.size}, entries to resume: ${entries.length}`);

  // DEF-112: capture per-tab outcome so the native host (and hush.log) gets
  // the actual play() resolution counts, not just bookkeeping success.
  const settled = await Promise.allSettled(
    entries.map(async ([tabId, info]) => {
      if (info.method === 'tab-mute') {
        // DEF-119: tab.muted=true on YouTube triggers MediaSession-pause on
        // the player itself; unmuting alone leaves the video paused. Send a
        // play-if-paused to the content script after the unmute. If the
        // content script can't reach the media (Shadow DOM still hostile),
        // we at least leave the diagnostic in the response so the regression
        // is visible in hush.log instead of a silent stuck pause.
        await chrome.tabs.update(tabId, { muted: false }).catch(() => {});
        const playDiag = await sendToContentScript(tabId, {
          action: 'play-if-paused',
        });
        return { tabId, method: 'tab-mute', unmuted: true, play_diag: playDiag };
      }
      const diag = await sendToContentScript(tabId, {
        action: 'resume-media',
        rewindSecs,
        fadeInMs,
      });
      return { tabId, method: info.method ?? 'content-script', diag };
    })
  );
  const diagnostic = settled.map((s) =>
    s.status === 'fulfilled' ? s.value : { error: String(s.reason) }
  );
  console.log('[Hush] resume diagnostic:', JSON.stringify(diagnostic));

  pausedTabs.clear();
  await persistPausedTabs(); // DEF-098 / DEF-110
  const resp = buildStatusResponse('playing');
  resp._resume = diagnostic;
  return resp;
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
  await persistPausedTabs(); // DEF-098 / DEF-110

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
    // DEF-119: see resumeAllPausedTabs comment.
    await chrome.tabs.update(tabId, { muted: false }).catch(() => {});
    await sendToContentScript(tabId, { action: 'play-if-paused' });
  } else {
    await sendToContentScript(tabId, {
      action: 'resume-media',
      rewindSecs,
      fadeInMs,
    });
  }
  pausedTabs.delete(tabId);
  await persistPausedTabs(); // DEF-098 / DEF-110

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
    console.log(`[Hush] onRemoved: dropping paused tab ${tabId}`);
    pausedTabs.delete(tabId);
    persistPausedTabs(); // fire-and-forget cleanup
    updateBadge();
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === 'loading' && pausedTabs.has(tabId)) {
    console.log(`[Hush] onUpdated(loading): dropping paused tab ${tabId}`, changeInfo);
    pausedTabs.delete(tabId);
    persistPausedTabs(); // fire-and-forget cleanup
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

// Chrome MV3 caps service-worker lifetime at ~5 min even with active ports.
// A 30s alarm reliably wakes us before then so the native host stays connected.
chrome.alarms.create('hush-keepalive', { periodInMinutes: 0.5 });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'hush-keepalive' && nativePort === null) {
    connectNativeHost();
  }
});

connectNativeHost();
console.log('[Hush] Service worker started');
