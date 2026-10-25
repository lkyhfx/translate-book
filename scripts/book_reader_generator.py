#!/usr/bin/env python3
"""Convert a static book.html into one offline, chapter-mounted reader HTML.

No network resources or build toolchain are needed. Requires beautifulsoup4.
Usage: python book_reader_generator.py existing.html reader.html
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from bs4 import BeautifulSoup, NavigableString, Tag, Comment

BRACKET_NOTE = re.compile(r'（([0-9]{1,3})）|【([一二三四五六七八九十百〇零0-9]+)】')
DEFINITION_START = re.compile(r'(?m)^\s*(?:（([0-9]{1,3})）|【([一二三四五六七八九十百〇零0-9]+)】)')
ROMAN = re.compile(r'^(?:I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV|XVI|XVII|XVIII|XIX|XX|XXI|XXII|XXIII|XXIV|XXV|XXVI|XXVII|XXVIII|XXIX|XXX)$')


def _note_key(raw):
    return re.sub(r'\s+', '', raw)


def _repair_empty_numbered_notes(soup, article):
    """Recover explicit numbered footnote placeholders lost in HTML conversion.

    Pandoc sometimes produces <ol start="12"><li></li></ol> instead of a
    superscript note reference. The numbered placeholder is enough evidence
    to attach the annotation to the immediately preceding paragraph.
    """
    for ol in list(article.find_all('ol')):
        if ol.get_text(' ',strip=True) or len(ol.find_all('li',recursive=False)) != 1:
            continue
        number=ol.get('start')
        if not number or not number.isdigit():
            continue
        previous=ol.find_previous_sibling('p')
        if previous and BRACKET_NOTE.search(article.get_text(' ',strip=True)):
            previous.append(NavigableString('（'+number+'）'))
            ol.decompose()


def _extract_simple_notes(soup, article):
    notes = {}
    blocks = article.find_all(['p', 'blockquote'], recursive=False)
    entire = article.get_text(' ', strip=True)
    for p in blocks:
        text = p.get_text('\n', strip=True)
        starts = list(DEFINITION_START.finditer(text))
        if not starts or starts[0].start() != 0:
            continue
        # Only treat the paragraph as note definitions if another distinct
        # location in the book refers to the matching marker. This avoids
        # silently erasing numbered prose or ordinary bracketed text.
        candidates = []
        for i, m in enumerate(starts):
            key = _note_key(m.group(1) or m.group(2))
            explanation = text[m.end(): starts[i+1].start() if i+1 < len(starts) else len(text)].strip()
            if not explanation:
                candidates = []
                break
            candidates.append((key, explanation))
        if not candidates:
            continue
        refs_exist = all(
            entire.count(('（' + key + '）') if key.isascii() and key.isdigit() else ('【' + key + '】')) >= 2
            for key, _ in candidates
        )
        if refs_exist:
            for key, explanation in candidates:
                notes[key] = html.escape(explanation).replace('\n', '<br>')
            p.decompose()
    return notes


def _extract_pandoc_notes(article):
    notes = {}
    for section in list(article.select('section.footnotes, section[role="doc-endnotes"], div.footnotes')):
        for li in section.select('li[id]'):
            key = li.get('id')
            if not key:
                continue
            for back in li.select('a[role="doc-backlink"], a.footnote-back'):
                back.decompose()
            notes[key] = li.decode_contents().strip()
        section.decompose()
    return notes


def _create_marker(soup, key, label):
    sup = soup.new_tag('sup', attrs={'class': 'note-sup'})
    btn = soup.new_tag('button', attrs={'type': 'button', 'class': 'note-ref', 'data-note': key, 'aria-label': f'查看注释 {label}'})
    btn.string = label
    sup.append(btn)
    return sup


def _inject_note_refs(soup, article, notes):
    if not notes:
        return
    for a in list(article.find_all('a', href=True)):
        dest = unquote(a.get('href') or '').lstrip('#')
        if dest in notes:
            label = a.get_text(' ', strip=True).strip('[]（）') or dest.removeprefix('fn')
            a.replace_with(_create_marker(soup, dest, label))
    # Also handle ordinary textual markers that translators/Pandoc leave as
    # literal characters, e.g. （10） and 【一】.
    for textnode in list(article.find_all(string=True)):
        if not isinstance(textnode, NavigableString) or not textnode.parent:
            continue
        if textnode.parent.name in ('script','style','button','sup','code','pre','a'):
            continue
        raw = str(textnode)
        matches = [m for m in BRACKET_NOTE.finditer(raw) if _note_key(m.group(1) or m.group(2)) in notes]
        if not matches:
            continue
        pieces=[]
        last=0
        for m in matches:
            key = _note_key(m.group(1) or m.group(2))
            if m.start() > last:
                pieces.append(NavigableString(raw[last:m.start()]))
            pieces.append(_create_marker(soup,key,key))
            last=m.end()
        if last < len(raw):
            pieces.append(NavigableString(raw[last:]))
        for new in reversed(pieces):
            textnode.insert_after(new)
        textnode.extract()


def _sanitize_article(article):
    for bad in list(article.select('script, style, iframe, object, embed, form, input, meta')):
        bad.decompose()
    for tag in article.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith('on'):
                del tag[attr]
            elif attr.lower() in ('href','src') and str(tag.get(attr, '')).strip().lower().startswith(('javascript:', 'vbscript:')):
                del tag[attr]


def _embed_assets(article, source_dir):
    embedded=0
    for img in article.find_all('img', src=True):
        src = img['src']
        split = urlsplit(src)
        if split.scheme or src.startswith('//') or split.path.startswith('/'):
            continue
        path=(source_dir / unquote(split.path)).resolve()
        if not path.is_relative_to(source_dir.resolve()) or not path.is_file():
            continue
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if content_type not in ('image/png','image/jpeg','image/webp','image/gif','image/svg+xml','image/avif'):
            continue
        img['src'] = f'data:{content_type};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')
        img['loading'] = 'lazy'
        embedded += 1
    return embedded


def _sections(soup, article, book_title):
    direct=list(article.children)
    roman_candidates=[x for x in direct if isinstance(x,Tag) and x.name=='p' and ROMAN.fullmatch(x.get_text(' ',strip=True))]
    if len(roman_candidates)>=2:
        for p in roman_candidates:
            p.name = 'h2'
            p['class'] = ['roman-chapter']
    headings=[x for x in article.find_all(re.compile(r'^h[1-6]$'),recursive=False)]
    levels=sorted(set(int(x.name[1]) for x in headings))
    # Prefer the first repeated major level; skip one H1 that is just book title.
    levels_counts={lev:sum(int(x.name[1])==lev for x in headings) for lev in levels}
    chapter_level=next((lev for lev in levels if levels_counts[lev]>=2), levels[0] if levels else 0)
    if levels == [1] and len(headings)==1 and roman_candidates:
        chapter_level=2
    if len(headings)==1 and roman_candidates:
        chapter_level=2
    chapter_markers={id(x) for x in headings if int(x.name[1])==chapter_level}
    title=book_title.strip() or '正文'
    chapters=[]
    curr=[]
    curr_title='I' if roman_candidates else '开始阅读'
    id_map={}
    chapter_num=0
    def flush():
        nonlocal curr,curr_title,chapter_num
        if not curr:return
        content='\n'.join(str(x) for x in curr if isinstance(x,Tag) or str(x).strip())
        if not content.strip():
            curr=[]
            return
        chapter_num+=1
        chapter_id=f'chapter-{chapter_num}'
        chapters.append({'id':chapter_id,'title':curr_title,'html':content})
        for x in curr:
            if isinstance(x,Tag):
                for inner in [x]+list(x.find_all(id=True)):
                    if inner.get('id'):
                        id_map[inner['id']]=chapter_id
        curr=[]
    for x in list(article.children):
        if not isinstance(x,Tag):
            if str(x).strip():curr.append(x)
            continue
        if id(x) in chapter_markers and curr:
            flush()
        if id(x) in chapter_markers:
            curr_title=x.get_text(' ',strip=True)
        curr.append(x)
    flush()
    # Body can be heading-free; if a giant chapter remains, gracefully split at
    # direct block boundaries to cap mounted DOM size without breaking paragraphs.
    adjusted=[]
    for ch in chapters:
        if len(ch['html'])<70000:
            adjusted.append(ch)
            continue
        temp=BeautifulSoup(ch['html'],'html.parser')
        group=[];size=0;part=0
        for block in list(temp.contents):
            blen=len(str(block))
            if size+blen>55000 and group:
                part+=1
                adjusted.append({'id':f"{ch['id']}-part-{part}",'title':f"{ch['title']} · {part}",'html':'\n'.join(group)})
                group=[];size=0
            group.append(str(block));size+=blen
        if group:
            part+=1
            adjusted.append({'id':f"{ch['id']}-part-{part}",'title':f"{ch['title']} · {part}",'html':'\n'.join(group)})
    chapters=adjusted
    # Splitting long chapters changes mount IDs; recalculate anchor targets.
    id_map={}
    for ch in chapters:
        fragment=BeautifulSoup(ch['html'],'html.parser')
        for el in fragment.select('[id]'):
            id_map[el['id']]=ch['id']
    if not chapters:
        chapters=[{'id':'chapter-1','title':title,'html':'<p>（暂无正文）</p>'}]
    return chapters, id_map


READER_CSS = r'''
:root{--paper:#fffefa;--ink:#292b31;--muted:#73736f;--line:#dedbd2;--accent:#9b653d;--reader-font:Georgia,"Songti SC","STSong","Noto Serif CJK SC",serif;--reader-size:19px;--reader-weight:400;--reading-line:1.95;--reader-width:760px;color-scheme:light}
:root[data-theme=dark]{--paper:#16191b;--ink:#e8e4dc;--muted:#b0a9a1;--line:#414347;--accent:#d3a878;color-scheme:dark}
:root[data-theme=white]{--paper:#fff;--ink:#17191d;--muted:#696969;--line:#e6e6e6;--accent:#86532f;color-scheme:light}
:root[data-theme=sepia]{--paper:#f5eddd;--ink:#4a3b30;--muted:#806d5f;--line:#d6c5ab;--accent:#a16631;color-scheme:light}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:system-ui,-apple-system,sans-serif}
button,select{font:inherit;color:inherit}button{cursor:pointer}button:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.toolbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;padding:10px max(14px,calc((100vw - 1080px)/2));background:var(--paper);border-bottom:1px solid var(--line)}
.brand{font-size:14px;font-weight:700;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.toolbar button,.toolbar select{border:1px solid var(--line);border-radius:8px;background:transparent;padding:8px 10px;font-size:14px}
.toolbar button:hover,.chapter-nav button:hover{background:color-mix(in srgb,var(--ink) 5%,var(--paper))}
.settings{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.settings label{font-size:12px;color:var(--muted)}
.chapter-meta{text-align:center;color:var(--muted);font-size:13px;letter-spacing:.09em;padding-top:58px;margin:0}
article{width:min(var(--reader-width),calc(100% - 40px));min-height:55vh;margin:0 auto 80px;font-family:var(--reader-font);font-size:var(--reader-size);font-weight:var(--reader-weight);line-height:var(--reading-line);text-align:justify;overflow-wrap:anywhere}
article h1,article h2,article h3,article h4{font-family:var(--reader-font);line-height:1.55;text-align:center;font-weight:550;letter-spacing:.04em;margin:1.5em 0 1.3em}
article h1{font-size:1.65em}article h2{font-size:1.35em}article h3{font-size:1.15em}
article p{text-indent:2em;margin:0 0 1.25em}article blockquote{padding:.5em 1.1em;border-left:3px solid var(--accent);margin:1.5em 0;background:color-mix(in srgb,var(--ink) 4%,var(--paper))}article blockquote p{text-indent:0}
article img{max-width:100%;height:auto;display:block;margin:20px auto}article table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse;font-size:.9em}article td,article th{border:1px solid var(--line);padding:8px}article pre{max-width:100%;overflow:auto;font-size:.82em}
.note-sup{font-size:.67em;vertical-align:super;line-height:0}.note-ref{background:transparent;border:0;border-bottom:1px dotted var(--accent);color:var(--accent);padding:0 2px;border-radius:0;font-size:inherit}
.drawer-overlay{position:fixed;inset:0;background:#0007;z-index:29;display:none}.drawer-overlay.open{display:block}
.drawer{position:fixed;inset:0 auto 0 0;width:min(355px,92vw);z-index:30;background:var(--paper);border-right:1px solid var(--line);box-shadow:8px 0 30px #0002;transform:translateX(-105%);transition:transform .2s;display:flex;flex-direction:column}
.drawer.open{transform:translateX(0)}.drawer-title{padding:24px 21px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}.drawer-title button{border:0;background:transparent;font-size:24px}
#toc{overflow:auto;padding:10px 10px 50px}#toc button{display:block;width:100%;text-align:left;border:0;background:transparent;padding:12px;border-radius:8px;color:var(--ink);line-height:1.5}#toc button.active{background:color-mix(in srgb,var(--accent) 13%,var(--paper));color:var(--accent);font-weight:600}#toc button:hover{background:color-mix(in srgb,var(--accent) 9%,var(--paper))}
.chapter-nav{display:flex;gap:14px;justify-content:center;align-items:center;padding:20px 12px 70px;border-top:1px solid var(--line);max-width:760px;margin:auto}.chapter-nav button{border:1px solid var(--line);background:transparent;border-radius:10px;padding:11px 18px;max-width:40%;font-size:14px}.chapter-nav button:disabled{opacity:.3;cursor:default}.page-count{color:var(--muted);font-size:13px;white-space:nowrap}
#notePopover{position:fixed;inset:auto;left:0;top:0;max-width:min(380px,calc(100vw - 24px));max-height:55vh;overflow:auto;margin:0;border:1px solid var(--line);border-radius:12px;background:var(--paper);color:var(--ink);box-shadow:0 12px 35px #0003;padding:18px 20px;font-size:15px;line-height:1.65;z-index:50}
#notePopover::backdrop{background:transparent}.note-head{display:flex;justify-content:space-between;align-items:center;gap:20px;font-weight:700;margin-bottom:10px}.note-head button{border:none;background:transparent;font-size:20px}.note-body p{text-indent:0;margin:0 0 .7em}.note-body{overflow-wrap:anywhere}
:root:not([data-theme=dark]) a{color:var(--accent)}
@media(max-width:730px){.toolbar{flex-wrap:wrap;padding:9px 12px}.brand{flex-basis:50%}.settings{width:100%;justify-content:center;padding-top:3px}.settings label{display:none}article{width:calc(100% - 32px)}.chapter-meta{padding-top:36px}.toolbar button,.toolbar select{padding:7px 9px}}
@media print{.toolbar,.drawer,.drawer-overlay,.chapter-nav,#notePopover{display:none!important}article{min-height:auto;width:100%;margin:auto;color:#000}.chapter-meta{padding-top:0}:root{--paper:#fff;--ink:#000}}
'''

READER_JS = r'''(() => {
'use strict';
const payload=JSON.parse(document.getElementById('bookPayload').textContent);
const chapters=payload.chapters, notes=payload.notes||{}, idMap=payload.idMap||{};
const body=document.getElementById('chapterBody'),toc=document.getElementById('toc');
const drawer=document.getElementById('drawer'),overlay=document.getElementById('overlay');
const pop=document.getElementById('notePopover');
const key='book-reader:'+payload.key;
let current=0,lastTrigger=null;
function loadSettings(){try{return JSON.parse(localStorage.getItem(key+':settings')||'{}')}catch{return {}}}
function saveSettings(o){try{localStorage.setItem(key+':settings',JSON.stringify(o))}catch{}}
function remember(n){try{localStorage.setItem(key+':chapter',String(n))}catch{}}
function recall(){try{return Number(localStorage.getItem(key+':chapter'))||0}catch{return 0}}
function applySetting(){const o=loadSettings();let font=o.font||'song';let size=Number(o.size)||19;let theme=o.theme||'light';let weight=Number(o.weight)||400;if(!['song','fangsong','kai','sans'].includes(font))font='song';size=Math.max(14,Math.min(30,size));if(!['light','white','sepia','dark'].includes(theme))theme='light';if(![300,400,500,600,700].includes(weight))weight=400;const fonts={song:'Georgia,"Songti SC","Noto Serif CJK SC",serif',fangsong:'"FangSong","STFangsong",serif',kai:'"Kaiti SC","KaiTi",serif',sans:'"PingFang SC","Microsoft YaHei",system-ui,sans-serif'};document.documentElement.style.setProperty('--reader-font',fonts[font]);document.documentElement.style.setProperty('--reader-size',size+'px');document.documentElement.style.setProperty('--reader-weight',String(weight));document.documentElement.dataset.theme=theme;document.getElementById('fontSelect').value=font;document.getElementById('fontSize').textContent=size;document.getElementById('weightSelect').value=String(weight);document.getElementById('themeSelect').value=theme;}
function openToc(){drawer.classList.add('open');overlay.classList.add('open');document.getElementById('tocButton').setAttribute('aria-expanded','true')}
function closeToc(){drawer.classList.remove('open');overlay.classList.remove('open');document.getElementById('tocButton').setAttribute('aria-expanded','false')}
function closeNote(){if(typeof pop.hidePopover==='function'){try{pop.hidePopover()}catch{}}pop.hidden=true;if(lastTrigger){lastTrigger.setAttribute('aria-expanded','false');lastTrigger=null}}
function showNote(ref){const key=ref.dataset.note;if(!(key in notes))return;closeNote();lastTrigger=ref;ref.setAttribute('aria-expanded','true');document.getElementById('noteLabel').textContent='注释 '+ref.textContent;document.getElementById('noteBody').innerHTML=notes[key];pop.hidden=false;if(typeof pop.showPopover==='function')pop.showPopover();const rect=ref.getBoundingClientRect();const maxWidth=Math.min(380,innerWidth-24);const left=Math.max(12,Math.min(rect.left,innerWidth-maxWidth-12));const top=rect.bottom+240>innerHeight?Math.max(12,rect.top-220):rect.bottom+12;pop.style.left=left+'px';pop.style.top=top+'px'}
function show(index,scroll=true){if(!chapters.length)return;index=Math.max(0,Math.min(chapters.length-1,index));closeNote();current=index;body.innerHTML=JSON.parse(document.getElementById('chapter-json-'+index).textContent);document.getElementById('currentChapter').textContent=chapters[index].title;document.getElementById('counter').textContent=(index+1)+' / '+chapters.length;document.getElementById('prev').disabled=index===0;document.getElementById('next').disabled=index===chapters.length-1;for(const [i,button] of Array.from(toc.children).entries()){button.classList.toggle('active',i===index);button.setAttribute('aria-current',i===index?'true':'false')}remember(index);if(location.hash!=='#'+chapters[index].id){history.replaceState(null,'','#'+chapters[index].id)}if(scroll)scrollTo({top:0,behavior:'instant'})}
chapters.forEach((ch,i)=>{const btn=document.createElement('button');btn.type='button';btn.textContent=ch.title;btn.addEventListener('click',()=>{show(i);closeToc()});toc.append(btn)});
body.addEventListener('click',e=>{const note=e.target.closest('button.note-ref');if(note){showNote(note);return}const a=e.target.closest('a[href^="#"]');if(!a)return;const id=decodeURIComponent(a.getAttribute('href').slice(1));if(idMap[id]){e.preventDefault();const target=chapters.findIndex(ch=>ch.id===idMap[id]||ch.id.startsWith(idMap[id]+'-part-'));if(target!==-1)show(target);document.getElementById(id)?.scrollIntoView()}});
document.getElementById('tocButton').onclick=()=>drawer.classList.contains('open')?closeToc():openToc();document.getElementById('closeToc').onclick=closeToc;overlay.onclick=closeToc;document.getElementById('prev').onclick=()=>show(current-1);document.getElementById('next').onclick=()=>show(current+1);
document.getElementById('smaller').onclick=()=>{const o=loadSettings();o.size=Math.max(14,(Number(o.size)||19)-1);saveSettings(o);applySetting()};
document.getElementById('larger').onclick=()=>{const o=loadSettings();o.size=Math.min(30,(Number(o.size)||19)+1);saveSettings(o);applySetting()};
document.getElementById('fontSelect').onchange=e=>{const o=loadSettings();o.font=e.target.value;saveSettings(o);applySetting()};
document.getElementById('weightSelect').onchange=e=>{const o=loadSettings();o.weight=Number(e.target.value);saveSettings(o);applySetting()};
document.getElementById('themeSelect').onchange=e=>{const o=loadSettings();o.theme=e.target.value;saveSettings(o);applySetting()};
document.getElementById('closeNote').onclick=closeNote;
pop.addEventListener('toggle',e=>{if(e.newState==='closed'&&lastTrigger)closeNote()});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeToc();closeNote()}if(e.target.closest('button,input,select,textarea'))return;if(e.key==='ArrowRight'&&current<chapters.length-1)show(current+1);if(e.key==='ArrowLeft'&&current>0)show(current-1)});
document.addEventListener('click',e=>{if(!pop.hidden&&!e.target.closest('#notePopover,.note-ref'))closeNote()});
window.addEventListener('hashchange',()=>{const target=location.hash.slice(1);let index=chapters.findIndex(ch=>ch.id===target);if(index<0&&idMap[target])index=chapters.findIndex(ch=>ch.id===idMap[target]);if(index>=0&&index!==current)show(index)});
applySetting();let idx=chapters.findIndex(ch=>ch.id===location.hash.slice(1));if(idx<0&&idMap[location.hash.slice(1)])idx=chapters.findIndex(ch=>ch.id===idMap[location.hash.slice(1)]);show(idx<0?recall():idx,false);
})();'''


def make_reader(input_path, output_path):
    raw=Path(input_path).read_text('utf-8')
    soup=BeautifulSoup(raw,'html.parser')
    book_title=(soup.title.get_text(strip=True) if soup.title else Path(input_path).stem) or Path(input_path).stem
    author=(soup.find('meta',attrs={'name':'author'}) or {}).get('content','')
    for node in list(soup.body.select('.toc-toggle,.toc-sidebar,.toc-overlay,script,style,#title-block-header')):
        node.decompose()
    for comment in list(soup.body.find_all(string=lambda x: isinstance(x,Comment))):
        comment.extract()
    _sanitize_article(soup.body)
    embedded=_embed_assets(soup.body,Path(input_path).parent)
    _repair_empty_numbered_notes(soup,soup.body)
    notes=_extract_pandoc_notes(soup.body)
    notes.update(_extract_simple_notes(soup,soup.body))
    _inject_note_refs(soup,soup.body,notes)
    for ol in list(soup.body.find_all('ol')):
        if not ol.get_text(' ',strip=True):ol.decompose()
    chapters,id_map=_sections(soup,soup.body,book_title)
    book_key=hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    json_safe=lambda text: text.replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    metadata=[{'id':ch['id'],'title':ch['title']} for ch in chapters]
    data=json_safe(json.dumps({'key':book_key,'chapters':metadata,'notes':notes,'idMap':id_map},ensure_ascii=False,separators=(',',':')))
    chapter_data='\n'.join(f'<script type="application/json" id="chapter-json-{i}">'+json_safe(json.dumps(ch['html'],ensure_ascii=False))+'</script>' for i,ch in enumerate(chapters))
    page=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="author" content="{html.escape(author,quote=True)}"><meta name="book-reader" content="v1"><title>{html.escape(book_title)}</title><style>{READER_CSS}</style></head><body>
<div class="toolbar"><button id="tocButton" aria-expanded="false" aria-controls="drawer" type="button">☰ 目录</button><div class="brand">{html.escape(book_title)}</div><div class="settings"><label for="fontSelect">字体</label><select id="fontSelect" title="字体"><option value="song">宋体</option><option value="fangsong">仿宋</option><option value="kai">楷体</option><option value="sans">黑体</option></select><button id="smaller" type="button" aria-label="缩小字号">A−</button><span id="fontSize" aria-live="polite">19</span><button id="larger" type="button" aria-label="增大字号">A+</button><label for="weightSelect">字重</label><select id="weightSelect" aria-label="字重"><option value="300">细</option><option value="400">正常</option><option value="500">中</option><option value="600">半粗</option><option value="700">粗</option></select><select id="themeSelect" aria-label="阅读主题"><option value="light">暖白</option><option value="white">纯白</option><option value="sepia">米黄</option><option value="dark">深色</option></select></div></div>
<div class="drawer-overlay" id="overlay"></div><aside class="drawer" id="drawer" aria-label="全书目录"><div class="drawer-title"><strong>章节目录</strong><button id="closeToc" aria-label="关闭目录">×</button></div><nav id="toc"></nav></aside>
<p class="chapter-meta" id="currentChapter"></p><article id="chapterBody" aria-live="off"></article>
<div class="chapter-nav"><button id="prev" type="button">← 上一章</button><span id="counter" class="page-count"></span><button id="next" type="button">下一章 →</button></div>
<div id="notePopover" popover="auto" hidden role="dialog" aria-label="注释"><div class="note-head"><span id="noteLabel">注释</span><button id="closeNote" type="button" aria-label="关闭注释">×</button></div><div id="noteBody" class="note-body"></div></div>
<script id="bookPayload" type="application/json">{data}</script>{chapter_data}<script>{READER_JS}</script></body></html>'''
    Path(output_path).write_text(page,encoding='utf-8')
    return {'chapters':len(chapters),'notes':len(notes),'embedded_images':embedded,'bytes':Path(output_path).stat().st_size}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('input')
    parser.add_argument('output')
    a=parser.parse_args()
    print(json.dumps(make_reader(a.input,a.output),ensure_ascii=False,indent=2))

if __name__=='__main__':main()