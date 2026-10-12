/**
 * Pollinate Chrome Extension - Options Page Logic
 */

document.addEventListener("DOMContentLoaded", () => {
  const portInput = document.getElementById("server-port");
  const tokenInput = document.getElementById("capture-token");
  const form = document.getElementById("settings-form");
  const testButton = document.getElementById("test-button");
  const statusBanner = document.getElementById("status-banner");

  function showStatus(message, isSuccess) {
    statusBanner.textContent = message;
    statusBanner.className = isSuccess ? "success" : "error";
  }

  // Load existing configuration
  chrome.storage.sync.get({ port: 8765, token: "" }, (items) => {
    portInput.value = items.port || 8765;
    tokenInput.value = items.token || "";
  });

  // Save settings
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const port = parseInt(portInput.value, 10);
    const token = tokenInput.value.trim();

    if (!port || port < 1 || port > 65535) {
      showStatus("Please enter a valid port number between 1 and 65535.", false);
      return;
    }

    if (!token) {
      showStatus("Capture token cannot be empty.", false);
      return;
    }

    chrome.storage.sync.set({ port, token }, () => {
      showStatus("Settings saved successfully.", true);
    });
  });

  // Test connection to local capture server
  testButton.addEventListener("click", async () => {
    const port = parseInt(portInput.value, 10) || 8765;
    const token = tokenInput.value.trim();

    if (!token) {
      showStatus("Please enter a token before testing connection.", false);
      return;
    }

    showStatus("Connecting to 127.0.0.1:" + port + "...", true);

    try {
      const response = await fetch(`http://127.0.0.1:${port}/count`, {
        method: "GET",
        headers: {
          "X-Capture-Token": token,
        },
      });

      if (response.status === 200) {
        showStatus("Connection successful! Pollinate server is online and ready.", true);
      } else if (response.status === 401) {
        showStatus("Connection failed: 401 Unauthorized (invalid capture token).", false);
      } else {
        showStatus(`Server returned error status: ${response.status}`, false);
      }
    } catch (err) {
      showStatus(`Cannot connect to server at 127.0.0.1:${port}. Is pollinate server running?`, false);
    }
  });
});
