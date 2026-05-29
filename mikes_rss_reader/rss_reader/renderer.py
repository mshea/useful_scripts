import html
import os
import re
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import jinja2


# --- Jinja environment ---
_script_dir = Path(__file__).resolve().parent
_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_script_dir.parent / "templates")),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _read_css(name):
    p = _script_dir.parent / "static" / name
    return p.read_text(encoding="utf-8")


# --- CSS bundles (read once at import time) ---
COMMON_CSS = _read_css("common.css")
LIST_CSS = _read_css("list.css")
ARTICLE_CSS = _read_css("article.css")
READING_LIST_CSS_EXTRA = _read_css("reading_list.css")
SEARCH_CSS_EXTRA = _read_css("search.css")


# --- JavaScript constants ---
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
    setTimeout(function(){copybtn.textContent='Copy text'},2000);
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


READING_LIST_JS = """
const RL_KEY='readlater';
function getRL(){try{return JSON.parse(localStorage.getItem(RL_KEY)||'{}')}catch(e){return{}}}
function saveRL(d){localStorage.setItem(RL_KEY,JSON.stringify(d))}

function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function render(){
  var rl=getRL();
  var items=Object.values(rl);
  var ul=document.getElementById('list');
  var empty=document.getElementById('empty');
  if(!items.length){ul.innerHTML='';empty.style.display='';return}
  empty.style.display='none';
  ul.innerHTML=items.map(function(item){
    return '<li>'
      +'<a href="/articles/'+item.slug+'.html">'+item.title+'</a>'
      +'<button class="rm" data-slug="'+item.slug+'">&#9733;</button>'
      +'<br><span class="src">'+item.source+'</span>'
      +'</li>';
  }).join('');
  ul.querySelectorAll('button.rm').forEach(function(btn){
    btn.addEventListener('click',function(){
      var rl=getRL();
      delete rl[btn.dataset.slug];
      saveRL(rl);
      render();
    });
  });
}

document.getElementById('clear-all').addEventListener('click',function(){
  if(!confirm('Clear all saved articles?'))return;
  saveRL({});
  render();
});

document.getElementById('export-btn').addEventListener('click',function(){
  var items=Object.values(getRL());
  var rows=items.map(function(item){
    return '<li data-slug="'+esc(item.slug)+'" data-title="'+esc(item.title)+'" data-link="'+esc(item.link)+'" data-source="'+esc(item.source||'')+'">'
      +'<a href="'+esc(item.link)+'">'+esc(item.title)+'</a><br>'
      +'<span class="src">'+esc(item.source||'')+'</span>'
      +'</li>';
  }).join('\\n');
  var dateStr=new Date().toLocaleDateString();
  var exportHtml='<!DOCTYPE html>\\n<html lang="en">\\n<head>\\n'
    +'<meta charset="utf-8">\\n<meta name="viewport" content="width=device-width,initial-scale=1">\\n'
    +'<title>Reading List — '+dateStr+'</title>\\n<style>\\n'
    +exportCss
    +'\\n</style>\\n</head>\\n<body>\\n'
    +'<h1>Reading List — '+dateStr+' ('+items.length+' items)</h1>\\n'
    +'<ul>\\n'+rows+'\\n</ul>\\n'
    +'</body>\\n</html>\\n';
  var blob=new Blob([exportHtml],{type:'text/html'});
  var a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='reading-list.html';
  a.click();
});

document.getElementById('import-btn').addEventListener('click',function(){
  var input=document.createElement('input');
  input.type='file';
  input.accept='.html';
  input.onchange=function(){
    var reader=new FileReader();
    reader.onload=function(e){
      try{
        var doc=(new DOMParser()).parseFromString(e.target.result,'text/html');
        var rl=getRL();
        doc.querySelectorAll('li[data-slug]').forEach(function(li){
          var slug=li.dataset.slug;
          if(slug)rl[slug]={slug:slug,title:li.dataset.title||'',link:li.dataset.link||'',source:li.dataset.source||''};
        });
        saveRL(rl);
        render();
      }catch(err){alert('Could not parse HTML file')}
    };
    reader.readAsText(input.files[0]);
  };
  input.click();
});

render();
""".strip()

