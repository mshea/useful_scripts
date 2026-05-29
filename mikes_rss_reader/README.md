# rss_reader

A lightweight, self-hosted RSS reader that turns an OPML subscription file into a static, dark-themed HTML site. No database server, no JavaScript framework, no cloud lock-in — just Python, SQLite, and a web server.

## What it does

1. Reads your RSS subscriptions from an OPML file.
2. Fetches the latest articles.
3. Stores them in a local SQLite database.
4. Generates static HTML pages:
   - **feeds.html** — today's articles, grouped by category.
   - **articles/\<slug\>.html** — one page per article.
   - **archive.html** — archive index by month.
   - **archive/YYYY-MM-DD.html** — one page per past day.
   - **reading-list.html** — client-side reading list (stars saved to `localStorage`; supports export/import/clear).
   - **search.html** — client-side search page (loads `search.db` via sql.js for instant FTS).
   - **search.db** — a stripped-down SQLite FTS database for client-side search (titles, sources, keywords).

## Features

- **Zero client-side dependencies.** Pure HTML/CSS/JS. Works in any browser.
- **Read-later stars.** Saved to `localStorage`; no server session needed.
- **Keyword extraction.** Tags articles automatically with YAKE.
- **Obsidian export.** One-click "Save to Obsidian" links on every article.
- **Search.** A tiny SQLite FTS database is served to the browser for instant search across titles, sources, and keywords (not article bodies).
- **Dark theme.** Easy on the eyes, no publisher CSS bleeding through.
- **Summarizer support.** Optional backend endpoint for article summarization.

## Requirements

- Python 3.10+
- [feedparser](https://pypi.org/project/feedparser/)
- [yake](https://pypi.org/project/yake/)

```bash
pip install -r requirements.txt
```

## Quick start

```bash
python3 run.py --generate-config
```

Edit `config.json`:

```json
{
  "opml_file": "subscriptions.opml",
  "output_dir": "public_html",
  "db_path": "feeds.db",
  "days_back": 1,
  "feed_timeout": 10,
  "category_order": ["Favorite Blogs", "RPGs"],
  "timezone": "America/New_York",
  "obsidian": {
    "vault": "My Vault",
    "folder": "Clippings"
  },
  "summarizer_endpoint": "/summarizer/summarize/text"
}
```

Run it:

```bash
python3 run.py
```

Set up a cron job or systemd timer to run it on a schedule.

## Configuration

All settings live in `config.json`. Relative paths are resolved against the directory containing the config file.

Environment variables prefixed with `OPML_` override config values:

```bash
OPML_OUTPUT_DIR=/var/www/html python3 run.py
```

### Key options

| Key | Description |
|-----|-------------|
| `opml_file` | Path to your OPML subscription file. Must be inside `output_dir` so it's served by the web server. |
| `output_dir` | Root directory for all generated HTML. **This directory must be web-accessible.** |
| `db_path` | SQLite database for article storage. |
| `days_back` | How many days of articles to fetch and display. |
| `feed_timeout` | Seconds to wait per feed. |
| `category_order` | Categories to display first in the feed list. |
| `timezone` | Timezone for article timestamps (e.g. `America/New_York`). |
| `obsidian.vault` | Obsidian vault name for export links. |
| `obsidian.folder` | Folder inside the vault for new notes. |
| `summarizer_endpoint` | Backend URL for the "Summarize" button. Set to `""` to disable. |

## Command-line flags

| Flag | Description |
|------|-------------|
| `--generate-config` | Write a sample `config.json` and exit. |
| `--render-only` | Skip fetching; regenerate HTML from the existing DB. |
| `--rebuild-articles` | Rebuild all per-article pages from the full DB. |
| `--backfill` | Update categories on existing DB rows from OPML. |
| `--category NAME` | Only fetch feeds in this category. |
| `--feed SUBSTRING` | Only fetch feeds whose URL contains this substring. |

## Output structure

```
public_html/
├── feeds.html              # today's articles
├── archive.html            # archive index
├── reading-list.html       # reading list (localStorage-based stars)
├── search.html             # client-side search page
├── search.db               # SQLite FTS database for client-side search
├── subscriptions.opml      # your OPML file (canonical copy lives here)
├── articles/
│   └── <slug>.html         # one page per article
└── archive/
    └── YYYY-MM-DD.html     # one page per past day
```

Serve `output_dir` with any static web server (nginx, Apache, Caddy, `python -m http.server`, etc.). This directory must be web-accessible — the OPML file lives there and is linked from the feed page for download.

## License

[CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/) — This work is dedicated to the public domain.
