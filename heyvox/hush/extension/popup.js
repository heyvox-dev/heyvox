/**
 * popup.js — Hush Extension Popup
 *
 * Fetches current state from the background service worker and renders
 * the list of paused tabs plus a "Resume All" button.
 */

(() => {
  'use strict';

  // ---------------------------------------------------------------------------
  // DOM references
  // ---------------------------------------------------------------------------

  const logo = /** @type {HTMLElement} */ (document.getElementById('logo'));
  const statePill = /** @type {HTMLElement} */ (document.getElementById('statePill'));
  const mainBody = /** @type {HTMLElement} */ (document.getElementById('mainBody'));
  const loadingMsg = document.getElementById('loadingMsg');

  // ---------------------------------------------------------------------------
  // State fetch
  // ---------------------------------------------------------------------------

  /**
   * Requests current status from the background service worker.
   * @returns {Promise<{state: string, tabs: Array<{id: number, title: string, url: string}>, pausedCount: number}>}
   */
  async function fetchStatus() {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ action: 'get-status' }, (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        resolve(response);
      });
    });
  }

  /**
   * Sends a "resume-all" command to the background service worker.
   * @returns {Promise<object>}
   */
  async function requestResumeAll() {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ action: 'resume-all' }, (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        resolve(response);
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------------

  /**
   * Updates the state pill text and data attribute.
   * @param {string} state
   */
  function renderStatePill(state) {
    statePill.textContent = state || 'idle';
    statePill.dataset.state = state || 'idle';
  }

  /**
   * Updates the logo to reflect the current state.
   * @param {string} state
   */
  function renderLogo(state) {
    if (state === 'paused') {
      logo.classList.add('is-paused');
    } else {
      logo.classList.remove('is-paused');
    }
  }

  /**
   * Renders a single tab row.
   * @param {{id: number, title: string, url: string}} tab
   * @returns {HTMLElement}
   */
  function createTabItem(tab) {
    const item = document.createElement('div');
    item.className = 'tab-item';

    // Favicon placeholder (generic icon via SVG)
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'tab-icon');
    icon.setAttribute('viewBox', '0 0 16 16');
    icon.setAttribute('fill', 'none');
    icon.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 1a6 6 0 1 1 0 12A6 6 0 0 1 8 2zm0 2.5a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm-.5 3.5v4h1v-4H7.5z');
    path.setAttribute('fill', 'currentColor');
    icon.appendChild(path);

    // Try to use the actual favicon
    const hostname = tryGetHostname(tab.url);
    if (hostname) {
      const img = document.createElement('img');
      img.className = 'tab-icon';
      img.src = `https://www.google.com/s2/favicons?domain=${hostname}&sz=16`;
      img.alt = '';
      img.onerror = () => {
        img.replaceWith(icon);
      };
      item.appendChild(img);
    } else {
      item.appendChild(icon);
    }

    const title = document.createElement('span');
    title.className = 'tab-title';
    title.textContent = tab.title || tab.url || `Tab ${tab.id}`;
    title.title = tab.url || '';

    item.appendChild(title);
    return item;
  }

  /**
   * Safely extracts the hostname from a URL string.
   * @param {string} url
   * @returns {string | null}
   */
  function tryGetHostname(url) {
    try {
      return new URL(url).hostname;
    } catch {
      return null;
    }
  }

  /**
   * Creates the "Resume All" button.
   * @param {boolean} enabled
   * @returns {HTMLButtonElement}
   */
  function createResumeButton(enabled) {
    const btn = document.createElement('button');
    btn.className = 'btn-resume';
    btn.textContent = 'Resume All';
    btn.disabled = !enabled;

    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = 'Resuming...';
      try {
        await requestResumeAll();
        await render();
      } catch (err) {
        console.error('[Hush popup] Resume failed:', err);
        btn.disabled = false;
        btn.textContent = 'Resume All';
      }
    });

    return btn;
  }

  /**
   * Renders the full popup body given a status response.
   * @param {{state: string, tabs: Array<{id: number, title: string, url: string}>, pausedCount: number}} status
   */
  function renderBody(status) {
    const { state, tabs } = status;

    mainBody.innerHTML = '';

    if (!tabs || tabs.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'empty-state';
      empty.textContent = state === 'paused'
        ? 'Media paused across all tabs.'
        : 'No media is currently paused by Hush.';
      mainBody.appendChild(empty);
    } else {
      const label = document.createElement('p');
      label.className = 'tabs-label';
      label.textContent = `Paused tabs (${tabs.length})`;
      mainBody.appendChild(label);

      const list = document.createElement('div');
      list.className = 'tab-list';
      for (const tab of tabs) {
        list.appendChild(createTabItem(tab));
      }
      mainBody.appendChild(list);
    }

    const resumeEnabled = state === 'paused' && tabs.length > 0;
    mainBody.appendChild(createResumeButton(resumeEnabled));
  }

  /**
   * Renders an error message in the popup body.
   * @param {string} message
   */
  function renderError(message) {
    mainBody.innerHTML = '';
    const err = document.createElement('p');
    err.className = 'error-msg';
    err.textContent = message;
    mainBody.appendChild(err);

    // Show a disabled resume button so the layout stays consistent
    mainBody.appendChild(createResumeButton(false));
  }

  // ---------------------------------------------------------------------------
  // Main render loop
  // ---------------------------------------------------------------------------

  /**
   * Fetches the current status and re-renders the entire popup.
   */
  async function render() {
    try {
      const status = await fetchStatus();
      renderStatePill(status.state);
      renderLogo(status.state);
      renderBody(status);
    } catch (err) {
      console.error('[Hush popup] Failed to fetch status:', err);
      renderStatePill('idle');
      renderLogo('idle');
      renderError('Could not connect to Hush background service.');
    }
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  // Remove loading message immediately and kick off the real render
  if (loadingMsg) loadingMsg.remove();
  render();
})();
