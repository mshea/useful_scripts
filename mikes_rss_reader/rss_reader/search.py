import os
import sqlite3
from pathlib import Path


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
