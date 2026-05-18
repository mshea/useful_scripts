#!/usr/bin/env python3
"""
rss_reader.py — Parse an OPML subscription file, fetch RSS/Atom feeds, and
generate a static, dark-themed HTML feed reader with per-article pages,
category grouping, search across titles/sources/keywords, client-side read-later stars, and
Obsidian export links.

Features
- Fetches feeds and stores articles in SQLite.
- Generates a daily index page (feeds.html) grouped by OPML category.
- Creates per-article HTML pages with local navigation.
- Builds an archive index and per-day archive pages.
- Search via a stripped-down SQLite FTS database served to the client (titles, sources, keywords).
- Read-later list persisted in browser localStorage.
- Keyword extraction with YAKE for quick tagging.
- Obsidian vault links for clipping articles.
- "Summarize" button on article pages (configurable backend endpoint; see config.json).

Dependencies
- Python 3.10+
- feedparser
- yake

Install dependencies:
    pip install -r requirements.txt

Quick start
    python3 rss_reader.py --generate-config
    # Edit config.json, then run:
    python3 rss_reader.py

Command-line options
    -c, --config FILE     Path to JSON config file (default: config.json)
    --generate-config     Write a sample config.json and exit
    --render-only         Skip fetching; regenerate HTML from the existing DB.
    --rebuild-articles    Rebuild all per-article pages from the full DB.
    --backfill            Update category columns on existing DB rows from OPML.
    --category NAME       Only fetch feeds in this category.
    --feed SUBSTRING      Only fetch feeds whose URL contains this substring.

Configuration
All settings live in config.json. Relative paths are resolved against the
directory containing config.json. Environment variables prefixed with OPML_
can override any config value (e.g., OPML_OUTPUT_DIR=/var/www).

Output structure
    public_html/
    ├── feeds.html              # today's articles
    ├── archive.html            # archive index
    ├── search.db               # SQLite FTS database for client-side search
    ├── subscriptions.opml      # copy of the source OPML
    ├── articles/
    │   └── <slug>.html         # one page per article
    └── archive/
        └── YYYY-MM-DD.html     # one page per past day

License
    CC0 1.0 Universal — This work is dedicated to the public domain.
    See https://creativecommons.org/publicdomain/zero/1.0/
"""

# SPDX-License-Identifier: CC0-1.0
# To the extent possible under law, the author has waived all copyright
# and related or neighboring rights to this work.

import sys
import socket
import sqlite3
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
import feedparser
import html
import hashlib
import re
import urllib.parse
import os
import shutil
import json
import argparse
from zoneinfo import ZoneInfo


def _config_dir():
    """Return the directory used for resolving relative config paths."""
    return Path.cwd()


def load_config(config_path):
    """Load config.json and apply environment overrides.

    Returns a dict with all settings. Relative paths are resolved against
    the directory containing the config file.
    """
    path = Path(config_path).resolve()
    base = path.parent

    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Environment overrides: OPML_KEY_NAME maps to key.name
    for key, value in os.environ.items():
        if not key.startswith("OPML_"):
            continue
        parts = key[5:].lower().split("_", 1)
        if len(parts) == 1:
            cfg[parts[0]] = value
        else:
            cfg.setdefault(parts[0], {})[parts[1]] = value

    # Resolve relative paths against config file directory
    def resolve(val):
        if isinstance(val, str) and val and not os.path.isabs(val):
            return str(base / val)
        return val

    # Resolve relative paths against output_dir
    def resolve_out(val):
        if isinstance(val, str) and val and not os.path.isabs(val):
            return str(out / val)
        return val

    cfg["opml_file"] = resolve(cfg.get("opml_file", "subscriptions.opml"))
    cfg["output_dir"] = resolve(cfg.get("output_dir", "public_html"))
    cfg["db_path"] = resolve(cfg.get("db_path", "feeds.db"))

    out = Path(cfg["output_dir"])
    files = cfg.get("output_files", {})
    cfg["feeds_html"] = resolve_out(files.get("feeds_html", str(out / "feeds.html")))
    cfg["archive_html"] = resolve_out(files.get("archive_html", str(out / "archive.html")))
    cfg["search_db"] = resolve_out(files.get("search_db", str(out / "search.db")))
    cfg["opml_copy"] = resolve_out(files.get("opml_copy", str(out / "subscriptions.opml")))
    cfg["articles_dir"] = resolve_out(files.get("articles_dir", str(out / "articles")))
    cfg["archive_dir"] = resolve_out(files.get("archive_dir", str(out / "archive")))

    cfg.setdefault("days_back", 1)
    cfg.setdefault("feed_timeout", 10)
    cfg.setdefault("category_order", [])
    cfg.setdefault("timezone", "America/New_York")
    cfg.setdefault("summarizer_endpoint", "")
    cfg.setdefault("obsidian", {"vault": "My Vault", "folder": "Clippings"})
    return cfg


