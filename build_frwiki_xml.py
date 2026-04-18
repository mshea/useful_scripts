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

Output layout
-------------
    index.html                  home page with category grid and live search
    style.css                   stylesheet (black & white, mobile-friendly)
    search.js                   client-side search logic
    search-data.js              generated title index (~6 MB for 70k articles)
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
            mwparserfromhell, extract the infobox as a floated aside table,
            then convert the remaining wikitext line-by-line to HTML.

Configuration
-------------
SITE_NAME, ATTRIBUTION, and the category maps below are the main things you
might want to adjust. Paths are supplied at runtime, not hardcoded here.
"""

import argparse
import html as hl
import json
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
_RE_REF_SELF  = re.compile(r'<ref\b[^>]*/>', re.I)
_RE_REF_BLOCK = re.compile(r'<ref\b[^>]*>.*?</ref>', re.I | re.S)
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

    target, sep, display = inner.partition('|')
    target  = target.strip()
    display = display.strip() if sep else target

    anchor = ''
    if '#' in target:
        target, fragment = target.split('#', 1)
        anchor = '#' + re.sub(r'[^\w\-]', '_', fragment)

    disp = hl.escape(display)
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


def convert_page(wikitext):
    """
    Convert a full article's wikitext to (infobox_html, body_html, categories).

    Uses mwparserfromhell to cleanly extract the infobox template and category
    links before doing line-by-line conversion of the body text.
    """
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
    # Clean up orphaned bracket/quote punctuation left by removed links
    body = re.sub(r'\[\[|\]\]', '', body)
    body = re.sub(r"'{2,3}", '', body)

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
        if text:
            out.append(f'<p>{text}</p>')
        i += 1

    close_lists()
    return infobox_html, '\n'.join(out), categories


# ---------------------------------------------------------------------------
# HTML page templates
# ---------------------------------------------------------------------------

CSS = """\
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#fff;color:#000;line-height:1.7;font-size:16px}
a{color:#000;text-decoration:underline}
a:hover{text-decoration:none}
/* header */
#hdr{background:#000;color:#fff;padding:.7rem 1rem;position:sticky;top:0;
  z-index:100;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap}
#site-title{font-size:1rem;font-weight:700;color:#fff;
  white-space:nowrap;text-decoration:none}
#site-title:hover{opacity:.8;text-decoration:none}
/* search */
#search-wrap{position:relative;flex:1;min-width:140px}
#search-box{width:100%;padding:.4rem .7rem;border:none;border-radius:2px;
  font-size:.95rem;background:#fff;color:#000}
#search-box:focus{outline:2px solid #fff}
#search-res{position:absolute;top:calc(100% + 2px);left:0;right:0;
  background:#fff;border:1px solid #000;max-height:60vh;overflow-y:auto;
  z-index:200;display:none;box-shadow:2px 2px 0 #000}
#search-res.on{display:block}
.sr{display:flex;justify-content:space-between;align-items:baseline;
  padding:.45rem .9rem;border-bottom:1px solid #ccc;color:#000;
  text-decoration:none;font-size:.9rem}