SEARCH_JS = """
const RL_KEY='readlater';
function getRL(){try{return JSON.parse(localStorage.getItem(RL_KEY)||'{}')}catch(e){return{}}}
function saveRL(d){localStorage.setItem(RL_KEY,JSON.stringify(d))}

var db=null;
var dbError=null;
var statusEl=document.getElementById('status');
var ul=document.getElementById('results');
var debugEl=document.getElementById('debug');
function dbg(m){debugEl.textContent+=m+'\\n';}
window.onerror=function(msg,src,line){dbg('ERR '+msg+' (line '+line+')');};

try{

statusEl.textContent='Initializing SQL engine\\u2026';
setTimeout(function(){
try{
  initSqlJs({locateFile:function(f){return '/'+f}}).then(function(SQL){
    statusEl.textContent='Loading database\\u2026';
    var start=performance.now();
    fetch('/search.db')
      .then(function(r){
        if(!r.ok) throw new Error('HTTP '+r.status);
        return r.arrayBuffer();
      })
      .then(function(buf){
        db=new SQL.Database(new Uint8Array(buf));
        var ms=Math.round(performance.now()-start);
        statusEl.textContent='Database loaded ('+(buf.byteLength/1024/1024).toFixed(1)+' MB). Ready to search.';
        document.getElementById('query').focus();
      })
      .catch(function(e){
        dbError=e;
        statusEl.textContent='Failed to load database: '+e.message;
      });
  }).catch(function(e){
    dbError=e;
    statusEl.textContent='Failed to initialize SQL engine: '+e.message;
    dbg('initSqlJs: '+e.message);
  });
}catch(e){
  dbg('initSqlJs throw: '+e.message);
}
},0);

var debounce=null;
document.getElementById('query').addEventListener('input',function(){
  clearTimeout(debounce);
  debounce=setTimeout(runSearch,250);
});
document.getElementById('btn').addEventListener('click',runSearch);

function esc(s){
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function runSearch(){
  var q=document.getElementById('query').value.trim();
  if(!db){
    if(dbError){statusEl.textContent='Database unavailable: '+dbError.message;}
    else{statusEl.textContent='Still loading database\\u2026';}
    return;
  }
  if(q.length<2){ul.innerHTML='';statusEl.textContent='';return}
  var rows;
  try{
    var res=db.exec(
      'SELECT a.slug,a.dt,a.title,a.link,a.source,a.site_link '+      'FROM articles_fts f JOIN articles a ON f.docid=a.rowid '+      'WHERE articles_fts MATCH ? '+      'ORDER BY a.dt DESC LIMIT 50',
      [q+'*']
    );
    rows=res.length?res[0].values:[];
  }catch(e){
    statusEl.textContent='Invalid query.';
    ul.innerHTML='';
    return;
  }
  if(!rows.length){
    ul.innerHTML='';
    statusEl.textContent='No results.';
    return;
  }
  statusEl.textContent=rows.length+(rows.length===50?' (first 50)':'')+' result'+(rows.length===1?'':'s');
  var rl=getRL();
  ul.innerHTML=rows.map(function(r){
    var slug=r[0],dt=r[1],title=r[2],link=r[3],source=r[4],siteLink=r[5];
    var localLink='/articles/'+slug+'.html';
    var srcHtml=siteLink
      ?'<a href="'+esc(siteLink)+'" class="src">'+esc(source)+'</a>'
      :'<span class="src">'+esc(source)+'</span>';
    var dtStr=dt?dt.slice(0,10):'';
    var starred=rl[slug]?'★':'☆';
    var savedCls=rl[slug]?' saved':'';
    return '<li>'
      +'<button class="star'+savedCls+'" data-slug="'+esc(slug)+'" data-title="'+esc(title)+'" data-link="'+esc(link)+'" data-source="'+esc(source)+'">'+starred+'</button>'
      +'<a href="'+localLink+'">'+esc(title)+'</a>'
      +'<br>'+srcHtml+'<span class="dt">'+dtStr+'</span>'
      +'</li>';
  }).join('');

  ul.querySelectorAll('button.star').forEach(function(btn){
    btn.addEventListener('click',function(){
      var rl=getRL();
      var slug=btn.dataset.slug;
      if(rl[slug]){
        delete rl[slug];
        btn.classList.remove('saved');
        btn.textContent='☆';
      }else{
        rl[slug]={slug:slug,title:btn.dataset.title,link:btn.dataset.link,source:btn.dataset.source};
        btn.classList.add('saved');
        btn.textContent='★';
      }
      saveRL(rl);
    });
  });
}
}catch(e){
  dbg('SCRIPT CRASH: '+e.message);
}
""".strip()


# --- HTML helpers ---
def sanitize_content_styles(html_text):
    """Strip ALL inline style attributes from feed content."""
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
    name = re.sub(r'[<>"/\\|?*\x00-\x1f#^[\]]', '', name)
    name = name.strip('. ')
    return name[:120]


def render_source_link(site_link, source_esc):
    if site_link:
        return f'<a href="{site_link}" class="src">{source_esc}</a>'
    return f'<span class="src">{source_esc}</span>'


def _render_template(name, title, css, body, js=""):
    tmpl = _env.get_template("base.html")
    return tmpl.render(title=title, css=css, body=body, js=js)


def render_feed_page(title_tag, h1_html, nav_html, body_html):
    """Render a standard feed list page (today or archive day)."""
    body = _env.get_template("feed_list.html").render(
        h1_html=h1_html, nav_html=nav_html, body_html=body_html
    )
    return _render_template("feed_list.html", title_tag, COMMON_CSS + "\n" + LIST_CSS, body, STAR_JS)


def render_article_page(title_esc, slug, ext_link, source_esc, source_html, dt_str,
                        obsidian_url, kw_html, summarize_btn, summary_box, content, js):
    """Render a single article page."""
    body = _env.get_template("article.html").render(
        feeds_name="feeds.html",
        reading_list_name="reading-list.html",
        title_esc=title_esc,
        slug=slug,
        ext_link=ext_link,
        source_esc=source_esc,
        source_html=source_html,
        dt_str=dt_str,
        obsidian_url=obsidian_url,
        kw_html=kw_html,
        summarize_btn=summarize_btn,
        summary_box=summary_box,
        content=content,
    )
    return _render_template("article.html", title_esc, COMMON_CSS + "\n" + ARTICLE_CSS, body, js)


