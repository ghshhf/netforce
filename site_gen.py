# -*- coding: utf-8 -*-
"""netforce / site_gen.py —— 资源检索站生成器。

产品定位（主人定）：天网资源库的对外检索界面。
  * 核心交互 = 搜索（搜到即所得），不是翻频道。频道/形态只是「随便逛」的辅助入口。
  * 数据源 = kb_index.db（fine_classify 多维标签：topics 一级领域 / forms 资源形态 /
    lang 语种 / quality 质量分 / value 价值档）。topics 轴天然零「其他」。
  * 排序 = 质量分优先，让高质量资源自动浮顶。
  * 纯前端、不引 CDN、浅底深字、移动端响应式。

产物（_kb/site/）：
  index.html  搜索 + 频道/形态辅助导航 + 质量排序 + 分页
  idx/p*.js   轻量索引分片（window.KB.push）
  doc/<u>.html 详情页（摘要 + 标签 + 原文链接）
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import time
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
KB_DB = os.path.join(KB, "kb_index.db")
OUT = os.path.join(KB, "site")
SHARD = 2000

CSS = """
:root{--bg:#f7f8fa;--panel:#fff;--ink:#1a1c20;--sub:#5b616e;--line:#e3e6eb;
--acc:#2f6feb;--acc2:#eef3fe;--chip:#f0f2f5;--ch:#eaf1ff;--gold:#b8860b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:18px 24px}
h1{margin:0 0 2px;font-size:19px;font-weight:650}
.sub{color:var(--sub);font-size:13px}
.wrap{max-width:1080px;margin:0 auto;padding:18px 24px 60px}
input[type=search],select{font:inherit;color:var(--ink);background:var(--panel);
border:1px solid var(--line);border-radius:9px;padding:10px 13px;outline:none}
input[type=search]:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc2)}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
input[type=search]{flex:1;min-width:260px;font-size:15px}
.count{color:var(--sub);font-size:13px;margin:0 0 10px}
.chanbar{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.chan{font:inherit;font-size:13px;background:var(--panel);border:1px solid var(--line);
border-radius:999px;padding:6px 13px;cursor:pointer;color:var(--sub);white-space:nowrap}
.chan i{font-style:normal;opacity:.6;margin-left:4px}
.chan:hover{border-color:var(--acc);color:var(--acc)}
.chan.on{background:var(--acc);border-color:var(--acc);color:#fff}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:14px 16px;margin:0 0 10px}
.card h3{margin:0 0 4px;font-size:16px;font-weight:600;line-height:1.4}
.meta{color:var(--sub);font-size:12.5px;margin:0 0 6px;word-break:break-all}
.chip{display:inline-block;background:var(--chip);color:#39404d;border-radius:6px;padding:1px 7px;font-size:12px;margin:0 5px 4px 0}
.chip.ch{background:var(--ch);color:#1f4fb0}
.chip.form{background:#eafaf0;color:#1d7a45}
.q{display:inline-block;font-size:11px;font-weight:700;color:var(--gold);border:1px solid var(--gold);
border-radius:5px;padding:0 5px;margin-left:6px;vertical-align:middle}
.snip{color:#2c313a;font-size:14px;margin:0}
mark{background:#fff2a8;color:inherit;padding:0 1px;border-radius:2px}
.pager{display:flex;gap:8px;align-items:center;margin:18px 0 0}
.pager button{font:inherit;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 14px;cursor:pointer;color:var(--ink)}
.pager button:disabled{opacity:.45;cursor:default}
article{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:26px 30px}
article h1{font-size:24px;margin:0 0 10px;line-height:1.35}
.body{white-space:pre-wrap;word-wrap:break-word;font-size:15.5px;line-height:1.85;margin:18px 0 0;color:#22262e}
.empty{color:var(--sub);padding:34px 0;text-align:center}
@media (max-width:640px){
 header{padding:13px 14px}.wrap{padding:14px 14px 40px}
 .bar{flex-direction:column;align-items:stretch;gap:8px}
 input[type=search]{min-width:0;width:100%;font-size:16px}select{width:100%;font-size:16px}
 .chanbar{overflow-x:auto;flex-wrap:nowrap;padding-bottom:4px;-webkit-overflow-scrolling:touch}
 .chan{flex:0 0 auto}.card{padding:12px 13px;margin-bottom:8px}.card h3{font-size:15px}
 article{padding:18px 15px}article h1{font-size:20px}.body{font-size:15px;line-height:1.8}
 .pager button{padding:9px 16px}
}
"""

JS = """
const PER=20;let rows=[],view=[],page=0,curK='',activeCh='全部',activeForm='全部';
const $=s=>document.querySelector(s);
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function hl(s,k){s=esc(s);if(!k)return s;
  try{return s.replace(new RegExp('('+k.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&')+')','gi'),'<mark>$1</mark>')}catch(e){return s}}
function chip(t){return '<span class="chip'+(t.cls?(' '+t.cls):'')+'">'+esc(t.v)+'</span>'}
function channels(){const s=new Set();rows.forEach(r=>(r.ch||[]).forEach(c=>s.add(c)));return [...s]}
function forms(){const s=new Set();rows.forEach(r=>(r.fm||[]).forEach(c=>s.add(c)));return [...s]}
function renderChans(){
  const cs=channels().sort((a,b)=>rows.filter(r=>(r.ch||[]).includes(b)).length-rows.filter(r=>(r.ch||[]).includes(a)).length);
  const all=['全部',...cs];
  $('#chans').innerHTML=all.map(c=>{
    const n=c==='全部'?rows.length:rows.filter(r=>(r.ch||[]).includes(c)).length;
    return '<button class="chan'+(c===activeCh?' on':'')+'" data-ch="'+esc(c)+'">'+esc(c)+' <i>'+n.toLocaleString()+'</i></button>';
  }).join('');
  $('#chans').querySelectorAll('button').forEach(b=>b.onclick=()=>{activeCh=b.dataset.ch;renderChans();apply();});
}
function renderForms(){
  const fs=forms();
  if(!fs.length){$('#formbar').style.display='none';return;}
  const all=['全部',...fs.sort((a,b)=>rows.filter(r=>(r.fm||[]).includes(b)).length-rows.filter(r=>(r.fm||[]).includes(a)).length)];
  $('#formbar').innerHTML='形态：'+all.map(c=>{
    const n=c==='全部'?rows.length:rows.filter(r=>(r.fm||[]).includes(c)).length;
    return '<button class="chan'+(c===activeForm?' on':'')+'" data-f="'+esc(c)+'">'+esc(c)+' <i>'+n.toLocaleString()+'</i></button>';
  }).join('');
  $('#formbar').querySelectorAll('button').forEach(b=>b.onclick=()=>{activeForm=b.dataset.f;renderForms();apply();});
}
function apply(){
  const k=$('#q').value.trim().toLowerCase(),host=$('#host').value,lang=$('#lang').value;
  curK=$('#q').value.trim();
  view=rows.filter(r=>{
    if(activeCh!=='全部'&&!(r.ch||[]).includes(activeCh))return false;
    if(activeForm!=='全部'&&!(r.fm||[]).includes(activeForm))return false;
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
    '<div class="card"><h3><a href="doc/'+esc(r.u)+'.html">'+hl(r.t||'(无标题)',curK)+'</a>'
    +(r.q?('<span class="q">Q'+r.q+'</span>'):'')+'</h3>'+
    '<p class="meta">'+esc(r.h||'')+(r.l?' · '+esc(r.l):'')+(r.v?' · '+esc(r.v)+'价值':'')+'</p>'+
    (r.p&&r.p.length?r.p.map(chip).join(''):'')+
    (r.fm&&r.fm.length?r.fm.map(t=>({v:t,cls:'form'})).map(chip).join(''):'')+
    '<p class="snip">'+hl(r.s||'',curK)+'</p>'+
    (r.url?'<p class="meta" style="margin:6px 0 0">原文：<a href="'+esc(r.url)+'" target="_blank" rel="noopener">'+esc(r.url.slice(0,90))+'</a></p>':'')+
    '</div>').join(''):'<div class="empty">该条件下还没内容，换个关键词或清掉筛选。</div>';
  $('#prev').disabled=page<=0;$('#next').disabled=page>=tp-1;$('#pinfo').textContent=(page+1)+' / '+tp;
  window.scrollTo(0,0);
}
function fill(sel,vals){const s=$(sel);vals.sort().forEach(v=>{const o=document.createElement('option');
  o.value=v;o.textContent=v+'（'+rows.filter(r=>(sel==='#host'?r.h:r.l)===v).length+'）';s.appendChild(o)})}
function boot(){
  rows=(window.KB||[]).flat();
  renderChans();renderForms();
  fill('#host',[...new Set(rows.map(r=>r.h).filter(Boolean))].slice(0,300));
  fill('#lang',[...new Set(rows.map(r=>r.l).filter(Boolean))]);
  $('#total').textContent=rows.length.toLocaleString();
  $('#q').addEventListener('input',()=>{clearTimeout(window._t);window._t=setTimeout(apply,120)});
  ['#host','#lang'].forEach(s=>$(s).addEventListener('change',apply));
  $('#prev').onclick=()=>{if(page>0){page--;render()}};
  $('#next').onclick=()=>{page++;render()};
  apply();
}
document.addEventListener('DOMContentLoaded',boot);
"""

STATUS_TOKENS = ("未命中", "待语义复核", "待补标", "未识别/待人工看一眼", "未分类")


def esc(s):
    return html.escape(str(s or ""), quote=True)


def snip(text, n=200):
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:n]


def parse_list(s):
    try:
        v = json.loads(s or "[]")
        return [str(x).strip() for x in v if str(x).strip()
                and not any(t in str(x) for t in STATUS_TOKENS)]
    except Exception:
        return []


def channel_of(topics):
    """topics 是一级领域/二级主题（fine_classify 产出，如「AI/机器学习」）。
    频道 = 一级（/ 前段）。零「其他」——没命中就不进频道，靠搜索兜底。"""
    ch = set()
    for t in topics:
        ch.add(t.split("/", 1)[0].strip() or "其他")
    ch.discard("")
    return sorted(ch)


def uid_of(domain):
    return re.sub(r"[^a-z0-9]", "_", (domain or "x").lower())[:64]


def fmt_ts(ts, full=False):
    """kb_index.db 的 ts 是 float Unix 时间戳，转成可读日期。"""
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return str(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S" if full else "%Y-%m-%d")


def ro_conn():
    return sqlite3.connect("file:%s?mode=ro" % KB_DB.replace("\\", "/"), uri=True, timeout=30)


def fetch(limit, docs):
    c = ro_conn()
    where = "WHERE topics!='' AND topics!='[]' AND topics NOT LIKE '%未命中%'"
    rows = c.execute(
        "SELECT domain,url,title,descr,snippet,topics,forms,lang,quality,value,ts "
        "FROM docs %s ORDER BY quality DESC%s" % (where, (" LIMIT %d" % limit if limit else ""),)
    ).fetchall()
    print("从 kb_index 抽取 %d 条（有 topics 且非未命中）" % len(rows))
    c.close()
    return rows


def build_index(rows):
    idx_dir = os.path.join(OUT, "idx")
    os.makedirs(idx_dir, exist_ok=True)
    for fn in os.listdir(idx_dir):
        if fn.endswith(".js"):
            try:
                os.remove(os.path.join(idx_dir, fn))
            except OSError:
                pass
    shards = []
    for i in range(0, len(rows), SHARD):
        part = rows[i:i + SHARD]
        recs = []
        for (domain, url, title, descr, snippet, topics, forms, lang, qual, value, ts) in part:
            tp = parse_list(topics)[:4]
            fm = parse_list(forms)
            recs.append({"u": uid_of(domain), "t": (title or "")[:120], "h": domain or "",
                         "l": lang or "", "p": tp, "fm": fm, "ch": channel_of(tp),
                         "q": qual or 0, "v": value or "", "n": len(snippet or ""),
                         "d": fmt_ts(ts),
                         "url": (url or "")[:300], "s": snip(snippet or descr, 200)})
        name = "p%02d.js" % (i // SHARD)
        with open(os.path.join(OUT, "idx", name), "w", encoding="utf-8") as f:
            f.write("window.KB=window.KB||[];window.KB.push(%s);\n" %
                    json.dumps(recs, ensure_ascii=False, separators=(",", ":")))
        shards.append(name)
    return shards


def build_index_html(rows, shards):
    stats = {"总条数": len(rows),
             "来源站点": len({r[0] for r in rows if r[0]}),
             "总字符": sum(len((r[4] or "")) for r in rows)}
    tags = "".join("<script src=\"idx/%s\"></script>\n" % s for s in shards)
    page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>天网 · 资源检索站</title><style>%(css)s</style></head><body>
<header><h1>天网 · 资源检索站</h1>
<div class="sub">本地资源库 <b id="total">–</b> 条 ｜ 覆盖 <b>%(hosts)s</b> 个站点 ｜ 按质量分排序 ｜ 搜到即所得</div></header>
<div class="wrap">
<div class="bar"><input id="q" type="search" placeholder="直接描述你要找的资源，例如：本地跑大模型量化工具 / 免费数据集 / 前端组件库" autofocus>
<select id="host"><option value="">全部来源</option></select>
<select id="lang"><option value="">全部语种</option></select></div>
<div class="chanbar" id="chans"></div>
<div class="chanbar" id="formbar"></div>
<p class="count" id="count"></p><div id="list"></div>
<div class="pager"><button id="prev">上一页</button><span class="count" id="pinfo"></span><button id="next">下一页</button></div>
</div>
%(tags)s<script>%(js)s</script></body></html>""" % {
        "css": CSS, "hosts": format(stats["来源站点"], ","),
        "tags": tags, "js": JS}
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    return stats


def build_docs(rows):
    d = os.path.join(OUT, "doc")
    os.makedirs(d, exist_ok=True)
    n = 0
    for (domain, url, title, descr, snippet, topics, forms, lang, qual, value, ts) in rows:
        if not snippet and not descr:
            continue
        tp = parse_list(topics)
        fm = parse_list(forms)
        chips = "".join('<span class="chip ch">%s</span>' % esc(x) for x in tp)
        chips += "".join('<span class="chip form">%s</span>' % esc(x) for x in fm)
        body = "\n".join(l for l in (snippet or "").splitlines() if l.strip())[:15000]
        page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head><body>
<header><h1 style="font-size:15px"><a href="../index.html">← 返回检索</a></h1></header>
<div class="wrap"><article><h1>%s</h1>
<p class="meta">%s ｜ %s ｜ 质量 %s ｜ %s价值 ｜ %s</p>
%s
<p class="desc">%s</p>
<p class="meta" style="margin:16px 0 0">原始链接：<a href="%s" target="_blank" rel="noopener">%s</a></p>
<div class="body">%s</div></article></div></body></html>""" % (
            esc(title), CSS, esc(title),             esc(domain or ""), esc(lang or ""),
            esc(str(qual or "")), esc(value or ""), esc(fmt_ts(ts, True)),
            chips, esc(descr or "")[:400],
            esc(url or ""), esc((url or "")[:120]), esc(body))
        with open(os.path.join(d, "%s.html" % uid_of(domain)), "w", encoding="utf-8") as f:
            f.write(page)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = 全量")
    ap.add_argument("--docs", type=int, default=0, help="详情页条数，0 = 与索引同批")
    a = ap.parse_args()
    t0 = time.time()
    rows = fetch(a.limit, a.docs)
    shards = build_index(rows)
    stats = build_index_html(rows, shards)
    doc_rows = rows[:a.docs] if a.docs else rows
    n = build_docs(doc_rows)
    dt = time.time() - t0
    with open(os.path.join(OUT, "STATS.md"), "w", encoding="utf-8") as f:
        f.write("# 天网资源检索站 · 生成报告\n\n- 生成时间：%s\n- 耗时：%.1fs\n- 索引条数：%s\n"
                "- 索引分片：%d 个（每片 %d 条）\n- 详情页：%s 个\n- 来源站点：%s\n\n"
                "数据源：kb_index.db（fine_classify 多维标签）。搜索优先，分类/形态为辅助导航。\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), dt, format(stats["总条数"], ","),
                    len(shards), SHARD, format(n, ","), format(stats["来源站点"], ",")))
    print("完成 %.1fs：索引 %d 条 / %d 片；详情页 %d 个 → %s" %
          (dt, stats["总条数"], len(shards), n, OUT))


if __name__ == "__main__":
    main()