def generate_config(path="config.json"):
    """Write a sample config.json."""
    sample = {
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
        "summarizer_endpoint": "",
        "output_files": {
            "feeds_html": "feeds.html",
            "archive_html": "archive.html",
            "search_db": "search.db",
            "opml_copy": "subscriptions.opml",
            "articles_dir": "articles",
            "archive_dir": "archive"
        }
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sample, f, indent=2)
        f.write("\n")
    print(f"Wrote sample config to {path}")


def sanitize_content_styles(html_text):
    """Strip ALL inline style attributes from feed content so that publisher
    CSS (Blogger, WordPress mobile, etc.) doesn't fight the dark theme."""
    return re.sub(r'\s*style="[^"]*"', '', html_text, flags=re.IGNORECASE)


def strip_html(text):
    """Convert HTML to readable plain text with paragraph breaks."""
    text = re.sub(r'<(br|p|div|h[1-6]|li|tr|blockquote)(\s[^>]*)?>',
                  '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|div|h[1-6]|li|tr|blockquote)>',
                  '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = '\n'.join(line.strip() for line in text.splitlines())
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def sanitize_filename(name):
    """Remove characters invalid in Obsidian/filesystem file names."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#^[\]]', '', name)
    name = name.strip('. ')
    return name[:120]


def render_source_link(site_link, source_esc):
    if site_link:
        return f'<a href="{site_link}" class="src">{source_esc}</a>'
    return f'<span class="src">{source_esc}</span>'


PAGE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font:18px/1.25 Verdana,Geneva,sans-serif;color:#ffffff;background:#000000;padding:.75rem}
h1{font-size:.85rem;color:#555;margin-bottom:.5rem}
nav{font-size:1.1rem;margin-bottom:1rem}
nav a{color:#aaa;text-decoration:none;padding:.2rem 0;display:inline-block}
nav a:hover{color:#fff}
h2{font-size:.9rem;color:#777;text-transform:uppercase;letter-spacing:.05em;margin:1.5rem 0 .4rem}
ul{list-style:none}
li{padding:.5rem 0;border-bottom:1px solid #333}
li a{font-size:1em;color:#e8e8e8;text-decoration:none;overflow-wrap:break-word}
li a:visited{color:#888}
.src{font-size:.8rem;font-weight:bold;color:#aaa;text-decoration:none}
a.src:hover{text-decoration:underline}
.dt{font-size:.8rem;color:#555;margin-left:.4rem}
a.ext{font-size:.75rem;color:#444;text-decoration:none;margin-left:.3rem}
a.ext:hover{color:#888}
button.star{background:none;border:none;cursor:pointer;font-size:1.4em;color:#555;padding:0 0 0 .5rem;float:right;line-height:1}
button.star.saved{color:#f0c040}
@media(min-width:600px){body{max-width:780px;margin:1.5rem auto;padding:0 1rem}}
""".strip()


RL_FUNCTIONS_JS = """
const RL_KEY='readlater';
function getRL(){try{return JSON.parse(localStorage.getItem(RL_KEY)||'{}')}catch(e){return{}}}
function saveRL(d){localStorage.setItem(RL_KEY,JSON.stringify(d))}
""".strip()


STAR_JS = RL_FUNCTIONS_JS + """
document.querySelectorAll('button.star').forEach(function(btn){
  var slug=btn.dataset.slug;
  if(getRL()[slug]){btn.classList.add('saved');btn.textContent='★'}
  btn.addEventListener('click',function(){
    var rl=getRL();
    if(rl[slug]){delete rl[slug];btn.classList.remove('saved');btn.textContent='☆'}
    else{rl[slug]={slug:slug,title:btn.dataset.title,link:btn.dataset.link,source:btn.dataset.source};btn.classList.add('saved');btn.textContent='★'}
    saveRL(rl);
  });
});
""".strip()


def make_article_page_js(summarizer_endpoint):
    if not summarizer_endpoint:
        return RL_FUNCTIONS_JS.strip()
    return RL_FUNCTIONS_JS + """
var btn=document.querySelector('button.star');
var slug=btn.dataset.slug;
if(getRL()[slug]){btn.classList.add('saved');btn.textContent='★'}
btn.addEventListener('click',function(){
  var rl=getRL();
  if(rl[slug]){delete rl[slug];btn.classList.remove('saved');btn.textContent='☆'}
  else{rl[slug]={slug:slug,title:btn.dataset.title,link:btn.dataset.link,source:btn.dataset.source};
    btn.classList.add('saved');btn.textContent='★'}
  saveRL(rl);
});
var copybtn=document.getElementById('copy-btn');
copybtn.addEventListener('click',function(){
  var text=document.querySelector('.content').innerText;
  navigator.clipboard.writeText(text).then(function(){
    copybtn.textContent='Copied!';
    setTimeout(function(){copybtn.textContent='Copy article body'},2000);
  });
});
var sumbtn=document.getElementById('summarize-btn');
var sumbox=document.getElementById('summary-box');
sumbtn.addEventListener('click',function(){
  sumbtn.disabled=true;
  sumbtn.textContent='Summarizing...';
  sumbox.style.display='none';
  var text=document.querySelector('.content').innerText;
  var url=sumbtn.dataset.url;
  fetch('""" + summarizer_endpoint + """',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text,url:url})})
    .then(function(r){return r.json()})
    .then(function(d){
      sumbox.textContent=d.summary||d.error||'No summary returned.';
      sumbox.style.display='block';
      sumbtn.textContent='Summarize';
      sumbtn.disabled=false;
    })
    .catch(function(e){
      sumbox.textContent='Error: '+e.message;
      sumbox.style.display='block';
      sumbtn.textContent='Summarize';
      sumbtn.disabled=false;
    });
});
""".strip()




def parse_opml(opml_path):
    """Return list of (title, url, category) from OPML file."""
    tree = ET.parse(opml_path)
    root = tree.getroot()
    feeds = []
    body = root.find("body")
    if body is None:
        return feeds
    for group in body:
        group_url = group.get("xmlUrl") or group.get("xmlurl")
        if group_url:
            title = group.get("title") or group.get("text") or group_url
            feeds.append((title, group_url, ""))
            continue
        category = group.get("title") or group.get("text") or ""
        for outline in group:
            url = outline.get("xmlUrl") or outline.get("xmlurl")
            if not url:
                continue
            title = outline.get("title") or outline.get("text") or url
            feeds.append((title, url, category))
    return feeds


def init_db(db_path):
    """Open DB, ensure schema is current, return open connection."""
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            dt TEXT,
            title TEXT,
            link TEXT UNIQUE,
            source TEXT,
            site_link TEXT,
            summary TEXT,
            category TEXT DEFAULT '',
            feed_url TEXT DEFAULT '',
            content TEXT DEFAULT '',
            slug TEXT DEFAULT ''
        )
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(articles)").fetchall()}
    for col, defn in [
        ("category", "TEXT DEFAULT ''"),
        ("feed_url", "TEXT DEFAULT ''"),
        ("content", "TEXT DEFAULT ''"),
        ("slug", "TEXT DEFAULT ''"),
        ("keywords", "TEXT DEFAULT ''"),
    ]:
        if col not in cols:
            con.execute(f"ALTER TABLE articles ADD COLUMN {col} {defn}")
    fts_row = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='articles_fts'"
    ).fetchone()
    fts_sql = fts_row[0] if fts_row else ''
    if not fts_sql or ', keywords' not in fts_sql:
        con.execute("DROP TABLE IF EXISTS articles_fts")
        con.execute("""
            CREATE VIRTUAL TABLE articles_fts USING fts4(
                content='articles', title, source, summary, content, keywords
            )
        """)
    con.commit()
    return con


