# Ebook Maker Scripts

Two scripts to convert markdownload files into clean EPUBs.

These scripts work hand-in-hand with the [Markdownload](https://addons.mozilla.org/en-US/firefox/addon/markdownload/) Firefox plugin which can download markdown files from HTML pages on the web.

## Script 1: clean_markdown.py

Cleans markdown files from markdownload format.

**What it does:**
- Adds chapter numbers based on file timestamps (oldest file = chapter 1)
- Auto-detects and removes common prefix words from filenames
- Cleans filenames
- Cleans heading
- Downloads images to local `images/` directory
- Removes all external links (keeps text, removes URL)
- Updates image references to local paths
- Removes extra blank lines
- Normalizes content formatting

**Usage:**
```bash
python3 scripts/clean_markdown.py <input_dir> <output_dir>
```

**Example:**
```bash
python3 scripts/clean_markdown.py original_markdown/my-book clean_markdown/my-book
```

**Note:** The output directory will be created if it doesn't exist.

---

## Script 2: build_epub.py

Creates EPUB files from cleaned markdown.

**What it does:**
- Converts cleaned markdown files to EPUB format
- Generates EPUB with each markdown file as a chapter
- Includes any local images from the `images/` directory
- Automatically selects cover image using this priority:
  1. Looks for `cover.jpg/png/webp/jpeg` in images directory
  2. Uses first image found in any markdown file
  3. Falls back to first image file in images directory

**Requirements:**
```bash
pip install markdown ebooklib
```

**Usage:**
```bash
python3 scripts/build_epub.py "Book Name"
```

**Example:**
```bash
python3 scripts/build_epub.py "Book Name"
```

**Input:** `clean_markdown/[book-name]/` directory
**Output:** Root directory (creates single EPUB file)

---

## Workflow

1. Place markdownload files in `original_markdown/[book-name]/`
2. Run `clean_markdown.py original_markdown/[book-name] clean_markdown/[book-name]` to clean and number the files
3. Run `build_epub.py "[book-name]"` to generate the EPUB
4. Find your EPUB in the root directory

---

## License

CC0 1.0 Universal (CC0 1.0) Public Domain Dedication

To the extent possible under law, the author(s) have dedicated all copyright and related and neighboring rights to this software to the public domain worldwide. This software is distributed without any warranty.

You should have received a copy of the CC0 Public Domain Dedication along with this software. If not, see <http://creativecommons.org/publicdomain/zero/1.0/>.
