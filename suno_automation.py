"""Suno.com song generation automation via Playwright.

Reads a CSV file with columns (n, lyrics, styles, title) and for each row:
  1. Opens suno.com/create in Advanced mode.
  2. Fills the Lyrics, Styles, and Title fields.
  3. Clicks Create and waits for generation to complete.
  4. Downloads the produced MP3 clips via Suno's CDN.

Session is persisted via Chrome's user-data-dir, so you log in once and
subsequent runs reuse the saved cookies. Progress is tracked via a
'status' column in the CSV — interrupted runs resume where they left off.

Works on Windows, macOS, and Linux. Requires a real Chrome (or Edge)
install — Playwright's bundled Chromium is blocked by Google OAuth.

See README.md for full usage instructions.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

SUNO_BASE = "https://suno.com"
CREATE_URL = f"{SUNO_BASE}/create"

DEFAULT_CSV = Path("songs.csv")
DEFAULT_SESSION_DIR = Path("playwright_session")
DEFAULT_DOWNLOAD_DIR = Path("downloads")
DEFAULT_DELAY_S = 30
DEFAULT_GEN_TIMEOUT_S = 300  # 5 min per song
DEFAULT_CDP_PORT = 9222
DEFAULT_MIN_WAIT_AFTER_CLICK_S = 15
DEBUG_DIR = Path("debug")

# Common Chrome install locations — platform-adaptive.
_CHROME_PATHS: tuple[str, ...] = ()
if sys.platform == "win32":
    _CHROME_PATHS = (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    )
elif sys.platform == "darwin":
    _CHROME_PATHS = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
else:  # Linux and others
    _CHROME_PATHS = (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    )

STATUS_COL = "status"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

ORDINAL_COL = "n"
REQUIRED_COLS = ("lyrics", "styles", "title")
# Full canonical header order for write-back: n first, then content, status last.
CANONICAL_HEADER = (ORDINAL_COL, "lyrics", "styles", "title", STATUS_COL)

log = logging.getLogger("suno")


class NoClipsAppearedError(PlaywrightTimeoutError):
    """Raised when Create was clicked but zero new clips appeared in the
    workspace. Safe to retry — no credits were spent (submit never
    reached Suno's backend). Distinct from a phase-2 timeout where clips
    appeared but never finished (those DO cost credits)."""
    pass


# -----------------------------------------------------------------------------
# CSV state
# -----------------------------------------------------------------------------

@dataclass
class Row:
    index: int          # 0-based position in the file
    n: int              # 1-based ordinal from the 'n' column — what user references
    lyrics: str
    styles: str
    title: str
    status: str


def choose_csv_interactively(working_dir: Path) -> Path:
    """Pick a .csv from working_dir. Always prompts — even with a single CSV
    (confirmation) — so the user sees what they're about to process.
    """
    csvs = sorted(
        p for p in working_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".csv"
    )
    if not csvs:
        raise FileNotFoundError(f"No .csv files in {working_dir.resolve()}")
    print()
    print("CSV files available in this directory:")
    for i, c in enumerate(csvs, 1):
        try:
            import csv as _csv
            with c.open(encoding="utf-8", newline="") as f:
                row_count = sum(1 for _ in _csv.DictReader(f))
        except Exception:
            row_count = -1
        rc = f"{row_count} rows" if row_count >= 0 else "?"
        print(f"  [{i}] {c.name}  ({rc})")
    while True:
        ans = input(f"> select 1..{len(csvs)} [default 1]: ").strip()
        if not ans:
            return csvs[0]
        try:
            idx = int(ans)
            if 1 <= idx <= len(csvs):
                return csvs[idx - 1]
        except ValueError:
            pass
        print("  invalid; enter a number from the list")


def ask_range_interactively(total_rows: int) -> tuple[Optional[int], Optional[int]]:
    """Prompt for a row range, e.g. '1-50' or '20-' or empty for all."""
    print()
    print(f"CSV has {total_rows} rows (ordinal 'n' column).")
    print("Range examples:  1-50   (rows 1..50)")
    print("                 20-    (row 20 to end)")
    print("                 75     (only row 75)")
    print("                 (blank)  (everything)")
    raw = input("> range: ").strip()
    if not raw:
        return None, None
    if "-" in raw:
        a, _, b = raw.partition("-")
        start = int(a) if a.strip() else None
        end = int(b) if b.strip() else None
    else:
        start = end = int(raw)
    return start, end


def ask_style_suffix_interactively() -> str:
    """Prompt for extra STYLE TAGS appended to every row's Suno 'Styles' field.

    NOT a filename suffix. This text is typed into Suno's Styles input
    alongside the CSV's existing styles, applied only in memory (CSV is
    never modified).
    """
    print()
    print("Extra STYLE TAGS to append to the Suno 'Styles' field for every row.")
    print("(This goes into the music prompt, NOT the saved filename.)")
    print("  e.g.   long song, 3 minute length, extended outro")
    print("         lofi production, tape warmth")
    print("  Leave blank = append nothing (recommended for curated CSVs).")
    raw = input("> extra style tags: ").strip()
    return raw


def load_rows(csv_path: Path) -> tuple[list[Row], list[str]]:
    """Load rows from CSV. Auto-migrates older schemas that lack 'n' / 'status'."""
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        missing = [c for c in REQUIRED_COLS if c not in fieldnames]
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        needs_migration = ORDINAL_COL not in fieldnames
        if needs_migration:
            log.info("CSV has no '%s' column — adding and auto-numbering.", ORDINAL_COL)
            fieldnames = [ORDINAL_COL] + fieldnames
        if STATUS_COL not in fieldnames:
            fieldnames.append(STATUS_COL)

        rows: list[Row] = []
        for i, raw in enumerate(reader):
            raw_n = (raw.get(ORDINAL_COL) or "").strip()
            try:
                n_val = int(raw_n) if raw_n else i + 1
            except ValueError:
                n_val = i + 1
            rows.append(
                Row(
                    index=i,
                    n=n_val,
                    lyrics=raw.get("lyrics", "") or "",
                    styles=raw.get("styles", "") or "",
                    title=raw.get("title", "") or "",
                    status=(raw.get(STATUS_COL) or "").strip(),
                )
            )

    if needs_migration:
        # Persist the migration so the user sees the new column next time.
        write_rows(csv_path, rows, fieldnames)

    return rows, fieldnames


def write_rows(csv_path: Path, rows: list[Row], fieldnames: list[str]) -> None:
    """Atomically rewrite CSV. Always writes the canonical column order."""
    tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    # Preserve any extra user columns in fieldnames, but force known order for ours.
    known = set(CANONICAL_HEADER)
    extra = [c for c in fieldnames if c not in known]
    header = list(CANONICAL_HEADER) + extra
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for r in rows:
            row_dict = {
                ORDINAL_COL: r.n,
                "lyrics": r.lyrics,
                "styles": r.styles,
                "title": r.title,
                STATUS_COL: r.status,
            }
            for c in extra:
                row_dict[c] = ""
            writer.writerow(row_dict)
    tmp.replace(csv_path)


def pending(rows: Iterable[Row]) -> list[Row]:
    return [r for r in rows if r.status != STATUS_DONE]


# -----------------------------------------------------------------------------
# Browser / session
# -----------------------------------------------------------------------------

def _find_chrome_exe() -> Path:
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    for p in _CHROME_PATHS:
        if p and Path(p).is_file():
            return Path(p)
    for name in ("chrome", "chrome.exe", "google-chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise RuntimeError(
        "Chrome not found. Install Chrome from https://google.com/chrome, "
        "or set the CHROME_PATH env var to point at chrome.exe."
    )


def _wait_for_cdp(port: int, timeout_s: int = 30) -> None:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return
        except Exception as e:
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(
        f"Chrome DevTools port {port} never opened (last error: {last_err!r}). "
        "Is another Chrome instance blocking the user-data-dir? Close all Chrome "
        "windows or pass a different --cdp-port / --session-dir."
    )


def spawn_chrome_cdp(session_dir: Path, port: int) -> subprocess.Popen:
    """Spawn the system Chrome with DevTools Protocol enabled.

    Why not just use launch_persistent_context(channel='chrome')?
    Because Playwright's launcher adds '--enable-automation' and related
    flags that Google's sign-in flow detects and blocks with the
    'This browser or app may not be secure' error. By spawning Chrome
    ourselves via subprocess we omit those flags; Chrome appears to
    Google as an ordinary user session that simply has DevTools open.

    The resulting profile lives in session_dir (a regular Chrome user-data
    directory) so cookies / storage persist across runs.
    """
    chrome = _find_chrome_exe()
    session_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={session_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        # Suppress the 'Chrome didn't shut down correctly / Restore pages?'
        # dialog after we TerminateProcess() the Chrome subprocess.
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
    ]
    log.info("Launching system Chrome: %s (cdp port %d)", chrome.name, port)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_cdp(port)
    except Exception:
        proc.terminate()
        raise
    return proc


def acquire_context(
    pw: Playwright, args: argparse.Namespace
) -> tuple[BrowserContext, Callable[[], None]]:
    """Get a BrowserContext plus a cleanup closure, honoring --use-system-chrome."""
    if args.use_system_chrome:
        proc = spawn_chrome_cdp(args.session_dir, args.cdp_port)
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()

        def cleanup() -> None:
            try:
                browser.close()  # disconnects CDP; does NOT kill Chrome.
            except Exception:
                pass
            try:
                proc.terminate()  # let Chrome flush cookies to disk.
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        return ctx, cleanup

    ctx = launch_context(pw, args.session_dir, headless=args.headless, channel=args.browser)

    def cleanup_launched() -> None:
        try:
            ctx.close()
        except Exception:
            pass

    return ctx, cleanup_launched


def launch_context(
    pw: Playwright,
    session_dir: Path,
    headless: bool,
    channel: str = "chrome",
) -> BrowserContext:
    """Launch a persistent browser context.

    Default channel='chrome' uses the real Chrome installed on the system,
    not Playwright's bundled Chromium. This matters because Google's sign-in
    flow silently hangs on bundled Chromium (bot detection) — OAuth gets
    stuck on a spinning progress bar after the email step. Real Chrome
    passes those checks. Same applies to 'msedge' on Windows.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = dict(
        user_data_dir=str(session_dir),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1440, "height": 900},
    )
    if channel == "chromium":
        # Bundled Chromium — Google will likely block OAuth here.
        kwargs["user_agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    else:
        kwargs["channel"] = channel
    return pw.chromium.launch_persistent_context(**kwargs)


def _on_create_page(page: Page) -> bool:
    """True iff the browser actually sits on suno.com/create (no auth redirect).

    URL-based: when Suno hasn't authenticated us it bounces to its own
    marketing page or an OAuth provider (Google, Discord, etc.). When it
    has, we stay on /create. This is far more reliable than scraping for a
    'Sign in' button.
    """
    url = page.url
    return url.startswith("https://suno.com/create") or url.startswith(
        "https://www.suno.com/create"
    )


def ensure_logged_in(page: Page, interactive: bool = True) -> None:
    """Navigate to /create; if we got redirected away, walk the user through login."""
    try:
        page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        pass  # SPA may still be resolving auth; we'll check URL below.

    # Give the SPA a moment to settle / decide on redirect.
    page.wait_for_timeout(3000)

    if _on_create_page(page):
        log.info("Session reused — on %s.", page.url)
        return

    log.info("Not authenticated (current URL: %s).", page.url)
    if not interactive:
        raise RuntimeError(
            "Not logged in. Run once with --login-only to authenticate, then retry."
        )

    log.info("Complete login in the opened browser window.")
    log.info("Wait until you're looking at the Suno 'Create' page with the composer visible,")
    log.info("THEN come back here and press Enter. Do not rush.")
    input(">>> Press Enter only when you see the /create page in the browser... ")

    # Re-check. Don't re-goto — a fresh navigation mid-OAuth can race.
    page.wait_for_timeout(2000)
    if not _on_create_page(page):
        # Last-ditch: try a clean navigation now that OAuth should be done.
        try:
            page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(2000)

    if not _on_create_page(page):
        raise RuntimeError(
            f"Still not on /create (url={page.url}). Aborting — log in fully, then rerun."
        )
    log.info("Login confirmed. Session persisted to %s.", page.url)


# -----------------------------------------------------------------------------
# Suno page actions — STUBS. Fill in after DOM inspection via codegen.
# -----------------------------------------------------------------------------

def dump_page_state(page: Page, label: str) -> Optional[Path]:
    """Save full HTML + full-page screenshot of the current page state.

    Files land in ./debug/<timestamp>_<slug>.{html,png}. Both ALWAYS saved
    (as far as we can) — one without the other is half a picture.
    Returns the path stem if it succeeded at least partially, else None.
    """
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
    except Exception as e:
        log.warning("  dump: cannot create %s: %s", DEBUG_DIR, e)
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40] or "page"
    stem = DEBUG_DIR / f"{ts}_{slug}"
    ok_any = False
    try:
        (stem.with_suffix(".html")).write_text(page.content(), encoding="utf-8")
        ok_any = True
    except Exception as e:
        log.warning("  dump: HTML failed: %s", e)
    try:
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        ok_any = True
    except Exception as e:
        log.warning("  dump: screenshot failed: %s", e)
    if ok_any:
        try:
            log.info("  dumped page state → %s.{html,png} (url=%s)", stem, page.url)
        except Exception:
            log.info("  dumped page state → %s.{html,png}", stem)
    return stem if ok_any else None


