# Social-Media-Automation-Suite

A local Python automation prototype for running controlled Threads browsing sessions through existing Chrome / NstBrowser-style browser profiles.

The project is designed around profile-based sessions, Selenium browser control, configurable action weights, persistent state, diagnostic runs, and a small Flask control panel for starting, stopping, and monitoring warmer processes.

> **Responsible-use notice**
>
> This project is intended for academic, research, and authorized testing purposes only. Use it only with accounts and browser profiles you own or are explicitly allowed to manage. Do not use it for spam, deception, fake engagement, harassment, platform manipulation, or activity that violates Threads, Meta, or any website’s terms of service. For production integrations, prefer official APIs and platform-approved workflows wherever available.

---

## Features

- Launches configured Chrome profiles with remote debugging enabled.
- Attaches Selenium to the running browser through Chrome DevTools Protocol.
- Runs Threads browsing sessions with a Markov-style action dispatcher.
- Supports passive scrolling, reading posts, search visits, profile views, notifications checks, return-to-top behavior, and optional controlled engagement actions.
- Includes optional post creation with daily quota and cooldown state.
- Stores per-profile runtime state in JSON.
- Supports single-session mode, daemon mode, and diagnostic action testing.
- Includes a separate cookie/profile pre-warming script.
- Provides a Flask dashboard for process control, status, state viewing, and live log streaming.
- Uses content pools from `pools.json` for comments, captions, search topics, preflight sites, and cookie session sites.

---

## Project structure

```txt
.
├── main.py                    # Main CLI entry point
├── config.py                  # Runtime configuration
├── api.py                     # Chrome profile launch / stop helpers
├── browser.py                 # Selenium connection and driver resolution
├── session.py                 # Main warm-up session loop and action dispatcher
├── actions.py                 # Threads interaction actions
├── posting.py                 # Post creation flow
├── state.py                   # Persistent per-profile state and posting quotas
├── daemon.py                  # Long-running scheduler
├── cookie_bot.py              # Cookie / browsing-history pre-warm sessions
├── diagnostics.py             # Single-action diagnostic runner
├── scroll.py                  # Scrolling and navigation helpers
├── mouse.py                   # Cursor movement and typing helpers
├── pools.py                   # Loads content pools from pools.json
├── dom_selectors.py           # Threads DOM selector helpers
├── warmer_flask/
│   ├── app.py                 # Flask control panel backend
│   ├── requirements.txt       # Flask dashboard dependencies
│   └── templates/
│       └── index.html         # Dashboard UI
└── pools.json                 # Local content pool file, not included by default
```

---

## Requirements

- Python 3.10+
- Google Chrome
- Existing local Chrome profiles or NstBrowser-managed profile folders
- Logged-in Threads sessions inside each configured profile
- `pools.json` content pool file
- ChromeDriver, NstBrowser bundled ChromeDriver, or `webdriver-manager`

Install the main dependencies:

```bash
pip install selenium requests webdriver-manager pyautogui pyperclip Pillow piexif python-dotenv
```

For the Flask dashboard:

```bash
pip install -r warmer_flask/requirements.txt
```

or manually:

```bash
pip install flask waitress
```

---

## Configuration

Most runtime settings live in `config.py`.

### 1. Configure browser profiles

Edit `CHROME_PROFILES` in `config.py`:

```python
CHROME_PROFILES = [
    {
        "id": "demo1",
        "port": 9222,
        "dir": r"C:\Users\User\AppData\Local\Google\Chrome\User Data\Profile 4",
    },
    {
        "id": "demo2",
        "port": 9223,
        "dir": r"C:\Users\User\AppData\Local\Google\Chrome\User Data\Profile 5",
    },
]
```

Each profile needs:

- `id`: local name used by the warmer.
- `port`: unique Chrome remote debugging port.
- `dir`: local Chrome user data directory.

Make sure each configured profile is already logged into Threads before running sessions.

### 2. Optional environment variables

Create a `.env` file if you want to override profile selection or content pool location:

```env
PROFILE_IDS=demo1,demo2
COOKIE_PROFILE_IDS=demo1,demo2
POOLS_JSON_PATH=./pools.json
WARMER_SCRIPT_DIR=.
```

### 3. Create `pools.json`

The project expects a local `pools.json` file. A minimal example:

```json
{
  "comments": [
    "Nice post",
    "Interesting point",
    "This is useful"
  ],
  "post_captions": [
    "Testing a simple update.",
    "Small update for today."
  ],
  "POST_CAPTION_SHORTS": [
    "Quick thought",
    "Daily note"
  ],
  "POST_CAPTION_EMOJIS": [
    "✨",
    "📌",
    "💭"
  ],
  "PREFLIGHT_SITES_POOL": [
    "https://www.wikipedia.org",
    "https://www.wikinews.org"
  ],
  "COOKIE_SITE_POOL": {
    "news": [
      "https://www.bbc.com",
      "https://www.reuters.com"
    ],
    "reference": [
      "https://www.wikipedia.org"
    ]
  },
  "search_topics": [
    "technology",
    "design",
    "business",
    "startups"
  ]
}
```

