"""Signing in — the only job left for a browser.

`:login` opens a normal Firefox window (Playwright) on klangtui's own profile;
you sign in on the real site (captcha, Google, 2FA all just work) and we keep
the `oauth_token` cookie. After that every call is plain HTTPS. Firefox also
comes back, headless and briefly, if SoundCloud's bot-check refuses a like.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .api import AuthError

_SITE = "https://soundcloud.com/"


class Session:
    """Where the login lives: a token file, plus the Firefox profile it came from."""

    def __init__(self, data_dir: Path):
        self.profile = data_dir / "profile"
        self._token_path = data_dir / "token"

    def load(self) -> str | None:
        try:
            tok = self._token_path.read_text().strip()
            if tok:
                return tok
        except OSError:
            pass
        # signed in with an older klangtui: the token only lives in the profile
        tok = self._token_from_profile()
        if tok:
            self.save(tok)
        return tok

    def save(self, token: str):
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)

    def clear(self):
        """Sign out for real: drop the token and the browser profile's cookies."""
        try:
            self._token_path.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(self.profile, ignore_errors=True)

    def _token_from_profile(self) -> str | None:
        db = self.profile / "cookies.sqlite"
        if not db.exists():
            return None
        # copy first — Firefox keeps the live file (and its -wal) locked
        tmp = tempfile.mkdtemp(prefix="klangtui-")
        try:
            for suffix in ("", "-wal"):
                src = Path(f"{db}{suffix}")
                if src.exists():
                    shutil.copy(src, Path(tmp) / f"cookies.sqlite{suffix}")
            con = sqlite3.connect(Path(tmp) / "cookies.sqlite")
            try:
                row = con.execute(
                    "SELECT value FROM moz_cookies WHERE name='oauth_token' "
                    "AND host LIKE '%soundcloud.com' ORDER BY expiry DESC LIMIT 1"
                ).fetchone()
            finally:
                con.close()
            return row[0] if row and row[0] else None
        except (OSError, sqlite3.Error):
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --- Playwright plumbing ------------------------------------------------------

# Allow autoplay-free pages, trust the OS root store (antivirus HTTPS scanning),
# and drop the webdriver flag so a plain personal login doesn't trip the bot-check.
_PREFS = {
    "dom.webdriver.enabled": False,
    "useAutomationExtension": False,
    "security.enterprise_roots.enabled": True,
}
_STEALTH_JS = ("try { Object.defineProperty(navigator, 'webdriver', "
               "{get: () => false, configurable: true}); } catch (e) {}")

_install_tried = False


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise AuthError("signing in needs playwright: pip install playwright") from None
    return sync_playwright().start()


def _launch(pw, profile: Path, headless: bool, on_status):
    global _install_tried
    profile.mkdir(parents=True, exist_ok=True)
    kwargs = dict(headless=headless, viewport={"width": 1100, "height": 800},
                  locale="en-US", firefox_user_prefs=_PREFS)
    try:
        ctx = pw.firefox.launch_persistent_context(str(profile), **kwargs)
    except Exception as e:
        missing = "Executable doesn't exist" in str(e) or "playwright install" in str(e)
        if not missing or _install_tried:
            raise AuthError(f"Firefox failed to start: {e}") from e
        _install_tried = True
        on_status("first sign-in: downloading Firefox for the login window (~80 MB)…")
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "firefox"],
                           capture_output=True)
        if r.returncode != 0:
            raise AuthError("couldn't download Firefox — run: playwright install firefox") from e
        ctx = pw.firefox.launch_persistent_context(str(profile), **kwargs)
    try:
        ctx.add_init_script(_STEALTH_JS)
    except Exception:
        pass
    return ctx


def _cookie_token(ctx) -> str | None:
    for c in ctx.cookies(_SITE):
        if c.get("name") == "oauth_token" and c.get("value"):
            return c["value"]
    return None


def login_window(session: Session, on_status, cancel: threading.Event) -> str:
    """Open a visible Firefox, wait until you've signed in (or closed the
    window / pressed esc), return the token. Blocks — run it off the UI thread."""
    pw = _playwright()
    try:
        on_status("opening a Firefox window…")
        ctx = _launch(pw, session.profile, headless=False, on_status=on_status)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # land on the homepage and let you click "Sign in" — a cold jump to
            # /signin tends to hit SoundCloud's "something went wrong" page
            page.goto(_SITE, wait_until="domcontentloaded", timeout=60_000)
            on_status("sign in inside the Firefox window · esc cancels")
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline and not cancel.is_set():
                try:
                    if not ctx.pages:                 # you closed the window
                        break
                    tok = _cookie_token(ctx)
                except Exception:
                    break
                if tok:
                    session.save(tok)
                    return tok
                time.sleep(1)
        finally:
            try:
                ctx.close()                           # flushes cookies to the profile
            except Exception:
                pass
    finally:
        try:
            pw.stop()
        except Exception:
            pass
    raise AuthError("sign-in cancelled — nothing changed")


# Issued from inside a real soundcloud.com page, a write carries the browser's
# cookies, Origin and bot-check token — exactly what the site sends itself.
_JS_WRITE = """
async (a) => {
  try {
    const r = await fetch(a.url, { method: a.method, credentials: 'include',
      headers: { 'Authorization': 'OAuth ' + a.token, 'Accept': 'application/json' } });
    return r.status;
  } catch (e) { return -1; }
}
"""


def browser_write(session: Session, url: str, method: str, token: str, on_status) -> int:
    """Run one authenticated write from a headless soundcloud.com page, then
    close the browser again. Returns the HTTP status."""
    pw = _playwright()
    try:
        on_status("SoundCloud wants a real browser for this — asking Firefox…")
        ctx = _launch(pw, session.profile, headless=True, on_status=on_status)
        try:
            page = ctx.new_page()
            page.goto(_SITE, wait_until="domcontentloaded", timeout=45_000)
            return page.evaluate(_JS_WRITE, {"url": url, "method": method, "token": token})
        finally:
            ctx.close()
    finally:
        pw.stop()
