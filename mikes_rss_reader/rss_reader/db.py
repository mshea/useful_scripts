import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path


def init_db(db_path):
    """Open (or create) the SQLite database and set up tables."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """
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
        """
    )
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


def load_db(db_path, cutoff=None):
    """Return list of article dicts newer than cutoff (or all)."""
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


def rebuild_fts(con):
    """Rebuild the FTS virtual table from scratch."""
    con.execute("DELETE FROM articles_fts")
    con.execute("INSERT INTO articles_fts(docid, title, source, summary, content, keywords) SELECT rowid, title, source, summary, content, keywords FROM articles")
    con.commit()


def purge_old_articles(con, retention_days, articles_dir, archive_dir):
    """Delete articles older than retention_days from DB and remove orphan HTML files."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_str = cutoff.isoformat()
    old = con.execute(
        "SELECT slug FROM articles WHERE dt < ?", (cutoff_str,)
    ).fetchall()
    if old:
        slugs = [r[0] for r in old if r[0]]
        removed_files = 0
        articles_path = Path(articles_dir)
        for slug in slugs:
            p = articles_path / f"{slug}.html"
            if p.exists():
                p.unlink()
                removed_files += 1
        con.execute("DELETE FROM articles WHERE dt < ?", (cutoff_str,))
        rebuild_fts(con)
        con.commit()
        print(f"Purged {len(old)} articles older than {retention_days} days; removed {removed_files} HTML files")
    else:
        print(f"No articles older than {retention_days} days to purge")

    # Clean orphan article pages (files with no matching DB row)
    db_slugs = {r[0] for r in con.execute("SELECT slug FROM articles WHERE slug != ''").fetchall()}
    articles_path = Path(articles_dir)
    orphan_articles = 0
    for p in articles_path.glob("*.html"):
        slug = p.stem
        if slug not in db_slugs:
            p.unlink()
            orphan_articles += 1
    if orphan_articles:
        print(f"Removed {orphan_articles} orphan article pages")

    # Clean orphan archive pages (dates with no articles left in DB)
    db_dates = {r[0] for r in con.execute("SELECT DISTINCT DATE(dt) FROM articles").fetchall()}
    archive_path = Path(archive_dir)
    orphan_archives = 0
    for p in archive_path.glob("*.html"):
        if p.stem not in db_dates:
            p.unlink()
            orphan_archives += 1
    if orphan_archives:
        print(f"Removed {orphan_archives} orphan archive pages")

    con.execute("VACUUM")