def rebuild_fts(con):
    con.execute("DELETE FROM articles_fts")
    con.execute("INSERT INTO articles_fts(docid, title, source, summary, content, keywords) SELECT rowid, title, source, summary, content, keywords FROM articles")
    con.commit()


def build_search_db(source_db, search_db_path):
    """Create a stripped-down DB for client-side search (title, source, and keywords only)."""
    if os.path.exists(search_db_path):
        os.remove(search_db_path)
    src = sqlite3.connect(source_db)
    dst = sqlite3.connect(search_db_path)
    dst.execute("""
        CREATE TABLE articles (
            dt TEXT,
            title TEXT,
            link TEXT,
            source TEXT,
            site_link TEXT,
            slug TEXT DEFAULT '',
            keywords TEXT DEFAULT ''
        )
    """)
    dst.execute("""
        CREATE VIRTUAL TABLE articles_fts USING fts4(
            content='articles', title, source, keywords
        )
    """)
    rows = src.execute(
        "SELECT dt, title, link, source, site_link, slug, keywords FROM articles"
    ).fetchall()
    dst.executemany(
        "INSERT INTO articles VALUES (?,?,?,?,?,?,?)", rows
    )
    dst.execute("""
        INSERT INTO articles_fts(docid, title, source, keywords)
        SELECT rowid, title, source, keywords FROM articles
    """)
    dst.commit()
    src.close()
    dst.close()
    print(f"Wrote search DB ({len(rows)} rows) to {search_db_path}")