def open_create(page: Page) -> None:
    page.goto(CREATE_URL, wait_until="domcontentloaded")
    _dismiss_cookies(page)


def _dismiss_cookies(page: Page) -> None:
    """Click the cookies banner away if it's showing. Idempotent."""
    for name in ("Reject All", "Accept All Cookies"):
        try:
            page.get_by_role("button", name=name, exact=True).click(timeout=1500)
            log.info("Dismissed cookies banner (%s).", name)
            return
        except Exception:
            continue


def switch_to_advanced_mode(page: Page) -> None:
    """Switch the composer to 'Advanced' mode.

    Suno renamed what used to be 'Custom Mode' to 'Advanced' — one of the
    three pill tabs at the top of the composer (Simple / Advanced / Sounds).
    Clicking when already active is a no-op.
    """
    page.get_by_role("button", name="Advanced", exact=True).first.click(timeout=10000)
    # Let the Advanced sections expand before we try to fill them.
    page.wait_for_timeout(500)


def fill_advanced_fields(page: Page, *, lyrics: str, styles: str, title: str) -> None:
    """Fill the three Advanced-mode inputs.

    Field anchors (Suno UI as of 2026-04):
      - Lyrics: textarea with placeholder
        'Write some lyrics or leave blank for instrumental'.
      - Styles: textarea inside the 'Styles' collapsible section. No stable
        placeholder, so we scope by the section heading.
      - Title:  input with placeholder 'Song Title (Optional)'.
    """
    # Lyrics — distinct placeholder, so we can address it directly.
    lyrics_box = page.get_by_placeholder("Write some lyrics or leave blank for instrumental")
    lyrics_box.click()
    lyrics_box.fill(lyrics)

    # Styles — anchor on the leaf element whose text is exactly 'Styles',
    # then walk up to the nearest ancestor that contains a <textarea>, and
    # take that textarea. A naive filter like `div:has(:text("Styles"))`
    # matches the document body (which contains 'Styles' somewhere) and
    # resolves to the first textbox in the whole page — i.e. the
    # 'Search workspaces' input on the right rail. This XPath is narrow
    # enough to lock onto the Styles accordion specifically.
    styles_box = page.locator(
        "xpath=//*[normalize-space(text())='Styles']"
        "/ancestor::*[.//textarea][1]//textarea"
    ).first
    styles_box.click()
    styles_box.fill(styles)

    # Title — Suno renders TWO inputs with the same placeholder (likely a
    # desktop/mobile layout duplicate; one hidden via CSS). Strict mode
    # refuses an ambiguous match, so we filter to the visible one.
    title_box = page.locator(
        'input[placeholder="Song Title (Optional)"]:visible'
    ).first
    title_box.scroll_into_view_if_needed()
    title_box.click()
    title_box.fill(title)


