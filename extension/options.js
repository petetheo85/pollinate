/**
 * Pollinate Chrome Extension - Options Page Logic
 * Manages tab switching, server connection settings, and profile synchronization.
 */

document.addEventListener("DOMContentLoaded", () => {
  // Navigation Tabs
  const tabButtons = document.querySelectorAll(".tab-nav-btn");
  const tabPanes = document.querySelectorAll(".tab-pane");

  function setActiveTab(targetId) {
    tabButtons.forEach((b) => b.classList.toggle("active", b.getAttribute("data-tab") === targetId));
    tabPanes.forEach((p) => p.classList.toggle("active", p.id === targetId));
  }

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      setActiveTab(btn.getAttribute("data-tab"));
    });
  });

  const openDashboardBtn = document.getElementById("btn-open-dashboard");
  if (openDashboardBtn) {
    openDashboardBtn.addEventListener("click", () => {
      chrome.tabs.create({ url: "http://127.0.0.1:8501" });
    });
  }

  // Server Connection Elements
  const portInput = document.getElementById("server-port");
  const tokenInput = document.getElementById("capture-token");
  const connectionForm = document.getElementById("settings-form");
  const testButton = document.getElementById("test-button");
  const connectionStatusBanner = document.getElementById("status-banner");

  // Profile Elements
  const profileEditor = document.getElementById("profile-content");
  const profileForm = document.getElementById("profile-form");
  const saveProfileBtn = document.getElementById("save-profile-btn");
  const reloadProfileBtn = document.getElementById("reload-profile-btn");
  const profileStatusBanner = document.getElementById("profile-status-banner");

  function showStatus(banner, message, isSuccess) {
    banner.textContent = message;
    banner.className = isSuccess ? "status-banner success" : "status-banner error";
    banner.style.display = "block";
  }

  function getActiveConfig() {
    return {
      port: parseInt(portInput.value, 10) || 8765,
      token: tokenInput.value.trim(),
    };
  }

  // Load existing configuration from storage
  chrome.storage.sync.get({ port: 8765, token: "" }, (items) => {
    portInput.value = items.port || 8765;
    tokenInput.value = items.token || "";
    const hasToken = Boolean(items.token && items.token.trim());

    // First run or unconfigured token defaults to Server tab; otherwise defaults to Options
    setActiveTab(hasToken ? "tab-options" : "tab-server");

    if (hasToken) {
      loadProfileContent(items.port || 8765, items.token);
    } else {
      showStatus(
        connectionStatusBanner,
        "Welcome to Pollinate! Enter your Capture Bearer Token below and click Test Connection to get started.",
        true
      );
    }
  });

  // Save server connection settings
  connectionForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const port = parseInt(portInput.value, 10);
    const token = tokenInput.value.trim();

    if (!port || port < 1 || port > 65535) {
      showStatus(connectionStatusBanner, "Please enter a valid port number between 1 and 65535.", false);
      return;
    }

    if (!token) {
      showStatus(connectionStatusBanner, "Capture token cannot be empty.", false);
      return;
    }

    chrome.storage.sync.set({ port, token }, () => {
      showStatus(connectionStatusBanner, "Connection settings saved successfully.", true);
      loadProfileContent(port, token);
    });
  });

  // Test connection to local capture server
  testButton.addEventListener("click", async () => {
    const { port, token } = getActiveConfig();

    if (!token) {
      showStatus(connectionStatusBanner, "Please enter a token before testing connection.", false);
      return;
    }

    showStatus(connectionStatusBanner, `Connecting to 127.0.0.1:${port}...`, true);

    try {
      const response = await fetch(`http://127.0.0.1:${port}/count`, {
        method: "GET",
        headers: {
          "X-Capture-Token": token,
        },
      });

      if (response.status === 200) {
        showStatus(connectionStatusBanner, "Connection successful! Pollinate server is online and ready.", true);
        loadProfileContent(port, token);
      } else if (response.status === 401) {
        showStatus(connectionStatusBanner, "Connection failed: 401 Unauthorized (invalid capture token).", false);
      } else {
        showStatus(connectionStatusBanner, `Server returned error status: ${response.status}`, false);
      }
    } catch (_) {
      showStatus(connectionStatusBanner, `Cannot connect to server at 127.0.0.1:${port}. Is pollinate server running?`, false);
    }
  });

  // Fetch user profile from backend
  async function loadProfileContent(port, token) {
    if (!token) {
      showStatus(
        profileStatusBanner,
        "Capture Token is not set. Go to the Server Connection tab to configure your token.",
        false
      );
      return;
    }

    try {
      const response = await fetch(`http://127.0.0.1:${port}/profile`, {
        method: "GET",
        headers: {
          "X-Capture-Token": token,
        },
      });

      if (response.status === 200) {
        const data = await response.json();
        const content = data.content || "";
        profileEditor.value = content;
        syncRadiosWithMarkdown(content);
        profileStatusBanner.style.display = "none";
      } else if (response.status === 404) {
        showStatus(
          profileStatusBanner,
          "Server returned 404 Not Found. Please restart capture_server.py in terminal (Ctrl+C and run again) so the new /profile endpoint takes effect.",
          false
        );
      } else if (response.status === 401) {
        showStatus(
          profileStatusBanner,
          "401 Unauthorized. The capture token configured in Server Connection does not match the server.",
          false
        );
      } else {
        showStatus(profileStatusBanner, `Server returned error status: ${response.status}`, false);
      }
    } catch (_) {
      showStatus(
        profileStatusBanner,
        `Cannot connect to Pollinate server at 127.0.0.1:${port}. Ensure capture_server.py is running.`,
        false
      );
    }
  }

  // Save profile to backend
  profileForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { port, token } = getActiveConfig();

    if (!token) {
      showStatus(profileStatusBanner, "Please configure your Capture Token in the Server Connection tab first.", false);
      return;
    }

    saveProfileBtn.disabled = true;
    saveProfileBtn.textContent = "Saving...";

    try {
      const response = await fetch(`http://127.0.0.1:${port}/profile`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-Capture-Token": token,
        },
        body: JSON.stringify({ content: profileEditor.value }),
      });

      if (response.ok) {
        showStatus(profileStatusBanner, "✓ Profile saved to context/profile.md successfully.", true);
        syncRadiosWithMarkdown(profileEditor.value);
      } else if (response.status === 404) {
        showStatus(
          profileStatusBanner,
          "Server returned 404 Not Found. Please restart capture_server.py in terminal so the new /profile endpoint takes effect.",
          false
        );
      } else {
        showStatus(profileStatusBanner, "Failed to save profile. Server returned status " + response.status, false);
      }
    } catch (_) {
      showStatus(profileStatusBanner, "Could not connect to Pollinate server to save profile.", false);
    } finally {
      saveProfileBtn.disabled = false;
      saveProfileBtn.textContent = "Save Profile to Disk";
    }
  });

  // Reload profile from disk
  reloadProfileBtn.addEventListener("click", () => {
    const { port, token } = getActiveConfig();
    loadProfileContent(port, token);
  });

  // Synchronize Radio Buttons with Markdown content
  function syncRadiosWithMarkdown(text) {
    if (!text) return;

    // Detect Personality
    const personalityInputs = document.querySelectorAll('input[name="personality-preset"]');
    personalityInputs.forEach((input) => {
      const card = input.closest(".radio-card");
      let matched = false;
      if (input.value.includes("Comical") && (text.includes("Comical") || text.includes("witty"))) {
        matched = true;
      } else if (input.value.includes("Sarcastic") && (text.includes("Sarcastic") || text.includes("wry"))) {
        matched = true;
      } else if (input.value.includes("Candid") && (text.includes("Candid") || text.includes("no-nonsense"))) {
        matched = true;
      } else if (input.value.includes("Intelligent") && (text.includes("Intelligent") || text.includes("pragmatic"))) {
        matched = true;
      }

      if (matched) {
        input.checked = true;
        card.classList.add("selected");
      } else {
        card.classList.remove("selected");
      }
    });

    // Detect Length
    const lengthInputs = document.querySelectorAll('input[name="length-preset"]');
    lengthInputs.forEach((input) => {
      const card = input.closest(".radio-card");
      let matched = false;
      if (input.value.startsWith("1 to 2") && (text.includes("1 to 2") || text.includes("1–2") || text.includes("1-2"))) {
        matched = true;
      } else if (input.value.startsWith("3 to 5") && (text.includes("3 to 5") || text.includes("3–5") || text.includes("3-5"))) {
        matched = true;
      } else if (input.value.startsWith("2 to 3") && (text.includes("2 to 3") || text.includes("2–3") || text.includes("2-3") || text.includes("2 to 4"))) {
        matched = true;
      }

      if (matched) {
        input.checked = true;
        card.classList.add("selected");
      } else {
        card.classList.remove("selected");
      }
    });
  }

  // Handle Personality Radio Clicks
  document.querySelectorAll('#personality-radio-group .radio-card').forEach((card) => {
    card.addEventListener("click", () => {
      const input = card.querySelector('input[type="radio"]');
      if (!input) return;
      input.checked = true;

      document.querySelectorAll('#personality-radio-group .radio-card').forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");

      const val = input.value;
      let text = profileEditor.value;
      const regex = /(- \*\*Personality\*\*:).*/i;
      if (regex.test(text)) {
        text = text.replace(regex, `$1 ${val}`);
      } else {
        text += `\n- **Personality**: ${val}`;
      }
      profileEditor.value = text;
    });
  });

  // Handle Length Radio Clicks
  document.querySelectorAll('#length-radio-group .radio-card').forEach((card) => {
    card.addEventListener("click", () => {
      const input = card.querySelector('input[type="radio"]');
      if (!input) return;
      input.checked = true;

      document.querySelectorAll('#length-radio-group .radio-card').forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");

      const val = input.value;
      let text = profileEditor.value;

      // Update Structure & Length or Comment Length line
      const structRegex = /(- \*\*Structure & Length\*\*:).*/i;
      const lengthRegex = /(- \*\*Comment Length\*\*:).*/i;

      if (structRegex.test(text)) {
        text = text.replace(structRegex, `$1 ${val}`);
      } else if (lengthRegex.test(text)) {
        text = text.replace(lengthRegex, `- **Structure & Length**: ${val}`);
      } else {
        text += `\n- **Structure & Length**: ${val}`;
      }
      profileEditor.value = text;
    });
  });
});
