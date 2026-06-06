import json
import os
from pathlib import Path


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

    cfg["opml_file"] = resolve(cfg.get("opml_file", "subscriptions.opml"))
    cfg["output_dir"] = resolve(cfg.get("output_dir", "public_html"))
    cfg["db_path"] = resolve(cfg.get("db_path", "feeds.db"))

    # If the OPML file isn't inside output_dir, symlink it there so the
    # web server can serve the download link (renderer uses basename only).
    opml_in_out = Path(cfg["output_dir"]) / Path(cfg["opml_file"]).name
    opml_path = Path(cfg["opml_file"])
    if opml_path.resolve() != opml_in_out.resolve():
        if opml_in_out.is_symlink():
            opml_in_out.unlink()
        if not opml_in_out.exists():
            opml_in_out.symlink_to(opml_path.resolve())

    out = Path(cfg["output_dir"])
    cfg["feeds_html"] = str(out / "feeds.html")
    cfg["archive_html"] = str(out / "archive.html")
    cfg["search_db"] = str(out / "search.db")
    cfg["articles_dir"] = str(out / "articles")
    cfg["archive_dir"] = str(out / "archive")

    cfg.setdefault("days_back", 1)
    cfg.setdefault("feed_timeout", 10)
    cfg.setdefault("category_order", [])
    cfg.setdefault("timezone", "America/New_York")
    cfg.setdefault("summarizer_endpoint", "")
    cfg.setdefault("retention_days", 30)
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
        "summarizer_endpoint": "",
        "retention_days": 30,
        "obsidian": {
            "vault": "My Vault",
            "folder": "Clippings"
        }
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sample, f, indent=2)
        f.write("\n")
    print(f"Wrote sample config to {path}")