def _create_button_strategies(page: Page) -> list[tuple[str, object]]:
    """Return (name, locator) candidates for the composer Create button.

    Tried in order; first one yielding a visible+enabled element wins.
    The primary selector is aria-label="Create song" — confirmed from a
    DOM dump (see debug/*.html). Earlier attempts with name='Create'
    returned 0 matches because Suno sets the accessible name via aria-label.
    """
    create_text_re = re.compile(r"^\s*Create\s*$", re.IGNORECASE)
    return [
        ('button[aria-label="Create song"]',
         page.locator('button[aria-label="Create song"]')),
        ("role=button name='Create song' exact",
         page.get_by_role("button", name="Create song", exact=True)),
        ("button filter has_text=Create (exact regex)",
         page.locator("button").filter(has_text=create_text_re)),
        ('xpath button normalized text == "Create"',
         page.locator('xpath=//button[normalize-space(.)="Create"]')),
    ]


def click_create(page: Page) -> None:
    """Click the big Create button at the bottom of the composer.

    Multiple 'Create' items exist in DOM (sidebar nav, '+ Create' next to
    Hooks, plus desktop/mobile layout duplicates of the composer button).
    Previous attempts with role+exact-name returned 0 matches — likely the
    button's accessible name isn't exactly 'Create' (SVG icon contributes
    to it). So we try several strategies and pick the first visible+enabled
    candidate across all of them.
    """
    strategies = _create_button_strategies(page)
    deadline = time.time() + 15
    picked_name: Optional[str] = None
    btn = None

    # First pass is verbose; subsequent retries stay quiet unless we fail.
    verbose_remaining = 1
    while time.time() < deadline and btn is None:
        for name, loc in strategies:
            try:
                count = loc.count()
            except Exception as e:
                if verbose_remaining:
                    log.info("  create-strategy %-40s error: %s", name, e)
                continue
            if verbose_remaining:
                log.info("  create-strategy %-40s matches=%d", name, count)
            for i in range(count):
                c = loc.nth(i)
                try:
                    visible = c.is_visible()
                    enabled = c.is_enabled() if visible else False
                except Exception:
                    visible, enabled = False, False
                if verbose_remaining:
                    try:
                        txt = (c.inner_text(timeout=500) or "").strip()[:60]
                    except Exception:
                        txt = "<?>"
                    log.info("    [%d] visible=%s enabled=%s text=%r", i, visible, enabled, txt)
                if visible and enabled:
                    btn = c
                    picked_name = name
                    break
            if btn is not None:
                break
        verbose_remaining = max(0, verbose_remaining - 1)
        if btn is None:
            page.wait_for_timeout(400)

    if btn is None:
        # Dump page state so we can actually see what's there.
        dump_page_state(page, "click_create_no_candidate")
        total_candidates = 0
        for _, loc in strategies:
            try:
                total_candidates += loc.count()
            except Exception:
                pass
        raise PlaywrightTimeoutError(
            f"No visible+enabled Create button found across "
            f"{len(strategies)} strategies (total raw matches: {total_candidates}). "
            "See debug/*.html for the DOM."
        )

    # Commit any pending field value: Suno's Styles textarea uses React
    # state that commits on blur, so we nudge focus away before the submit.
    try:
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)
    except Exception:
        pass

    log.info("  clicking Create via strategy: %s", picked_name)
    # SINGLE click only. Any retry/verification here has a real cost —
    # double-click submission spends credits twice — so we prefer a failed
    # row (easily retried with --retry-failed) over a billing surprise.
    btn.click()