Do not commit private captions, personal data, real account identifiers, or sensitive media files if the repository will become public.

---

## Usage

### Run all configured profiles

```bash
python main.py
```

This starts each profile from `PROFILE_IDS`, attaches Selenium, performs the optional preflight browsing step, navigates to Threads, checks login status, runs a session, and then stops the profile.

### Run a single profile

```bash
python main.py --profile-id demo1
```

### Skip preflight browsing

```bash
python main.py --profile-id demo1 --no-preflight
```

### Run daemon mode

```bash
python main.py --daemon
```

Daemon mode keeps the scheduler running and manages each profile independently using persisted state.

### Run diagnostics

Run all diagnostic actions once:

```bash
python main.py --profile-id demo1 --test-actions
```

Run one specific action:

```bash
python main.py --profile-id demo1 --test-actions like
python main.py --profile-id demo1 --test-actions comment
python main.py --profile-id demo1 --test-actions search
python main.py --profile-id demo1 --test-actions post
```

Valid diagnostic aliases include:

```txt
passive
scroll
like
active
notifications
notify
profile
view_profile
follow
read
read_post
comment
search
home
click_home
top
return_top
post
create_post
```

---

## Action weight overrides

The CLI exposes optional per-action weight overrides:

```bash
python main.py \
  --profile-id demo1 \
  --like 0.15 \
  --comment 0.02 \
  --follow 0.01 \
  --search 0.08 \
  --post 0.00
```

Available flags:

```txt
--like
--notify
--profile
--read-post
--comment
--follow
--scroll
--search
--post
```

For public or demo use, keep engagement-related weights low or disabled, especially `--like`, `--comment`, `--follow`, and `--post`.

---

## Cookie pre-warm sessions

The repo also includes `cookie_bot.py`, which visits configured websites from `COOKIE_SITE_POOL`.

Run for one profile:

```bash
python cookie_bot.py --profile demo1
```

Run multiple cookie sessions:

```bash
python cookie_bot.py --profile demo1 --runs 3
```

Run for all configured cookie profiles:

```bash
python cookie_bot.py --all-profiles
```

Attach to an already-open browser:

```bash
python cookie_bot.py --attach 127.0.0.1:9222 --label demo1
```

---

## Flask control panel

Start the dashboard:

```bash
cd warmer_flask
python app.py
```

Open:

```txt
http://localhost:5000
```

The dashboard provides:

- Daemon start / stop controls.
- Single-session start / stop controls.
- Cookie session controls.
- Test-action controls.
- Process status.
- Heartbeat state.
- Post state.
- Cookie state.
- Live log streaming.

By default, the Flask app resolves the warmer script directory from:

```env
WARMER_SCRIPT_DIR
```

or falls back to the parent directory of `warmer_flask`.

---

## Runtime files

The project may generate local runtime files such as:

```txt
post_state.json
post_state.json.lock
post_state.json.tmp
post_state.json.bak
cookie_state.json
heartbeat.json
daemon.pid
logs/
screenshots/
media/
test_actions.log
nstbrowser_warmer.log
mouse_moves.log
```

These should generally be ignored in public repositories because they may contain account-specific state, logs, screenshots, or local machine paths.

Recommended `.gitignore` additions:

```gitignore
.env
pools.json

post_state.json
post_state.json.*
cookie_state.json
heartbeat.json
daemon.pid

logs/
screenshots/
media/
*.log

__pycache__/
*.pyc
.venv/
venv/
```

---

## Public release checklist

Before making this repository public:

- Remove real profile IDs.
- Remove real local Chrome profile paths.
- Remove real captions, comments, search topics, and media.
- Remove screenshots and logs.
- Remove `.env` files.
- Remove generated state JSON files.
- Add a sanitized `pools.example.json`.
- Add a `.gitignore`.
- Review the code for private account names, tokens, paths, or personal data.
- Keep the responsible-use notice in the README.
- Consider disabling engagement actions by default for demo/public use.

---

## Troubleshooting

### Chrome executable not found

Update `_CHROME_CANDIDATES` in `api.py` with your Chrome executable path.

Example Windows path:

```python
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]
```

### ChromeDriver version mismatch

The project tries to resolve ChromeDriver in this order:

1. NstBrowser bundled driver.
2. `webdriver-manager`.
3. System `chromedriver` from `PATH`.

Install `webdriver-manager` if needed:

```bash
pip install webdriver-manager
```

### Profile appears logged out

Open the configured Chrome profile manually and log into Threads. Then run the warmer again.

### `pools.json` not found

Either place `pools.json` next to the main scripts or set:

```env
POOLS_JSON_PATH=/absolute/path/to/pools.json
```

### Flask dashboard cannot find `main.py`

Set:

```env
WARMER_SCRIPT_DIR=/absolute/path/to/nstbrowser-threads-warmer
```
