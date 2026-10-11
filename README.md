# Pollinate

Pollinate is a local workflow tool for engaging thoughtfully with LinkedIn posts in the AI engineering space (clip → review → copy & post manually).

## Design & Constraints

Pollinate is intentionally designed with strict constraints:
- **No browser automation:** No Selenium, Playwright, Puppeteer, or automated form filling. No interaction with LinkedIn's DOM or internal APIs.
- **User-triggered, selection-only:** Every capture happens solely when you highlight text on a post and explicitly click the browser extension menu.
- **Manual posting step:** Approved comments are copied to your clipboard and opened in a tab for you to paste and post yourself.
- **Why?** Grounded human judgment matters. Automation risks spam, account bans, and hallucinated comments. Pollinate handles the cognitive load of drafting and tracking while keeping you entirely in control.

---

## Configuration

Settings follow strict engineering principles:
1. **Single Reader:** Only `config.py` touches the environment. All other modules import from `config`.
2. **Precedence:** Real process environment variables override `.env`, which overrides defaults in `config.py`.
3. **No Drift:** Non-secret tuning constants (weights, retry limits, thresholds, SQLite timeouts) live directly in `config.py` rather than `.env`.
4. **Shared Database Path:** Relative database paths resolve against the project root (`PROJECT_ROOT`), ensuring both the capture server and Streamlit app always share the exact same SQLite database.

### Supported Provider

The application currently supports Gemini via Google's SDK (`google-genai`).
* `LLM_API_KEY`: A Google AI API key.
* `MODEL_NAME`: Must be a model supported by the Gemini API. Refer to the [Gemini models documentation](https://ai.google.dev/gemini-api/docs/models).
* Other providers are not supported yet.

### Environment Variables

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `LLM_API_KEY` | Yes | Google AI API key for Gemini. |
| `MODEL_NAME` | Yes | Model identifier supported by the Gemini API. |
| `CAPTURE_TOKEN` | Yes | Bearer token authenticating capture requests. Generate below. |
| `CAPTURE_PORT` | Optional | Local port for FastAPI capture server (default: `8765`). |
| `DB_PATH` | Optional | SQLite database path (default: `pollinate.db`, resolved to project root). |

### Generating the Capture Token

Generate a secure 32-byte token with Python:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
Paste this value into `.env` as `CAPTURE_TOKEN`.

> **Important:** The browser extension runs inside Chrome and cannot read `.env`. You must enter the exact same `CAPTURE_PORT` and `CAPTURE_TOKEN` into the extension Options page. If the extension badge shows an exclamation mark (`!`), a port or token mismatch is the first thing to check.