_SONG_UUID_RE = re.compile(r"^/song/([0-9a-f-]{36})$")


def get_top_song_uuids(page: Page, n: int = 8) -> list[str]:
    """Return up to `n` workspace song UUIDs in DOM order (newest-first).

    Suno sorts the workspace by Newest by default; brand-new clips always
    insert at positions [0] and [1]. Using DOM order is far safer than
    set-difference across snapshots, which is vulnerable to virtualized
    scrolling (rows outside the viewport don't render, so a set delta can
    erroneously flag a previously-offscreen OLD song as 'new'). That bug
    is how an old song from months ago ended up saved under the name of
    a freshly-generated row.
    """
    try:
        hrefs = page.eval_on_selector_all(
            'a[href^="/song/"]',
            f"els => els.slice(0, {max(n * 3, 12)}).map(e => e.getAttribute('href'))",
        )
    except Exception:
        hrefs = []
    uuids: list[str] = []
    seen: set[str] = set()
    for h in hrefs or []:
        m = _SONG_UUID_RE.match(h or "")
        if m:
            u = m.group(1)
            if u not in seen:
                uuids.append(u)
                seen.add(u)
                if len(uuids) >= n:
                    break
    return uuids


def _scroll_workspace_to_top(page: Page) -> None:
    """Ensure the workspace list is scrolled to position 0 before snapshotting.

    The workspace is a virtualized scroll container. If the user left it
    scrolled down, our 'top N uuids' are actually mid-list uuids. Scroll
    up first to guarantee we're looking at the newest entries.
    """
    try:
        page.evaluate(
            """() => {
                const link = document.querySelector('a[href^="/song/"]');
                if (!link) return;
                let el = link.parentElement;
                for (let i = 0; i < 15 && el; i++) {
                    if (el.scrollHeight > el.clientHeight + 20) {
                        el.scrollTop = 0;
                        return;
                    }
                    el = el.parentElement;
                }
            }"""
        )
        page.wait_for_timeout(400)
    except Exception:
        pass


def _row_title(page: Page, uuid: str) -> str:
    """Return the visible title of the workspace row for this uuid, or ''."""
    try:
        return (
            page.locator(f'a[href="/song/{uuid}"]').first.inner_text(timeout=1500) or ""
        ).strip()
    except Exception:
        return ""


def _generating_uuids(page: Page, uuids: list[str]) -> list[str]:
    """Of the given uuids, return those whose workspace row is NOT fully ready.

    The duration label (e.g. '1:59', '10:14') is the authoritative 'done'
    signal: the same DOM slot that shows a spinner during generation gets
    replaced with the duration text once the audio is fully rendered and
    uploaded to Suno's CDN. Spinner-gone alone is a premature signal —
    Suno sometimes removes the spinner while the MP3 is still being
    finalized server-side, which is how earlier Sunflower Days downloads
    came back truncated (0.4 MB / 1.3 MB).

    A row is considered NOT done if:
      - it's missing from the DOM, or
      - it still shows a <svg.animate-spin>, or
      - it has no mm:ss (or h:mm:ss) duration label anywhere inside the row.
    """
    try:
        return page.evaluate(
            """
            (uuids) => {
                const DUR_RE = /^\\d{1,2}:\\d{2}(:\\d{2})?$/;
                const still = [];
                for (const uuid of uuids) {
                    const link = document.querySelector(`a[href="/song/${uuid}"]`);
                    if (!link) { still.push(uuid); continue; }
                    // Walk up to find the row container (has an image / play button).
                    let row = link;
                    for (let i = 0; i < 12 && row; i++) {
                        if (row.querySelector && row.querySelector('img')) break;
                        row = row.parentElement;
                    }
                    if (!row) { still.push(uuid); continue; }
                    if (row.querySelector('svg.animate-spin')) {
                        still.push(uuid); continue;
                    }
                    // Look for a mm:ss text node inside the row.
                    let hasDuration = false;
                    const walker = document.createTreeWalker(
                        row, NodeFilter.SHOW_TEXT, null, false
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        const t = (node.textContent || '').trim();
                        if (DUR_RE.test(t)) { hasDuration = true; break; }
                    }
                    if (!hasDuration) still.push(uuid);
                }
                return still;
            }
            """,
            uuids,
        )
    except Exception:
        return list(uuids)  # assume still generating on error