.sr:hover{background:#f0f0f0}
.sr .cat{font-size:.72rem;color:#666;margin-left:.5rem;white-space:nowrap}
.sr-none{padding:.7rem 1rem;color:#666;font-size:.9rem}
/* layout */
main{max-width:820px;margin:0 auto;padding:1.5rem 1rem}
.bc{font-size:.8rem;color:#666;margin-bottom:1rem}
/* article */
article h1{font-size:1.75rem;margin-bottom:.9rem;line-height:1.25}
article h2{font-size:1.2rem;margin:1.6rem 0 .45rem;
  border-bottom:1px solid #000;padding-bottom:.2rem}
article h3{font-size:1.05rem;margin:1.2rem 0 .35rem}
article h4,article h5,article h6{font-size:1rem;margin:1rem 0 .3rem}
article p{margin-bottom:.8rem}
p.indent{margin-left:1.5rem;margin-bottom:.5rem}
p.redirect{font-style:italic;color:#555}
article ul,article ol{margin:0 0 .8rem 1.5rem}
article li{margin-bottom:.2rem}
article hr{border:none;border-top:1px solid #000;margin:1.5rem 0}
article code{background:#f0f0f0;padding:.1em .3em;border-radius:2px;font-size:.88em}
.nl{color:#888;text-decoration:none}
.attr{margin-top:2rem;padding-top:.8rem;border-top:1px solid #ccc;
  font-size:.72rem;color:#999}
.cats-list{margin-top:.5rem;font-size:.8rem;color:#666}
.cats-list a{color:#555;margin-right:.5rem}
/* infobox */
aside.infobox{float:right;clear:right;margin:0 0 1rem 1.5rem;padding:.75rem;
  border:1px solid #000;font-size:.85rem;max-width:280px;min-width:180px;
  overflow-wrap:break-word}
aside.infobox .infobox-title{font-weight:700;margin-bottom:.4rem;font-size:.9rem}
aside.infobox table{width:100%;border-collapse:collapse}
aside.infobox th{text-align:left;padding:.2rem .4rem;font-weight:600;
  vertical-align:top;width:40%;border-bottom:1px solid #ddd;font-size:.8rem}
aside.infobox td{padding:.2rem .4rem;vertical-align:top;
  border-bottom:1px solid #ddd;font-size:.8rem}
/* wiki tables */
table{border-collapse:collapse;margin-bottom:.8rem;font-size:.9rem;
  max-width:100%;display:block;overflow-x:auto}
th,td{border:1px solid #ccc;padding:.3rem .5rem;text-align:left;vertical-align:top}
th{background:#f0f0f0;font-weight:600}
/* home page category grid */
.cats{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
  gap:1rem;margin-top:1rem}
.cat-card{background:#fff;border:1px solid #000;padding:1rem;
  text-decoration:none;color:#000;display:block}
.cat-card:hover{background:#f0f0f0;text-decoration:none}
.cat-card h2{font-size:1rem;margin-bottom:.25rem;border:none;padding:0}
.cat-card p{font-size:.82rem;color:#555}
/* category index listing */
.entry-grid{columns:2;column-gap:1.5rem}
.entry-grid a{display:block;padding:.15rem 0;font-size:.92rem;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* mobile */
@media(max-width:600px){
  article h1{font-size:1.3rem}
  body{font-size:15px}
  .entry-grid{columns:1}
  aside.infobox{float:none;max-width:100%;margin:0 0 1rem}
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
    """Return the opening HTML for any page, including sticky search header."""
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
<header id="hdr">
  <a href="index.html" id="site-title">{hl.escape(site_name)}</a>
  <div id="search-wrap">
    <input id="search-box" type="search" placeholder="Search\u2026"
      autocomplete="off" spellcheck="false">
    <div id="search-res"></div>
  </div>
</header>"""


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

    xml.sax.parse(str(xml_path), _SAXHandler(on_page))


def write_pages(xml_path, dest, site_name, attribution):
    """
    Pass 2 — convert every article and write its HTML file.

    Returns (cat_entries, search_items) for use in later passes.
    """
    cat_entries:  dict[str, list] = defaultdict(list)
    search_items: list[dict]      = []
    count = 0

    def on_page(title, wikitext):
        nonlocal count

        rel_path = _title_map.get(title.lower())
        if not rel_path:
            return

        try:
            infobox_html, body_html, categories = convert_page(wikitext)
        except Exception as e:
            infobox_html = ''
            body_html    = f'<p>[parse error: {hl.escape(str(e))}]</p>'
            categories   = []

        parts    = Path(rel_path).parts        # ('pages', 'cat-slug', 'slug.html')
        broad    = parts[1].replace('-', ' ').title() if len(parts) > 1 else 'Miscellaneous'
        cat_href = safe_href(f'pages/{parts[1]}/index.html')

        breadcrumb = (
            f'<a href="index.html">Home</a> › '
            f'<a href="{cat_href}">{hl.escape(broad)}</a> › '
            f'{hl.escape(title)}'
        )

        cats_footer = ''
        if categories:
            links = ' '.join(
                f'<a href="index.html">{hl.escape(c)}</a>'
                for c in categories[:8]
            )
            cats_footer = f'<div class="cats-list">Categories: {links}</div>'

        html = (
            make_page_header(title, rel_path, site_name)
            + '\n<main>'
            + f'\n<div class="bc">{breadcrumb}</div>'
            + '\n<article>'
            + f'\n<h1>{hl.escape(title)}</h1>'
            + '\n' + infobox_html
            + '\n' + body_html
            + '\n' + cats_footer
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

    xml.sax.parse(str(xml_path), _SAXHandler(on_page))
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
            + '\n<main>'
            + f'\n<div class="bc"><a href="index.html">Home</a> › {hl.escape(broad)}</div>'
            + f'\n<article><h1>{hl.escape(broad)}</h1>'
            + f'\n<p style="color:#666;margin-bottom:1.5rem">{len(entries):,} articles</p>'
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
    """Write the root index.html with the category grid and search bar."""
    cards = ''
    for cat in sorted(cat_entries):
        count    = len(cat_entries[cat])
        cat_slug = slugify(cat)
        desc     = CAT_DESCRIPTIONS.get(cat, '')
        cards += (
            f'<a class="cat-card" href="{safe_href(f"pages/{cat_slug}/index.html")}">'
            f'<h2>{hl.escape(cat)}</h2>'
            f'<p>{desc}</p>'
            f'<p style="font-size:.75rem;color:#999;margin-top:.3rem">'
            f'{count:,} articles</p>'
            f'</a>\n'
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
<header id="hdr">
  <a href="index.html" id="site-title">{hl.escape(site_name)}</a>
  <div id="search-wrap">
    <input id="search-box" type="search" placeholder="Search {total:,} articles\u2026"
      autocomplete="off" spellcheck="false">
    <div id="search-res"></div>
  </div>
</header>
<main>
<article>
<h1>{hl.escape(site_name)}</h1>
<p style="color:#666;margin-bottom:1.5rem">{total:,} articles &middot; {hl.escape(attribution)}</p>
<div class="cats">
{cards}
</div>
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
    args = parser.parse_args()

    xml_path = args.xml or Path(input('Path to XML dump: ').strip())
    out_path = args.out or Path(input('Output directory: ').strip())

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

    print('Pass 2: converting articles...', flush=True)
    cat_entries, search_items = write_pages(xml_path, out_path, SITE_NAME, ATTRIBUTION)

    print('Pass 3: category indexes...', flush=True)
    write_category_indexes(out_path, cat_entries, SITE_NAME)

    print('Pass 4: search index...', flush=True)
    write_search_index(out_path, search_items)

    print('Pass 5: home page...', flush=True)
    write_home_page(out_path, cat_entries, search_items, SITE_NAME, ATTRIBUTION)

    print(f'\nDone. Open: {out_path / "index.html"}')


if __name__ == '__main__':
    main()