def _normalize_quotes(text):
    """Convert smart quotes to ASCII so YAKE's stopword filter catches contractions."""
    return text.replace("'", "'").replace(""", '"').replace(""", '"')


def _is_subsumed(shorter, longer):
    """Return True if shorter phrase is a contiguous substring of longer phrase."""
    return shorter != longer and shorter in longer


def _clean_keyword(kw):
    """Strip leading/trailing punctuation and lowercase."""
    return kw.strip(".,;:!?-'\"()[]{}").lower()


_JUNK_WORDS = frozenset({
    "people", "things", "thing", "time", "times", "work", "game", "games",
    "year", "years", "day", "days", "way", "ways", "world", "life", "good",
    "bad", "great", "small", "big", "old", "new", "long", "short", "high",
    "low", "right", "left", "real", "true", "false", "full", "part",
    "feel", "felt", "hope", "yeah", "yes", "no", "oh", "ok", "okay",
    "look", "looked", "see", "saw", "know", "knew", "think", "thought",
    "say", "said", "tell", "told", "ask", "asked", "use", "used",
    "get", "got", "make", "made", "come", "came", "go", "went", "going",
    "take", "took", "give", "gave", "find", "found", "try", "tried",
    "need", "needed", "want", "wanted", "like", "liked", "love", "loved",
    "help", "helped", "start", "started", "turn", "turned", "put",
    "keep", "kept", "let", "lets", "set", "sets", "play", "played",
    "run", "ran", "move", "moved", "live", "lived", "show", "showed",
    "open", "opened", "close", "closed", "add", "added", "cut", "cuts",
    "end", "ends", "hit", "hits", "line", "lines", "kind", "sort",
    "lot", "bit", "piece", "place", "point", "case", "fact", "idea",
    "example", "reason", "question", "problem", "issue", "change",
    "number", "group", "hand", "head", "side", "area", "name", "home",
    "house", "room", "school", "job", "team", "word", "book", "paper",
    "page", "story", "note", "list", "item", "step", "tip", "post",
})


