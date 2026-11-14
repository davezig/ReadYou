#!/usr/bin/env python3
"""Deliver a digest of article titles and lead sentences from onejailbreak.com.

Strategy, cheapest first:
  1. Look for an RSS/Atom feed (most sites expose one). Feeds carry titles,
     links and often a summary, so they are the lightest way to get the data.
  2. If there is no feed, parse the homepage for article links.
  3. For each article without a usable summary, fetch the article page and
     take its first couple of sentences.

Cloudflare: requests are sent with curl_cffi's Chrome TLS impersonation, which
gets past many bot checks without a browser. If the site still answers with a
challenge page and Playwright is installed, the page is loaded in a real
headless Chromium instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

DEFAULT_BASE = "https://www.onejailbreak.com/"
FEED_PATHS = ["feed/", "feed", "rss.xml", "atom.xml"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "seen_urls.json"
PROFILE_DIR = HERE / "browser-profile"

log = logging.getLogger("onejailbreak")


class Blocked(Exception):
    """The site refused us or served a bot challenge."""


def looks_like_challenge(html: str) -> bool:
    markers = ("Just a moment...", "cf-challenge", "challenge-platform", "cf_chl_opt")
    return any(m in html for m in markers)


class Fetcher:
    def __init__(self, delay: float, force_browser: bool, headed: bool):
        self.delay = delay
        self.force_browser = force_browser
        self.headed = headed
        self._last = 0.0
        self._pw = None
        self._ctx = None
        self._page = None
        self.browser_available = _playwright_available()
        try:
            from curl_cffi import requests as cffi_requests

            self.session = cffi_requests.Session(impersonate="chrome")
            self.backend = "curl_cffi"
        except ImportError:
            import requests

            self.session = requests.Session()
            self.session.headers.update(
                {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
            )
            self.backend = "requests"
        log.debug("HTTP backend: %s, browser fallback: %s", self.backend, self.browser_available)

    def _throttle(self) -> None:
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def get(self, url: str) -> str:
        if self.force_browser:
            return self._browser_get(url)
        self._throttle()
        try:
            resp = self.session.get(url, timeout=30)
        except Exception as exc:  # network errors, TLS errors, etc.
            raise Blocked(f"request failed for {url}: {exc}") from exc
        text = resp.text
        if resp.status_code == 200 and not looks_like_challenge(text):
            return text
        if self.browser_available and (
            resp.status_code in (403, 429, 503) or looks_like_challenge(text)
        ):
            log.info("Blocked over HTTP (status %s); retrying in headless browser", resp.status_code)
            return self._browser_get(url)
        if resp.status_code == 404:
            raise Blocked(f"404 Not Found: {url}")
        if looks_like_challenge(text):
            raise Blocked(f"Cloudflare challenge page for {url} (install Playwright and use --browser)")
        raise Blocked(f"HTTP {resp.status_code} for {url}")

    def _browser_get(self, url: str) -> str:
        if not self.browser_available:
            raise Blocked("Playwright is not installed; cannot use a browser")
        if self._page is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            # A persistent profile keeps Cloudflare cookies between runs, which
            # helps the challenge pass on later runs.
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=not self.headed,
                user_agent=USER_AGENT,
                locale="en-US",
            )
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._throttle()
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # Give a managed challenge up to ~30 seconds to finish on its own.
        for _ in range(30):
            html = self._page.content()
            if not looks_like_challenge(html):
                return html
            time.sleep(1)
        raise Blocked(f"Cloudflare challenge did not clear in the browser for {url}")

    def close(self) -> None:
        if self._ctx is not None:
            self._ctx.close()
        if self._pw is not None:
            self._pw.stop()


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def strip_html(fragment: str) -> str:
    return BeautifulSoup(fragment, "html.parser").get_text(" ", strip=True)


def items_from_feed(xml_text: str) -> list[dict]:
    """Parse RSS 2.0 or Atom. Raises ET.ParseError if this is not XML."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d: dict = {}
        for child in el:
            name = _local(child.tag)
            if name == "title" and child.text:
                d["title"] = " ".join(child.text.split())
            elif name == "link":
                d.setdefault("url", child.get("href") or (child.text or "").strip())
            elif name in ("description", "summary", "content") and child.text:
                d.setdefault("summary", strip_html(child.text))
        if d.get("title") and d.get("url"):
            items.append(d)
    return items


def items_from_homepage(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base).netloc
    candidates: list[tuple[str, str]] = []

    # Prefer <article> blocks: the heading link inside each one is the title.
    for art in soup.find_all("article"):
        heading = art.find(["h1", "h2", "h3"])
        link = (heading.find("a", href=True) if heading else None) or art.find("a", href=True)
        if link:
            title = (heading or link).get_text(" ", strip=True)
            candidates.append((title, link["href"]))

    # Fallback: any heading that contains a link.
    if not candidates:
        for heading in soup.find_all(["h1", "h2", "h3"]):
            link = heading.find("a", href=True)
            if link:
                candidates.append((heading.get_text(" ", strip=True), link["href"]))

    items, seen = [], set()
    for title, href in candidates:
        url = urljoin(base, href).split("#")[0]
        if not title or urlparse(url).netloc != host:
            continue
        if url in seen or url.rstrip("/") == base.rstrip("/"):
            continue
        seen.add(url)
        items.append({"title": " ".join(title.split()), "url": url})
    return items