def wait_for_generation(
    page: Page,
    pre_top: list[str],
    timeout_s: int,
    expect_count: int = 2,
    min_wait_after_click_s: int = DEFAULT_MIN_WAIT_AFTER_CLICK_S,
    expected_title: Optional[str] = None,
) -> list[str]:
    """Wait for `expect_count` new clips to appear at the TOP of the workspace.

    Strategy: Suno sorts workspace Newest-first, so new clips are inserted
    at positions [0] and [1]. We capture the top-N uuids BEFORE Create and
    wait for the first `expect_count` positions to contain uuids that
    weren't in that pre-snapshot. This avoids the virtualized-scrolling
    trap where an old song scrolling into view looks like a 'new' song to
    a naive set-difference.

    Phases:
      0. Hard sleep `min_wait_after_click_s` for Suno to insert new rows.
      1. Poll until top `expect_count` uuids differ from `pre_top`.
      2. Poll until each new row shows a duration label (see
         _generating_uuids).

    If `expected_title` is given, log a warning if the rendered title of
    the new rows doesn't match — useful when debugging mis-picked rows.
    """
    # Phase 0: fixed wait before we trust the DOM.
    if min_wait_after_click_s > 0:
        log.info(
            "  waiting %ds after Create click for Suno to insert new rows...",
            min_wait_after_click_s,
        )
        page.wait_for_timeout(min_wait_after_click_s * 1000)

    _scroll_workspace_to_top(page)

    deadline = time.time() + timeout_s
    pre_top_set = set(pre_top)
    new_uuids: list[str] = []
    # Phase 1: poll until top positions are populated with uuids not in pre_top.
    while time.time() < deadline:
        current_top = get_top_song_uuids(page, n=max(expect_count + 4, 8))
        # New uuids must be CONTIGUOUS at the top — stop at first old uuid.
        leading_new: list[str] = []
        for u in current_top:
            if u in pre_top_set:
                break
            leading_new.append(u)
            if len(leading_new) >= expect_count:
                break
        if len(leading_new) >= expect_count:
            new_uuids = leading_new[:expect_count]
            break
        page.wait_for_timeout(1000)

    if len(new_uuids) < expect_count:
        # Phase-1 failure: no new clips appeared. This means the Create
        # submit never reached Suno's backend — NO CREDITS WERE SPENT.
        # Distinct exception class so the run loop can safely auto-retry.
        raise NoClipsAppearedError(
            f"Only {len(new_uuids)} new clip(s) at top of workspace within "
            f"{timeout_s}s (expected {expect_count}). "
            f"pre_top={pre_top[:5]}"
        )

    log.info("  new clip uuids (top of workspace):")
    for u in new_uuids:
        t = _row_title(page, u)
        log.info("    %s = %r", u, t)
        if expected_title and t and expected_title.strip().lower() not in t.lower():
            log.warning(
                "    WARNING: row title %r doesn't contain expected %r — "
                "mis-picked row? Double-check before trusting the download.",
                t, expected_title,
            )

    # Phase 2: wait for duration labels on those rows.
    last_log = 0.0
    while time.time() < deadline:
        still = _generating_uuids(page, new_uuids)
        if not still:
            log.info("  all %d clip(s) finished generating.", len(new_uuids))
            return new_uuids
        now = time.time()
        if now - last_log > 15:
            log.info("  still generating: %d of %d (%s)", len(still), len(new_uuids), still)
            last_log = now
        page.wait_for_timeout(3000)
    raise PlaywrightTimeoutError(
        f"Clips did not finish within {timeout_s}s: {_generating_uuids(page, new_uuids)}"
    )


# A plausible lower bound for ANY real MP3 from Suno, even a short clip.
# Suno's cdn1.suno.ai/<uuid>.mp3 appears to serve a streaming-quality file
# at roughly 32-128kbps; a ~30-second clip is well over 100KB either way.
# Anything below this is almost certainly a partial upload, HTTP error body,
# or CDN cache miss — not a real audio file.
MIN_EXPECTED_MP3_BYTES = 100_000  # 100 KB


def _fetch_via_cdn(page: Page, uuid: str) -> Optional[bytes]:
    """Try common Suno CDN URL patterns; return audio bytes on first 200."""
    # Suno's CDN serves audio at a few predictable locations. The auth
    # context of the page (cookies) is reused via page.context.request.
    candidates = [
        f"https://cdn1.suno.ai/{uuid}.mp3",
        f"https://audiopipe.suno.ai/?item_id={uuid}",
        f"https://cdn1.suno.ai/audio_{uuid}.mp3",
    ]
    for url in candidates:
        try:
            resp = page.context.request.get(url, timeout=30000)
        except Exception as e:
            log.info("  cdn try %s → exception: %s", url, e)
            continue
        if resp.ok:
            body = resp.body()
            if body and len(body) > 1024:  # sanity: tiny responses aren't audio
                log.info("  cdn hit %s (%d bytes)", url, len(body))
                return body
        log.info("  cdn try %s → status %d", url, resp.status)
    return None


def _fetch_with_retry(
    page: Page, uuid: str, min_bytes: int = MIN_EXPECTED_MP3_BYTES
) -> Optional[bytes]:
    """Fetch audio bytes. Short-circuit once we have >= min_bytes OR stable size.

    The previous retry logic assumed undersized responses meant the CDN
    was still finalizing. That was wrong: cdn1.suno.ai/<uuid>.mp3 serves
    the same size forever (Suno publishes a single streaming-quality MP3
    per clip). Retrying past 2 identical responses just wastes time.
    """
    best: Optional[bytes] = None
    prev_len = -1
    stable_count = 0
    for attempt in range(1, 4):  # at most 3 tries
        body = _fetch_via_cdn(page, uuid)
        if body is None:
            log.info("  cdn: attempt %d got nothing for %s", attempt, uuid)
        else:
            if len(body) >= min_bytes:
                return body
            log.info(
                "  cdn: attempt %d got %d bytes (< %d min).", attempt, len(body), min_bytes
            )
            if best is None or len(body) > len(best):
                best = body
            if len(body) == prev_len:
                stable_count += 1
                if stable_count >= 1:
                    log.info("  cdn: size is stable — CDN is done, not partial.")
                    return body
            prev_len = len(body)
        if attempt < 3:
            page.wait_for_timeout(5000)
    if best is not None:
        log.info("  cdn: returning best result (%d bytes).", len(best))
    return best