def render_article_li(a):
    from zoneinfo import ZoneInfo
    from .fetcher import article_slug
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


# --- File writers ---
def save_article_pages(articles, articles_dir, cfg):
    """Write one HTML file per article into articles_dir."""
    from zoneinfo import ZoneInfo
    from .fetcher import article_slug
    Path(articles_dir).mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(cfg["timezone"])
    summarizer_endpoint = cfg.get("summarizer_endpoint", "")
    js = make_article_page_js(summarizer_endpoint)
    vault = cfg["obsidian"]["vault"]
    folder = cfg["obsidian"]["folder"]

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

        page = render_article_page(
            title_esc, slug, ext_link, source_esc, source_html, dt_str,
            obsidian_url, kw_html, summarize_btn, summary_box, content, js,
        )
        out.write_text(page, encoding="utf-8")
    print(f"Wrote {len(articles)} article pages to {articles_dir}")


def render_html(articles, cfg):
    """Generate feeds.html (today's articles)."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    groups = group_by_category(articles, cfg["category_order"])
    count = len(articles)
    feeds_name = os.path.basename(cfg["feeds_html"])
    archive_name = os.path.basename(cfg["archive_html"])
    rl_name = "reading-list.html"
    search_name = "search.html"
    opml_name = os.path.basename(cfg["opml_file"])
    nav_html, body_html = render_sections_and_nav(
        groups, extra_nav=f'<a href="/{archive_name}">Archive</a> &bull; <a href="/{rl_name}">&#9733; Reading List</a> &bull; <a href="/{search_name}">Search</a> &bull; <a href="/{opml_name}" download>OPML</a>'
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
    rl_name = "reading-list.html"
    search_name = "search.html"
    nav_html, body_html = render_sections_and_nav(
        groups, extra_nav=f'<a href="/{archive_name}">Archive</a> &bull; <a href="/{feeds_name}">Today</a> &bull; <a href="/{rl_name}">&#9733; Reading List</a> &bull; <a href="/{search_name}">Search</a>'
    )
    page = render_feed_page(f"Feeds — {day_str}", f"{count} articles &bull; {day_str}", nav_html, body_html)
    Path(cfg["archive_dir"]).mkdir(parents=True, exist_ok=True)
    out = Path(cfg["archive_dir"]) / f"{day.strftime('%Y-%m-%d')}.html"
    out.write_text(page, encoding="utf-8")


def render_archive_index(days_counts, cfg):
    """Render the archive index page."""
    from datetime import date
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
                label = "Last 24 hours"
            else:
                href = f"/archive/{d.strftime('%Y-%m-%d')}.html"
                label = date_str
            items.append((label, href, count))
        sections.append((month_str, items))

    feeds_name = os.path.basename(cfg["feeds_html"])
    body = _env.get_template("archive_index.html").render(
        feeds_name=feeds_name, sections=sections
    )
    page = _render_template("archive_index.html", "Feeds — Archive", COMMON_CSS + "\n" + LIST_CSS, body)
    Path(cfg["archive_html"]).write_text(page, encoding="utf-8")
    print(f"Wrote archive index with {len(days_counts)} days to {cfg['archive_html']}")


def render_reading_list_page(cfg):
    """Write the reading-list.html page to output_dir."""
    feeds_html = os.path.basename(cfg["feeds_html"])
    archive_html = os.path.basename(cfg["archive_html"])
    search_html = "search.html"
    export_css = (COMMON_CSS + "\n" + LIST_CSS).replace("\\", "\\\\").replace("`", "\\`")
    js = "var exportCss=`" + export_css + "`;\n" + READING_LIST_JS
    body = _env.get_template("reading_list.html").render(
        feeds_name=feeds_html, archive_name=archive_html, search_name=search_html
    )
    css = COMMON_CSS + "\n" + LIST_CSS + "\n" + READING_LIST_CSS_EXTRA
    page = _render_template("reading_list.html", "Reading List", css, body, js)
    out = Path(cfg["output_dir"]) / "reading-list.html"
    out.write_text(page, encoding="utf-8")
    print(f"Wrote reading list to {out}")


def render_search_page(cfg):
    """Write the search.html page to output_dir."""
    feeds_html = os.path.basename(cfg["feeds_html"])
    archive_html = os.path.basename(cfg["archive_html"])
    reading_list_html = "reading-list.html"
    body = _env.get_template("search.html").render(
        feeds_name=feeds_html, archive_name=archive_html, reading_list_name=reading_list_html
    )
    css = COMMON_CSS + "\n" + LIST_CSS + "\n" + SEARCH_CSS_EXTRA
    page = _render_template("search.html", "Search Feeds", css, body, SEARCH_JS)
    out = Path(cfg["output_dir"]) / "search.html"
    out.write_text(page, encoding="utf-8")
    print(f"Wrote search page to {out}")