def extract_keywords(text, top=10):
    """Extract top keywords from article text using YAKE."""
    if not text or len(text) < 200:
        return ""
    try:
        import yake
        plain = re.sub(r'<[^>]+>', ' ', text)
        plain = re.sub(r'\s+', ' ', plain).strip()
        if len(plain) < 200:
            return ""
        plain = _normalize_quotes(html.unescape(plain))
        extractor = yake.KeywordExtractor(
            lan="en", n=2, dedupLim=0.9, top=top * 3, features=None
        )
        raw = extractor.extract_keywords(plain)

        cleaned = []
        for kw, score in raw:
            kw_lower = kw.lower()
            if "n't" in kw_lower or "'ve" in kw_lower or "'ll" in kw_lower or "'re" in kw_lower:
                continue
            kw_clean = _clean_keyword(kw)
            if not kw_clean or len(kw_clean) < 3:
                continue
            if re.fullmatch(r'\d+([.,]\d+)?', kw_clean):
                continue
            if " " not in kw_clean and kw_clean in _JUNK_WORDS:
                continue
            cleaned.append((kw_clean, score))

        deduped = []
        for i, (kw_i, s_i) in enumerate(cleaned):
            subsumed = False
            for j, (kw_j, s_j) in enumerate(cleaned):
                if i != j and _is_subsumed(kw_i, kw_j):
                    subsumed = True
                    break
            if not subsumed:
                deduped.append((kw_i, s_i))

        seen = set()
        result = []
        for kw, _ in deduped:
            if kw not in seen:
                seen.add(kw)
                result.append(kw.title())
            if len(result) >= top:
                break
        return ", ".join(result)
    except Exception:
        return ""


def insert_articles(articles, con):
    """Insert or replace a list of article dicts into an open DB connection."""
    con.executemany(
        "INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(a["dt"].isoformat(), a["title"], a["link"], a["source"], a["site_link"],
          a["summary"], a.get("category", ""), a.get("feed_url", ""), a.get("content", ""),
          article_slug(a["link"]), a.get("keywords", ""))
         for a in articles],
    )
    con.commit()


def fetch_and_save(feeds, cutoff, con, timeout):
    """Fetch feeds one by one, writing each feed's articles to DB immediately."""
    total = 0
    socket.setdefaulttimeout(timeout)
    for feed_title, url, category in feeds:
        try:
            d = feedparser.parse(url, request_headers={"User-Agent": "rss_reader/1.0"})
        except Exception as e:
            print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
            continue

        source = d.feed.get("title") or feed_title
        site_link = d.feed.get("link", "")
        articles = []

        for entry in d.entries:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub:
                dt = datetime(*pub[:6], tzinfo=timezone.utc)
            else:
                continue

            if dt < cutoff:
                continue

            link = entry.get("link", "")
            summary = entry.get("summary", "")
            content_list = entry.get("content", [])
            content = content_list[0].get("value", "") if content_list else summary

            title = entry.get("title", "")
            if not title:
                text = re.sub(r'\s+', ' ', strip_html(content)).strip()
                m = re.search(r'.*?[.!?](?=\s|$)', text)
                sentence = m.group(0) if m else text[:100]
                snippet = sentence[:100]
                title = (snippet[:97] + "..." if len(sentence) > 100 else snippet) or "(no title)"

            text_for_kw = " ".join({summary, content}) if summary and content != summary else (content or summary)
            keywords = extract_keywords(text_for_kw, top=10)

            articles.append({
                "dt": dt,
                "title": title,
                "link": link,
                "source": source,
                "site_link": site_link,
                "summary": summary,
                "content": content,
                "category": category,
                "feed_url": url,
                "keywords": keywords,
            })

        if articles:
            insert_articles(articles, con)
            print(f"  {source}: {len(articles)} articles")
            total += len(articles)

    print(f"Saved {total} articles total")


def backfill_categories(opml_path, db_path):
    """Update category on existing DB rows using OPML feed_url→category map."""
    feeds = parse_opml(opml_path)
    url_to_cat = {url: cat for _, url, cat in feeds}
    con = init_db(db_path)
    updated = 0
    for url, cat in url_to_cat.items():
        if not cat:
            continue
        cur = con.execute(
            "UPDATE articles SET category=? WHERE feed_url=? AND (category='' OR category IS NULL)",
            (cat, url),
        )
        updated += cur.rowcount
    con.commit()
    con.close()
    print(f"Backfilled categories on {updated} rows")