def _download_via_ui(
    page: Page, uuid: str, out_path: Path
) -> Optional[Path]:
    """Fallback: click the '⋮' menu on the clip row and pick Download → MP3.

    Suno's row menu doesn't use standard ARIA menuitems. Items are
    <button class="context-menu-button">Text</button>. The 'Download'
    entry has a right-arrow indicating a submenu (MP3 / WAV); hovering
    it opens the submenu. Structure confirmed from debug HTML dumps.
    """
    link = page.locator(f'a[href="/song/{uuid}"]').first
    try:
        link.scroll_into_view_if_needed(timeout=5000)
        link.hover()
    except Exception:
        pass
    # Open the row's More options menu.
    more_btn = page.locator(
        f'xpath=//a[@href="/song/{uuid}"]/ancestor::*'
        f'[descendant::button[@aria-label="More options"]][1]'
        f'//button[@aria-label="More options"]'
    ).first
    try:
        more_btn.click(timeout=5000)
    except Exception as e:
        log.warning("  UI download: cannot open More menu for %s: %s", uuid, e)
        dump_page_state(page, f"dl_menu_open_fail_{uuid[:8]}")
        return None
    page.wait_for_timeout(300)

    # Find and hover the 'Download' parent item (has a submenu arrow).
    download_btn = (
        page.locator("button.context-menu-button")
        .filter(has_text=re.compile(r"^\s*Download\s*$", re.I))
        .first
    )
    try:
        download_btn.wait_for(state="visible", timeout=3000)
        download_btn.hover()
        page.wait_for_timeout(600)  # let submenu render
    except Exception as e:
        log.warning("  UI download: no 'Download' item in menu: %s", e)
        dump_page_state(page, f"dl_menu_no_download_{uuid[:8]}")
        try: page.keyboard.press("Escape")
        except Exception: pass
        return None

    # Now pick from the submenu. Try MP3 first, then WAV, then any matching audio.
    submenu_candidates = [
        re.compile(r"mp3\s*audio", re.I),
        re.compile(r"^\s*MP3\s*$", re.I),
        re.compile(r"mp3", re.I),
        re.compile(r"wav\s*audio", re.I),
        re.compile(r"wav", re.I),
    ]
    for pat in submenu_candidates:
        item = (
            page.locator("button.context-menu-button")
            .filter(has_text=pat)
            .first
        )
        try:
            if not item.is_visible(timeout=1200):
                continue
            with page.expect_download(timeout=90000) as dl_info:
                item.click()
            dl_info.value.save_as(str(out_path))
            log.info("  UI download: saved via submenu %r → %s", pat.pattern, out_path.name)
            return out_path
        except Exception as e:
            log.debug("  UI download: submenu item %r failed: %s", pat.pattern, e)
            continue

    log.warning("  UI download: no submenu match for %s", uuid)
    dump_page_state(page, f"dl_submenu_no_match_{uuid[:8]}")
    try: page.keyboard.press("Escape")
    except Exception: pass
    return None


def download_clips(page: Page, clips: list[str], out_dir: Path, base_name: str) -> list[Path]:
    """Download each clip by uuid. Prefer direct CDN fetch; fall back to UI.

    Returns list of written paths (may be shorter than `clips` if a fetch
    fails; per-clip errors are logged, not raised, so one bad clip doesn't
    drop the others.)
    """
    out: list[Path] = []
    for i, uuid in enumerate(clips):
        dest = out_dir / f"{base_name}_{i}_{uuid[:8]}.mp3"
        # Primary: CDN fetch with auth cookies, retrying while partial.
        body = _fetch_with_retry(page, uuid)
        if body is not None and len(body) >= MIN_EXPECTED_MP3_BYTES:
            dest.write_bytes(body)
            log.info("  saved %s (%d bytes)", dest.name, len(body))
            out.append(dest)
            continue
        # Fallback: UI menu click. Downloads via menu are served from a
        # finalized asset URL so partial-size issues shouldn't apply.
        log.info("  cdn returned undersized/empty for %s, trying UI menu.", uuid)
        result = _download_via_ui(page, uuid, dest)
        if result is not None and result.exists() and result.stat().st_size >= MIN_EXPECTED_MP3_BYTES:
            out.append(result)
        elif body is not None:
            # Last resort: keep the undersized CDN body, but flag it loudly.
            dest.write_bytes(body)
            log.warning(
                "  %s saved but only %d bytes (likely truncated). Rerun this row later.",
                dest.name, len(body),
            )
            out.append(dest)
    return out


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

def safe_basename(title: str, idx: int) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title).strip()
    cleaned = cleaned.replace(" ", "_")[:60] or "song"
    return f"{idx:04d}_{cleaned}"


def count_downloaded_files(row: Row, out_dir: Path) -> int:
    """Count MP3s already on disk for this row. File-presence is the
    authoritative 'done' signal — the CSV status column is a hint that we
    self-heal against actual file state on each run. If the user deletes
    files, those rows become pending again; if they have files but no CSV
    marker (e.g. interrupted between download and write_rows), we treat
    them as done."""
    if not out_dir.exists():
        return 0
    prefix = safe_basename(row.title, row.index)
    return sum(1 for _ in out_dir.glob(f"{prefix}_*.mp3"))


def reconcile_status_with_files(
    rows: list[Row], out_dir: Path, expected_per_row: int = 2
) -> tuple[int, int]:
    """Walk rows and sync status with what's actually in out_dir.

    Returns (newly_marked_done, reset_to_pending).
    """
    newly_done = 0
    reset_pending = 0
    for r in rows:
        have = count_downloaded_files(r, out_dir)
        if have >= expected_per_row:
            if r.status != STATUS_DONE:
                r.status = STATUS_DONE
                newly_done += 1
        else:
            if r.status == STATUS_DONE:
                r.status = ""
                reset_pending += 1
    return newly_done, reset_pending


