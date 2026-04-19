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
    python3 build_frwiki_xml.py

    # Or pass paths directly to skip the prompts:
    python3 build_frwiki_xml.py dump.xml ./wiki-html

    # Random sample of N articles (useful for testing):
    python3 build_frwiki_xml.py dump.xml ./wiki-html --sample 300

    # Only articles whose title contains TEXT:
    python3 build_frwiki_xml.py dump.xml ./wiki-html --filter "Elminster"

    # Stop after N articles:
    python3 build_frwiki_xml.py dump.xml ./wiki-html --limit 1000

Output layout
-------------
    index.html                  home page with category list and live search
    style.css                   stylesheet (black & white, mobile-friendly)
    search.js                   client-side search logic
    search-data.js              generated title index (~5 MB for 56k articles)
    pages/<category>/<slug>.html   one file per article

All hrefs are root-relative and resolved via <base href>, so the folder can
be moved anywhere and opened with any browser via file://.

How it works
------------
Two SAX passes over the XML (streaming, low memory):

  Pass 1 — collect every main-namespace title, detect its broad category from
            the infobox template name or [[Category:...]] tags, and build a
            title → output-path map used to resolve [[wikilinks]].

  Pass 2 — for each article: strip citations and non-article templates with
            mwparserfromhell, extract the infobox as a full-width block at the
            top of the page, then convert the remaining wikitext line-by-line
            to HTML. Stub articles (no prose and no infobox) are skipped.

