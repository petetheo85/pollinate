# Pollinate

Pollinate is a local-first workflow tool for drafting grounded, authentic responses to LinkedIn posts. It combines a Chrome Side Panel extension with a local Python backend and Gemini inference.

## Architecture & Design Decisions

- **Zero DOM Scraping or Injection**: The extension contains no content scripts, no hover overlays, and no automated DOM interaction with `linkedin.com`. This eliminates bot-detection risks and prevents account flags.
- **User-Initiated Selection Only**: Captures happen only when the user explicitly highlights post text and comments, right-clicks, and selects "Pollinate" (or presses `Alt+Shift+P`).
- **Local-Only Boundary**: Extension host permissions are strictly locked to `http://127.0.0.1/*`. The backend server binds solely to `127.0.0.1` and authenticates requests using a bearer token verified via constant-time comparison (`hmac.compare_digest`).
- **Single Source of Truth**: User profile, expertise, tone, and length constraints live in `context/profile.md`. Both the LLM prompt engine and the extension Options page read and write this file directly.
- **Manual Posting**: Pollinate handles analysis and response drafting. Copying and posting to LinkedIn remains an intentional, manual user action.

---

## Setup

### 1. Backend Environment

Clone the repository and install dependencies in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Generate a secure bearer token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Configure `.env`:

```ini
LLM_API_KEY=your_gemini_api_key
MODEL_NAME=gemini-3.8-flash
CAPTURE_TOKEN=your_generated_bearer_token
CAPTURE_PORT=8765
```

### 3. Profile Setup

Inspect or edit `context/profile.md` to define your identity, technical stack, voice preferences, and length constraints. You can also edit this directly from the extension Options page.

### 4. Start Pollinate

Start both the capture server and Streamlit dashboard together with a single command:

```bash
.venv/bin/python run.py
```

- FastAPI server binds to `http://127.0.0.1:8765`.
- Streamlit dashboard binds to `http://127.0.0.1:8501`.
- To run only the capture server without the dashboard, run `.venv/bin/python capture_server.py`.

### 5. Install Chrome Extension

1. Navigate to `chrome://extensions` in Google Chrome.
2. Enable **Developer mode** using the toggle in the top-right corner.
3. Click **Load unpacked** and select the `extension/` directory within this project.
4. Pin Pollinate to your browser toolbar: click the puzzle icon (Extensions) in Chrome's top bar, find Pollinate, and click the pin icon.

### 6. First-Time Extension Configuration

1. Right-click the Pollinate toolbar icon and select **Options** (or click the extension icon to open the Side Panel, then click the gear icon `⚙️`).
2. On first launch, the **Server** tab opens automatically:
   - Verify the port matches `CAPTURE_PORT` (default `8765`).
   - Paste the `CAPTURE_TOKEN` value configured in your `.env`.
   - Click **Save Settings**, then **Test Connection** to verify server connectivity.
3. Switch to the **Options** tab to customize your profile facts, tone presets, and draft length constraints.

> **Note:** If you edit files inside `extension/`, navigate back to `chrome://extensions` and click the reload button (`🔄`) on the Pollinate card to apply updates.

---

## Usage Workflow

1. **Highlight Post**: On LinkedIn, select the text of a post along with any visible comments you want considered for context.
2. **Trigger Pollinate**: Right-click the selection and choose **Pollinate** (or use the shortcut `Alt+Shift+P`).
3. **Review Analysis & Drafts**: The Chrome Side Panel opens with:
   - Author metadata, topic tags, and constructive relevance rationale.
   - Relevance and Opportunity scores (1–10).
   - Duplicate warning if you have already commented on this post.
   - Three distinct draft perspectives:
     - **Experience**: Practitioner observation grounded in your profile facts.
     - **Question**: Technical or strategic question advancing implementation.
     - **Counterpoint**: Polite, constructive alternative perspective or boundary condition.
4. **Refine**:
   - Edit any draft directly in the textarea.
   - Click **Simplify** to rewrite the drafts in punchier, down-to-earth language.
   - Enter custom steering guidance (e.g., *"Focus on latency trade-offs"*) and click **Regenerate**.
5. **Post & Track**:
   - Click **Copy to Clipboard**, paste into LinkedIn, and post manually.
   - Click **Mark as Posted** in the side panel to save your final comment and timestamp to history.
6. **Browse History & Author CRM**:
   - Click the dashboard icon (`📊`) in the side panel header (or **Open Dashboard** in Options) to review past comments, author interactions, and generate direct message outreach drafts.

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `LLM_API_KEY` | Yes | — | Google AI API key for Gemini. |
| `MODEL_NAME` | Yes | `gemini-3.8-flash` | Gemini model identifier. |
| `CAPTURE_TOKEN` | Yes | — | Bearer token authenticating extension requests (minimum 24 characters). |
| `CAPTURE_PORT` | No | `8765` | Local port for the FastAPI server. |
| `DB_PATH` | No | `pollinate.db` | Path to SQLite database, resolved against project root. |

---

## Testing

Run the test suite (excluding external network tests):

```bash
PYTHONPATH=. pytest -m "not integration"
```