def process_row(
    page: Page,
    row: Row,
    out_dir: Path,
    gen_timeout_s: int,
    min_wait_after_click_s: int = DEFAULT_MIN_WAIT_AFTER_CLICK_S,
    style_suffix: str = "",
) -> list[Path]:
    log.info("  step 1/7: open /create")
    open_create(page)
    log.info("  step 2/7: switch to Advanced")
    switch_to_advanced_mode(page)
    log.info("  step 3/7: fill fields")
    effective_styles = row.styles
    if style_suffix:
        effective_styles = (
            row.styles.rstrip(", ") + ", " + style_suffix
            if row.styles else style_suffix
        )
    fill_advanced_fields(page, lyrics=row.lyrics, styles=effective_styles, title=row.title)
    log.info("  step 4/7: snapshot workspace TOP (newest-first) before Create")
    _scroll_workspace_to_top(page)
    pre_top = get_top_song_uuids(page, n=8)
    log.info("  baseline top-%d: %s", len(pre_top), pre_top)
    log.info("  step 5/7: click Create")
    click_create(page)
    log.info("  step 6/7: wait for generation (timeout=%ds)", gen_timeout_s)
    clips = wait_for_generation(
        page, pre_top, gen_timeout_s,
        min_wait_after_click_s=min_wait_after_click_s,
        expected_title=row.title,
    )
    log.info("  step 7/7: download clips")
    return download_clips(page, clips, out_dir, safe_basename(row.title, row.index))


def run_browse(args: argparse.Namespace) -> int:
    """Open the saved-session browser and wait for the user to Enter.

    Intended for poking around Suno manually — checking account settings,
    model version selector, verifying the download location, inspecting the
    'More options' menu on a finished clip, etc. Session cookies persist so
    you stay logged in between runs.
    """
    with sync_playwright() as pw:
        ctx, cleanup = acquire_context(pw, args)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        log.info("Browser open. Do whatever you need — settings, downloads, etc.")
        log.info("Press Enter here when you're done to close the browser.")
        try:
            input(">>> Enter to close... ")
        except (KeyboardInterrupt, EOFError):
            pass
        cleanup()
    return 0


def run_login_only(args: argparse.Namespace) -> int:
    """Open a headed browser, let the user log in, save session, exit.

    Use this ONCE before the first real run. It does nothing else — no
    CSV reading, no generation — so you can take your time logging in
    (password manager, 2FA, Google account picker, whatever) without the
    script barreling into unimplemented generation code.
    """
    with sync_playwright() as pw:
        ctx, cleanup = acquire_context(pw, args)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page, interactive=True)
            log.info("Login flow complete. Session saved to %s", args.session_dir)
            return 0
        except Exception as e:
            log.error("Login not completed: %s", e)
            return 2
        finally:
            cleanup()


