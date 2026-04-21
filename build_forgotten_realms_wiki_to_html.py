#!/usr/bin/env python3
"""
forgotten-realms-wiki-to-html
=============================
Converts the Forgotten Realms Wiki XML dump into a self-contained static
website you can browse offline directly from the filesystem — no web server
required.

Getting the XML dump
--------------------
The Forgotten Realms Wiki publishes a monthly XML dump via Fandom:

  1. Go to https://forgottenrealms.fandom.com/wiki/Special:Statistics
  2. Click "Database download" in the sidebar, or go directly to the dump
     index at https://s3.amazonaws.com/wikia_xml_dumps/f/fo/
  3. Download the latest forgottenrealms_pages_current.xml.7z
  4. Decompress it:  7z x forgottenrealms_pages_current.xml.7z

The resulting .xml file is what you pass to this script.

License
-------
CC0 1.0 Universal — public domain dedication.
https://creativecommons.org/publicdomain/zero/1.0/
Do whatever you want with this code.

Note: the *wiki content* itself is not covered by this license. Check the
source wiki's license before distributing the generated HTML. The Forgotten
Realms Wiki content is CC BY-SA 3.0.

Requirements
------------
    pip install mwparserfromhell

Usage
-----
    # Prompts for paths interactively:
    python3 build_forgotten_realms_wiki_to_html.py

    # Or pass paths directly to skip the prompts:
    python3 build_forgotten_realms_wiki_to_html.py dump.xml ./wiki-html

    # Random sample of N articles (useful for testing):
    python3 build_forgotten_realms_wiki_to_html.py dump.xml ./wiki-html --sample 300

    # Only articles whose title contains TEXT:
    python3 build_forgotten_realms_wiki_to_html.py dump.xml ./wiki-html --filter "Elminster"

    # Stop after N articles:
    python3 build_forgotten_realms_wiki_to_html.py dump.xml ./wiki-html --limit 1000

    # Regenerate style.css and search.js only (no article conversion):
    python3 build_forgotten_realms_wiki_to_html.py --assets-only ./wiki-html

    # Use 8 parallel worker processes for faster conversion:
    python3 build_forgotten_realms_wiki_to_html.py dump.xml ./wiki-html --workers 8

Output layout
-------------
    index.html                      home page with category list and live search
    categories.html                 top 200 wiki category tags by article count
    style.css                       stylesheet (black & white, mobile-friendly)
    search.js                       client-side search logic
    search-data.js                  generated title index (~5 MB for 72k articles)
    pages/<category>/index.html     A–Z article index for each broad category
    pages/<category>/<slug>.html    one file per article
    pages/<wiki-cat>/<slug>.html    wiki category hierarchy pages

All hrefs are root-relative and resolved via <base href>, so the folder can
be moved anywhere and opened with any browser via file://.

Note: the script appends a forgotten_realms_wiki/ subdirectory to whatever
output path you supply, so output lands at <your-path>/forgotten_realms_wiki/.

How it works
------------
Seven passes (first two are SAX streaming over the XML, rest are in-memory):

  Pass 1 — collect every main-namespace title, detect its broad category from
            the infobox template name or [[Category:...]] tags, and build a
            title → output-path map used to resolve [[wikilinks]]. Also counts
            all raw [[Category:...]] tags and builds the category tree.

  Pass 2 — for each article: strip citations and non-article templates with
            mwparserfromhell, extract the infobox as a full-width block at the
            top of the page, then convert the remaining wikitext line-by-line
            to HTML. Stub articles (no prose and no infobox) are skipped.

  Pass 3 — write one A–Z index.html per broad category.

  Pass 4 — write wiki category hierarchy pages (one per raw [[Category:...]] tag).

  Pass 5 — write categories.html listing the top 200 wiki categories by count.

  Pass 6 — write search-data.js title index.

  Pass 7 — write root index.html.

Configuration
-------------
SITE_NAME, ATTRIBUTION, and the category maps below are the main things you
might want to adjust. Paths are supplied at runtime, not hardcoded here.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor
import html as hl
import json
import random
import re
import xml.sax
import xml.sax.handler
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import mwparserfromhell as mwp


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


SITE_NAME   = 'Forgotten Realms Wiki'
ATTRIBUTION = 'Forgotten Realms Wiki · CC BY-SA 3.0 · forgottenrealms.fandom.com'

# Top-level browse sections shown on the home page.
# Each entry is (Section Title, [wiki category names to link to]).
BROWSE_SECTIONS = [
    ('Geography',          ['Locations']),
    ('Characters',         ['Inhabitants']),
    ('Organizations',      ['Organizations', 'Organizations on Toril']),
    ('Items & Artifacts',  ['Items', 'Magic items', 'Weapons', 'Armor']),
    ('Spells',             ['Wizard spells', 'Cleric spells', 'Sorcerer spells']),
    ('Creatures',          ['Creatures', 'Undead', 'Dragons']),
    ('Events & History',   ['Events on Toril']),
    ('Deities & Religion', ['Deities']),
    ('Books & Media',      ['Books']),
]

# Map infobox template names → broad category shown on the home page.
# Add entries here to reclassify article types.
INFOBOX_TO_CAT = {
    'person':        'Characters',
    'npc':           'Characters',
    'creature':      'Creatures',
    'race':          'Creatures',
    'dragon':        'Creatures',
    'fungus':        'Creatures',
    'plant':         'Creatures',
    'location':      'Places',
    'building':      'Places',
    'settlement':    'Places',
    'celestial body':'Places',
    'spell':         'Spells',
    'psionic power': 'Spells',
    'item':          'Items & Artifacts',
    'bgitem':        'Items & Artifacts',
    'substance':     'Items & Artifacts',
    'ship':          'Items & Artifacts',
    'book':          'Books & Media',
    'bookiu':        'Books & Media',
    'real':          'Books & Media',
    'game':          'Books & Media',
    'computer game': 'Books & Media',
    'organization':  'Organizations',
    'deity':         'Deities & Religion',
    'demigod':       'Deities & Religion',
    'class':         'Classes',
    'event':         'Events & History',
    'conflict':      'Events & History',
    'disease':       'Events & History',
    'language':      'Languages & Peoples',
    'ethnicity':     'Languages & Peoples',
}

# Fallback: match substrings in [[Category:...]] tags when no infobox is found.
# First match wins, so put more specific terms before general ones.
WIKICAT_RULES = [
    ('inhabitant',  'Characters'),
    ('wizard',      'Characters'), ('fighter',  'Characters'),
    ('cleric',      'Characters'), ('rogue',    'Characters'),
    ('ranger',      'Characters'), ('bard',     'Characters'),
    ('paladin',     'Characters'), ('druid',    'Characters'),
    ('warrior',     'Characters'),
    ('dragon',      'Creatures'),  ('undead',   'Creatures'),
    ('lycanthrope', 'Creatures'),  ('monster',  'Creatures'),
    ('creature',    'Creatures'),  ('plant',    'Creatures'),
    ('fungus',      'Creatures'),  ('vegetation','Creatures'),
    ('settlement',  'Places'),     ('city',     'Places'),
    ('region',      'Places'),     ('nation',   'Places'),
    ('plane',       'Places'),     ('location', 'Places'),
    ('celestial',   'Places'),
    ('cantrip',     'Spells'),     ('spell',    'Spells'),
    ('psionic',     'Spells'),
    ('magic item',  'Items & Artifacts'), ('artifact', 'Items & Artifacts'),
    ('weapon',      'Items & Artifacts'), ('armor',    'Items & Artifacts'),
    ('substance',   'Items & Artifacts'), ('watercraft','Items & Artifacts'),
    ('vessel',      'Items & Artifacts'), ('vehicle',  'Items & Artifacts'),
    ('item',        'Items & Artifacts'),
    ('novel',       'Books & Media'), ('sourcebook', 'Books & Media'),
    ('comic',       'Books & Media'), ('adventure',  'Books & Media'),
    ('book',        'Books & Media'), ('game',       'Books & Media'),
    ('guild',       'Organizations'), ('organization', 'Organizations'),
    ('religion',    'Deities & Religion'), ('deity', 'Deities & Religion'),
    ('war',         'Events & History'), ('battle',   'Events & History'),
    ('conflict',    'Events & History'), ('event',    'Events & History'),
    ('festival',    'Events & History'), ('holiday',  'Events & History'),
    ('plague',      'Events & History'), ('disease',  'Events & History'),
    ('ceremony',    'Events & History'), ('war',      'Events & History'),
    ('language',    'Languages & Peoples'), ('ethnicity', 'Languages & Peoples'),
    ('dialect',     'Languages & Peoples'),
]

# Infobox params that are purely visual (images, captions) — skipped in output.
SKIP_INFOBOX_PARAMS = {'image', 'caption', 'caption2', 'map', 'map caption', 'map image'}

# Templates stripped from inline text entirely.
STRIP_TEMPLATES = {
    'defaultsort', 'ga', 'fa', 'dab', 'stub', 'sectstub', 'incomplete',
    'cleanup', 'refs', 'refonly', 'appearances', 'index', 'ftb', 'si',
    'yearlinkname', 'yearlink', 'yearbox', 'lunarcalendarbox', 'roll of years',
    'hatnote', 'otheruses', 'otheruses4', 'redirect', 'about', 'for',
    'nocat', 'interlang', 'featured article', 'good article',
}

# Templates where only the first positional parameter is kept as plain text.
PASSTHROUGH_TEMPLATES = {'w', 'wikipedia', 'smallcaps', 'nowrap', 'lang', 'p', 'plainlist'}

# Namespace prefixes whose wikilinks are stripped (no page generated for them).
STRIP_LINK_NAMESPACES = (
    'file:', 'image:', 'category:', 'user:', 'user talk:',
    'template:', 'talk:', 'special:', 'help:', 'wikipedia:',
)

# Category tags to exclude from categories.html (image dumps, metadata, templates).
_CAT_NOISE = re.compile(
    r'^(images?\b|maps?\b|screenshots?\b|photographs?\b|concept art|'
    r'sourcebook (covers?|back)|novel covers?|signatures?\b|symbols?\b|'
    r'2nd edition maps?|year of|age of humanity|the present age|'
    r'illustrations?\b)',
    re.I
)

# Derived sets used internally — do not edit these directly.
_STRIP_NS      = frozenset(ns.rstrip(':') for ns in STRIP_LINK_NAMESPACES)
_INFOBOX_NAMES = frozenset(INFOBOX_TO_CAT) | {'adventure'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(s):
    """Convert a title to a filesystem-safe URL slug."""
    s = re.sub(r'[^\w\s\-]', '', s)
    s = re.sub(r'\s+', '-', s.strip())
    return s[:80].lower()


def safe_href(path):
    """URL-encode a root-relative path for use in an href attribute."""
    return quote(str(path), safe='/')


def detect_category(wikitext):
    """
    Determine the broad category for an article.

    Checks infobox template names first (most reliable), then falls back to
    [[Category:...]] tags. Articles with neither go to Miscellaneous.
    """
    # Scan all templates near the top; return on the first infobox match.
    # We scan all (not just the first) because utility templates like
    # {{redirect}} or {{GA}} often appear before the real infobox.
    for m in re.finditer(r'\{\{\s*([A-Za-z][A-Za-z ]*)', wikitext[:800]):
        name = m.group(1).strip().lower()
        for key, cat in INFOBOX_TO_CAT.items():
            if name == key or name.startswith(key + ' '):
                return cat

    for cat_tag in re.findall(r'\[\[Category:([^\]|]+)', wikitext, re.I):
        text = cat_tag.lower()
        for keyword, cat in WIKICAT_RULES:
            if keyword in text:
                return cat

    return 'Miscellaneous'


# ---------------------------------------------------------------------------
# Wikitext → HTML conversion
# ---------------------------------------------------------------------------

# Pre-compiled patterns used by the inline converter
_RE_REF_SELF    = re.compile(r'<ref\b[^>]*/>', re.I)
_RE_REF_BLOCK   = re.compile(r'<ref\b[^>]*>.*?</ref>', re.I | re.S)
_RE_REF_CONTENT = re.compile(r'<ref\b[^>]*>(.*?)</ref>', re.I | re.S)
_RE_NOWIKI    = re.compile(r'<nowiki>(.*?)</nowiki>', re.I | re.S)
_RE_COMMENT   = re.compile(r'<!--.*?-->', re.S)
_RE_GALLERY   = re.compile(r'<gallery\b[^>]*>.*?</gallery>', re.I | re.S)
_RE_MATH      = re.compile(r'<math\b[^>]*>.*?</math>', re.I | re.S)
_RE_BR        = re.compile(r'<br\s*/?>', re.I)
_RE_WIKILINK  = re.compile(r'\[\[([^\]]+)\]\]')
_RE_BOLD_EM   = re.compile(r"'''(.+?)'''", re.S)
_RE_EM        = re.compile(r"''(.+?)''", re.S)
_RE_EXT_LINK  = re.compile(r'\[https?://\S+ ([^\]]+)\]')
_RE_EXT_BARE  = re.compile(r'\[https?://\S+\]')
_RE_TPL       = re.compile(r'\{\{((?:[^{}]|\{\{[^{}]*\}\})*)\}\}')
_RE_SAFE_TAGS = re.compile(r'<(?!/?(strong|em|br|a|b|i|code|span|sup|sub)[\s>/])[^>]*>',
                           re.I)

# Populated during pass 1.
_title_map:       dict[str, str]       = {}                 # lower title → output path
_cat_map:         dict[str, str]       = {}                 # cat slug → original category name
_wiki_cat_counts: dict[str, int]       = defaultdict(int)   # raw wiki category → article count
_cat_parents:     dict[str, list[str]] = defaultdict(list)  # category → parent categories
_cat_children:    dict[str, list[str]] = defaultdict(list)  # category → child categories


def _replace_template(m):
    """
    Collapse a single {{template}} occurrence in inline text.
    Returns rendered text or empty string.
    """
    inner = m.group(1)
    name_part, _, rest = inner.partition('|')
    name   = name_part.strip().lower()
    params = [p.strip() for p in rest.split('|')] if rest else []

    if name in STRIP_TEMPLATES:
        return ''
    if name.startswith(('cite ', 'cite/', 'citation')):
        return ''
    if name in PASSTHROUGH_TEMPLATES:
        return params[0] if params else ''
    if 'year' in name or name in ('th', 'nth'):
        return params[0] if params else ''

    # Unknown template: keep positional params as plain text
    positional = [p for p in params if '=' not in p]
    return ' '.join(positional) if positional else ''


def _strip_templates(s):
    """Iteratively collapse {{templates}} up to four levels deep."""
    for _ in range(4):
        prev = s
        s = _RE_TPL.sub(_replace_template, s)
        if s == prev:
            break
    return s


def _replace_wikilink(m):
    """Replace a [[wikilink]] with an <a> tag or a .nl span for dead links."""
    inner    = m.group(1)
    ns_lower = inner.split(':')[0].lower() if ':' in inner else ''
    if ns_lower in _STRIP_NS:
        return ''
    # Strip interlanguage links (2–3 letter language codes like fr:, de:, es:)
    if re.match(r'^[a-z]{2,3}$', ns_lower):
        return ''

    target, sep, display = inner.partition('|')
    target  = target.strip()
    display = display.strip() if sep else target

    anchor = ''
    if '#' in target:
        target, fragment = target.split('#', 1)
        anchor = '#' + re.sub(r'[^\w\-]', '_', fragment)

    # Apply bold/italic to display before HTML-escaping, then restore the tags
    disp = _RE_BOLD_EM.sub(r'<strong>\1</strong>', display)
    disp = _RE_EM.sub(r'<em>\1</em>', disp)
    disp = hl.escape(disp)
    disp = (disp.replace('&lt;strong&gt;', '<strong>').replace('&lt;/strong&gt;', '</strong>')
                .replace('&lt;em&gt;', '<em>').replace('&lt;/em&gt;', '</em>'))
    path = _title_map.get(target.lower())
    if path:
        return f'<a href="{safe_href(path)}{anchor}">{disp}</a>'
    return f'<span class="nl">{disp}</span>'


def inline(s):
    """Convert inline wikitext (bold, italic, links, templates) to HTML."""
    s = _RE_REF_BLOCK.sub('', s)
    s = _RE_REF_SELF.sub('', s)
    s = _RE_NOWIKI.sub(lambda m: hl.escape(m.group(1)), s)
    s = _RE_BR.sub('<br>', s)
    s = _strip_templates(s)
    s = _RE_WIKILINK.sub(_replace_wikilink, s)
    s = _RE_EXT_LINK.sub(r'<a href="#" rel="noopener">\1</a>', s)
    s = _RE_EXT_BARE.sub('', s)
    s = _RE_BOLD_EM.sub(r'<strong>\1</strong>', s)
    s = _RE_EM.sub(r'<em>\1</em>', s)
    s = _RE_SAFE_TAGS.sub('', s)   # strip unknown HTML tags
    return s.strip()


def _is_infobox(template_name):
    n = template_name.strip().lower()
    return any(n == k or n.startswith(k + ' ') or n.startswith(k + '/') for k in _INFOBOX_NAMES)


def render_infobox(template):
    """
    Convert a MediaWiki infobox template to an HTML <aside> table.
    Skips image/caption params and footnote references.
    """
    name = str(template.name).strip()
    rows = []
    for param in template.params:
        key   = str(param.name).strip()
        key_l = key.lower()
        if key.isdigit():
            continue
        if key_l in SKIP_INFOBOX_PARAMS:
            continue
        if 'ref' in key_l and key_l not in ('reference', 'references'):
            continue

        raw = str(param.value).strip()
        if not raw:
            continue

        raw = _RE_GALLERY.sub('', raw)
        raw = _RE_REF_BLOCK.sub('', raw)
        raw = _RE_REF_SELF.sub('', raw)

        val = inline(raw)
        if not val:
            continue

        rows.append(f'<tr><th>{hl.escape(key)}</th><td>{val}</td></tr>')

    if not rows:
        return ''

    return (
        f'<aside class="infobox">'
        f'<div class="infobox-title">{hl.escape(name)}</div>'
        f'<table>{"".join(rows)}</table>'
        f'</aside>'
    )


def render_wikitable(block_lines):
    """Convert a MediaWiki {| table block to an HTML <table>."""
    out    = ['<table>']
    in_row = False

    for raw in block_lines[1:]:   # first line is the opening {| ... line
        ln = raw.strip()
        if ln == '|}':
            break
        if ln.startswith('|-'):
            if in_row:
                out.append('</tr>')
            out.append('<tr>')
            in_row = True
            continue
        if ln.startswith('!'):
            if not in_row:
                out.append('<tr>')
                in_row = True
            for cell in re.split(r'!!', ln[1:]):
                cell = re.sub(r'^[^|]*\|(?!\|)', '', cell.strip()).strip()
                out.append(f'<th>{inline(cell)}</th>')
            continue
        if ln.startswith('|') and not ln.startswith('|}'):
            if not in_row:
                out.append('<tr>')
                in_row = True
            for cell in re.split(r'\|\|', ln[1:]):
                cell = re.sub(r'^[^|]*\|(?!\|)', '', cell.strip()).strip()
                out.append(f'<td>{inline(cell)}</td>')
            continue
        # continuation line inside a cell
        if out and out[-1].endswith('</td>'):
            out[-1] = out[-1][:-5] + ' ' + inline(ln) + '</td>'

    if in_row:
        out.append('</tr>')
    out.append('</table>')
    return '\n'.join(out)


def _collect_sources(wikitext):
    """
    Extract unique source names from <ref> citation templates in raw wikitext.
    Returns a list of plain-text strings (deduplicated, in order of first appearance).
    """
    seen    = set()
    sources = []
    for m in _RE_REF_CONTENT.finditer(wikitext):
        content = m.group(1).strip()
        for tpl_m in _RE_TPL.finditer(content):
            inner     = tpl_m.group(1)
            name_part, _, rest = inner.partition('|')
            tpl_name  = name_part.strip().lower()
            if not tpl_name.startswith(('cite ', 'cite/', 'citation')):
                continue
            params = [p.strip() for p in rest.split('|')] if rest else []
            slash = name_part.find('/')
            if slash != -1:
                rest_name = name_part[slash + 1:]
                second_slash = rest_name.find('/')
                if second_slash != -1:
                    before = rest_name[:second_slash].strip()
                    after  = rest_name[second_slash + 1:].strip()
                    # Magazine: Cite dragon/123/Article Title -> "Article Title"
                    # Book edition: Cite book/Title/Hardcover -> "Title"
                    title = after if re.match(r'^\d+$', before) else before
                else:
                    title = rest_name.strip()
            else:
                title = next((p.split('=', 1)[1].strip()
                               for p in params if re.match(r'title\s*=', p, re.I)), '')
                if not title:
                    title = next((p for p in params
                                  if '=' not in p and not p.strip().isdigit()), '')
            title = title.strip("'\"")
            if title and title not in seen:
                seen.add(title)
                sources.append(title)
    return sources


def convert_page(wikitext):
    """
    Convert a full article's wikitext to (infobox_html, body_html, categories).

    Uses mwparserfromhell to cleanly extract the infobox template and category
    links before doing line-by-line conversion of the body text.
    """
    sources = _collect_sources(wikitext)

    # Strip block-level noise before parsing
    wikitext = _RE_COMMENT.sub('', wikitext)
    wikitext = _RE_GALLERY.sub('', wikitext)
    wikitext = _RE_MATH.sub('', wikitext)

    code = mwp.parse(wikitext)

    # Extract infobox and strip all other templates
    infobox_html = ''
    for tpl in code.filter_templates(recursive=False):
        name = str(tpl.name).strip()
        if _is_infobox(name) and not infobox_html:
            infobox_html = render_infobox(tpl)
        try:
            code.remove(tpl)
        except Exception:
            pass

    # Extract [[Category:...]] links; strip non-article namespaces
    categories = []
    for wl in code.filter_wikilinks():
        target = str(wl.title).strip()
        target_l = target.lower()
        if target_l.startswith('category:'):
            categories.append(target[9:].strip())
        if target_l.startswith('category:') or any(target_l.startswith(ns)
                                                    for ns in STRIP_LINK_NAMESPACES):
            try:
                code.remove(wl)
            except Exception:
                pass

    body = str(code)

    # Line-by-line conversion
    lines = body.split('\n')
    out   = []
    in_ul = in_ol = False
    i     = 0

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append('</ul>')
            in_ul = False
        if in_ol:
            out.append('</ol>')
            in_ol = False

    while i < len(lines):
        ln = lines[i].rstrip()

        # WikiTable block
        if ln.strip().startswith('{|'):
            close_lists()
            block = [ln]
            i += 1
            while i < len(lines):
                block.append(lines[i])
                if lines[i].strip() == '|}':
                    i += 1
                    break
                i += 1
            out.append(render_wikitable(block))
            continue

        # Heading:  == text ==
        m = re.match(r'^(={2,6})\s*(.+?)\s*\1\s*$', ln)
        if m:
            close_lists()
            level = len(m.group(1))
            text  = m.group(2)
            if text.strip().lower() in ('further reading', 'external links', 'external link',
                                        'connections', 'appendix', 'gallery', 'appearances',
                                        'references', 'notes', 'see also'):
                # Skip this heading and all lines until the next same-or-higher heading
                i += 1
                while i < len(lines):
                    nxt = lines[i].rstrip()
                    nm = re.match(r'^(={2,6})\s*(.+?)\s*\1\s*$', nxt)
                    if nm and len(nm.group(1)) <= level:
                        break
                    i += 1
                continue
            aid   = re.sub(r'[^\w\-]', '_', text)
            out.append(f'<h{level} id="{aid}">{inline(text)}</h{level}>')
            i += 1
            continue

        # Unordered list:  * item
        if ln.startswith('*'):
            if in_ol:
                out.append('</ol>')
                in_ol = False
            if not in_ul:
                out.append('<ul>')
                in_ul = True
            out.append(f'<li>{inline(ln.lstrip("*").strip())}</li>')
            i += 1
            continue

        # Ordered list:  # item
        if ln.startswith('#') and not ln.upper().startswith('#REDIRECT'):
            if in_ul:
                out.append('</ul>')
                in_ul = False
            if not in_ol:
                out.append('<ol>')
                in_ol = True
            out.append(f'<li>{inline(ln.lstrip("#").strip())}</li>')
            i += 1
            continue

        # Definition term:  ; term  (often used as a bold label before : definitions)
        if ln.startswith(';'):
            close_lists()
            term = ln.lstrip(';').strip().rstrip(':').strip()
            if term:
                out.append(f'<p><strong>{inline(term)}</strong></p>')
            i += 1
            continue

        # Indented line:  : text
        if ln.startswith(':'):
            close_lists()
            out.append(f'<p class="indent">{inline(ln.lstrip(":").strip())}</p>')
            i += 1
            continue

        # Horizontal rule:  ----
        if re.match(r'^-{4,}$', ln.strip()):
            close_lists()
            out.append('<hr>')
            i += 1
            continue

        # Blank line
        if not ln.strip():
            close_lists()
            i += 1
            continue

        # Redirect page
        if ln.strip().lower().startswith('#redirect'):
            wl_m = _RE_WIKILINK.search(ln)
            if wl_m:
                target = wl_m.group(1).split('|')[0].strip()
                path   = _title_map.get(target.lower())
                if path:
                    out.append(
                        f'<p class="redirect">Redirects to: '
                        f'<a href="{safe_href(path)}">{hl.escape(target)}</a></p>'
                    )
            i += 1
            continue

        # Paragraph
        close_lists()
        text = inline(ln.strip())
        if text and text != '<br>':
            out.append(f'<p>{text}</p>')
        i += 1

    close_lists()

    # Remove headings with no content before the next same-or-shallower heading.
    # A deeper sub-heading (e.g. h3 inside h2) is not content by itself, but
    # non-heading items (p, ul, table…) anywhere in the section count.
    filtered = []
    for idx, item in enumerate(out):
        m_lvl = re.match(r'<h([2-6])[\s>]', item)
        if m_lvl:
            level = int(m_lvl.group(1))
            has_content = False
            for subsequent in out[idx + 1:]:
                if not subsequent.strip():
                    continue
                sm = re.match(r'<h([2-6])[\s>]', subsequent)
                if sm:
                    if int(sm.group(1)) <= level:
                        break  # reached peer/parent heading with no real content
                    # deeper heading — keep scanning
                else:
                    has_content = True
                    break
            if not has_content:
                continue
        filtered.append(item)

    return infobox_html, '\n'.join(filtered), categories, sources


# ---------------------------------------------------------------------------
# HTML page templates
# ---------------------------------------------------------------------------

CSS = """\
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;font-size:18px;line-height:1.7}
a{color:#000}
main{max-width:820px;margin:0 auto;padding:1.5rem 1rem}
h1{font-size:1.75rem;margin:.5rem 0 .9rem;line-height:1.25}
h2{font-size:1.5rem;margin:1.6rem 0 .4rem}
h3,h4,h5,h6{font-size:1.2rem;margin:1.2rem 0 .3rem}
p{margin-bottom:.8rem}
ul,ol{margin:0 0 .8rem 1.5rem}
article{display:flow-root}
aside.infobox{width:100%;margin:0 0 1.5rem 0;padding:.75rem;border:1px solid #000;overflow-wrap:break-word}
aside.infobox .infobox-title{font-weight:700;margin-bottom:.4rem}
aside.infobox table{width:100%;border-collapse:collapse;display:table}
aside.infobox th,aside.infobox td{padding:.2rem .4rem;vertical-align:top;border-bottom:1px solid #ddd;text-align:left}
aside.infobox th{font-weight:600;width:30%}
table{border-collapse:collapse;margin-bottom:.8rem;max-width:100%;display:block;overflow-x:auto}
th,td{border:1px solid #ccc;padding:.3rem .5rem;text-align:left;vertical-align:top}
th{background:#f0f0f0;font-weight:600}
#search-wrap{position:relative;margin-bottom:1rem}
#search-box{width:100%;padding:.4rem .7rem;border:1px solid #ccc;font-size:inherit}
#search-res{position:absolute;top:calc(100% + 2px);left:0;right:0;background:#fff;border:1px solid #000;max-height:60vh;overflow-y:auto;z-index:200;display:none}
#search-res.on{display:block}
.sr{display:block;padding:.4rem .9rem;border-bottom:1px solid #ccc;text-decoration:none}
.sr:hover{background:#f0f0f0}
.sr .cat{color:#333}
.cats-list{margin-top:1.5rem;padding-top:.8rem;border-top:1px solid #ccc;color:#666}
.entry-grid{columns:2;column-gap:1.5rem}
.entry-grid a{display:block;padding:.15rem 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media(max-width:600px){
  h1{font-size:1.3rem}
  .entry-grid{columns:1}
}
"""

SEARCH_JS = """\
(function () {
  var data = window.SD || [];
  var box  = document.getElementById('search-box');
  var res  = document.getElementById('search-res');
  if (!box || !res) return;

  function show(q) {
    q = q.trim().toLowerCase();
    res.innerHTML = '';
    if (q.length < 2) { res.classList.remove('on'); return; }

    var hits = [];
    for (var i = 0; i < data.length && hits.length < 60; i++) {
      if (data[i].t.toLowerCase().indexOf(q) >= 0) hits.push(data[i]);
    }
    if (!hits.length) {
      res.innerHTML = '<div class="sr-none">No results</div>';
      res.classList.add('on');
      return;
    }
    var frag = document.createDocumentFragment();
    hits.forEach(function (d) {
      var a = document.createElement('a');
      a.href = d.p; a.className = 'sr';
      a.innerHTML = '<span>' + d.t.replace(/</g, '&lt;') + '</span>'
                  + '<span class="cat"> \u2014 ' + d.c + '</span>';
      frag.appendChild(a);
    });
    res.appendChild(frag);
    res.classList.add('on');
  }

  box.addEventListener('input', function () { show(this.value); });

  document.addEventListener('click', function (e) {
    if (!res.contains(e.target) && e.target !== box) res.classList.remove('on');
  });

  box.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { res.classList.remove('on'); this.value = ''; }
    if (e.key === 'Enter')  { var f = res.querySelector('.sr'); if (f) location.href = f.href; }
    if (e.key === 'ArrowDown') { var f = res.querySelector('.sr'); if (f) { f.focus(); e.preventDefault(); } }
  });

  res.addEventListener('keydown', function (e) {
    var cur = document.activeElement;
    if (e.key === 'ArrowDown' && cur.nextElementSibling) { cur.nextElementSibling.focus(); e.preventDefault(); }
    if (e.key === 'ArrowUp') {
      if (cur.previousElementSibling) cur.previousElementSibling.focus();
      else box.focus();
      e.preventDefault();
    }
    if (e.key === 'Escape') { res.classList.remove('on'); box.focus(); }
  });
}());

// Jump-bar anchor clicks replace history state instead of pushing a new entry,
// so Back skips past them to the actual previous page.
document.querySelectorAll('.jump-bar a[href*="#"]').forEach(function (a) {
  a.addEventListener('click', function (e) {
    var href = this.getAttribute('href');
    var hash = href.indexOf('#');
    if (hash === -1) return;
    var id = href.slice(hash + 1);
    var el = document.getElementById(id);
    if (!el) return;
    e.preventDefault();
    el.scrollIntoView();
    history.replaceState(null, '', href);
  });
});
"""


def make_page_header(title_text, rel_path, site_name):
    """Return the opening HTML for any page."""
    depth = len(Path(rel_path).parts) - 1
    base  = '../' * depth
    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="{base}">
<title>{hl.escape(title_text)} — {hl.escape(site_name)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<main>
<div id="search-wrap">
  <input id="search-box" type="search" placeholder="Search\u2026"
    autocomplete="off" spellcheck="false">
  <div id="search-res"></div>
</div>"""


PAGE_FOOTER = """\
<script src="search-data.js"></script>
<script src="search.js"></script>
</body>
</html>"""


def render_page(title, rel_path, site_name, breadcrumb, body):
    return (
        make_page_header(title, rel_path, site_name)
        + f'\n<div class="bc">{breadcrumb}</div>'
        + '\n<article>\n' + body
        + '\n</article>\n</main>\n'
        + PAGE_FOOTER
    )


# ---------------------------------------------------------------------------
# Build passes
# ---------------------------------------------------------------------------

class _SAXHandler(xml.sax.handler.ContentHandler):
    """Minimal SAX handler that buffers text and fires on complete <page> elements."""

    def __init__(self, on_page, on_cat_page=None):
        self._on_page     = on_page
        self._on_cat_page = on_cat_page
        self._tag         = None
        self._buf         = []
        self._ns          = ''
        self._title       = ''

    def startElement(self, name, attrs):
        self._tag = name
        self._buf = []

    def endElement(self, name):
        val = ''.join(self._buf)
        if name == 'ns':
            self._ns = val.strip()
        elif name == 'title':
            self._title = val.strip()
        elif name == 'text':
            if self._ns == '0' and self._title:
                self._on_page(self._title, val)
            elif self._ns == '14' and self._title and self._on_cat_page:
                self._on_cat_page(self._title, val)
        self._tag = None

    def characters(self, content):
        if self._tag:
            self._buf.append(content)


def collect_titles(xml_path):
    """
    Pass 1 — scan the XML for main-namespace article titles.

    Builds _title_map (lower-case title → output path) which the converter
    uses to resolve [[wikilinks]]. Also tallies all [[Category:...]] tags into
    _wiki_cat_counts for categories.html.
    """
    slug_counts: dict[str, int] = defaultdict(int)

    def on_page(title, text):
        cat      = detect_category(text)
        cat_slug = slugify(cat)
        _cat_map[cat_slug] = cat
        base     = slugify(title)
        slug_counts[base] += 1
        n    = slug_counts[base]
        slug = base if n == 1 else f'{base}-{n}'
        _title_map[title.lower()] = f'pages/{cat_slug}/{slug}.html'

        # Count raw wiki category tags for categories.html
        for cat_tag in re.findall(r'\[\[Category:([^\]|]+)', text, re.I):
            cat_name = cat_tag.strip()
            if cat_name and '{{' not in cat_name and not _CAT_NOISE.match(cat_name):
                _wiki_cat_counts[cat_name] += 1

    def on_cat_page(title, text):
        if ':' not in title:
            return
        cat_name = title.split(':', 1)[1].strip()
        for parent_tag in re.findall(r'\[\[Category:([^\]|]+)', text, re.I):
            parent = parent_tag.strip()
            if parent and parent.lower() != cat_name.lower():
                _cat_parents[cat_name].append(parent)

    with open(xml_path, 'rb') as f:
        xml.sax.parse(f, _SAXHandler(on_page, on_cat_page))

    for child, parents in _cat_parents.items():
        for parent in parents:
            _cat_children[parent].append(child)


def _worker_init(title_map, cat_map):
    _title_map.update(title_map)
    _cat_map.update(cat_map)


def _process_article(args):
    title, wikitext, rel_path, dest_str, site_name, attribution = args
    dest = Path(dest_str)

    try:
        infobox_html, body_html, categories, sources = convert_page(wikitext)
    except Exception as e:
        infobox_html = ''
        body_html    = f'<p>[parse error: {hl.escape(str(e))}]</p>'
        categories   = []
        sources      = []

    if not infobox_html and '<p>' not in body_html:
        return None

    parts    = Path(rel_path).parts
    broad    = _cat_map.get(parts[1], 'Miscellaneous') if len(parts) > 1 else 'Miscellaneous'
    cat_href = safe_href(f'pages/{parts[1]}/index.html')

    breadcrumb = (
        f'<a href="index.html">Home</a> › '
        f'<a href="{cat_href}">{hl.escape(broad)}</a> › '
        f'{hl.escape(title)}'
    )

    cats_footer = ''
    if categories:
        cats_footer = '<div class="cats-list">Categories: ' + \
            ' · '.join(hl.escape(c) for c in categories[:8]) + '</div>'

    body = (
        f'<h1>{hl.escape(title)}</h1>'
        + '\n' + infobox_html
        + '\n' + body_html
        + '\n' + cats_footer
        + ('\n<h2>Sources</h2>\n<ul>\n'
           + '\n'.join(f'<li>{hl.escape(s)}</li>' for s in sources)
           + '\n</ul>' if sources else '')
        + f'\n<div class="attr">{hl.escape(attribution)}</div>'
    )
    html = render_page(title, rel_path, site_name, breadcrumb, body)

    out_file = dest / rel_path
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding='utf-8')

    return (broad, title, rel_path, categories)


def write_pages(xml_path, dest, site_name, attribution, limit=0, filter_titles=None, workers=1):
    cat_entries:       dict[str, list] = defaultdict(list)
    wiki_cat_articles: dict[str, list] = defaultdict(list)
    search_items: list[dict]           = []
    articles = []

    def on_page(title, wikitext):
        if limit and len(articles) >= limit:
            return
        if filter_titles and filter_titles.lower() not in title.lower():
            return
        rel_path = _title_map.get(title.lower())
        if rel_path:
            articles.append((title, wikitext, rel_path, str(dest), site_name, attribution))

    with open(xml_path, 'rb') as f:
        xml.sax.parse(f, _SAXHandler(on_page))

    def handle_result(result, i):
        if result is None:
            return
        broad, title, rel_path, categories = result
        cat_entries[broad].append((title, rel_path))
        search_items.append({'t': title, 'p': rel_path, 'c': broad})
        for wcat in categories:
            wiki_cat_articles[wcat].append((title, rel_path))
        if i % 2000 == 0:
            print(f'  {i:,}/{len(articles):,}', flush=True)

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers,
                                 initializer=_worker_init,
                                 initargs=(_title_map, _cat_map)) as pool:
            for i, result in enumerate(pool.map(_process_article, articles, chunksize=50), 1):
                handle_result(result, i)
    else:
        for i, args in enumerate(articles, 1):
            handle_result(_process_article(args), i)

    print(f'  {len(search_items):,}/{len(articles):,} done', flush=True)
    return cat_entries, search_items, wiki_cat_articles


def write_category_indexes(dest, cat_entries, site_name):
    """Write one index.html per broad category listing all its articles A–Z."""
    from collections import defaultdict as _dd

    for broad, entries in sorted(cat_entries.items()):
        cat_slug = slugify(broad)
        rel_idx  = f'pages/{cat_slug}/index.html'

        sorted_entries = sorted(entries, key=lambda x: x[0].lower())

        by_letter = _dd(list)
        for t, r in sorted_entries:
            letter = t[0].upper() if t and t[0].isalpha() else '#'
            by_letter[letter].append((t, r))
        letters = sorted(by_letter)

        jump_bar = ' '.join(
            f'<a href="{rel_idx}#{ltr}">{ltr}</a>' for ltr in letters
        )

        sections = []
        for ltr in letters:
            links = '\n'.join(
                f'<a href="{safe_href(r)}">{hl.escape(t)}</a>'
                for t, r in by_letter[ltr]
            )
            sections.append(
                f'<h2 id="{ltr}" style="margin-top:1.5rem">{ltr}</h2>'
                f'\n<div class="entry-grid">{links}</div>'
            )

        body = (
            f'<h1>{hl.escape(broad)}</h1>'
            + f'\n<p style="margin-bottom:.75rem">{len(entries):,} articles</p>'
            + f'\n<p class="jump-bar" style="margin-bottom:1.5rem">{jump_bar}</p>'
            + '\n' + '\n'.join(sections)
        )
        html = render_page(broad, rel_idx, site_name,
                           f'<a href="index.html">Home</a> › {hl.escape(broad)}', body)
        out = dest / rel_idx
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding='utf-8')


def write_search_index(dest, search_items):
    """Write search-data.js containing all article titles and paths."""
    search_items.sort(key=lambda x: x['t'].lower())
    js = 'var SD=' + json.dumps(search_items, ensure_ascii=False, separators=(',', ':')) + ';'
    (dest / 'search-data.js').write_text(js, encoding='utf-8')
    print(f'  {len(search_items):,} entries, {len(js) // 1024} KB', flush=True)


def _subtree_articles(cat, wiki_cat_articles, memo):
    """Return frozenset of article titles reachable from cat (BFS, cycle-safe, memoized)."""
    if cat in memo:
        return memo[cat]
    visited = set()
    queue = [cat]
    titles = {}  # title -> rel_path (last seen wins)
    while queue:
        c = queue.pop()
        if c in visited:
            continue
        visited.add(c)
        for t, r in wiki_cat_articles.get(c, []):
            titles[t] = r
        queue.extend(_cat_children.get(c, []))
    memo[cat] = titles
    return titles


def write_wiki_cat_hierarchy_pages(dest, wiki_cat_articles, site_name):
    """Write one page per wiki category showing subcategories + articles.

    Subcategories with >100 articles are shown as linked headings (click through
    to that category's own page). Subcategories with ≤100 articles are expanded
    inline. Articles not covered by any subcategory appear in an "Other" section.
    Uses subtree article counts so structural categories (no direct articles) are
    included as long as their descendants have articles.
    """
    (dest / 'wiki-categories').mkdir(parents=True, exist_ok=True)

    # Include any category that is a parent of a category with articles
    all_cats = set(wiki_cat_articles.keys())
    for parent in list(_cat_children.keys()):
        all_cats.add(parent)

    memo = {}

    # Multiple category names can produce the same slug (e.g. "Locations" and
    # "locations"). Keep only the one with the largest subtree so the canonical
    # page isn't overwritten by a near-empty shadow category.
    slug_to_cat: dict[str, tuple[str, int]] = {}
    for cn in all_cats:
        sl = slugify(cn)
        sz = len(_subtree_articles(cn, wiki_cat_articles, memo))
        if sl not in slug_to_cat or sz > slug_to_cat[sl][1]:
            slug_to_cat[sl] = (cn, sz)
    all_cats = {cat for cat, _ in slug_to_cat.values()}

    for cat_name in sorted(all_cats):
        slug = slugify(cat_name)
        rel  = f'wiki-categories/{slug}.html'

        # Use subtree counts so structural (no-direct-article) categories are included
        children_with_counts = [
            (c, len(_subtree_articles(c, wiki_cat_articles, memo)))
            for c in _cat_children.get(cat_name, [])
        ]
        children_with_counts = [(c, n) for c, n in children_with_counts if n >= 50]
        children_with_counts.sort(key=lambda x: -x[1])

        # "Other" = all subtree articles not covered by a shown heading
        # (includes direct articles + articles from subcategories below the threshold)
        child_titles = set()
        for c, _ in children_with_counts:
            child_titles |= _subtree_articles(c, wiki_cat_articles, memo).keys()
        all_subtree = _subtree_articles(cat_name, wiki_cat_articles, memo)
        other_articles = sorted(
            [(t, r) for t, r in all_subtree.items() if t not in child_titles],
            key=lambda x: x[0].lower()
        )

        jump_items = []
        sections   = []

        for child, count in children_with_counts:
            anchor     = re.sub(r'[^\w\-]', '_', child)
            child_href = safe_href(f'wiki-categories/{slugify(child)}.html')
            jump_items.append(
                f'<a href="{safe_href(rel)}#{anchor}">{hl.escape(child)}</a> ({count:,})'
            )
            if count > 100:
                sections.append(
                    f'<h2 id="{anchor}" style="margin-top:1.5rem">'
                    f'<a href="{child_href}">{hl.escape(child)}</a> ({count:,})</h2>'
                )
            else:
                child_entries = sorted(_subtree_articles(child, wiki_cat_articles, memo).items(),
                                       key=lambda x: x[0].lower())
                links = '\n'.join(
                    f'<a href="{safe_href(r)}">{hl.escape(t)}</a>'
                    for t, r in child_entries
                )
                sections.append(
                    f'<h2 id="{anchor}" style="margin-top:1.5rem">'
                    f'{hl.escape(child)} ({count:,})</h2>'
                    f'\n<div class="entry-grid">{links}</div>'
                )

        if other_articles:
            other_links = '\n'.join(
                f'<a href="{safe_href(r)}">{hl.escape(t)}</a>'
                for t, r in other_articles
            )
            if children_with_counts:
                jump_items.append(
                    f'<a href="{safe_href(rel)}#Other">Other ({len(other_articles):,})</a>'
                )
                sections.append(
                    f'<h2 id="Other" style="margin-top:1.5rem">Other ({len(other_articles):,})</h2>'
                    f'\n<div class="entry-grid">{other_links}</div>'
                )
            else:
                sections.append(f'<div class="entry-grid">{other_links}</div>')

        jump_bar = (
            '<p class="jump-bar" style="margin-bottom:1.5rem">'
            + ' · '.join(jump_items) + '</p>'
        ) if jump_items else ''

        parents = _cat_parents.get(cat_name, [])
        parent_links = ' · '.join(
            f'<a href="{safe_href(f"wiki-categories/{slugify(p)}.html")}">{hl.escape(p)}</a>'
            for p in parents[:5]
        )
        parent_html = (
            f'<p style="margin-bottom:.5rem;color:#666">In: {parent_links}</p>'
            if parent_links else ''
        )

        total = len(all_subtree)
        body = (
            f'<h1>{hl.escape(cat_name)}</h1>'
            + f'\n{parent_html}'
            + (f'\n<p style="margin-bottom:.75rem">{total:,} articles</p>' if total else '')
            + ('\n' + jump_bar if jump_bar else '')
            + '\n' + '\n'.join(sections)
        )
        breadcrumb = (f'<a href="index.html">Home</a> › '
                      f'<a href="categories.html">Categories</a> › {hl.escape(cat_name)}')
        html = render_page(cat_name, rel, site_name, breadcrumb, body)
        (dest / rel).write_text(html, encoding='utf-8')

    print(f'  {len(all_cats):,} category pages written', flush=True)


def write_wiki_categories_page(dest, top_cats, site_name, top_n=200):
    """Write categories.html listing the top N raw wiki category tags by article count."""
    rel = 'categories.html'

    rows = '\n'.join(
        f'<tr><td style="text-align:right;padding-right:1rem">{i}</td>'
        f'<td><a href="{safe_href(f"wiki-categories/{slugify(cat)}.html")}">{hl.escape(cat)}</a></td>'
        f'<td style="text-align:right">{count:,}</td></tr>'
        for i, (cat, count) in enumerate(top_cats, 1)
    )

    body = (
        f'<h1>Top {top_n} Wiki Categories</h1>'
        + f'\n<p style="margin-bottom:1.5rem">{len(_wiki_cat_counts):,} total categories across the corpus</p>'
        + '\n<table style="max-width:100%;display:table">'
        + '\n<thead><tr><th>#</th><th>Category</th><th>Articles</th></tr></thead>'
        + '\n<tbody>' + rows + '\n</tbody>'
        + '\n</table>'
    )
    html = render_page('Wiki Categories', rel, site_name,
                       '<a href="index.html">Home</a> › Wiki Categories', body)
    (dest / rel).write_text(html, encoding='utf-8')
    print(f'  {top_n} categories written', flush=True)



def write_home_page(dest, cat_entries, search_items, wiki_cat_articles, site_name, attribution):
    """Write the root index.html."""
    total = len(search_items)

    type_items = sorted(cat_entries.items(), key=lambda x: -len(x[1]))
    type_links = ', '.join(
        f'<a href="{safe_href(f"pages/{slugify(cat)}/index.html")}">'
        f'{hl.escape(cat)}</a> ({len(entries):,})'
        for cat, entries in type_items
    )

    memo = {}

    def meaningful_cats(root_cats, top_n=20):
        """Walk the full subtree of root_cats; return top N categories that have
        direct articles (skipping structural intermediate nodes), sorted by subtree count."""
        visited = set()
        queue = list(root_cats)
        candidates = {}
        while queue:
            c = queue.pop()
            if c in visited:
                continue
            visited.add(c)
            if wiki_cat_articles.get(c):
                candidates[c] = len(_subtree_articles(c, wiki_cat_articles, memo))
            queue.extend(_cat_children.get(c, []))
        return sorted(candidates.items(), key=lambda x: -x[1])[:top_n]

    browse_sections = []
    for section, cats in BROWSE_SECTIONS:
        ranked = meaningful_cats(cats)
        items = [
            f'<a href="{safe_href(f"wiki-categories/{slugify(cat)}.html")}">'
            f'{hl.escape(cat)}</a> ({count:,})'
            for cat, count in ranked
        ]
        if items:
            browse_sections.append(f'<h2>{hl.escape(section)}</h2>\n<p>{", ".join(items)}</p>')

    sections_html = [
        f'<h2>Article Types ({total:,})</h2>\n<p>{type_links}</p>',
    ] + browse_sections

    html = f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{hl.escape(site_name)} — Offline Edition</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<main>
<div id="search-wrap">
  <input id="search-box" type="search" placeholder="Search {total:,} articles…"
    autocomplete="off" spellcheck="false">
  <div id="search-res"></div>
</div>
<article>
<h1>{hl.escape(site_name)}</h1>
<p style="margin-bottom:1.5rem">{total:,} articles &middot; {hl.escape(attribution)}</p>
{chr(10).join(sections_html)}
<p style="margin-top:1.5rem;color:#666"><a href="categories.html">All wiki categories ({len(_wiki_cat_counts):,})</a></p>
</article>
</main>
<script src="search-data.js"></script>
<script src="search.js"></script>
</body>
</html>"""
    (dest / 'index.html').write_text(html, encoding='utf-8')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Convert the Forgotten Realms Wiki XML dump to a static offline website.'
    )
    parser.add_argument(
        'xml', type=Path, nargs='?',
        metavar='dump.xml',
        help='Path to the forgottenrealms_pages_current.xml dump file',
    )
    parser.add_argument(
        'out', type=Path, nargs='?',
        metavar='output-dir',
        help='Directory to write the HTML site into (created if it does not exist)',
    )
    parser.add_argument(
        '--limit', type=int, default=0, metavar='N',
        help='Stop after converting N articles (default: no limit)',
    )
    parser.add_argument(
        '--filter', dest='filter_titles', default='', metavar='TEXT',
        help='Only convert articles whose title contains TEXT (case-insensitive)',
    )
    parser.add_argument(
        '--sample', type=int, default=0, metavar='N',
        help='Convert a random sample of N articles',
    )
    parser.add_argument(
        '--assets-only', action='store_true',
        help='Regenerate style.css and search.js only, skip article conversion',
    )
    parser.add_argument(
        '--workers', type=int, default=1, metavar='N',
        help='Number of parallel worker processes for article conversion (default: 1)',
    )
    args = parser.parse_args()

    if args.assets_only:
        out_path = args.out or Path(input('Output directory: ').strip())
        out_path = out_path / 'forgotten_realms_wiki'
        out_path.mkdir(parents=True, exist_ok=True)
        (out_path / 'style.css').write_text(CSS,       encoding='utf-8')
        (out_path / 'search.js').write_text(SEARCH_JS, encoding='utf-8')
        print('Assets updated.')
        return

    xml_path = args.xml or Path(input('Path to XML dump: ').strip())
    out_path = args.out or Path(input('Output directory: ').strip())
    out_path = out_path / 'forgotten_realms_wiki'

    if not xml_path.exists():
        parser.error(f'XML file not found: {xml_path}')

    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / 'style.css').write_text(CSS,       encoding='utf-8')
    (out_path / 'search.js').write_text(SEARCH_JS, encoding='utf-8')

    import time
    _build_start = time.time()

    def elapsed():
        s = int(time.time() - _build_start)
        return f'{s // 60}m {s % 60}s'

    print(f'Source : {xml_path}')
    print(f'Output : {out_path}')
    print()

    print('Pass 1: collecting titles and category counts...', flush=True)
    collect_titles(xml_path)
    print(f'  {len(_title_map):,} articles, {len(_wiki_cat_counts):,} unique categories, '
          f'{len(_cat_children):,} category tree nodes [{elapsed()}]', flush=True)

    if args.sample:
        keys = random.sample(list(_title_map.keys()), min(args.sample, len(_title_map)))
        for k in list(_title_map):
            if k not in set(keys):
                del _title_map[k]
        print(f'  sampled {len(_title_map):,} articles', flush=True)

    print('Pass 2: converting articles...', flush=True)
    cat_entries, search_items, wiki_cat_articles = write_pages(
        xml_path, out_path, SITE_NAME, ATTRIBUTION,
        limit=args.limit, filter_titles=args.filter_titles, workers=args.workers)
    print(f'  [{elapsed()}]', flush=True)

    top_cats = sorted(_wiki_cat_counts.items(), key=lambda x: -x[1])[:200]

    print(f'Pass 3: category indexes... [{elapsed()}]', flush=True)
    write_category_indexes(out_path, cat_entries, SITE_NAME)

    print(f'Pass 4: wiki category hierarchy pages... [{elapsed()}]', flush=True)
    write_wiki_cat_hierarchy_pages(out_path, wiki_cat_articles, SITE_NAME)

    print(f'Pass 5: wiki categories index... [{elapsed()}]', flush=True)
    write_wiki_categories_page(out_path, top_cats, SITE_NAME)

    print(f'Pass 6: search index... [{elapsed()}]', flush=True)
    write_search_index(out_path, search_items)

    print(f'Pass 7: home page... [{elapsed()}]', flush=True)
    write_home_page(out_path, cat_entries, search_items, wiki_cat_articles, SITE_NAME, ATTRIBUTION)

    print(f'\nDone in {elapsed()}. Open: {out_path / "index.html"}')


if __name__ == '__main__':
    main()
