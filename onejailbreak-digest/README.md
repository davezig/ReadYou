# onejailbreak digest

A small Python script that lists each article on www.onejailbreak.com with its
title and the first two sentences, for a daily cron job.

## How it works

1. **Feed first.** It tries `/feed/`, `/feed`, `/rss.xml`, `/atom.xml`. A feed
   gives titles, links and usually a summary, and is the lightest request.
2. **Homepage fallback.** If there is no feed, it reads article links from the
   homepage.
3. **Lead sentences.** For each article it takes the first long paragraph of the
   article page (or the feed summary, or the meta description as a last resort).
4. **Cloudflare.** Requests use `curl_cffi`, which makes them look like Chrome at
   the TLS level. This gets past many bot checks with no browser at all. If the
   site still serves a challenge page and Playwright is installed, the page is
   loaded in headless Chromium instead.

## Setup

Python 3.10+ is required.

```bash
cd onejailbreak-digest
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python onejailbreak_digest.py
```

To enable the browser fallback for Cloudflare, also run:

```bash
pip install playwright
playwright install chromium
```

## Usage

```bash
python onejailbreak_digest.py                       # all articles, printed
python onejailbreak_digest.py --new-only            # only articles not seen before
python onejailbreak_digest.py --output digest.md    # also save to a file
python onejailbreak_digest.py --browser             # always use headless Chromium
python onejailbreak_digest.py --browser --headed    # visible browser window
python onejailbreak_digest.py -v                    # debug logging
```

`--new-only` records seen URLs in `seen_urls.json`, so a daily run only shows new
posts. Remove that file to start over.

### Email delivery (optional)

Set these environment variables and pass `--email`:

```bash
export SMTP_HOST=smtp.gmail.com SMTP_PORT=587
export SMTP_USER=you@gmail.com SMTP_PASSWORD='app-password'
export MAIL_TO=you@gmail.com
python onejailbreak_digest.py --new-only --email
```

For Gmail, use an app password rather than your account password.

## Cron (Linux / macOS)

Run `crontab -e` and add a line such as this one for 7:00 every morning:

```
0 7 * * * cd /home/you/onejailbreak-digest && /home/you/onejailbreak-digest/.venv/bin/python onejailbreak_digest.py --new-only --output /home/you/onejailbreak-digest/latest.md >> /home/you/onejailbreak-digest/cron.log 2>&1
```

Use the absolute path to the venv's Python, because cron has a minimal `PATH`.
Cron also does not load your shell profile, so if you use `--email`, set the
`SMTP_*` and `MAIL_TO` variables at the top of the crontab
(`SMTP_HOST=smtp.gmail.com` on its own line, and so on) or prefix the command
with them.

On Windows, use Task Scheduler and point it at `.venv\Scripts\python.exe` with
the script path as its argument.

## Cloudflare notes

- The script cannot promise to pass every Cloudflare check. Plain HTTP with
  Chrome impersonation works for many sites. A managed challenge needs a real
  browser.
- If runs fail with "Cloudflare challenge page", install Playwright and try
  `--browser`. Add `--headed` if headless still fails. A headed browser needs a
  desktop session, so cron may need to run while you are logged in.
- Requests are spaced 2 seconds apart (`--delay`) to be polite to the site.
- The browser keeps a profile in `browser-profile/`, so Cloudflare cookies can
  persist between runs.

## Exit codes

- `0` success, including "nothing new"
- `1` the site could not be fetched or no articles were found (cron logs show why)
- `2` a flag needs a dependency that is not installed