def load_db(db_path, cutoff=None):
    """Load articles from SQLite, sorted by dt desc. If cutoff given, only newer articles."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if cutoff:
        rows = con.execute(
            "SELECT * FROM articles WHERE dt >= ? ORDER BY dt DESC",
            (cutoff.isoformat(),),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM articles ORDER BY dt DESC"
        ).fetchall()
    con.close()
    articles = []
    for r in rows:
        a = dict(r)
        a["dt"] = datetime.fromisoformat(a["dt"])
        articles.append(a)
    return articles


def article_slug(link):
    return hashlib.md5(link.encode()).hexdigest()[:12]


def save_article_pages(articles, articles_dir, cfg):
    """Write one HTML file per article into articles_dir."""
    Path(articles_dir).mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(cfg["timezone"])
    summarizer_endpoint = cfg.get("summarizer_endpoint", "")
    js = make_article_page_js(summarizer_endpoint)
    vault = cfg["obsidian"]["vault"]
    folder = cfg["obsidian"]["folder"]
    feeds_html = os.path.basename(cfg["feeds_html"])

    for a in articles:
        slug = article_slug(a["link"])
        out = Path(articles_dir) / f"{slug}.html"
        title_esc = html.escape(re.sub(r'<[^>]+>', '', html.unescape(a["title"])))
        source_esc = html.escape(html.unescape(a["source"]))
        site_link = html.escape(a.get("site_link", ""))
        ext_link = html.escape(a["link"])
        dt_str = a["dt"].astimezone(tz).strftime("%B %-d, %Y %H:%M %Z")
        content = a.get("content") or a.get("summary") or ""
        content = sanitize_content_styles(content)
        source_html = render_source_link(site_link, source_esc)
        keywords = a.get("keywords", "")
        kw_html = f'<p class="keywords" style="font-size:1rem;color:#aaa;margin:.4rem 0 1rem">Keywords: {html.escape(keywords)}</p>' if keywords else ''
        summarize_btn = f' &bull; <button id="summarize-btn" class="summarize" data-url="{ext_link}">Summarize</button>' if summarizer_endpoint else ''
        summary_box = '<div id="summary-box"></div>' if summarizer_endpoint else ''

        note_name = sanitize_filename(html.unescape(a["title"]))
        note_content = (
            f"# {html.unescape(a['title'])}\n\n"
            f"**Source:** {html.unescape(a['source'])}\n"
            f"**Date:** {dt_str}\n"
            f"**URL:** {a['link']}\n\n"
            f"---\n\n"
            f"{strip_html(content)}"
        )
        obsidian_url = "obsidian://new?" + urllib.parse.urlencode({
            "vault": vault,
            "file": f"{folder}/{note_name}",
            "content": note_content,
        }, quote_via=urllib.parse.quote)

        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_esc}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font:20px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;color:#ffffff;background:#000000;padding:1rem;overflow-wrap:break-word}}
.meta{{font-size:1.1rem;line-height:1.4;color:#666;margin-bottom:0}}
.meta a{{color:#aaa;text-decoration:none;padding:.3rem 0;display:inline-block}}
.meta a:hover{{text-decoration:underline}}
h1{{font-size:1.4rem;color:#e8e8e8;margin-bottom:.75rem;line-height:1.3}}
.content{{max-width:700px;overflow-wrap:break-word;word-break:break-word;overflow-x:hidden;margin-top:.5rem}}
.content a{{color:#7aa;overflow-wrap:break-word;font-size:1.1em}}
.content img{{max-width:100%;height:auto}}
.content pre,.content code{{white-space:pre-wrap;word-break:break-all;overflow-x:auto}}
.content p{{margin:.85rem 0}}.content p:first-child{{margin-top:0}}
.content h2,.content h3{{color:#ccc;margin:1.2rem 0 .4rem}}
.content ul,.content ol{{margin:.75rem 0 .75rem 1.5rem}}
.content li{{margin:.3rem 0}}
.back{{font-size:1rem;margin-bottom:1rem}}
.back a{{color:#555;text-decoration:none;padding:.2rem 0;display:inline-block}}
.back a:hover{{color:#aaa}}
button.star{{background:none;border:none;cursor:pointer;font-size:1.1rem;color:#555;padding:0 .2rem;vertical-align:middle;line-height:1}}
button.star.saved{{color:#f0c040}}
button.copy{{background:none;border:none;cursor:pointer;font-size:1.1rem;color:#aaa;padding:.3rem 0;vertical-align:middle}}
button.summarize{{background:none;border:none;cursor:pointer;font-size:1.1rem;color:#aaa;padding:.3rem 0;vertical-align:middle}}
button.summarize:disabled{{color:#555;cursor:default}}
#summary-box{{margin:.75rem 0;padding:.6rem .75rem;background:#111;border-left:3px solid #555;color:#ccc;line-height:1.55;display:none}}
@media(min-width:600px){{body{{max-width:780px;margin:1.5rem auto;padding:0 1rem}}}}
</style>
</head>
<body>
<p class="back"><a href="/{feeds_html}">&larr; Feeds</a> &bull; <a href="/reading-list.html">&#9733; Reading List</a></p>
<h1>{title_esc}</h1>
<p class="meta">{source_html} &bull; <span>{dt_str}</span><br><a href="{ext_link}">Original</a> &bull; <a href="{obsidian_url}" class="obs">Save to Obsidian</a> &bull; <button id="copy-btn" class="copy">Copy article body</button>{summarize_btn} &bull; <button class="star" data-slug="{slug}" data-title="{title_esc}" data-link="{ext_link}" data-source="{source_esc}">&#9734;</button></p>{kw_html}
{summary_box}
<div class="content">{content}</div>
<script>{js}</script>
</body>
</html>
"""
        out.write_text(page, encoding="utf-8")
    print(f"Wrote {len(articles)} article pages to {articles_dir}")


