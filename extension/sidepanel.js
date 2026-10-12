/**
 * Pollinate Chrome Extension - Side Panel Logic
 * Renders inline comment drafts, post analysis, copy actions, and status updates.
 */

let currentPostData = null;
let currentDraftIndex = 0;
let userEditedDrafts = {};

function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.sync.get({ port: 8765, token: "" }, (items) => {
      resolve(items);
    });
  });
}

let buzzInterval = null;
const buzzMessages = [
  "Scouting post context and active discourse...",
  "Extracting key nectar and author perspective...",
  "Filtering synthetic filler from the hive...",
  "Pollinating drafts with grounded experience...",
  "Harvesting...",
];
let buzzIndex = 0;

function showView(viewId) {
  if (buzzInterval) {
    clearInterval(buzzInterval);
    buzzInterval = null;
  }

  document.querySelectorAll(".view-state").forEach((el) => {
    el.classList.remove("active");
  });
  const target = document.getElementById(viewId);
  if (target) {
    target.classList.add("active");
  }

  if (viewId === "view-loading") {
    buzzIndex = 0;
    const stepEl = document.getElementById("loading-buzz-step");
    if (stepEl) {
      stepEl.textContent = buzzMessages[0];
      buzzInterval = setInterval(() => {
        if (buzzIndex < buzzMessages.length - 1) {
          buzzIndex += 1;
          stepEl.textContent = buzzMessages[buzzIndex];
        } else {
          clearInterval(buzzInterval);
          buzzInterval = null;
        }
      }, 2000);
    }
  }
}

function renderPost(data) {
  if (!data) return;
  currentPostData = data;
  userEditedDrafts = {};
  currentDraftIndex = 0;

  // Metadata
  document.getElementById("post-author").textContent = data.author_name || "Unknown Author";
  document.getElementById("post-headline").textContent = data.author_headline || "";
  document.getElementById("pill-relevance").textContent = `Relevance: ${data.relevance_score}/10`;
  document.getElementById("pill-opportunity").textContent = `Opportunity: ${data.opportunity_score}/10`;
  document.getElementById("post-reason").textContent = data.one_line_reason || "";

  // Prior interaction
  const interactionPill = document.getElementById("pill-interaction");
  if (data.prior_interaction && data.prior_interaction.comments_posted > 0) {
    interactionPill.style.display = "inline-block";
    interactionPill.textContent = `Repeat Author (${data.prior_interaction.comments_posted} prior comments)`;
  } else {
    interactionPill.style.display = "none";
  }

  // Duplicate Warning
  const dupBanner = document.getElementById("duplicate-banner");
  if (data.duplicate_warning) {
    const postDate = new Date(data.duplicate_warning.posted_at).toLocaleDateString();
    dupBanner.innerHTML = `⚠️ <strong>Already commented on ${postDate}:</strong><br><em>"${data.duplicate_warning.final_comment}"</em>`;
    dupBanner.style.display = "block";
  } else {
    dupBanner.style.display = "none";
  }

  // Drafts
  const drafts = data.drafts || [];
  const tabsContainer = document.getElementById("tabs-container");
  tabsContainer.innerHTML = "";

  drafts.forEach((draft, idx) => {
    const btn = document.createElement("button");
    btn.className = `tab-btn ${idx === currentDraftIndex ? "active" : ""}`;
    const angleText = typeof draft.angle === "object" ? draft.angle.value : String(draft.angle);
    btn.textContent = angleText.charAt(0).toUpperCase() + angleText.slice(1);
    btn.addEventListener("click", () => switchDraft(idx));
    tabsContainer.appendChild(btn);
  });

  renderActiveDraft();
  showView("view-content");
}

function switchDraft(index) {
  // Save edits to current draft before switching
  const editor = document.getElementById("draft-text");
  userEditedDrafts[currentDraftIndex] = editor.value;

  currentDraftIndex = index;
  const tabs = document.querySelectorAll(".tab-btn");
  tabs.forEach((tab, idx) => {
    tab.classList.toggle("active", idx === currentDraftIndex);
  });

  renderActiveDraft();
}

function renderActiveDraft() {
  if (!currentPostData || !currentPostData.drafts || !currentPostData.drafts[currentDraftIndex]) {
    return;
  }
  const draft = currentPostData.drafts[currentDraftIndex];
  const insightEl = document.getElementById("draft-insight");
  insightEl.innerHTML = `<strong>Adds beyond existing:</strong> ${draft.adds_beyond_existing || "Original insight"}`;

  const editor = document.getElementById("draft-text");
  editor.value = userEditedDrafts[currentDraftIndex] !== undefined
    ? userEditedDrafts[currentDraftIndex]
    : draft.text;
}

