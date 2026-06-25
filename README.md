# Social Media Automation Suite

A local operations dashboard for authorized creator and business profiles. It connects Selenium to existing NstBrowser-managed Chrome profiles, runs configured profile operations, publishes approved content, and records session reports for review.

> Responsible use
>
> Use this project only with profiles you own or are explicitly authorized to manage. It is not intended for account creation, scraping, spam, fake engagement, deceptive personalization, or platform bypasses. Follow the platform's terms and prefer official APIs for production integrations.

## What it provides

- Existing-profile startup and Selenium attachment through Chrome DevTools Protocol.
- Configurable profile sessions, scheduling, persistent state, diagnostics, and posting quotas.
- Approved caption and reply pools with fail-closed publishing controls.
- Deterministic caption preparation without invented wording or random embellishment.
- Optional brand-voice limits for tone, emojis, hashtags, banned terms, and explicitly allowed abbreviations.
- CSV session reports and a local Flask dashboard for process controls and audit visibility.

The suite does not generate captions or replies. It selects one complete entry from the applicable approved pool, prepares its formatting, validates it, and either publishes it unchanged or rejects it with a policy reason.

## Project structure

```text
.
├── main.py                 # CLI entry point
├── config.py               # Runtime and brand-voice configuration
├── content_policy.py       # Caption/reply preparation and validation
├── pools.py                # Approved content-pool loading and profile lookup
├── posting.py              # Approved publishing flow
├── actions.py              # Profile operations and reply controls
├── session.py              # Session dispatcher
├── state.py                # Persistent profile and posting state
├── reporting.py            # CSV session reports
├── diagnostics.py          # Diagnostic action runner
├── api.py                  # NstBrowser profile startup and stop helpers
├── browser.py              # Selenium connection handling
├── warmer_flask/           # Local operations dashboard
└── pools.json              # Private approved content (not committed)
```

## Requirements

- Python 3.10+
- NstBrowser and Google Chrome
- Existing, authorized profiles with active platform sessions
- A local `pools.json`
- A compatible ChromeDriver

Install the runtime dependencies:

```bash
pip install selenium requests webdriver-manager pyautogui pyperclip Pillow python-dotenv
pip install -r warmer_flask/requirements.txt
```

## Configuration

The dashboard writes private runtime settings to `operations_ui_config.json`. You can also use a `.env` file:

```env
NSTBROWSER_API_KEY=your-local-api-key
PROFILE_IDS=profile-id-1,profile-id-2
POOLS_JSON_PATH=./pools.json
OPERATIONS_SCRIPT_DIR=.
```

### Approved content pools and media

Create `pools.json` next to the main scripts:

```json
{
  "defaults": {
    "approved_replies": [
      "Thank you for sharing this perspective."
    ],
    "approved_captions": [
      "A clear process gives creative teams more time for meaningful work."
    ],
    "approved_media": [],
    "search_topics": [
      "creator business operations",
      "content strategy"
    ]
  },
  "profiles": {
    "profile-id-1": {
      "approved_replies": [
        "This is a useful point for teams planning their next release."
      ],
      "approved_captions": [
        "Our latest product update is now available to customers."
      ],
      "approved_media": [
        "profile-id-1/product-update.jpg",
        "profile-id-1/launch-note.png"
      ],
      "search_topics": [
        "creator operations"
      ]
    },
    "profile-id-2": {
      "approved_replies": [],
      "approved_captions": [
        "A new customer story is available today."
      ],
      "approved_media": [
        "profile-id-2/customer-story.webp"
      ]
    }
  },
  "PREFLIGHT_SITES_POOL": [
    "https://www.wikipedia.org",
    "https://www.wikinews.org"
  ]
}
```

Each caption or reply must be a complete, independently approved message. Do not place fragments in these pools; the publishing path never combines entries.

Profile entries are exact overrides. If a profile defines an empty list, that content type is intentionally disabled for that profile. If a key is omitted from a profile entry, the value falls back to `defaults`, then to the legacy top-level keys if present.

Approved media paths are always relative to `MEDIA_POOL_DIR`, which defaults to the local `media/` folder:

```text
media/
├── profile-id-1/
│   ├── product-update.jpg
│   └── launch-note.png
└── profile-id-2/
    └── customer-story.webp
```

Supported media extensions are `.jpg`, `.jpeg`, `.png`, and `.webp`. Paths that are absolute, leave the media folder, have unsupported extensions, or do not exist are rejected before publishing. Used media is tracked per profile as `used_media` in `post_state.json`.

Legacy `comments` and `post_captions` keys are read only to support local configuration migration. New configurations should use `approved_replies` and `approved_captions`.

### Brand voice and content policy

Brand voice is optional and can be edited in the dashboard or added to `operations_ui_config.json`:

```json
{
  "brand_voice": {
    "tone": "clear and professional",
    "max_emojis": 1,
    "max_hashtags": 3,
    "banned_terms": ["guaranteed results", "limited-time miracle"],
    "allowed_abbreviations": ["API", "SaaS"]
  }
}
```

The `tone` value documents the intended editorial standard. The remaining values are enforced by validation. Text is rejected when it is empty, emoji-only, spam-oriented, excessively repetitive, over the configured emoji or hashtag limits, contains banned or unapproved casual terms, or appears accidentally concatenated.

`prepare_caption_for_publishing()` performs deterministic Unicode and whitespace cleanup. `validate_caption()` and `validate_reply()` return a boolean; detailed policy functions are available for audit messages. Preparation does not add emojis, hashtags, slang, personalization, or new copy.

## Usage

Run all configured profiles:

```bash
python main.py
```

Run one authorized profile:

```bash
python main.py --profile-id PROFILE_ID
```

Run the scheduler:

```bash
python main.py --daemon
```

Run diagnostics:

```bash
python main.py --profile-id PROFILE_ID --test-actions
python main.py --profile-id PROFILE_ID --test-actions post
python main.py --profile-id PROFILE_ID --test-actions comment
```

The internal `comment` action name is retained for CLI and report compatibility; its publishing path uses only approved replies that pass reply controls.

## Operations dashboard

Start the local dashboard:

```bash
cd warmer_flask
python app.py
```

Open `http://localhost:5000`. The dashboard provides:

- Profile startup and stop controls.
- Single-session, scheduler, and diagnostic controls.
- Brand-voice and action configuration.
- Schedule and heartbeat state.
- Profile-operation logs.
- Downloadable run, session, action, and diagnostic reports.

Existing `WARMER_SCRIPT_DIR` and `warmer_ui_config.json` values are read as migration fallbacks. New settings use `OPERATIONS_SCRIPT_DIR` and `operations_ui_config.json`.

## Validation tests

Run the policy tests without browser startup or private credentials:

```bash
python -m unittest discover -s tests -v
```

## Runtime data and auditability

Local runs may create:

```text
operations_ui_config.json
profile_operations.log
post_state.json
heartbeat.json
reports/*.csv
logs/
screenshots/
media/
```

These files can contain profile identifiers, approved copy, local paths, screenshots, or operational history. Keep them private and excluded from source control.

Before publishing the repository, remove credentials, profile identifiers, private content pools, media, logs, screenshots, reports, and generated state. Keep the responsible-use notice and use sanitized examples only.