def first_sentences(text: str, n: int = 2, max_chars: int = 400) -> str:
    text = " ".join(text.split())
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(sentences[:n]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + "…"
    return out


def lead_from_article_page(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    for p in container.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) >= 60:  # skip nav, captions and bylines
            return first_sentences(text)
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    return first_sentences(meta.get("content", "")) if meta else ""


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def find_feed_items(fetcher: Fetcher, base: str) -> list[dict]:
    for path in FEED_PATHS:
        url = urljoin(base, path)
        try:
            items = items_from_feed(fetcher.get(url))
        except (Blocked, ET.ParseError) as exc:
            log.debug("No usable feed at %s (%s)", url, exc)
            continue
        if items:
            log.info("Found feed %s (%d items)", url, len(items))
            return items
    return []


def discover(fetcher: Fetcher, base: str) -> tuple[list[dict], str]:
    # The homepage is the live source of truth: the feed on this site is
    # out of date, so it is used only for summaries and as a fallback.
    feed_items = find_feed_items(fetcher, base)
    feed_by_url = {i["url"]: i for i in feed_items}

    homepage_error: Blocked | None = None
    try:
        items = items_from_homepage(fetcher.get(base), base)
    except Blocked as exc:
        homepage_error, items = exc, []

    if items:
        for item in items:
            feed_match = feed_by_url.get(item["url"])
            if feed_match and feed_match.get("summary"):
                item["summary"] = feed_match["summary"]
        return items, "homepage"

    if feed_items:
        log.warning("Homepage gave no articles; falling back to the feed, which may be stale")
        return feed_items, "feed"
    if homepage_error:
        raise homepage_error
    return [], "homepage"


def enrich(fetcher: Fetcher, items: list[dict], max_articles: int) -> list[dict]:
    for item in items[:max_articles]:
        if item.get("summary"):
            item["lead"] = first_sentences(item["summary"])
            continue
        try:
            item["lead"] = lead_from_article_page(fetcher.get(item["url"]))
        except Blocked as exc:
            log.warning("Could not read article %s: %s", item["url"], exc)
            item["lead"] = ""
    return items[:max_articles]


def load_seen() -> set[str]:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(urls: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(urls), indent=0))


def render_markdown(items: list[dict], source: str, base: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# onejailbreak digest — {stamp}", f"Source: {base} ({source}), {len(items)} article(s)", ""]
    for item in items:
        lines.append(f"## {item['title']}")
        if item.get("lead"):
            lines.append(item["lead"])
        lines.append(item["url"])
        lines.append("")
    return "\n".join(lines)


def send_email(body: str, subject: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM", os.environ.get("SMTP_USER", ""))
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE, help="site to scrape (default: %(default)s)")
    p.add_argument("--max-articles", type=int, default=25)
    p.add_argument("--new-only", action="store_true",
                   help="only include articles not seen in a previous run (good for cron)")
    p.add_argument("--output", type=Path, help="also write the digest to this file")
    p.add_argument("--email", action="store_true",
                   help="email the digest (needs SMTP_HOST, MAIL_TO, and optionally SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM)")
    p.add_argument("--browser", action="store_true",
                   help="always use headless Chromium (needs Playwright)")
    p.add_argument("--headed", action="store_true",
                   help="with --browser, show the browser window (helps with stubborn Cloudflare checks)")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between requests (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    base = args.base if args.base.endswith("/") else args.base + "/"

    if args.browser and not _playwright_available():
        log.error("--browser needs Playwright: pip install playwright && playwright install chromium")
        return 2

    fetcher = Fetcher(delay=args.delay, force_browser=args.browser, headed=args.headed)
    try:
        items, source = discover(fetcher, base)
        if not items:
            log.error("No articles found at %s. The site layout may have changed.", base)
            return 1
        seen = load_seen()
        if args.new_only:
            items = [i for i in items if i["url"] not in seen]
            log.info("%d new article(s) since last run", len(items))
        items = enrich(fetcher, items, args.max_articles)
    except Blocked as exc:
        log.error("Could not fetch %s: %s", base, exc)
        return 1
    finally:
        fetcher.close()

    if not items:
        log.info("Nothing new to report.")
        return 0

    digest = render_markdown(items, source, base)
    print(digest)
    if args.output:
        args.output.write_text(digest, encoding="utf-8")
    if args.email:
        send_email(digest, f"onejailbreak digest ({len(items)} articles)")

    seen |= {i["url"] for i in items}
    save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