// Copy to clipboard
document.getElementById("btn-copy").addEventListener("click", async () => {
  const editor = document.getElementById("draft-text");
  const text = editor.value;
  if (!text) return;

  await navigator.clipboard.writeText(text);
  const copyBtn = document.getElementById("btn-copy");
  const origText = copyBtn.textContent;
  copyBtn.textContent = "Copied";
  copyBtn.style.backgroundColor = "var(--success)";
  copyBtn.style.color = "#fff";

  setTimeout(() => {
    copyBtn.textContent = origText;
    copyBtn.style.backgroundColor = "var(--accent)";
    copyBtn.style.color = "#000";
  }, 2000);
});

// Mark as posted
document.getElementById("btn-mark-posted").addEventListener("click", async () => {
  if (!currentPostData) return;
  const editor = document.getElementById("draft-text");
  const finalComment = editor.value.trim();
  if (!finalComment) return;

  const { port, token } = await getConfig();
  const markBtn = document.getElementById("btn-mark-posted");
  markBtn.textContent = "Saving...";
  markBtn.disabled = true;

  try {
    const response = await fetch(`http://127.0.0.1:${port}/posts/${currentPostData.post_id}/posted`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Capture-Token": token,
      },
      body: JSON.stringify({ final_comment: finalComment }),
    });

    if (response.ok) {
      markBtn.textContent = "Saved to History";
      await chrome.storage.local.set({ pollinate_state: { status: "idle" } });
      setTimeout(() => {
        showView("view-idle");
        markBtn.disabled = false;
        markBtn.textContent = "Mark as Posted";
      }, 1500);
    } else {
      markBtn.disabled = false;
      markBtn.textContent = "Error saving";
    }
  } catch (err) {
    markBtn.disabled = false;
    markBtn.textContent = "Connection error";
  }
});

async function executeRegenerate(guidance = "") {
  if (!currentPostData) return;
  const { port, token } = await getConfig();
  showView("view-loading");

  try {
    const response = await fetch(`http://127.0.0.1:${port}/posts/${currentPostData.post_id}/regenerate`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Capture-Token": token,
      },
      body: JSON.stringify(guidance ? { guidance } : {}),
    });

    if (response.ok) {
      const data = await response.json();
      const guidanceInput = document.getElementById("regen-guidance-input");
      if (guidanceInput) guidanceInput.value = "";
      await chrome.storage.local.set({ pollinate_state: { status: "ready", data } });
      renderPost(data);
    } else {
      const err = await response.json().catch(() => ({}));
      showError(err.detail || "Regeneration failed.");
    }
  } catch (err) {
    showError("Could not connect to Pollinate server.");
  }
}

// Regenerate with custom guidance
document.getElementById("btn-regenerate").addEventListener("click", () => {
  const guidanceInput = document.getElementById("regen-guidance-input");
  const guidance = guidanceInput ? guidanceInput.value.trim() : "";
  executeRegenerate(guidance);
});

// Simplify drafts
const simplifyBtn = document.getElementById("btn-simplify");
if (simplifyBtn) {
  simplifyBtn.addEventListener("click", () => {
    executeRegenerate("Make these drafts simpler, punchier, down-to-earth, and conversational. Cut elevated vocabulary and stiff phrasing.");
  });
}

function showError(msg) {
  document.getElementById("error-message").textContent = msg;
  showView("view-error");
}

document.getElementById("btn-retry").addEventListener("click", () => {
  showView("view-idle");
});

document.getElementById("open-settings").addEventListener("click", () => {
  if (chrome.runtime.openOptionsPage) {
    chrome.runtime.openOptionsPage();
  } else {
    window.open("options.html");
  }
});

// Sync state on launch & storage change
async function init() {
  const result = await chrome.storage.local.get("pollinate_state");
  const state = result.pollinate_state;

  if (!state || state.status === "idle") {
    showView("view-idle");
  } else if (state.status === "loading") {
    showView("view-loading");
  } else if (state.status === "error") {
    showError(state.message || "An error occurred.");
  } else if (state.status === "ready" && state.data) {
    renderPost(state.data);
  }
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.pollinate_state) {
    const newState = changes.pollinate_state.newValue;
    if (!newState || newState.status === "idle") {
      showView("view-idle");
    } else if (newState.status === "loading") {
      showView("view-loading");
    } else if (newState.status === "error") {
      showError(newState.message || "An error occurred.");
    } else if (newState.status === "ready" && newState.data) {
      renderPost(newState.data);
    }
  }
});

document.addEventListener("DOMContentLoaded", init);
