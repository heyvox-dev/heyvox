/**
 * content.js — Hush Content Script
 *
 * Injected into every page. Finds HTML5 media elements (including those
 * inside Shadow DOM trees) and responds to pause/resume/query commands
 * from the background service worker.
 *
 * Message protocol (chrome.runtime.onMessage):
 *   { action: "query-media" }  → boolean (true if any media is playing)
 *   { action: "pause-media" }  → number (count of elements paused)
 *   { action: "resume-media", rewindSecs?: number, fadeInMs?: number }
 *       → number (count of elements resumed)
 *   { action: "play-if-paused" }  → { played, failed, alreadyPlaying, total }
 *       (DEF-119: unconditional play() on any paused media; used after a
 *        tab-mute resume to recover players that MediaSession-paused on mute)
 */

(() => {
  'use strict';

  // Track elements that this content script paused so we only resume those.
  /** @type {Set<HTMLMediaElement>} */
  const pausedByHush = new Set();

  // ---------------------------------------------------------------------------
  // Shadow DOM traversal
  // ---------------------------------------------------------------------------

  /**
   * Recursively collects all video and audio elements in a root node,
   * including those nested inside Shadow DOM trees.
   *
   * @param {Document | ShadowRoot | Element} root
   * @param {HTMLMediaElement[]} [acc]
   * @returns {HTMLMediaElement[]}
   */
  function collectMediaElements(root, acc = []) {
    // Direct descendants
    const direct = root.querySelectorAll('video, audio');
    for (const el of direct) {
      acc.push(/** @type {HTMLMediaElement} */ (el));
    }

    // Shadow roots on any element in the subtree
    const allElements = root.querySelectorAll('*');
    for (const el of allElements) {
      if (el.shadowRoot) {
        collectMediaElements(el.shadowRoot, acc);
      }
    }

    return acc;
  }

  /**
   * Returns all media elements currently in the page.
   * @returns {HTMLMediaElement[]}
   */
  function getAllMediaElements() {
    return collectMediaElements(document);
  }

  // ---------------------------------------------------------------------------
  // MutationObserver — watch for dynamically added media
  // ---------------------------------------------------------------------------

  /**
   * Handles newly added nodes; cleans up pausedByHush if elements are removed.
   * @type {MutationObserver}
   */
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.removedNodes) {
        if (node instanceof HTMLMediaElement) {
          pausedByHush.delete(node);
        } else if (node instanceof Element) {
          // Check subtree for removed media
          const removed = node.querySelectorAll('video, audio');
          for (const el of removed) {
            pausedByHush.delete(/** @type {HTMLMediaElement} */ (el));
          }
        }
      }
    }
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  // ---------------------------------------------------------------------------
  // Media control helpers
  // ---------------------------------------------------------------------------

  /**
   * Returns true if there is any media element actively playing.
   * @returns {boolean}
   */
  function isAnyMediaPlaying() {
    const elements = getAllMediaElements();
    return elements.some((el) => !el.paused && !el.ended && el.readyState >= 2);
  }

  /**
   * Pauses all currently playing media elements and records them so we can
   * resume them later.
   * @returns {{count: number, total: number, skipped: object[], host: string, frame: string}}
   *   Full diagnostic — background.js reads .count for the success check and
   *   forwards the rest into the native host log so we don't need to crack
   *   open the page DevTools console to debug why a YouTube pause fell back
   *   to tab-mute.
   */
  function pauseAllMedia() {
    const elements = getAllMediaElements();
    let count = 0;
    const skipReasons = [];

    for (const el of elements) {
      const reason = el.paused
        ? 'already-paused'
        : el.ended
          ? 'ended'
          : el.readyState < 2
            ? `readyState=${el.readyState}`
            : null;
      if (reason) {
        skipReasons.push({
          tag: el.tagName,
          src: (el.currentSrc || el.src || '').slice(0, 80),
          reason,
        });
        continue;
      }
      try {
        el.pause();
        pausedByHush.add(el);
        count += 1;
      } catch (err) {
        console.warn('[Hush] Could not pause element:', err);
        skipReasons.push({ tag: el.tagName, reason: `error:${err?.message}` });
      }
    }

    // DEF-105 follow-up diagnostic: tells us why pause returned 0 — the
    // background script falls back to tab-mute when count === 0, which on
    // YouTube means the video never actually pauses, just goes silent.
    console.log(
      `[Hush] pauseAllMedia: total=${elements.length} paused=${count} skipped=${skipReasons.length}`,
      skipReasons,
    );

    // DEF-125 diagnostic: include host + frame so the background script can
    // log which frame's content script responded (with all_frames:true the
    // top frame and any iframes race, only the first reply wins).
    return {
      count,
      total: elements.length,
      skipped: skipReasons,
      host: location.hostname || '',
      frame: window === window.top ? 'top' : 'iframe',
    };
  }

  /**
   * Smoothly fades a media element's volume from startVol to targetVol.
   * @param {HTMLMediaElement} el
   * @param {number} startVol  - starting volume (0–1)
   * @param {number} targetVol - ending volume (0–1)
   * @param {number} durationMs - fade duration in milliseconds
   */
  function fadeVolume(el, startVol, targetVol, durationMs) {
    const steps = Math.max(1, Math.round(durationMs / 50)); // ~50ms per step
    const stepMs = durationMs / steps;
    const delta = (targetVol - startVol) / steps;
    let step = 0;

    el.volume = startVol;

    const timer = setInterval(() => {
      step += 1;
      if (step >= steps) {
        el.volume = targetVol;
        clearInterval(timer);
      } else {
        el.volume = Math.min(1, Math.max(0, startVol + delta * step));
      }
    }, stepMs);
  }

  /**
   * Resumes all media elements that this content script previously paused.
   * Optionally rewinds and fades in.
   * @param {number} [rewindSecs=0] - seconds to rewind before playing
   * @param {number} [fadeInMs=0]   - fade-in duration in milliseconds (0 = instant)
   * @returns {number} Number of elements resumed
   */
  async function resumeAllMedia(rewindSecs = 0, fadeInMs = 0) {
    // DEF-112: returns a diagnostic object so the upstream can tell whether
    // play() actually resolved vs only being called. Truthful status >
    // bookkeeping-success.
    const elements = [...pausedByHush];
    const promises = [];
    const errors = [];
    const setStates = []; // pre-play state snapshot per element
    let attempted = 0;
    let skippedAlreadyPlaying = 0;

    for (const el of elements) {
      if (!el.paused) {
        skippedAlreadyPlaying += 1;
        continue;
      }

      attempted += 1;
      const originalVolume = el.volume;
      const snap = {
        tag: el.tagName,
        currentTime: el.currentTime,
        duration: el.duration,
        readyState: el.readyState,
        muted: el.muted,
        src: (el.currentSrc || el.src || '').slice(0, 80),
      };
      setStates.push(snap);

      try {
        if (fadeInMs > 0) el.volume = 0.1;

        // Call play() first — seeking before play() races with MSE players
        // (YouTube) and causes AbortError. Defer seek + fade to .then().
        const playPromise = el.play();
        if (playPromise instanceof Promise) {
          promises.push(
            playPromise
              .then(() => {
                if (rewindSecs > 0 && isFinite(el.duration)) {
                  el.currentTime = Math.max(0, el.currentTime - rewindSecs);
                }
                if (fadeInMs > 0) fadeVolume(el, 0.1, originalVolume, fadeInMs);
                return { ok: true };
              })
              .catch((err) => {
                el.volume = originalVolume;
                const msg = `${err?.name || 'Error'}: ${err?.message || String(err)}`;
                console.warn('[Hush] play() rejected:', msg, snap);
                errors.push(msg);
                return { ok: false, error: msg };
              })
          );
        } else {
          // Legacy synchronous play()
          if (rewindSecs > 0 && isFinite(el.duration)) {
            el.currentTime = Math.max(0, el.currentTime - rewindSecs);
          }
          if (fadeInMs > 0) fadeVolume(el, 0.1, originalVolume, fadeInMs);
          promises.push(Promise.resolve({ ok: true }));
        }
      } catch (err) {
        const msg = `${err?.name || 'Error'}: ${err?.message || String(err)}`;
        console.warn('[Hush] Could not resume element:', msg);
        errors.push(msg);
        promises.push(Promise.resolve({ ok: false, error: msg }));
      }
    }

    pausedByHush.clear();

    const results = await Promise.all(promises);
    const played = results.filter((r) => r.ok).length;
    const failed = results.length - played;

    return {
      tracked: elements.length,
      attempted,
      played,
      failed,
      skippedAlreadyPlaying,
      errors,
      snapshot: setStates,
    };
  }

  // ---------------------------------------------------------------------------
  // DEF-119 — Recover from tab-mute pause that triggered MediaSession-pause
  // ---------------------------------------------------------------------------

  /**
   * Calls play() on every paused media element on the page (incl. Shadow DOM).
   * Unlike resumeAllMedia(), this is not restricted to elements pausedByHush —
   * YouTube and similar players pause themselves when the tab is muted via
   * chrome.tabs.update({ muted: true }), so on resume we must restart them
   * without having tracked them on the pause side.
   * @returns {Promise<object>} diagnostic
   */
  async function playIfPaused() {
    const elements = getAllMediaElements();
    const snapshots = [];
    const promises = [];
    let attempted = 0;
    let alreadyPlaying = 0;

    for (const el of elements) {
      if (!el.paused) {
        alreadyPlaying += 1;
        continue;
      }
      if (el.ended) continue;
      attempted += 1;
      const snap = {
        tag: el.tagName,
        currentTime: el.currentTime,
        duration: el.duration,
        readyState: el.readyState,
        muted: el.muted,
      };
      snapshots.push(snap);
      try {
        const p = el.play();
        if (p instanceof Promise) {
          promises.push(
            p.then(() => ({ ok: true })).catch((err) => ({
              ok: false,
              error: `${err?.name || 'Error'}: ${err?.message || String(err)}`,
            }))
          );
        } else {
          promises.push(Promise.resolve({ ok: true }));
        }
      } catch (err) {
        promises.push(
          Promise.resolve({
            ok: false,
            error: `${err?.name || 'Error'}: ${err?.message || String(err)}`,
          })
        );
      }
    }
    const results = await Promise.all(promises);
    const played = results.filter((r) => r.ok).length;
    return {
      total: elements.length,
      attempted,
      played,
      failed: results.length - played,
      alreadyPlaying,
      snapshot: snapshots,
    };
  }

  // ---------------------------------------------------------------------------
  // Text injection
  // ---------------------------------------------------------------------------

  /**
   * Inserts text into the currently focused element using execCommand or
   * InputEvent fallback. Works with contentEditable, textarea, and input fields.
   * @param {string} text - Text to insert
   * @returns {boolean} True if text was inserted successfully
   */
  function insertText(text) {
    const el = document.activeElement;
    if (!el) return false;

    // For input/textarea elements
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      const start = el.selectionStart ?? el.value.length;
      const end = el.selectionEnd ?? el.value.length;
      // execCommand preserves undo stack
      el.focus();
      if (document.execCommand('insertText', false, text)) {
        return true;
      }
      // Fallback: direct value manipulation + InputEvent
      el.value = el.value.slice(0, start) + text + el.value.slice(end);
      el.selectionStart = el.selectionEnd = start + text.length;
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
      return true;
    }

    // For contentEditable elements (Electron apps, rich text editors)
    if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') {
      el.focus();
      if (document.execCommand('insertText', false, text)) {
        return true;
      }
      // Fallback: InputEvent
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
      return true;
    }

    return false;
  }

  /**
   * Simulates pressing Enter by dispatching keyboard events on the focused element.
   * @param {number} count - Number of Enter presses
   * @returns {boolean} True if events were dispatched
   */
  function pressEnter(count) {
    const el = document.activeElement;
    if (!el) return false;

    for (let i = 0; i < count; i++) {
      // Dispatch full keyboard event sequence
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
      el.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
      // For input/textarea, also insert newline via execCommand
      if (el.tagName === 'TEXTAREA') {
        document.execCommand('insertLineBreak', false);
      }
      el.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
    }
    return true;
  }

  // ---------------------------------------------------------------------------
  // Message listener
  // ---------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message.action !== 'string') return false;

    switch (message.action) {
      case 'query-media':
        sendResponse(isAnyMediaPlaying());
        return false;

      case 'pause-media': {
        // DEF-125: now returns {count, total, skipped, host, frame} so the
        // background script can forward the diagnostic into hush.log. The
        // .count field preserves the historical "truthy = success" contract.
        const result = pauseAllMedia();
        sendResponse(result);
        return false;
      }

      case 'resume-media': {
        const rewind = message.rewindSecs || 0;
        const fade = message.fadeInMs || 0;
        resumeAllMedia(rewind, fade).then((diag) => sendResponse(diag));
        return true; // async response
      }

      case 'play-if-paused': {
        // DEF-119: post-unmute recovery for tab-mute pauses.
        playIfPaused().then((diag) => sendResponse(diag));
        return true; // async response
      }

      case 'type-text': {
        const ok = insertText(message.text || '');
        sendResponse(ok);
        return false;
      }

      case 'press-enter': {
        const ok = pressEnter(message.count || 1);
        sendResponse(ok);
        return false;
      }

      default:
        return false;
    }
  });
})();
