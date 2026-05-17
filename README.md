# Tatva — AI Wellness Companion Chatbot
### Complete Deployment, Integration & Configuration Guide

> **System Overview:** Tatva is a two-part, enterprise-grade customer support chatbot built for AdiShila (adishila.in), India's premium Karelian shungite wellness brand. It combines a deterministic menu-navigation engine with a Gemini AI fallback — achieving near-zero cost for the vast majority of interactions while retaining the warmth and flexibility of a live AI for complex, open-ended queries.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Feature Reference](#2-feature-reference)
3. [How the Hybrid Engine Works](#3-how-the-hybrid-engine-works)
4. [Security Measures](#4-security-measures)
5. [Cost Effectiveness](#5-cost-effectiveness)
6. [Deployment Configuration — Backend (`main.py`)](#6-deployment-configuration--backend-mainpy)
7. [Deployment Configuration — Frontend (`adishila_chatbot_v3.html`)](#7-deployment-configuration--frontend-adishila_chatbot_v3html)
8. [Migrating to a New Brand or Platform](#8-migrating-to-a-new-brand-or-platform)
9. [Environment Variables Reference](#9-environment-variables-reference)
10. [API Endpoint Reference](#10-api-endpoint-reference)
11. [Embedding the Widget Into a Website](#11-embedding-the-widget-into-a-website)
12. [Analytics & Observability](#12-analytics--observability)
13. [Unique Selling Points](#13-unique-selling-points)
14. [Dependency Installation](#14-dependency-installation)
15. [Known Constraints & Edge Cases](#15-known-constraints--edge-cases)

---

## 1. Architecture Overview

The system consists of exactly two files that communicate over HTTP:

```
┌──────────────────────────────────────┐        HTTPS / JSON
│   adishila_chatbot_v3.html           │ ──────────────────────► ┌──────────────────────────┐
│   (Frontend Widget)                  │                          │   main.py                │
│                                      │ ◄────────────────────── │   (FastAPI Backend)      │
│  • Floating chat button & panel      │                          │                          │
│  • Message rendering & pill UI       │                          │  • Session management    │
│  • Offline detection & queue         │                          │  • Deterministic router  │
│  • CSAT star-rating card             │                          │  • Gemini AI fallback    │
│  • Escalation card renderer          │                          │  • Rate limiting         │
│  • Zen audio notification            │                          │  • Analytics logging     │
└──────────────────────────────────────┘                          └──────────────────────────┘
                                                                           │
                                                                           ▼
                                                                  ┌─────────────────┐
                                                                  │  Google Gemini  │
                                                                  │  2.5 Flash API  │
                                                                  │  (AI Fallback)  │
                                                                  └─────────────────┘
```

**Communication flow for every message:**

1. Frontend sends `POST /chat` with `session_id`, `session_token`, `X-Widget-Key` header, and the user's message.
2. Backend validates the session token and API key.
3. Backend runs frustration detection, injection checks, and CSAT triggers.
4. Backend attempts deterministic routing first (zero AI cost).
5. If no deterministic match, message is forwarded to Gemini 2.5 Flash with a brand-constrained system prompt.
6. Backend returns a structured JSON response with a `mode` field (`structured`, `natural`, `escalation`, `csat`, `csat_complete`, `fallback`).
7. Frontend parses numbered lists into interactive pill buttons, strips nav markers, and renders the appropriate card type.

---

## 2. Feature Reference

### 2.1 Hierarchical Menu Navigation

The chatbot presents a seven-item main menu and context-aware sub-menus for Orders, Products, Recommendations, Wholesale, How to Use, Support, and FAQ. Navigation is managed as a stack (`nav_history`) so the user can always go **← Back** one level or jump to **⌂ Main Menu**.

**How to reproduce:** Send `"1"` through `"7"` (or natural-language equivalents like `"orders"`, `"wholesale"`) to navigate. Once inside a sub-menu, send the sub-item number or name. Send `"back"` or `"main menu"` at any time.

### 2.2 Fuzzy-Match Intent Routing

The router uses `rapidfuzz.fuzz.token_set_ratio` with an 85% threshold and `fuzz.ratio` at 80% for single-word inputs. This means users can type `"kavach"`, `"Kavach Shield"`, `"kavach sheld"` (typo), or even `"2"` in the products menu and all resolve correctly.

**Practical benefit:** Eliminates hard string matching failures that plague basic keyword bots without calling an LLM.

### 2.3 Gemini AI Fallback

When no deterministic intent matches, the backend initialises or reuses a `genai.Client` chat session and forwards the user message with:
- A language tag (`[RESPOND IN: en/hi/hinglish/ru]`)
- A brand-constrained constraint clause
- Conversation history maintained within the Gemini chat object

The chat object resets every 100 turns to prevent token bloat. API calls use `asyncio.wait_for` with a 25-second timeout and an exponential back-off retry loop (3 attempts, doubling delay).

### 2.4 Multilingual Support

Language detection is performed by the `lingua` library, covering **English, Hindi, Bengali, Marathi, Tamil, Telugu, Gujarati, Urdu, and Russian**. A supplementary Hinglish detector checks for common romanised Hindi markers (`kya`, `mujhe`, `nahi`, etc.) since `lingua` cannot detect transliteration.

The detected language code is injected into the Gemini prompt so the AI responds in the same language the user is writing in.

### 2.5 Frustration Detection & Auto-Escalation

A cumulative frustration counter (`frustration_signals`) tracks negative language signals across turns:

- **+1** for any word from the frustration word list (`worst`, `scam`, `fraud`, `bakwaas`, `consumer court`, etc.)
- **+1** for ALL-CAPS messages longer than 10 characters or triple exclamation marks
- **−1 decay** on every calm turn (prevents a single venting message permanently locking escalation)

When the counter reaches **≥ 2**, the bot immediately generates a unique ticket ID (`TKT-XXXXXXXX`) and hands off to the human support contact, rendering an escalation card in the frontend. Input is then disabled.

### 2.6 CSAT (Customer Satisfaction) Survey

After at least 3 turns, if the user sends a short gratitude message (≤ 8 words matching phrases like `"thank you"`, `"dhanyawad"`, `"problem solved"`, `"awesome"`), the backend returns `mode: "csat"`.

The frontend then shows an interactive five-star card. Clicking a star locks in the rating, renders a confirmation, and silently submits the rating string back to the backend, which logs it as a `csat_rating` analytics event.

**One-shot guarantee:** CSAT is prompted at most once per session (`csat_prompted` flag).

### 2.7 Duplicate Response Detection

The backend maintains a rolling deque of the last 20 MD5 hashes of response prefixes per session. If the same response would repeat, it appends a human-handoff suggestion. This prevents the bot from appearing stuck in a loop.

### 2.8 Prompt Injection Defence

Before any processing, every incoming message is scanned for injection phrases (`"ignore previous"`, `"system prompt"`, `"forget instructions"`, `"new instructions"`, etc.). Matching messages receive a polite deflection and are never forwarded to the AI.

### 2.9 Rate Limiting

`slowapi` enforces:
- `POST /session/start` → **10 requests/minute per IP**
- `POST /chat` → **20 requests/minute per IP**

An additional in-session speed check blocks users who trigger more than 15 unique intents within 60 seconds of session creation. When rate-limited, the frontend shows a countdown timer that unlocks the input box automatically.

### 2.10 Session Management with TTL

Sessions are stored in a `cachetools.TTLCache` with:
- **Capacity:** 5,000 concurrent sessions
- **TTL:** 3,600 seconds (1 hour of inactivity expiry)
- **Thread-safe access** via `threading.Lock`

Each session carries a cryptographically secure `session_token` (`secrets.token_urlsafe(32)`). Every chat request must present this token in the `X-Session-Token` header. A mismatch returns HTTP 403.

If the frontend receives a 401 (session expired), it automatically calls `/session/start` silently and retries the failed message — the user sees no interruption.

### 2.11 Offline Queue & Reconnection

The frontend listens to the browser's `online`/`offline` events. If the device goes offline mid-conversation, an orange banner appears and outgoing messages are queued in `state.messageQueue`. When connectivity is restored, the queue is drained automatically.

### 2.12 Pill-Button UI & Quick Actions

All numbered list items in backend responses (`1. Kavach Shield`) are parsed out of the text and rendered as interactive pill buttons inside the message bubble. Clicking a pill auto-sends that text as a user message — no typing required.

Navigation controls (`← Back`, `⌂ Main Menu`) and related-topic suggestions are rendered in a separate quick-action strip below the chat window, keeping the main bubble clean.

### 2.13 Related Topic Suggestions

A `RELATED` map in the backend cross-links intents. For example, viewing `track_order` also surfaces `shipping_info` and `cancel_order` as quick-action chips. This drives deeper engagement without any AI cost.

### 2.14 Contextual Typing Indicator

The frontend detects whether a message is likely a personal description (>60 characters, or contains wellness keywords like `"stressed"`, `"exhausted"`, `"overwhelmed"`). For these, it shows an animated cycling status (`"Understanding your situation..."`, `"Looking into your energy needs..."`) instead of plain dots. This is purely a frontend UX enhancement with no backend involvement.

### 2.15 Zen Audio Notification

A Web Audio API chime (432 Hz sine + 864 Hz overtone with exponential decay) plays when a new bot message arrives while the chat is closed, or when the AI fallback responds. No audio file is required — it is synthesised in-browser.

### 2.16 Rotating Log Files

Two log files are maintained:
- `server.log` — operational logs, max 5 MB × 3 backups
- `analytics.jsonl` — JSONL-format analytics events, max 10 MB × 5 backups

Both use `RotatingFileHandler` so the server never runs out of disk space from logs.

---

## 3. How the Hybrid Engine Works

```
User message received
        │
        ▼
Prompt injection check ──── MATCH ──► Return deflection (no AI, no cost)
        │
        ▼
CSAT awaiting check ──── TRUE ──► Log rating, return thank-you
        │
        ▼
Frustration score update
        │
        ├── score ≥ 2 ──► Generate ticket, return escalation card
        │
        ▼
CSAT trigger check ──── MATCH ──► Return CSAT prompt (no AI, no cost)
        │
        ▼
Deterministic intent router
        │
        ├── MATCH ──► Return static response + related links (ZERO AI cost)
        │
        └── NO MATCH ──► Gemini 2.5 Flash with brand system prompt
                              │
                              ├── Success ──► Dedup check ──► Return response
                              ├── Timeout (25s) ──► Exponential retry × 3
                              ├── 429/Quota ──► Return graceful fallback message
                              └── 5xx ──► HTTP error propagated to frontend
```

The vast majority of e-commerce support queries (order tracking, product info, shipping, FAQs) resolve deterministically. The AI is only invoked for novel, open-ended, or conversational messages — keeping Gemini API costs very low.

---

## 4. Security Measures

| Layer | Mechanism | Location |
|---|---|---|
| API authentication | `X-Widget-Key` header validated against `FRONTEND_SECRET_KEY` env var on every request | `main.py` — `verify_frontend_key()` |
| Session integrity | Per-session `session_token` (`secrets.token_urlsafe(32)`) in `X-Session-Token` header | `main.py` — `SessionState` |
| Input validation | Pydantic model: message 1–1,000 chars, session_id alphanumeric 8–64 chars | `main.py` — `ChatRequest` |
| Prompt injection defence | Blocklist scan before any processing | `main.py` — `check_prompt_injection()` |
| Rate limiting | IP-based via `slowapi` (10/min sessions, 20/min chat) | `main.py` — `@limiter.limit` |
| In-session speed limit | >15 unique intents in <60 seconds → HTTP 429 | `main.py` — chat endpoint |
| CORS whitelist | Only specific origins allowed | `main.py` — `allow_origins` list |
| Log rotation | Capped file sizes prevent disk exhaustion | `main.py` — `RotatingFileHandler` |
| Secret management | Secrets read from environment variables, never hardcoded | `main.py` — `os.getenv()` |
| Character limit | Frontend caps input at 1,000 chars with visual counter | `adishila_chatbot_v3.html` |
| Session auto-expiry | 1-hour TTL cache purges idle sessions from memory | `main.py` — `TTLCache` |

> **Important:** The `frontendKey` value in the HTML (`"Adishila"`) **must match** `FRONTEND_SECRET_KEY` on the server. For production, use a long, random string (minimum 32 characters). The current value is a placeholder.

---

## 5. Cost Effectiveness

### Why this architecture is extremely cheap to run

**Static responses handle ~85–95% of all traffic.** The `STATIC_RESPONSES` dictionary contains pre-written answers for every menu, every product, every FAQ, every order action, every usage instruction, and every wholesale query. These cost absolutely nothing to serve — they are simple dictionary lookups.

**Gemini 2.5 Flash is the most cost-efficient LLM available for fallback.** It is invoked only when a user asks something outside the structured menu — unusual questions, personal situations, or open-ended conversation. For a typical e-commerce support chatbot, this is a small minority of messages.

**Session state is in-memory.** No database is required. TTLCache holds up to 5,000 sessions; at an average session size of ~2 KB, peak memory usage is roughly 10 MB — negligible on any modern server.

**No vector database, no embeddings, no fine-tuning.** The deterministic router uses `rapidfuzz` (pure Python, no GPU, no inference cost) for fuzzy matching.

**Gemini chat context resets every 30 turns.** This prevents token counts from growing unbounded over long sessions, keeping per-session AI cost capped.

**Estimated running costs (illustrative):**

| Component | Cost model |
|---|---|
| Backend hosting (e.g., Render free/starter tier) | $0–$7/month |
| Gemini 2.5 Flash API | Pay-per-token, only on AI fallback calls (~5–15% of messages) |
| Frontend | Static HTML file — zero hosting cost when embedded in existing website |
| Database | None required |
| CDN/Fonts | Google Fonts & Tailwind CDN — free |

For a small-to-medium brand receiving a few hundred chat sessions per day, total monthly cost is typically **under $5**.

---

## 6. Deployment Configuration — Backend (`main.py`)

### 6.1 Values That Must Change Per Deployment

#### CORS Origins — Line ~75

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://tatva-chatbot.netlify.app", "https://adishila.in", "adishila.in"],
    # ↑ REPLACE with your own domain(s)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Replace `allow_origins` with an exact list of domains that will host your frontend widget. Wildcards (`"*"`) are strongly discouraged in production as they eliminate CORS protection.

#### AI Model — Line ~285 (inside `chat_endpoint`)

```python
state.chat = ai_client.chats.create(model="gemini-2.5-flash", config=chat_config)
```

Change `"gemini-2.5-flash"` to any other Gemini model string if you upgrade or need a different capability/cost tradeoff (e.g., `"gemini-2.0-flash"` for lower cost, or `"gemini-2.5-pro"` for more reasoning).

#### System Prompt / AI Persona — Lines ~88–106

```python
system_instruction = """
You are Tatva, the official multilingual AI Wellness Companion for AdiShila (adishila.in).
...
RIGID PRODUCT CATALOG (YOU MUST ONLY RECOMMEND THESE EXACT ITEMS):
- Kavach Shield
- Vastu Dosh Pyramid
...
"""
```

Replace the entire `system_instruction` string with your brand's persona, product catalog, and operational rules. This is the most important customisation for a new deployment.

#### Static Responses Dictionary — Lines ~185–310

The `STATIC_RESPONSES` dictionary contains every pre-written response. Each key maps to a menu item or intent. Update:
- All product names, prices, and descriptions
- Shipping terms and timelines
- Contact email and WhatsApp number
- Wholesale MOQs and margins
- FAQ content

#### Related Topics Map — Lines ~312–320

```python
RELATED = {
    "track_order": ["shipping_info", "cancel_order"],
    "prod_kavach": ["prod_trishul", "rec_emf"],
    ...
}
```

Update intent keys and values to reflect your own product/content links.

#### Deterministic Router — Lines ~323–430

The `get_deterministic_intent()` function maps user phrases to intent keys. If you add new menu items or change product names, add corresponding `is_match()` blocks here. The structure is `is_match(msg, ["phrase1", "phrase2", "number"])`.

#### Frustration Word List — Lines ~155–160

```python
FRUSTRATION_SIGNALS = [
    "worst", "useless", "scam", "fraud", "bakwaas", "bewakoof", ...
]
```

Extend or localise this list with slang or terms relevant to your user base and language.

#### Session Start Opening Message — Lines ~448–457

```python
return {
    "opening_message": "Welcome to your safe space. I am Tatva, your personal wellness guide..."
}
```

Change the opening message to match your bot's name and brand voice.

#### Log File Names — Lines ~52–58

```python
log_handler = RotatingFileHandler("server.log", ...)
analytics_handler = RotatingFileHandler("analytics.jsonl", ...)
```

Change file paths if your deployment environment requires logs in a specific directory (e.g., `/var/log/tatva/`).

#### Server Port — Last lines

```python
port = int(os.getenv("PORT", 8000))
uvicorn.run(app, host="0.0.0.0", port=port, timeout_keep_alive=30)
```

The port defaults to 8000 but reads from the `PORT` environment variable — most PaaS platforms (Render, Railway, Heroku) set this automatically.

### 6.2 Values That Are Fine to Leave Unchanged

- Rate limit values (`10/minute`, `20/minute`) — reasonable defaults for most deployments
- Session TTL (3600 seconds) and max sessions (5000) — tune only if memory is constrained
- Chat object reset interval (every 30 turns) — balances context retention with token cost
- Timeout values (25 seconds, 3 retries, exponential back-off) — sensible for Gemini Flash

---

## 7. Deployment Configuration — Frontend (`adishila_chatbot_v3.html`)

### 7.1 Values That Must Change Per Deployment

#### Backend API URL and Frontend Key — Lines ~258–264

```javascript
const CONFIG = {
    apiBase: "https://tatva-ai-customer-support-chatbot.onrender.com",
    // ↑ REPLACE with your backend's public URL
    frontendKey: "Adishila",
    // ↑ REPLACE with the exact value of FRONTEND_SECRET_KEY on your server
    minTypingMs: 2000,
    maxTypingMs: 3500,
    sessionRetries: 2
};
```

`apiBase` must point to wherever you deploy `main.py`. `frontendKey` must exactly match the `FRONTEND_SECRET_KEY` environment variable on the backend. **This key is visible in the HTML source**, so treat it as a shared secret (not a user-facing password) — it protects against casual scraping of your API, not a determined attacker.

#### Bot Name and Brand Text

Search the HTML for `"Tatva"` and replace with your bot's name. Key locations:
- `<title>Tatva Wellness Chat</title>` — browser tab title
- `.font-brand` div containing `"Tatva"` — header name
- `"Your Wellness Companion"` — header subtitle
- `"Welcome · स्वागत है"` — welcome heading (remove or translate the Hindi if not applicable)
- `"How can Tatva guide your wellness today?"` — welcome subtext
- All `info@adishila.in` and `+91 86301 79867` references — update to your contact details

#### Colour Scheme — Tailwind config block (~Lines 13–27)

```javascript
tailwind.config = {
    theme: {
        extend: {
            colors: {
                'Tatva-bg': '#12110A',       // Main background (near-black warm)
                'Tatva-surface': '#1C1A14',  // Card/bubble surface
                'Tatva-border': '#2A271E',   // Border colour
                'Tatva-gold': '#C4A462',     // Primary accent (gold)
                'Tatva-text': '#EAE6D9',     // Primary text (warm white)
                'Tatva-user': '#8C4A32',     // User message bubble
                'Tatva-escalation': '#2A1810'// Escalation card background
            }
        }
    }
}
```

Replace hex values to match your brand palette. `Tatva-gold` is the most prominent accent — it appears on all interactive elements.

#### OM Symbol & Hindi Text

The OM symbol (`ॐ`) and `"स्वागत है"` welcome text appear in the widget header area. Remove or replace these if your brand is not rooted in Indian wellness culture.

#### `transformText()` Function — Lines ~358–363

```javascript
function transformText(text) {
    let t = text || "";
    t = t.replace(/Please email info@adishila\.in with:/gi, "Just drop us an email...");
    t = t.replace(/AdiShila/gi, "Tatva");
    return t;
}
```

This function post-processes backend text on the frontend. Update or remove the regex replacements to match your brand name and preferred phrasing. This is useful when migrating a backend that still references the old brand name.

#### Loading Messages in Typing Indicator — Lines ~436–441

```javascript
const loadingMessages = [
    "Understanding your situation...",
    "Looking into your energy needs...",
    "Personalizing the best products for you...",
    "Preparing guidance..."
];
```

Customise these cycling status messages to suit your brand's tone.

#### Mobile Fullscreen Breakpoint — CSS ~Lines 80–90

```css
@media (max-width: 479px) {
    #as-panel { width: 100vw !important; height: 100vh !important; ... }
}
```

The widget goes fullscreen on viewports under 479px. Adjust this breakpoint if needed.

### 7.2 Optional Enhancements

- **Custom avatar icon:** Replace the SVG in the `#as-launcher` button and bot message avatars with your brand's logo SVG.
- **Custom fonts:** Replace the Google Fonts import with your brand typography.
- **Disable audio:** Remove or comment out the `playZenChime()` calls if audio notifications are not desired.
- **Widget position:** Change `bottom-6 right-6` Tailwind classes to reposition the launcher button.

---

## 8. Migrating to a New Brand or Platform

Follow this checklist in order when deploying Tatva for a different brand:

**Backend (`main.py`)**

- [ ] Set new `FRONTEND_SECRET_KEY` environment variable (use `secrets.token_urlsafe(32)` to generate one)
- [ ] Set new `GEMINI_API_KEY` environment variable
- [ ] Update `allow_origins` in the CORS middleware to your website's domain
- [ ] Rewrite `system_instruction` with your brand name, persona, and product catalog
- [ ] Replace all entries in `STATIC_RESPONSES` with your own content
- [ ] Update `FRUSTRATION_SIGNALS` with any brand/locale-specific terms
- [ ] Update `CSAT_TRIGGERS` if your user base speaks different languages
- [ ] Rebuild the `get_deterministic_intent()` router to match your new menu structure
- [ ] Update the `opening_message` in `/session/start`
- [ ] Change contact email/WhatsApp references throughout `STATIC_RESPONSES`

**Frontend (`adishila_chatbot_v3.html`)**

- [ ] Set `CONFIG.apiBase` to your backend URL
- [ ] Set `CONFIG.frontendKey` to match `FRONTEND_SECRET_KEY`
- [ ] Replace all `"Tatva"`, `"AdiShila"`, `"adishila.in"` text references
- [ ] Update colour scheme in the Tailwind config
- [ ] Remove/replace OM symbol and Hindi text if not applicable
- [ ] Update `transformText()` regex patterns
- [ ] Update loading messages and placeholder text
- [ ] Replace contact info in error messages (`info@adishila.in`)

---

## 9. Environment Variables Reference

These must be set in your hosting environment (e.g., Render dashboard, Railway variables, `.env` file with a secrets manager). **Never hardcode secrets in source files.**

| Variable | Required | Description |
|---|---|---|
| `FRONTEND_SECRET_KEY` | **Yes** | Shared secret between backend and frontend widget. Must match `frontendKey` in the HTML. Use a random string of at least 32 characters. |
| `GEMINI_API_KEY` | **Yes** | Your Google AI Studio API key for the Gemini API. Obtain from [aistudio.google.com](https://aistudio.google.com). |
| `PORT` | No | Port for the Uvicorn server. Defaults to `8000`. Most PaaS providers set this automatically. |

The server will **refuse to start** and throw a `RuntimeError` if either required variable is missing. This is intentional — silent deployment with missing keys would cause confusing runtime failures.

---

## 10. API Endpoint Reference

### `GET /health`

Health check. Returns `{"status": "ok", "timestamp": "<ISO datetime>"}`. Use this for uptime monitoring and load balancer health checks. No authentication required.

---

### `GET /`

Root check. Returns `{"status": "ok", "service": "Tatva Wellness API"}`. No authentication required.

---

### `POST /session/start`

**Rate limit:** 10/minute per IP

**Headers required:**
```
X-Widget-Key: <FRONTEND_SECRET_KEY>
```

**Response:**
```json
{
  "session_id": "uuid-string",
  "session_token": "secure-random-token",
  "opening_message": "Welcome to your safe space. I am Tatva..."
}
```

Call this once when the widget opens for the first time. Store both `session_id` and `session_token` in frontend state. `session_token` must be sent with every subsequent `/chat` request.

---

### `POST /chat`

**Rate limit:** 20/minute per IP

**Headers required:**
```
X-Widget-Key: <FRONTEND_SECRET_KEY>
X-Session-Token: <session_token from /session/start>
Content-Type: application/json
```

**Request body:**
```json
{
  "message": "string (1–1000 chars)",
  "session_id": "string (alphanumeric, 8–64 chars)"
}
```

**Response modes:**

| `mode` | Meaning | Frontend action |
|---|---|---|
| `"structured"` | Deterministic static response | Parse pills + nav + render text bubble |
| `"natural"` | AI-generated response | Same as structured, play chime |
| `"escalation"` | Frustration threshold hit | Render escalation card, disable input |
| `"fallback"` | AI quota exhausted | Render escalation card |
| `"csat"` | Gratitude trigger | Show "Is there anything else?" + pills |
| `"csat_complete"` | Post-rating thank-you | Render text bubble |

**Error codes:**

| Code | Meaning | Frontend behaviour |
|---|---|---|
| 401 | Session expired | Auto-retry with new session |
| 403 | Invalid `X-Widget-Key` or `X-Session-Token` | Show error message |
| 429 | Rate limited | Show countdown timer |
| 504 | Gemini timeout after 3 retries | Show connection error |
| 502 | Gemini API unavailable | Show connection error |
| 500 | Unhandled server error | Show generic error |

---

## 11. Embedding the Widget Into a Website

The widget is a self-contained HTML file. To embed it into an existing website, you have two options:

### Option A — Inline Script Embed (Recommended)

Copy the `<style>` block and entire `<script>` block from the HTML file. Paste the `<div id="as-widget">...</div>` markup and the scripts into your site's HTML just before `</body>`. This keeps the widget in a single page context with no iframe overhead.

### Option B — iframe Embed

Host the HTML file on any static server (GitHub Pages, Netlify, Vercel, Cloudflare Pages — all free). Then embed it as an invisible iframe:

```html
<iframe
  src="https://your-widget-host.netlify.app/adishila_chatbot_v3.html"
  style="position:fixed;bottom:0;right:0;width:480px;height:720px;
         border:none;background:transparent;z-index:9999;pointer-events:none;"
  allow="microphone"
  title="Tatva Support"
></iframe>
```

> **Note:** The iframe approach sacrifices the `pointer-events:none` pass-through on the wrapper, so users clicking behind the iframe area may be blocked. The inline embed is preferable for full control.

### Required CSS on host page

Add to your site's global CSS to prevent the widget from being clipped:

```css
body { position: relative; }
```

Ensure nothing on your page has `z-index` above `9999`, or increase the widget's `z-[9999]` value.

---

## 12. Analytics & Observability

Every chat turn writes a structured `ChatEvent` to `analytics.jsonl` as a JSON line. Fields logged per event:

| Field | Description |
|---|---|
| `event_type` | `"message"` or `"csat_rating"` |
| `session_id` | Anonymised session UUID |
| `mode` | `"structured"`, `"natural"`, `"csat"`, etc. |
| `intent` | Resolved intent key (e.g., `"prod_kavach"`, `"ai_fallback"`) |
| `language` | Detected language code (`en`, `hi`, `hinglish`, `ru`, etc.) |
| `msg_length` | Character count of user message |
| `response_time_ms` | End-to-end processing time in milliseconds |
| `frustration_level` | Cumulative frustration score at time of event |
| `turn_number` | Turn counter within the session |
| `ticket_id` | Set only on escalation events |

**Using the analytics data:**

- Monitor `intent` distribution to identify which products are browsed most
- Track `mode: "natural"` / `"ai_fallback"` ratio to understand what is not covered by static responses (and add new static entries for high-frequency AI fallback queries)
- Track `frustration_level` trends to identify systemic issues (e.g., a specific shipping delay period)
- Monitor `response_time_ms` to catch Gemini API latency spikes
- Use `language` distribution to prioritise localization efforts

**Parsing the log with Python:**

```python
import json

with open("analytics.jsonl") as f:
    events = [json.loads(line) for line in f]

# AI fallback rate
ai_turns = sum(1 for e in events if e["intent"] == "ai_fallback")
total_turns = len(events)
print(f"AI fallback rate: {ai_turns/total_turns:.1%}")

# Most popular intents
from collections import Counter
intent_counts = Counter(e["intent"] for e in events if e["intent"])
print(intent_counts.most_common(10))
```

---

## 13. Unique Selling Points

### vs. Generic LLM Chatbots (ChatGPT API wrappers, etc.)

Most chatbots built on raw GPT/Claude/Gemini APIs send every single message to the AI model. Tatva's hybrid router means **the AI is only called when necessary**, dramatically reducing cost and latency. Static responses are also perfectly consistent — the product name, price, and instructions are identical every time, whereas pure-LLM bots can hallucinate details.

### vs. Rule-Based Keyword Bots (Tidio, Freshdesk basic bots)

Keyword bots fail the moment a user types a synonym, a typo, or a question phrased differently. Tatva uses `rapidfuzz` fuzzy matching with an 85% threshold, allowing natural phrasing and typo tolerance without any AI cost. Users can type `"kavach sheld"` or `"track my parcel"` and get the right answer.

### vs. Full AI Agents (Intercom Fin, Zendesk AI)

Enterprise AI agents cost $0.99–$1.99 per resolved conversation. Tatva's architecture targets **near-zero per-message cost** — the AI fallback is cheap (Gemini Flash pricing) and rare. A small brand handling 500 conversations/month would pay ~$0–2 in AI costs versus $500–1,000 for Intercom Fin.

### vs. Stateless Chatbots

Most embedded chatbots are stateless — each message is independent. Tatva maintains full session state: navigation context, frustration history, CSAT status, conversation turns, and response hashes. This enables genuinely contextual multi-turn conversations without re-asking the user where they are.

### vs. English-Only Bots

Most SMB-tier chatbots are English-only or offer expensive paid localization. Tatva detects and responds in **9 languages including Hindi, Hinglish, and Russian** out of the box, powered by the `lingua` library and Gemini's multilingual capability. This is critical for an Indian brand with a diverse user base.

### Key Differentiating Features Summary

- **Hybrid deterministic + AI engine** — best of both cost and flexibility
- **In-session frustration tracking with decay** — sophisticated escalation that doesn't over-trigger
- **Contextual navigation stack** — back/forward within menus like a real app
- **Offline-first with message queue** — works on unreliable connections
- **Zero-dependency frontend** — single HTML file, no npm, no build step
- **CSAT baked in** — star rating without any third-party survey tool
- **Duplicate response detection** — prevents bot loop frustration
- **Prompt injection defence** — protects your brand's AI from being jailbroken in a customer-facing context

---

## 14. Dependency Installation

### Backend Python Dependencies

Install with pip:

```bash
pip install \
  fastapi \
  uvicorn[standard] \
  pydantic \
  google-genai \
  cachetools \
  rapidfuzz \
  slowapi \
  lingua-language-detector
```

Or create a `requirements.txt`:

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
pydantic>=2.0.0
google-genai>=1.0.0
cachetools>=5.3.0
rapidfuzz>=3.6.0
slowapi>=0.1.9
lingua-language-detector>=2.0.0
```

Then run: `pip install -r requirements.txt`

### Running the Backend

```bash
export FRONTEND_SECRET_KEY="your-secret-here"
export GEMINI_API_KEY="your-gemini-api-key-here"

# Development
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Frontend Dependencies

None. The HTML file uses:
- **Tailwind CSS** via CDN (`cdn.tailwindcss.com`) — no build step
- **Google Fonts** via CDN
- **Web Audio API** — built into all modern browsers

---

## 15. Known Constraints & Edge Cases

**`lingua` language detector initialises lazily.** The first message in a new deployment may take a second longer as the detector builds its model. This is cached after first use within the process lifetime.

**Gemini chat history is in-process memory only.** If the backend restarts (e.g., on a free-tier platform with sleep mode), active Gemini chat objects are lost. New AI messages will start fresh context. Static/deterministic responses are unaffected. Mitigate by keeping the server warm with a scheduled ping to `/health`.

**Free-tier platforms may sleep.** Services like Render's free tier sleep after 15 minutes of inactivity. The first session start after sleep has a cold-start delay (10–30 seconds). Users see the typing indicator during this time. The frontend will retry up to `sessionRetries` times automatically. Upgrade to a paid tier or use a keep-alive cron job to eliminate this.

**The `frontendKey` is visible in HTML source.** This is a deliberate trade-off — it prevents casual API scraping but not a determined attacker. For higher security: (a) serve the HTML from a password-protected area, (b) rotate the key periodically, or (c) implement IP-allowlisting on the backend.

**Session capacity is 5,000 concurrent.** At 1-hour TTL, this supports roughly 5,000 active users at once. For higher traffic, increase `maxsize` in `TTLCache` or migrate session storage to Redis.

**The fuzzy router does not understand semantics.** If a user asks `"What is EMF?"` it will match `rec_emf` correctly. But novel phrasing that doesn't appear in any `is_match()` call will always fall through to the AI. Regularly reviewing `analytics.jsonl` for high-frequency `ai_fallback` intents and converting them to static responses is the recommended maintenance practice.

---

