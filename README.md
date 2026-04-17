# Suno CSV Automation

Automate bulk song generation on [suno.com](https://suno.com) from a CSV file. Fill a spreadsheet with prompts, run the script, walk away — come back to a folder of MP3s.

Built with [Playwright](https://playwright.dev/python/) driving a real Chrome browser, so it handles login, session persistence, and Suno's dynamic UI.

## Features

- **CSV-driven batch generation** — one row = one Suno generation (2 clips).
- **Resume on interrupt** — progress is saved after every row. Stop with Ctrl-C, rerun the same command to continue.
- **File-based status tracking** — the script checks `downloads/` for existing MP3s on startup. Deleted a file? It'll regenerate that row automatically.
- **Interactive prompts** — pick your CSV, choose a row range (e.g. `1-50`), optionally append extra style tags to every prompt.
- **Persistent login** — log in to Suno once via Google/Discord/email; the session is saved to disk for all future runs.
- **Auto-retry on lost submits** — if a Create click doesn't register (no credits spent), the script retries up to `--max-retries` times before moving on.
- **Debug dumps on failure** — every error saves a full HTML + screenshot to `debug/` for troubleshooting selector changes.

## Requirements

- **Python 3.10+**
- **Google Chrome** (or Microsoft Edge) installed on your system
- **Suno account** (Pro plan recommended for bulk generation — free tier has low credit limits)

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/suno-automation.git
cd suno-automation
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

## Quick Start

### 1. Log in to Suno (one-time)

```bash
python suno_automation.py --login-only --use-system-chrome
```

A Chrome window opens. Sign in to Suno (Google, Discord, or email). Once you see the `/create` page, return to the terminal and press Enter. Your session is saved — you won't need to log in again.

### 2. Prepare your CSV

Create a CSV file (or copy `example_songs.csv`):

```csv
n,lyrics,styles,title,status
1,,"bouncy ska-punk, 150 BPM, bright trumpet, video game OST, instrumental",My Platformer Theme,
2,,"fast drum and bass, 174 BPM, amen break, PSX racing OST, instrumental",Racing Track,
```

| Column | Description |
|--------|-------------|
| `n` | Row number (used for `--start`/`--end` range filtering) |
| `lyrics` | Song lyrics. Leave empty for instrumental. Use `[Verse]`, `[Chorus]`, `[Intro]` etc. for structure. |
| `styles` | Comma-separated style tags for Suno's "Styles" field. 5-10 tags recommended. |
| `title` | Song title |
| `status` | Leave empty for new rows. Automatically set to `done` or `failed` by the script. |

### 3. Generate

```bash
python suno_automation.py --use-system-chrome
```

The script will:
1. Ask you to pick a CSV (if multiple `.csv` files exist in the directory).
2. Ask for a row range (e.g. `1-50`, or blank for all).
3. Ask for optional extra style tags to append to every prompt.
4. Open Chrome, navigate to Suno, and process each row.

Output MP3s land in `downloads/` by default.

## CLI Reference

| Flag | Description |
|------|-------------|
| `--use-system-chrome` | **Required for Google OAuth.** Launches your real Chrome install instead of Playwright's Chromium (which Google blocks). |
| `--csv PATH` | Use a specific CSV file instead of interactive selection. |
| `--start N` | Only process rows where `n >= N`. |
| `--end N` | Only process rows where `n <= N`. |
| `--out-dir PATH` | Output directory for MP3s (default: `downloads/`). |
| `--login-only` | Open browser for manual login, save session, then exit. |
| `--browse` | Open browser with saved session for manual inspection (check settings, version, etc.). |
| `--retry-failed` | Reset all `failed` rows back to pending before running. |
| `--redo TITLE` | Reset rows matching TITLE (case-insensitive substring) back to pending. |
| `--stop-on-error` | Halt on first failure. Leaves browser open for inspection. |
| `--max-retries N` | Retry a row up to N times if Create click doesn't submit (default: 2). Only retries when no credits were spent. |
| `--style-suffix TEXT` | Extra style tags appended to every row at generation time (CSV is not modified). |
| `--no-ask-suffix` | Skip the interactive style-suffix prompt. |
| `--delay N` | Seconds to wait between rows (default: 30). |
| `--gen-timeout N` | Per-song generation timeout in seconds (default: 300). |
| `--min-wait-after-click N` | Seconds to wait after clicking Create before polling for new clips (default: 15). |
| `--cdp-port N` | Chrome DevTools Protocol port (default: 9222). Change if another Chrome instance uses it. |
| `--headless` | Run headless. Not recommended — Suno may block headless browsers. |
| `--browser CHANNEL` | Browser channel when not using `--use-system-chrome`: `chrome`, `msedge`, or `chromium`. |

## How It Works

For each CSV row, the script:

1. **Navigates** to `suno.com/create`.
2. **Switches** to Advanced mode (the tab formerly called "Custom").
3. **Fills** Lyrics, Styles, and Title fields.
4. **Snapshots** existing workspace clips (to detect new ones later).
5. **Clicks Create** and waits 15 seconds for Suno to insert new rows.
6. **Polls** the workspace until 2 new clips appear and their duration labels show up (confirming audio is fully rendered).
7. **Downloads** the MP3s via Suno's CDN (with auth cookies) into `--out-dir`.

After each row, the CSV is updated with `status=done` or `status=failed`.

## Tips for Good Suno Prompts

- **5-10 style tags** is the sweet spot. More than 15 dilutes the signal.
- **Tag order matters**: genre first, mood second, instruments third, production last.
- **Include BPM** for rhythmic music: `140 BPM` is a strong tempo anchor.
- **"instrumental"** at the end prevents Suno from adding vocals.
- **Game music tags** like `video game OST`, `VGM`, `PSX`, `retro`, `arcade` are recognized by Suno and steer generation effectively.
- **Empty lyrics = short track.** For longer instrumentals, add structural metatags:
  ```
  [Intro]
  [Main Theme]
  [Variation]
  [Break]
  [Return]
  [Outro]
  ```
- **Suno v4.5** produces more consistent results for instrumentals (up to 8 min). **v5.5** has better audio quality but higher variance. Switch versions via the dropdown on suno.com/create.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Google login hangs on spinning bar | Use `--use-system-chrome`. Playwright's Chromium is blocked by Google. |
| "Chrome not found" error | Install Chrome, or set `CHROME_PATH=/path/to/chrome`. |
| Port 9222 conflict | Add `--cdp-port 9333`. |
| "No visible Create button" | Suno may have updated their UI. Check `debug/*.html` for the current DOM. |
| Songs are too short (20-30s) | Add `[Intro] [Main Theme] [Break] [Outro]` to lyrics. Include BPM in styles. Try v4.5. |
| "Restore pages?" dialog on Chrome startup | Normal — Chrome was terminated by the script. Dismiss it or ignore. |
| Row marked `done` but MP3 missing | Delete the status or the MP3 — on next run the script reconciles automatically. |

## Known Limitations

- **Suno UI selectors may break** when Suno updates their frontend. The script dumps full HTML + screenshots to `debug/` on every failure to help fix selectors.
- **2 clips per generation** — Suno always produces 2 variants per Create click. Both are downloaded.
- **CDN quality** — downloads are streaming-quality MP3s from Suno's CDN. For higher quality, use Suno's UI Download menu (the script attempts this as a fallback if CDN fails).
- **Rate limits** — Suno may silently drop a Create request under heavy load. The script detects this (no new clips appear) and retries safely.

## License

MIT