Configuration
-------------
SITE_NAME, ATTRIBUTION, and the category maps below are the main things you
might want to adjust. Paths are supplied at runtime, not hardcoded here.
"""

import argparse
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

# Map infobox template names → broad category shown on the home page.
# Add entries here to reclassify article types.
INFOBOX_TO_CAT = {
    'person':       'Characters',
    'npc':          'Characters',
    'creature':     'Creatures',
    'race':         'Creatures',
    'location':     'Places',
    'building':     'Places',
    'settlement':   'Places',
    'spell':        'Spells',
    'item':         'Items & Artifacts',
    'book':         'Books & Media',
    'organization': 'Organizations',
    'deity':        'Deities & Religion',
    'demigod':      'Deities & Religion',
    'class':        'Classes',
}

# Fallback: match substrings in [[Category:...]] tags when no infobox is found.
# First match wins, so put more specific terms before general ones.
WIKICAT_RULES = [
    ('inhabitant',  'Characters'),
    ('wizard',      'Characters'), ('fighter',  'Characters'),
    ('cleric',      'Characters'), ('rogue',    'Characters'),
    ('ranger',      'Characters'), ('bard',     'Characters'),
    ('paladin',     'Characters'), ('druid',    'Characters'),
    ('dragon',      'Creatures'),  ('undead',   'Creatures'),
    ('lycanthrope', 'Creatures'),  ('monster',  'Creatures'),
    ('creature',    'Creatures'),
    ('settlement',  'Places'),     ('city',     'Places'),
    ('region',      'Places'),     ('nation',   'Places'),
    ('plane',       'Places'),     ('location', 'Places'),
    ('cantrip',     'Spells'),     ('spell',    'Spells'),
    ('magic item',  'Items & Artifacts'), ('artifact', 'Items & Artifacts'),
    ('weapon',      'Items & Artifacts'), ('armor',    'Items & Artifacts'),
    ('novel',       'Books & Media'), ('sourcebook', 'Books & Media'),
    ('comic',       'Books & Media'), ('adventure',  'Books & Media'),
    ('guild',       'Organizations'), ('organization', 'Organizations'),
    ('religion',    'Deities & Religion'), ('deity', 'Deities & Religion'),
]

# Short description shown on each category card on the home page.
CAT_DESCRIPTIONS = {
    'Characters':         'NPCs, heroes, villains, and historical figures',
    'Creatures':          'Monsters, races, and beings of Faerûn',
    'Places':             'Cities, regions, planes, and geography',
    'Spells':             'Arcane and divine spells across all editions',
    'Items & Artifacts':  'Magic items, weapons, armor, and relics',
    'Books & Media':      'Novels, sourcebooks, adventures, and comics',
    'Organizations':      'Guilds, orders, factions, and companies',
    'Deities & Religion': 'Gods, faiths, and divine powers',
    'Classes':            'Character classes and abilities',
    'Miscellaneous':      'Everything else',
}

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
    'template:', 'talk:', 'special:', 'help:',
)


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

# Populated during pass 1; used by the inline converter to resolve wikilinks.
_title_map: dict[str, str] = {}


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
    if ns_lower in ('file', 'image', 'category', 'user', 'user talk',
                    'template', 'talk', 'special', 'help', 'wikipedia'):
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
    """Return True if the template name looks like an article infobox."""
    n = template_name.strip().lower()
    for key in INFOBOX_TO_CAT:
        if n == key or n.startswith(key + ' ') or n.startswith(key + '/'):
            return True
    # catch additional infobox types not in INFOBOX_TO_CAT
    extra = {'book', 'adventure', 'spell', 'item', 'location', 'building',
             'organization', 'deity', 'creature', 'person', 'class', 'race'}
    return any(n == k or n.startswith(k + ' ') for k in extra)


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
h2{font-size:1.2rem;margin:1.6rem 0 .4rem}
h3,h4,h5,h6{font-size:1rem;margin:1.2rem 0 .3rem}
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
                  + '<span class="cat">' + d.c + '</span>';
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


# ---------------------------------------------------------------------------
# Build passes
# ---------------------------------------------------------------------------

class _SAXHandler(xml.sax.handler.ContentHandler):
    """Minimal SAX handler that buffers text and fires on complete <page> elements."""

    def __init__(self, on_page):
        self._on_page = on_page
        self._tag     = None
        self._buf     = []
        self._ns      = ''
        self._title   = ''

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
        self._tag = None

    def characters(self, content):
        if self._tag:
            self._buf.append(content)


def collect_titles(xml_path):
    """
    Pass 1 — scan the XML for main-namespace article titles.

    Builds _title_map (lower-case title → output path) which the converter
    uses to resolve [[wikilinks]].
    """
    slug_counts: dict[str, int] = defaultdict(int)

    def on_page(title, text):
        cat      = detect_category(text)
        cat_slug = slugify(cat)
        base     = slugify(title)
        slug_counts[base] += 1
        n    = slug_counts[base]
        slug = base if n == 1 else f'{base}-{n}'
        _title_map[title.lower()] = f'pages/{cat_slug}/{slug}.html'

    with open(xml_path, 'rb') as f:
        xml.sax.parse(f, _SAXHandler(on_page))


def write_pages(xml_path, dest, site_name, attribution, limit=0, filter_titles=None):
    """
    Pass 2 — convert every article and write its HTML file.

    Returns (cat_entries, search_items) for use in later passes.
    limit: stop after this many articles (0 = no limit)
    filter_titles: if set, only process articles whose title contains this string (case-insensitive)
    """
    cat_entries:  dict[str, list] = defaultdict(list)
    search_items: list[dict]      = []
    count = 0

    def on_page(title, wikitext):
        nonlocal count
        if limit and count >= limit:
            return
        if filter_titles and filter_titles.lower() not in title.lower():
            return

        rel_path = _title_map.get(title.lower())
        if not rel_path:
            return

        try:
            infobox_html, body_html, categories, sources = convert_page(wikitext)
        except Exception as e:
            infobox_html = ''
            body_html    = f'<p>[parse error: {hl.escape(str(e))}]</p>'
            categories   = []
            sources      = []

        parts    = Path(rel_path).parts        # ('pages', 'cat-slug', 'slug.html')
        broad    = parts[1].replace('-', ' ').title() if len(parts) > 1 else 'Miscellaneous'
        cat_href = safe_href(f'pages/{parts[1]}/index.html')

        breadcrumb = (
            f'<a href="index.html">Home</a> › '
            f'<a href="{cat_href}">{hl.escape(broad)}</a> › '
            f'{hl.escape(title)}'
        )

        # Skip stubs: no paragraph content and no infobox
        if not infobox_html and '<p>' not in body_html:
            return

        cats_footer = ''
        if categories:
            cats_footer = '<div class="cats-list">Categories: ' + \
                ' · '.join(hl.escape(c) for c in categories[:8]) + '</div>'

        html = (
            make_page_header(title, rel_path, site_name)
            + f'\n<div class="bc">{breadcrumb}</div>'
            + '\n<article>'
            + f'\n<h1>{hl.escape(title)}</h1>'
            + '\n' + infobox_html
            + '\n' + body_html
            + '\n' + cats_footer
            + ('\n<h2>Sources</h2>\n<ul>\n'
               + '\n'.join(f'<li>{hl.escape(s)}</li>' for s in sources)
               + '\n</ul>' if sources else '')
            + f'\n<div class="attr">{hl.escape(attribution)}</div>'
            + '\n</article>\n</main>'
            + '\n' + PAGE_FOOTER
        )

        out_file = dest / rel_path
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(html, encoding='utf-8')

        cat_entries[broad].append((title, rel_path))
        search_items.append({'t': title, 'p': rel_path, 'c': broad})

        count += 1
        if count % 2000 == 0:
            print(f'  {count:,}/{len(_title_map):,}', flush=True)

    with open(xml_path, 'rb') as f:
        xml.sax.parse(f, _SAXHandler(on_page))
    print(f'  {count:,}/{len(_title_map):,} done', flush=True)
    return cat_entries, search_items


def write_category_indexes(dest, cat_entries, site_name):
    """Write one index.html per broad category listing all its articles."""
    for broad, entries in sorted(cat_entries.items()):
        cat_slug = slugify(broad)
        rel_idx  = f'pages/{cat_slug}/index.html'

        links = '\n'.join(
            f'<a href="{safe_href(r)}">{hl.escape(t)}</a>'
            for t, r in sorted(entries, key=lambda x: x[0].lower())
        )
        html = (
            make_page_header(broad, rel_idx, site_name)
            + f'\n<div class="bc"><a href="index.html">Home</a> › {hl.escape(broad)}</div>'
            + f'\n<article><h1>{hl.escape(broad)}</h1>'
            + f'\n<p style="margin-bottom:1.5rem">{len(entries):,} articles</p>'
            + f'\n<div class="entry-grid">{links}</div>'
            + '\n</article>\n</main>'
            + '\n' + PAGE_FOOTER
        )
        out = dest / rel_idx
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding='utf-8')


def write_search_index(dest, search_items):
    """Write search-data.js containing all article titles and paths."""
    search_items.sort(key=lambda x: x['t'].lower())
    js = 'var SD=' + json.dumps(search_items, ensure_ascii=False, separators=(',', ':')) + ';'
    (dest / 'search-data.js').write_text(js, encoding='utf-8')
    print(f'  {len(search_items):,} entries, {len(js) // 1024} KB', flush=True)


def write_home_page(dest, cat_entries, search_items, site_name, attribution):
    """Write the root index.html with the category list and search bar."""
    links = '\n'.join(
        f'<li><a href="{safe_href(f"pages/{slugify(cat)}/index.html")}">'
        f'{hl.escape(cat)}</a> ({len(cat_entries[cat]):,})</li>'
        for cat in sorted(cat_entries)
    )

    total = len(search_items)
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
  <input id="search-box" type="search" placeholder="Search {total:,} articles\u2026"
    autocomplete="off" spellcheck="false">
  <div id="search-res"></div>
</div>
<article>
<h1>{hl.escape(site_name)}</h1>
<p style="margin-bottom:1.5rem">{total:,} articles &middot; {hl.escape(attribution)}</p>
<ul>
{links}
</ul>
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
    args = parser.parse_args()

    xml_path = args.xml or Path(input('Path to XML dump: ').strip())
    out_path = args.out or Path(input('Output directory: ').strip())
    out_path = out_path / 'forgotten_realms_wiki'

    if not xml_path.exists():
        parser.error(f'XML file not found: {xml_path}')

    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / 'style.css').write_text(CSS,       encoding='utf-8')
    (out_path / 'search.js').write_text(SEARCH_JS, encoding='utf-8')

    print(f'Source : {xml_path}')
    print(f'Output : {out_path}')
    print()

    print('Pass 1: collecting titles...', flush=True)
    collect_titles(xml_path)
    print(f'  {len(_title_map):,} articles', flush=True)

    if args.sample:
        keys = random.sample(list(_title_map.keys()), min(args.sample, len(_title_map)))
        for k in list(_title_map):
            if k not in set(keys):
                del _title_map[k]
        print(f'  sampled {len(_title_map):,} articles', flush=True)

    print('Pass 2: converting articles...', flush=True)
    cat_entries, search_items = write_pages(xml_path, out_path, SITE_NAME, ATTRIBUTION,
                                            limit=args.limit, filter_titles=args.filter_titles)

    print('Pass 3: category indexes...', flush=True)
    write_category_indexes(out_path, cat_entries, SITE_NAME)

    print('Pass 4: search index...', flush=True)
    write_search_index(out_path, search_items)

    print('Pass 5: home page...', flush=True)
    write_home_page(out_path, cat_entries, search_items, SITE_NAME, ATTRIBUTION)

    print(f'\nDone. Open: {out_path / "index.html"}')


if __name__ == '__main__':
    main()
