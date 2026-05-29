import socket
import sqlite3
import html
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import feedparser


def article_slug(link):
    return hashlib.md5(link.encode()).hexdigest()[:12]


def parse_opml(opml_path):
    """Return list of (title, url, category) from OPML file."""
    tree = ET.parse(opml_path)
    root = tree.getroot()
    feeds = []

    def walk(node, category):
        for outline in node.findall("outline"):
            url = outline.get("xmlUrl") or outline.get("xmlurl")
            text = outline.get("text", "")
            cat = outline.get("category", category)
            if url:
                feeds.append((text, url, cat))
            else:
                walk(outline, cat or text)

    walk(root, "")
    return feeds


def _parse_feed_date(entry):
    """Best-effort parse of a feed entry date."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return datetime.now(timezone.utc)


def fetch_and_save(feeds, cutoff, con, timeout=10):
    """Fetch each feed and insert/update articles in the database."""
    socket.setdefaulttimeout(timeout)
    inserted = updated = 0
    for title, url, category in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"  Feed error: {url}: {e}")
            continue
        for entry in parsed.entries:
            dt = _parse_feed_date(entry)
            if dt < cutoff:
                continue
            link = entry.get("link", "")
            if not link:
                continue
            slug = article_slug(link)
            content = entry.get("content", [{}])[0].get("value", "")
            summary = entry.get("summary", "")
            site_link = parsed.feed.get("link", "")
            existing = con.execute(
                "SELECT id FROM articles WHERE slug=?", (slug,)
            ).fetchone()
            if existing:
                con.execute(
                    "UPDATE articles SET title=?, summary=?, content=?, source=?, site_link=?, category=? WHERE slug=?",
                    (
                        entry.get("title", ""),
                        summary,
                        content,
                        title,
                        site_link,
                        category,
                        slug,
                    ),
                )
                updated += 1
            else:
                con.execute(
                    "INSERT INTO articles (slug, link, title, summary, content, dt, source, site_link, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        slug,
                        link,
                        entry.get("title", ""),
                        summary,
                        content,
                        dt.isoformat(),
                        title,
                        site_link,
                        category,
                    ),
                )
                inserted += 1
    con.commit()
    print(f"Inserted {inserted} articles, updated {updated}")


def backfill_categories(opml_file, db_path):
    """Update category on existing DB rows from OPML."""
    feeds = parse_opml(opml_file)
    con = sqlite3.connect(db_path)
    for title, url, category in feeds:
        con.execute(
            "UPDATE articles SET category=? WHERE source=?",
            (category, title),
        )
    con.commit()
    changed = con.total_changes
    con.close()
    print(f"Backfilled categories for {changed} rows")