def render_article_li(a):
    slug = article_slug(a["link"])
    dt_str = a["dt"].strftime("%b %-d, %H:%M")
    title_esc = html.escape(re.sub(r'<[^>]+>', '', html.unescape(a["title"])))
    source_esc = html.escape(html.unescape(a["source"]))
    local_link = f"/articles/{slug}.html"
    site_link = html.escape(a.get("site_link", ""))
    link_esc = html.escape(a["link"])
    source_html = render_source_link(site_link, source_esc)
    return (
        f'<li>'
        f'<button class="star" data-slug="{slug}" data-title="{title_esc}"'
        f' data-link="{link_esc}" data-source="{source_esc}">&#9734;</button>'
        f'<a href="{local_link}">{title_esc}</a>'
        f'<br>{source_html}'
        f'<span class="dt">{dt_str}</span></li>'
    )


def group_by_category(articles, category_order):
    """Return OrderedDict of category -> articles, preferred categories first."""
    groups = OrderedDict()
    for a in articles:
        cat = a.get("category") or "Uncategorized"
        groups.setdefault(cat, []).append(a)
    ordered = OrderedDict()
    for cat in category_order:
        if cat in groups:
            ordered[cat] = groups.pop(cat)
    ordered.update(groups)
    return ordered


def render_sections_and_nav(groups, extra_nav=""):
    """Return (nav_html, body_html) for a set of category groups."""
    sections = []
    for cat, cat_articles in groups.items():
        slug = cat.lower().replace(" ", "-")
        items = "\n".join(render_article_li(a) for a in cat_articles)
        sections.append(f'<h2 id="{slug}">{html.escape(cat)}</h2>\n<ul>{items}</ul>')

    body_html = "\n".join(sections) if sections else "<p>No articles found.</p>"

    nav_html = f'<nav>{extra_nav}</nav>' if extra_nav else ""

    return nav_html, body_html


def render_feed_page(title_tag, h1_html, nav_html, body_html):
    """Render a standard feed list page (today or archive day)."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_tag}</title>
<style>
{PAGE_CSS}
</style>
</head>
<body>
<h1>{h1_html}</h1>
{nav_html}
{body_html}
<script>{STAR_JS}</script>
</body>
</html>
"""


