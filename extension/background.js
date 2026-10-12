/**
 * Pollinate Chrome Extension - Background Service Worker
 * User-triggered selection capture, side panel orchestration, and status badges.
 */

const DEFAULT_PORT = 8765;
let temporaryBadgeTimer = null;

function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.sync.get({ port: DEFAULT_PORT, token: "" }, (items) => {
      resolve(items);
    });
  });
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text: text || "" });
  if (color) {
    chrome.action.setBadgeBackgroundColor({ color: color });
  }
}

function showTemporaryBadge(text, color, durationMs = 3000) {
  if (temporaryBadgeTimer) {
    clearTimeout(temporaryBadgeTimer);
  }
  setBadge(text, color);
  temporaryBadgeTimer = setTimeout(() => {
    temporaryBadgeTimer = null;
    setBadge("", "");
  }, durationMs);
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "pollinate-capture",
    title: "Capture to Pollinate",
    contexts: ["selection"],
  });

  // Enable opening side panel on extension action click
  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "pollinate-capture") {
    return;
  }

  const selectionText = (info.selectionText || "").trim();
  if (!selectionText) {
    return;
  }

  // Open the side panel for the active tab immediately upon capture
  if (tab?.id && chrome.sidePanel && chrome.sidePanel.open) {
    chrome.sidePanel.open({ tabId: tab.id }).catch(() => {});
  }

  const { port, token } = await getConfig();
  if (!token) {
    await chrome.storage.local.set({
      pollinate_state: {
        status: "error",
        message: "Capture token is not configured. Please open Options and save your token.",
      },
    });
    chrome.runtime.sendMessage({ type: "DRAFT_ERROR" }).catch(() => {});
    showTemporaryBadge("ERR", "#d9534f", 3000);
    return;
  }

  // Signal loading state in side panel
  await chrome.storage.local.set({
    pollinate_state: {
      status: "loading",
      selected_text: selectionText,
      url: tab?.url || "",
      page_title: tab?.title || "",
    },
  });
  chrome.runtime.sendMessage({ type: "DRAFT_LOADING" }).catch(() => {});

  const payload = {
    selected_text: selectionText,
    url: tab?.url || "",
    page_title: tab?.title || "",
  };

  try {
    const response = await fetch(`http://127.0.0.1:${port}/draft`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Capture-Token": token,
      },
      body: JSON.stringify(payload),
    });

    if (response.status === 200) {
      const data = await response.json();
      await chrome.storage.local.set({
        pollinate_state: {
          status: "ready",
          data: data,
        },
      });
      chrome.runtime.sendMessage({ type: "DRAFT_LOADED", data }).catch(() => {});

      if (data.duplicate_warning) {
        showTemporaryBadge("DUP", "#f0ad4e", 3000);
      } else {
        showTemporaryBadge("OK", "#5cb85c", 3000);
      }
    } else {
      const errJson = await response.json().catch(() => ({}));
      const message = errJson.detail || `Server returned ${response.status} error`;
      await chrome.storage.local.set({
        pollinate_state: {
          status: "error",
          message: message,
          post_id: errJson.post_id || null,
        },
      });
      chrome.runtime.sendMessage({ type: "DRAFT_ERROR" }).catch(() => {});
      showTemporaryBadge("ERR", "#d9534f", 3000);
    }
  } catch (error) {
    const message = `Cannot connect to server at 127.0.0.1:${port}. Is the server running?`;
    await chrome.storage.local.set({
      pollinate_state: {
        status: "error",
        message: message,
      },
    });
    chrome.runtime.sendMessage({ type: "DRAFT_ERROR" }).catch(() => {});
    showTemporaryBadge("ERR", "#d9534f", 3000);
  }
});
