# -*- coding: utf-8 -*-
"""netforce / site_gen.py —— 把本地语料库渲染成一个可直接双击打开的静态站点。

设计约束（主人口径）：
  * 不生端口、不常驻：只产出静态文件，浏览器 file:// 直接打开。
  * file:// 下 fetch JSON 会被 CORS 拦，所以索引走 <script src> 分片注入。
  * 浅底深字（本机显示环境要求），不引任何外部 CDN。

产物（默认 _kb/site/）：
  index.html          搜索 / 主题 / 来源 / 语言 四面筛选 + 分页，纯前端
  idx/p00.js ...      轻量索引分片（每条 uid/标题/域名/主题/摘要/质量），window.KB.push
  doc/<uid>.html      详情页（正文全文 + 元数据 + 原始链接）
  STATS.md            本次生成的口径与规模

用法：
    python site_gen.py --limit 3000           # 先小规模验证
    python site_gen.py --limit 0              # 全量（有正文的全部）
    python site_gen.py --limit 0 --docs 8000  # 只给质量最高的 8000 条生成详情页
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
DB = os.path.join(KB, "corpus.db")
OUT = os.path.join(KB, "site")
SHARD = 3000

CSS = """
:root{--bg:#f7f8fa;--panel:#fff;--ink:#1a1c20;--sub:#5b616e;--line:#e3e6eb;
--acc:#2f6feb;--acc2:#eef3fe;--chip:#f0f2f5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:18px 24px}
h1{margin:0 0 2px;font-size:19px;font-weight:650}
.sub{color:var(--sub);font-size:13px}
.wrap{max-width:1080px;margin:0 auto;padding:20px 24px 60px}
input[type=search],select{font:inherit;color:var(--ink);background:var(--panel);
border:1px solid var(--line);border-radius:9px;padding:10px 13px;outline:none}
input[type=search]{flex:1;min-width:260px}
input[type=search]:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc2)}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 16px}
.count{color:var(--sub);font-size:13px;margin:0 0 12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;
padding:14px 16px;margin:0 0 10px}
.card h3{margin:0 0 4px;font-size:16px;font-weight:600;line-height:1.4}
.meta{color:var(--sub);font-size:12.5px;margin:0 0 6px;word-break:break-all}
.chip{display:inline-block;background:var(--chip);color:#39404d;border-radius:6px;
padding:1px 7px;font-size:12px;margin:0 5px 4px 0}
.snip{color:#2c313a;font-size:14px;margin:0}
mark{background:#fff2a8;color:inherit;padding:0 1px;border-radius:2px}
.pager{display:flex;gap:8px;align-items:center;margin:18px 0 0}
.pager button{font:inherit;background:var(--panel);border:1px solid var(--line);
border-radius:8px;padding:7px 14px;cursor:pointer;color:var(--ink)}
.pager button:disabled{opacity:.45;cursor:default}
article{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:26px 30px}
article h1{font-size:24px;margin:0 0 10px;line-height:1.35}
.body{white-space:pre-wrap;word-wrap:break-word;font-size:15.5px;line-height:1.85;
margin:18px 0 0;color:#22262e}
.empty{color:var(--sub);padding:34px 0;text-align:center}
"""

JS = """
const PER=20;let rows=[],view=[],page=0,curK='';
const $=s=>document.querySelector(s);
function chip(t){return '<span class="chip">'+esc(t)+'</span>'}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function hl(s,k){s=esc(s);if(!k)return s;
  try{return s.replace(new RegExp('('+k.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&')+')','gi'),'<mark>$1</mark>')}catch(e){return s}}
function apply(){
  const k=$('#q').value.trim().toLowerCase(),cat=$('#cat').value,host=$('#host').value,lang=$('#lang').value;
  curK=$('#q').value.trim();
  view=rows.filter(r=>{
    if(cat&&r.c!==cat)return false;
    if(host&&r.h!==host)return false;
    if(lang&&r.l!==lang)return false;
    if(!k)return true;
    return (r.t||'').toLowerCase().includes(k)||(r.s||'').toLowerCase().includes(k)
      ||(r.p||[]).join(' ').toLowerCase().includes(k)||(r.h||'').toLowerCase().includes(k);
  });
  view.sort((a,b)=>(b.q||0)-(a.q||0)||(b.n||0)-(a.n||0));
  page=0;render();
}
function render(){
  const tot=view.length,tp=Math.max(1,Math.ceil(tot/PER));
  if(page>=tp)page=tp-1;
  const seg=view.slice(page*PER,page*PER+PER);
  $('#count').textContent='共 '+tot.toLocaleString()+' 条'+(tot?'（第 '+(page+1)+'/'+tp+' 页）':'');
  $('#list').innerHTML=seg.length?seg.map(r=>
    '<div class="card"><h3><a href="doc/'+r.u+'.html">'+hl(r.t||'(无标题)',curK)+'</a></h3>'+
    '<p class="meta">'+esc(r.h||'')+(r.l?' · '+esc(r.l):'')+(r.n?' · '+r.n.toLocaleString()+' 字符':'')+
    (r.d?' · '+esc(r.d):'')+'</p>'+
    (r.p&&r.p.length?r.p.map(chip).join(''):'')+
    '<p class="snip">'+hl(r.s||'',curK)+'</p>'+
    (r.url?'<p class="meta" style="margin:6px 0 0">原文：<a href="'+esc(r.url)+'" target="_blank" rel="noopener">'+esc(r.url.slice(0,90))+'</a></p>':'')+
    '</div>').join(''):'<div class="empty">没有匹配的条目。换个关键词，或清掉筛选条件。</div>';
  $('#prev').disabled=page<=0;$('#next').disabled=page>=tp-1;$('#pinfo').textContent=(page+1)+' / '+tp;
  window.scrollTo(0,0);
}
function fill(sel,vals){const s=$(sel);vals.sort().forEach(v=>{const o=document.createElement('option');
  o.value=v;o.textContent=v+'（'+rows.filter(r=>(sel==='#cat'?r.c:sel==='#host'?r.h:r.l)===v).length+'）';s.appendChild(o)})}
function boot(){
  rows=(window.KB||[]).flat();
  fill('#cat',[...new Set(rows.map(r=>r.c).filter(Boolean))].slice(0,300));
  fill('#host',[...new Set(rows.map(r=>r.h).filter(Boolean))].slice(0,300));
  fill('#lang',[...new Set(rows.map(r=>r.l).filter(Boolean))]);
  $('#total').textContent=rows.length.toLocaleString();
  $('#q').addEventListener('input',()=>{clearTimeout(window._t);window._t=setTimeout(apply,120)});
  ['#cat','#host','#lang'].forEach(s=>$(s).addEventListener('change',apply));
  $('#prev').onclick=()=>{if(page>0){page--;render()}};
  $('#next').onclick=()=>{page++;render()};
  apply();
}
document.addEventListener('DOMContentLoaded',boot);
"""


def esc(s):
    return html.escape(str(s or ""), quote=True)


def snip(text, n=180):
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:n]


def ro_conn():
    return sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=30)


def fetch(limit, docs):
    c = ro_conn()
    base = ("SELECT uid,url,host,title,descr,text,text_len,lang,category,topics,quality,ts "
            "FROM corpus WHERE text_len>200 AND title!='' ORDER BY quality DESC, text_len DESC")
    rows = c.execute(base + (" LIMIT %d" % limit if limit else ""), ).fetchall()
    print("抽取 %d 条（有正文且有标题）" % len(rows))
    return rows


def build_index(rows):
    os.makedirs(os.path.join(OUT, "idx"), exist_ok=True)
    shards = []
    for i in range(0, len(rows), SHARD):
        part = rows[i:i + SHARD]
        recs = []
        for (uid, url, host, title, descr, text, tlen, lang, cat, topics, qual, ts) in part:
            try:
                tp = json.loads(topics) if topics and topics.startswith("[") else \
                    ([t for t in re.split(r"[,，/]", topics) if t.strip()] if topics else [])
            except Exception:
                tp = []
            tp = [str(x) for x in tp][:4]
            recs.append({"u": uid, "t": (title or "")[:110], "h": host or "",
                          "c": cat or "", "l": lang or "", "p": tp,
                          "q": qual or 0, "n": tlen or 0, "d": (ts or "")[:10],
                          "url": (url or "")[:300], "s": snip(descr or text, 180)})
        name = "p%02d.js" % (i // SHARD)
        with open(os.path.join(OUT, "idx", name), "w", encoding="utf-8") as f:
            f.write("window.KB=window.KB||[];window.KB.push(%s);\n" %
                    json.dumps(recs, ensure_ascii=False, separators=(",", ":")))
        shards.append(name)
    return shards


def build_index_html(rows, shards):
    stats = {"总条数": len(rows),
             "有正文": sum(1 for r in rows if (r[6] or 0) > 200),
             "来源站点": len({r[2] for r in rows if r[2]}),
             "总字符": sum((r[6] or 0) for r in rows)}
    tags = "".join("<script src=\"idx/%s\"></script>\n" % s for s in shards)
    page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地语料库 · 直搜</title><style>%(css)s</style></head><body>
<header><h1>本地语料库 · 直搜</h1>
<div class="sub">离线索引 <b id="total">–</b> 条 ｜ 共 <b>%(chars)s</b> 字符 ｜ 来源 <b>%(hosts)s</b> 个站点 ｜ 数据不联网、全部本地</div></header>
<div class="wrap">
<div class="bar"><input id="q" type="search" placeholder="输入关键词直接搜（标题 / 摘要 / 主题 / 域名）" autofocus>
<select id="cat"><option value="">全部主题</option></select>
<select id="host"><option value="">全部来源</option></select>
<select id="lang"><option value="">全部语言</option></select></div>
<p class="count" id="count"></p><div id="list"></div>
<div class="pager"><button id="prev">上一页</button><span class="count" id="pinfo"></span><button id="next">下一页</button></div>
</div>
%(tags)s<script>%(js)s</script></body></html>""" % {
        "css": CSS, "chars": format(stats["总字符"], ","),
        "hosts": format(stats["来源站点"], ","), "tags": tags, "js": JS}
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    return stats


def build_docs(rows):
    d = os.path.join(OUT, "doc")
    os.makedirs(d, exist_ok=True)
    n = 0
    for (uid, url, host, title, descr, text, tlen, lang, cat, topics, qual, ts) in rows:
        if not text:
            continue
        try:
            tp = json.loads(topics) if topics and topics.startswith("[") else []
        except Exception:
            tp = []
        chips = "".join('<span class="chip">%s</span>' % esc(x) for x in ([cat] + [str(t) for t in tp]) if x)
        body = "\n".join(l for l in (text or "").splitlines() if l.strip())[:200000]
        page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head><body>
<header><h1 style="font-size:15px"><a href="../index.html">← 返回检索</a></h1></header>
<div class="wrap"><article><h1>%s</h1>
<p class="meta">%s ｜ %s ｜ %s 字符 ｜ 质量 %s ｜ %s</p>
%s
%s<p class="meta" style="margin:16px 0 0">原始链接：<a href="%s" target="_blank" rel="noopener">%s</a></p>
<div class="body">%s</div></article></div></body></html>""" % (
            esc(title), CSS, esc(title), esc(host or ""), esc(lang or ""),
            format(tlen or 0, ","), esc(str(qual or "")), esc((ts or "")[:19]),
            chips, esc(descr or "")[:400],
            esc(url or ""), esc((url or "")[:120]),
            esc(body))
        with open(os.path.join(d, "%s.html" % uid), "w", encoding="utf-8") as f:
            f.write(page)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3000, help="0 = 全量")
    ap.add_argument("--docs", type=int, default=0, help="详情页条数，0 = 与索引同批全部生成")
    a = ap.parse_args()
    t0 = time.time()
    rows = fetch(a.limit, a.docs)
    shards = build_index(rows)
    stats = build_index_html(rows, shards)
    doc_rows = rows[:a.docs] if a.docs else rows
    n = build_docs(doc_rows)
    dt = time.time() - t0
    rep = os.path.join(OUT, "STATS.md")
    with open(rep, "w", encoding="utf-8") as f:
        f.write("# 本地语料站 · 生成报告\n\n- 生成时间：%s\n- 耗时：%.1fs\n"
                "- 索引条数：%s\n- 索引分片：%d 个（每片 %d 条）\n- 详情页：%s 个\n"
                "- 总字符：%s\n- 来源站点：%s\n\n打开方式：双击 `index.html`。\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), dt, format(stats["总条数"], ","),
                    len(shards), SHARD, format(n, ","),
                    format(stats["总字符"], ","), format(stats["来源站点"], ",")))
    print("完成 %.1fs：索引 %d 条 / %d 片；详情页 %d 个 → %s" %
          (dt, stats["总条数"], len(shards), n, OUT))


if __name__ == "__main__":
    main()