def render_html(articles, cfg):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    groups = group_by_category(articles, cfg["category_order"])
    count = len(articles)
    feeds_name = os.path.basename(cfg["feeds_html"])
    archive_name = os.path.basename(cfg["archive_html"])
    opml_name = os.path.basename(cfg["opml_copy"])
    nav_html, body_html = render_sections_and_nav(
        groups, extra_nav=f'<a href="/{archive_name}">Archive</a> &bull; <a href="/reading-list.html">&#9733; Reading List</a> &bull; <a href="/search.html">Search</a> &bull; <a href="/{opml_name}" download>OPML</a>'
    )
    page = render_feed_page("Feeds", f"{count} articles &bull; today &bull; {now_str}", nav_html, body_html)
    Path(cfg["feeds_html"]).write_text(page, encoding="utf-8")
    print(f"Wrote {count} articles to {cfg['feeds_html']}")


def render_archive_day(articles, day, cfg):
    """Render a single day's archive page."""
    day_str = day.strftime("%B %-d, %Y")
    groups = group_by_category(articles, cfg["category_order"])
    count = len(articles)
    feeds_name = os.path.basename(cfg["feeds_html"])
    archive_name = os.path.basename(cfg["archive_html"])
    nav_html, body_html = render_sections_and_nav(
        groups, extra_nav=f'<a href="/{archive_name}">Archive</a> &bull; <a href="/{feeds_name}">Today</a> &bull; <a href="/reading-list.html">&#9733; Reading List</a> &bull; <a href="/search.html">Search</a>'
    )
    page = render_feed_page(f"Feeds — {day_str}", f"{count} articles &bull; {day_str}", nav_html, body_html)
    Path(cfg["archive_dir"]).mkdir(parents=True, exist_ok=True)
    out = Path(cfg["archive_dir"]) / f"{day.strftime('%Y-%m-%d')}.html"
    out.write_text(page, encoding="utf-8")


def render_archive_index(days_counts, cfg):
    """Render the archive index page. days_counts is list of (date, count) desc."""
    today = datetime.now(timezone.utc).date()

    by_month = OrderedDict()
    for d, count in days_counts:
        key = (d.year, d.month)
        by_month.setdefault(key, []).append((d, count))

    sections = []
    for (year, month), entries in by_month.items():
        month_str = date(year, month, 1).strftime("%B %Y")
        items = []
        for d, count in entries:
            date_str = d.strftime("%A, %B %-d")
            feeds_name = os.path.basename(cfg["feeds_html"])
            if d == today:
                href = f"/{feeds_name}"
                label = f"Last 24 hours"
            else:
                href = f"/archive/{d.strftime('%Y-%m-%d')}.html"
                label = date_str
            items.append(
                f'<li><a href="{href}">{label}</a>'
                f'<span class="dt"> &bull; {count} articles</span></li>'
            )
        sections.append(f'<h2>{month_str}</h2>\n<ul>\n' + "\n".join(items) + "\n</ul>")

    list_html = "\n".join(sections) if sections else "<p>No archive yet.</p>"
    feeds_name = os.path.basename(cfg["feeds_html"])

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Feeds — Archive</title>
<style>
{PAGE_CSS}
</style>
</head>
<body>
<h1>Archive</h1>
<nav><a href="/{feeds_name}">Today</a></nav>
<ul>
{list_html}
</ul>
</body>
</html>
"""

    Path(cfg["archive_html"]).write_text(page, encoding="utf-8")
    print(f"Wrote archive index with {len(days_counts)} days to {cfg['archive_html']}")


def build_archives(db_path, cfg, today_count=None):
    """Build per-day archive pages from all articles in DB."""
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


def _script_dir():
    return Path(__file__).resolve().parent


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

    articles = load_db(cfg["db_path"], cutoff)
    save_article_pages(articles, cfg["articles_dir"], cfg)
    render_html(articles, cfg)

    days_counts = build_archives(cfg["db_path"], cfg, today_count=len(articles))
    render_archive_index(days_counts, cfg)
    search_db = str(Path(cfg["db_path"]).with_suffix('')) + "_search.db"
    build_search_db(cfg["db_path"], search_db)
    shutil.copy2(search_db, cfg["search_db"])
    print(f"Copied search.db to {cfg['search_db']}")
    shutil.copy2(cfg["opml_file"], cfg["opml_copy"])
    print(f"Copied OPML to {cfg['opml_copy']}")


if __name__ == "__main__":
    main()