def run(args: argparse.Namespace) -> int:
    csv_path: Path = args.csv if args.csv is not None else choose_csv_interactively(Path("."))
    log.info("Using CSV: %s", csv_path)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, fieldnames = load_rows(csv_path)

    # File presence is authoritative — sync CSV status to match reality on disk.
    # If user deleted downloads, those rows become pending again automatically.
    newly_done, reset_pending = reconcile_status_with_files(rows, out_dir)
    if newly_done or reset_pending:
        log.info(
            "Reconciled CSV with %s: %d row(s) marked done (files present), "
            "%d row(s) reset to pending (files missing).",
            out_dir, newly_done, reset_pending,
        )
        write_rows(csv_path, rows, fieldnames)

    # If user didn't supply --start/--end, ask interactively.
    if args.start is None and args.end is None and not args.retry_failed and not args.redo:
        try:
            start_in, end_in = ask_range_interactively(len(rows))
        except (ValueError, EOFError, KeyboardInterrupt):
            start_in, end_in = None, None
        args.start, args.end = start_in, end_in

    # Ask for a styles-suffix (unless user explicitly disabled via --no-ask-suffix).
    style_suffix = args.style_suffix or ""
    if not args.no_ask_suffix and not style_suffix:
        try:
            style_suffix = ask_style_suffix_interactively()
        except (EOFError, KeyboardInterrupt):
            style_suffix = ""
    if style_suffix:
        # NOTE: we do NOT mutate row.styles. The CSV previously got polluted
        # by doing so: write_rows() runs after each completed row and would
        # persist the in-memory mutation back to disk, so on the next run
        # the suffix was effectively appended to the file. Pass style_suffix
        # through to process_row instead and apply it at the moment we type
        # into the Suno Styles textarea.
        log.info("Will append to every row's styles at generation time: %r", style_suffix)

    # Apply --start / --end ordinal range filter before pending/retry logic.
    if args.start is not None or args.end is not None:
        lo = args.start if args.start is not None else -(10**9)
        hi = args.end if args.end is not None else (10**9)
        before = len(rows)
        rows_in_range_ns = {r.n for r in rows if lo <= r.n <= hi}
        log.info(
            "Range filter n=[%s..%s]: %d of %d rows in scope.",
            args.start if args.start is not None else "-",
            args.end if args.end is not None else "-",
            len(rows_in_range_ns), before,
        )
    else:
        rows_in_range_ns = {r.n for r in rows}

    if args.retry_failed:
        reset = sum(1 for r in rows if r.status == STATUS_FAILED)
        for r in rows:
            if r.status == STATUS_FAILED:
                r.status = ""
        if reset:
            write_rows(csv_path, rows, fieldnames)
            log.info("Reset %d failed row(s) back to pending.", reset)

    if args.redo:
        needle = args.redo.lower()
        matched = [r for r in rows if needle in r.title.lower()]
        if not matched:
            log.warning("--redo %r matched no rows by title.", args.redo)
        else:
            for r in matched:
                log.info("  redoing row %d: %r (was status=%r)", r.index, r.title, r.status)
                r.status = ""
            write_rows(csv_path, rows, fieldnames)

    todo = [r for r in pending(rows) if r.n in rows_in_range_ns]
    log.info(
        "CSV: %d rows total, %d in range, %d pending to run.",
        len(rows), len(rows_in_range_ns), len(todo),
    )
    if not todo:
        log.info("Nothing to do.")
        return 0

    with sync_playwright() as pw:
        ctx, cleanup = acquire_context(pw, args)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            # Non-interactive on a real run — force the user to pre-authenticate
            # with --login-only so we never race an in-progress OAuth redirect.
            ensure_logged_in(page, interactive=False)
        except Exception as e:
            log.error("Login check failed: %s", e)
            cleanup()
            return 2

        for n, row in enumerate(todo, start=1):
            remaining = len(todo) - n
            log.info(
                "[%d/%d] title=%r (csv row %d) — %d remaining after this.",
                n, len(todo), row.title, row.index, remaining,
            )
            row_failed = False
            attempt = 1
            max_attempts = 1 + max(0, args.max_retries)
            while attempt <= max_attempts:
                try:
                    files = process_row(
                        page, row, out_dir, args.gen_timeout,
                        min_wait_after_click_s=args.min_wait_after_click,
                        style_suffix=style_suffix,
                    )
                    row.status = STATUS_DONE
                    log.info("  downloaded %d file(s): %s", len(files), [f.name for f in files])
                    row_failed = False
                    break
                except NoClipsAppearedError as e:
                    # Click didn't submit — NO credits spent. Safe to retry.
                    log.warning(
                        "  no clips appeared (attempt %d/%d) — Create submit lost. "
                        "Retrying (safe: no credits spent on this attempt).",
                        attempt, max_attempts,
                    )
                    dump_page_state(page, f"row{row.index:04d}_nosubmit_try{attempt}")
                    if attempt >= max_attempts:
                        row.status = STATUS_FAILED
                        row_failed = True
                        log.error("  out of retries; marking failed.")
                        break
                    attempt += 1
                    continue
                except (PlaywrightTimeoutError, NotImplementedError) as e:
                    # Phase-2 timeout or other generation error — clips MAY have
                    # been created (credits spent). Do NOT retry automatically.
                    row.status = STATUS_FAILED
                    row_failed = True
                    log.error("  generation failed: %s", e)
                    dump_page_state(page, f"row{row.index:04d}_failed")
                    break
                except Exception as e:
                    row.status = STATUS_FAILED
                    row_failed = True
                    log.exception("  unexpected error: %s", e)
                    dump_page_state(page, f"row{row.index:04d}_error")
                    break
            # Persist progress after every row so Ctrl-C is safe.
            write_rows(csv_path, rows, fieldnames)

            if row_failed and args.stop_on_error:
                log.error(
                    "  --stop-on-error set: halting after first failure. "
                    "Browser left open for inspection; see debug/ for dumps."
                )
                # Skip cleanup so the Chrome window stays open; the user can
                # poke around the DOM and close it manually when done.
                done = sum(1 for r in rows if r.status == STATUS_DONE)
                failed = sum(1 for r in rows if r.status == STATUS_FAILED)
                log.info("Halted. done=%d failed=%d total=%d", done, failed, len(rows))
                return 1

            if n < len(todo):
                log.info("  sleeping %ds before next row...", args.delay)
                time.sleep(args.delay)

        cleanup()

    done = sum(1 for r in rows if r.status == STATUS_DONE)
    failed = sum(1 for r in rows if r.status == STATUS_FAILED)
    log.info("Finished. done=%d failed=%d total=%d", done, failed, len(rows))
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Automate Suno.com song generation from a CSV.")
    p.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Input/state CSV. If omitted, scans current directory — uses the "
             "only .csv if there's one, otherwise prompts you to pick.",
    )
    p.add_argument(
        "--start",
        type=int,
        default=None,
        help="Process only rows whose 'n' column >= this value.",
    )
    p.add_argument(
        "--end",
        type=int,
        default=None,
        help="Process only rows whose 'n' column <= this value.",
    )
    p.add_argument(
        "--style-suffix",
        default=None,
        help="Extra text appended to every row's 'styles' field in memory "
             "(CSV file unchanged). E.g. 'long song, 3 minute length'. If "
             "omitted the script prompts interactively.",
    )
    p.add_argument(
        "--no-ask-suffix",
        action="store_true",
        help="Skip the interactive style-suffix prompt (and don't append anything).",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR, help="Where to save downloaded audio")
    p.add_argument("--session-dir", type=Path, default=DEFAULT_SESSION_DIR, help="Playwright user_data_dir")
    p.add_argument("--delay", type=int, default=DEFAULT_DELAY_S, help="Seconds between generations (default: 30)")
    p.add_argument("--gen-timeout", type=int, default=DEFAULT_GEN_TIMEOUT_S, help="Per-song generation timeout (s)")
    p.add_argument("--headless", action="store_true", help="Run headless (do NOT use for first login)")
    p.add_argument("--login-only", action="store_true", help="Just open browser for manual login and exit")
    p.add_argument("--retry-failed", action="store_true", help="Reset rows marked 'failed' back to pending before running")
    p.add_argument(
        "--redo",
        metavar="TITLE",
        help="Reset status to pending for any row whose title contains this "
             "(case-insensitive) substring. Use to re-generate a song that "
             "was already marked done.",
    )
    p.add_argument(
        "--browser",
        choices=["chrome", "msedge", "chromium"],
        default="chrome",
        help="Browser channel when using Playwright's launcher (ignored with --use-system-chrome)",
    )
    p.add_argument(
        "--use-system-chrome",
        action="store_true",
        help="Launch real Chrome as a plain subprocess and connect via CDP. Required to sign in with Google (avoids the --enable-automation flag that Google blocks).",
    )
    p.add_argument(
        "--cdp-port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help=f"DevTools port for --use-system-chrome (default: {DEFAULT_CDP_PORT})",
    )
    p.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Halt on first failing row instead of continuing. Leaves the "
             "browser open for inspection — useful while iterating on selectors.",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max in-run retries for a 'no clips appeared' failure (click "
             "lost — safe to retry, no credits spent). Other failure types "
             "are never auto-retried. Default: 2 (=3 attempts total per row).",
    )
    p.add_argument(
        "--min-wait-after-click",
        type=int,
        default=DEFAULT_MIN_WAIT_AFTER_CLICK_S,
        help=f"Seconds to wait after clicking Create before looking for new "
             f"clips (default: {DEFAULT_MIN_WAIT_AFTER_CLICK_S}). Prevents "
             f"racing a workspace that hasn't inserted the new rows yet.",
    )
    p.add_argument(
        "--browse",
        action="store_true",
        help="Open a browser with the saved Suno session for manual inspection "
             "(check settings, song quality, download location, etc.). Stays "
             "open until you press Enter. Does not process the CSV.",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = build_parser().parse_args(argv)
    try:
        if args.browse:
            return run_browse(args)
        if args.login_only:
            return run_login_only(args)
        return run(args)
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress saved to CSV — rerun to resume.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
