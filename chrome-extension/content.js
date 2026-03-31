/**
 * HeyVox Chrome Companion — Content Script
 *
 * Detects <video> and <audio> elements on the page, reports play/pause state
 * changes to the background worker, and executes pause/play commands.
 *
 * Requirement: CHROME-01
 */

(() => {
  "use strict";

  /** Current media state: "playing" | "paused" | "none" */
  let lastState = "none";

  /**
   * Scan the page for media elements and return aggregate state.
   * @returns {"playing" | "paused" | "none"}
   */
  function getMediaState() {
    const elements = document.querySelectorAll("video, audio");
    if (elements.length === 0) return "none";

    for (const el of elements) {
      if (!el.paused && !el.ended && el.readyState > 2) {
        return "playing";
      }
    }
    return "paused";
  }

  /**
   * Pause all playing media on the page.
   * @returns {number} Count of elements paused.
   */
  function pauseAll() {
    let count = 0;
    for (const el of document.querySelectorAll("video, audio")) {
      if (!el.paused) {
        el.pause();
        count++;
      }
    }
    return count;
  }

  /**
   * Play the first paused media element (most recently paused preferred).
   * @returns {boolean} Whether any element was resumed.
   */
  function playFirst() {
    for (const el of document.querySelectorAll("video, audio")) {
      if (el.paused && el.readyState > 2) {
        el.play().catch(() => {});
        return true;
      }
    }
    return false;
  }

  // Report state changes to background worker
  function reportState() {
    const state = getMediaState();
    if (state !== lastState) {
      lastState = state;
      chrome.runtime.sendMessage({
        type: "tab_state",
        state,
        url: location.href,
        title: document.title,
      });
    }
  }

  // Listen for commands from background worker
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "pause") {
      const count = pauseAll();
      sendResponse({ ok: true, paused: count });
    } else if (msg.type === "play") {
      const resumed = playFirst();
      sendResponse({ ok: true, resumed });
    } else if (msg.type === "query") {
      sendResponse({ state: getMediaState(), url: location.href, title: document.title });
    }
    return false; // synchronous response
  });

  // Poll for state changes (handles SPAs where media is added dynamically)
  setInterval(reportState, 1000);

  // Also listen for play/pause events on the document
  document.addEventListener("play", reportState, true);
  document.addEventListener("pause", reportState, true);
  document.addEventListener("ended", reportState, true);

  // Initial report
  reportState();
})();
