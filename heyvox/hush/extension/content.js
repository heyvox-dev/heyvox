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
   * @returns {number} Number of elements paused
   */
  function pauseAllMedia() {
    const elements = getAllMediaElements();
    let count = 0;

    for (const el of elements) {
      if (!el.paused && !el.ended && el.readyState >= 2) {
        try {
          el.pause();
          pausedByHush.add(el);
          count += 1;
        } catch (err) {
          console.warn('[Hush] Could not pause element:', err);
        }
      }
    }

    return count;
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
  function resumeAllMedia(rewindSecs = 0, fadeInMs = 0) {
    let count = 0;

    for (const el of pausedByHush) {
      if (el.paused) {
        try {
          // Rewind
          if (rewindSecs > 0 && isFinite(el.duration)) {
            el.currentTime = Math.max(0, el.currentTime - rewindSecs);
          }

          // Store original volume for fade-in
          const originalVolume = el.volume;

          // Start at low volume if fading
          if (fadeInMs > 0) {
            el.volume = 0.1;
          }

          const playPromise = el.play();
          if (playPromise instanceof Promise) {
            playPromise
              .then(() => {
                if (fadeInMs > 0) {
                  fadeVolume(el, 0.1, originalVolume, fadeInMs);
                }
              })
              .catch((err) => {
                // Autoplay policy may block play — log but don't crash
                console.warn('[Hush] play() rejected:', err);
                el.volume = originalVolume; // restore on failure
              });
          } else if (fadeInMs > 0) {
            fadeVolume(el, 0.1, originalVolume, fadeInMs);
          }

          count += 1;
        } catch (err) {
          console.warn('[Hush] Could not resume element:', err);
        }
      }
    }

    pausedByHush.clear();
    return count;
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
        const paused = pauseAllMedia();
        sendResponse(paused);
        return false;
      }

      case 'resume-media': {
        const rewind = message.rewindSecs || 0;
        const fade = message.fadeInMs || 0;
        const resumed = resumeAllMedia(rewind, fade);
        sendResponse(resumed);
        return false;
      }

      default:
        return false;
    }
  });
})();
