#!/usr/bin/env python3
"""
run.py — CLI entry point for the RSS reader.

Parses an OPML subscription file, fetches RSS/Atom feeds, and
generates a static, dark-themed HTML feed reader with per-article pages,
category grouping, search, client-side read-later stars, and Obsidian export links.

Usage:
    python3 run.py
    python3 run.py --render-only
    python3 run.py --rebuild-articles
    python3 run.py --generate-config
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rss_reader.config import load_config, generate_config
from rss_reader.db import init_db, load_db, rebuild_fts, purge_old_articles
from rss_reader.fetcher import parse_opml, fetch_and_save, backfill_categories
from rss_reader.search import build_search_db
from rss_reader.renderer import (
    save_article_pages,
    render_html,
    render_archive_day,
    render_archive_index,
    render_reading_list_page,
    render_search_page,
)


def _script_dir():
    return Path(__file__).resolve().parent


def build_archives(db_path, cfg, today_count=None):
    """Build per-day archive pages from all articles in DB."""
    from collections import OrderedDict
    Path(cfg["archive_dir"]).mkdir(parents=True, exist_ok=True)
    all_articles = load_db(db_path)

    by_day = OrderedDict()
    for a in all_articles:
        d = a["dt"].date()
        by_day.setdefault(d, []).append(a)

    today = datetime.now(timezone.utc).date()
    days_counts = []
    if today in by_day:
        count = today_count if today_count is not None else len(by_day[today])
        days_counts.append((today, count))
    for d in sorted(by_day.keys(), reverse=True):
        if d == today:
            continue
        articles = by_day[d]
        render_archive_day(articles, d, cfg)
        days_counts.append((d, len(articles)))

    print(f"Wrote {len(days_counts) - (1 if today in by_day else 0)} archive day pages to {cfg['archive_dir']}")
    return days_counts


def main():
    parser = argparse.ArgumentParser()
    default_config = str(_script_dir() / "config.json")
    parser.add_argument("-c", "--config", default=default_config, help="Path to JSON config file")
    parser.add_argument("--generate-config", action="store_true", help="Write a sample config.json and exit")
    parser.add_argument("--render-only", action="store_true", help="Skip fetching; regenerate HTML from existing DB")
    parser.add_argument("--rebuild-articles", action="store_true", help="Rebuild all article pages from full DB, then exit")
    parser.add_argument("--backfill", action="store_true", help="Backfill categories on existing DB rows from OPML, then render")
    parser.add_argument("--category", help="Only fetch feeds in this category (case-insensitive)")
    parser.add_argument("--feed", help="Only fetch feeds whose URL contains this string (case-insensitive)")
    args = parser.parse_args()

    if args.generate_config:
        generate_config(args.config)
        return

    cfg = load_config(args.config)
    days_back = int(cfg.get("days_back", 1))
    timeout = int(cfg.get("feed_timeout", 10))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    if args.rebuild_articles:
        save_article_pages(load_db(cfg["db_path"]), cfg["articles_dir"], cfg)
        return

    if args.backfill:
        backfill_categories(cfg["opml_file"], cfg["db_path"])
    elif not args.render_only:
        print(f"Parsing OPML: {cfg['opml_file']}")
        feeds = parse_opml(cfg["opml_file"])
        if args.category:
            feeds = [(t, u, c) for t, u, c in feeds if c.lower() == args.category.lower()]
        if args.feed:
            feeds = [(t, u, c) for t, u, c in feeds if args.feed.lower() in u.lower()]
        print(f"Found {len(feeds)} feeds. Fetching (cutoff: {cutoff.strftime('%Y-%m-%d %H:%M UTC')})...")
        con = init_db(cfg["db_path"])
        fetch_and_save(feeds, cutoff, con, timeout)
        rebuild_fts(con)
        con.close()

    # Build search DB before purging so search has all articles
    search_db = str(Path(cfg["db_path"]).with_suffix('')) + "_search.db"
    build_search_db(cfg["db_path"], search_db)
    shutil.copy2(search_db, cfg["search_db"])
    os.remove(search_db)
    print(f"Copied search.db to {cfg['search_db']}")

    # Purge articles older than retention_days
    retention = int(cfg.get("retention_days", 30))
    if retention > 0:
        con = init_db(cfg["db_path"])
        purge_old_articles(con, retention, cfg["articles_dir"], cfg["archive_dir"])
        con.close()

    articles = load_db(cfg["db_path"], cutoff)
    save_article_pages(articles, cfg["articles_dir"], cfg)

    # Also write pages for any articles missing their HTML (from archives/search)
    all_articles = load_db(cfg["db_path"])
    missing = [a for a in all_articles if not (Path(cfg["articles_dir"]) / f"{a['slug']}.html").exists()]
    if missing:
        print(f"Writing {len(missing)} missing article pages")
        save_article_pages(missing, cfg["articles_dir"], cfg)
    render_html(articles, cfg)

    days_counts = build_archives(cfg["db_path"], cfg, today_count=len(articles))
    render_archive_index(days_counts, cfg)

    render_reading_list_page(cfg)
    render_search_page(cfg)


if __name__ == "__main__":
    main()
