"""Unified interactive dashboard (Plotly, embedded inline) over the full run
history. One page renders every subsystem's results: a Suite selector picks the
subsystem (HTTP, WebSocket, …), then Run / Compare-vs / Sweep / Metric / Group
(focus one or small-multiples) / linear-log slice the data live. Reference lines
(configured limit / CPU cores) and a per-variant configuration panel travel with
each suite. Non-destructive: reads ``<outdir>/history/`` and embeds all runs.

Every run states its SUITE COVERAGE — the run picker tags each record with the
suites it contains (``[http+websocket]`` vs ``[websocket only]``) and the Suite
selector renders the suites a record is missing as disabled entries — so a
single-suite record is never presented as if it were the whole battery. Coverage
is measured against what the record DECLARED it would contain
(``expected_suites``) where it declared anything, falling back to the suites seen
across the history for records archived before that declaration existed; a record
missing a suite it promised is labeled INCOMPLETE, not merely narrow."""

from __future__ import annotations

import json
import pathlib

from benchmarks.core.results import load_history

_FALLBACK = [
    "#0072B2",
    "#009E73",
    "#E69F00",
    "#D55E00",
    "#CC79A7",
    "#56B4E9",
    "#F0E442",
    "#000000",
]


def run_blocks(runs: list[dict]) -> list[dict]:
    """The per-run payload the page embeds — everything the client needs about a
    record, and nothing the archive happens to carry that it does not."""
    blocks = []
    for r in runs:
        block = {
            "id": r["id"],
            "label": r.get("label", ""),
            "ts": r.get("ts", ""),
            "sha": r.get("sha", ""),
            "branch": r.get("branch", ""),
            "subject": r.get("subject", ""),
            "cores": r.get("cores"),
            "suites": r.get("suites", {}),
            # Per-suite origin stamps: a canonical record is fed by several suite
            # runs, so each suite states when IT was measured.
            "provenance": r.get("provenance", {}),
            "merged_from": r.get("merged_from", []),
        }
        # Coverage the record DECLARED. Emitted only when the entry actually
        # carries a declaration: an archive written before `expected_suites`
        # existed must render byte-for-byte as it always did (the JS falls back
        # to the history-wide suite set for it) — migrate on read, never rewrite.
        if r.get("expected_suites"):
            block["expected_suites"] = list(r["expected_suites"])
        blocks.append(block)
    return blocks


def write_dashboard(
    outdir: str, title: str = "hyperdjango — performance benchmarks"
) -> pathlib.Path:
    from plotly.offline import get_plotlyjs

    d = pathlib.Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    runs = load_history(outdir)
    blocks = run_blocks(runs)

    data = {
        "title": title,
        "cores": runs[-1].get("cores") if runs else None,
        "runs": blocks,
        "default_run": blocks[-1]["id"] if blocks else None,
    }

    doc = (
        _SHELL.replace("/*CSS*/", _CSS)
        .replace("/*TITLE*/", title)
        .replace("/*DATA*/", json.dumps(data))
        .replace("/*DASH*/", _DASH_JS)
        .replace("/*PLOTLY*/", get_plotlyjs())
    )
    out = d / "index.html"
    out.write_text(doc)
    return out


_CSS = """
:root{--bg:#fff;--surface:#f6f8fa;--ink:#1a1f26;--muted:#5b6570;--grid:#e6ebef;--border:#d7dde3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--surface:#171b21;--ink:#e6eaef;--muted:#9aa4af;--grid:#2a3038;--border:#2a3038}}
:root[data-theme=dark]{--bg:#0f1216;--surface:#171b21;--ink:#e6eaef;--muted:#9aa4af;--grid:#2a3038;--border:#2a3038}
:root[data-theme=light]{--bg:#fff;--surface:#f6f8fa;--ink:#1a1f26;--muted:#5b6570;--grid:#e6ebef;--border:#d7dde3}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0 auto;padding:24px;max-width:1180px}
h1{font-size:24px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px;margin:2px 0 16px}
.controls{display:flex;flex-wrap:wrap;gap:12px 22px;align-items:center;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:12px;position:sticky;top:0;z-index:5}
.ctl{display:flex;align-items:center;gap:8px}
.ctl>.lbl{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.seg{display:inline-flex;background:var(--bg);border:1px solid var(--border);border-radius:9px;overflow:hidden}
.seg button{border:0;border-left:1px solid var(--border);background:transparent;color:var(--muted);padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}
.seg button:first-child{border-left:0}
.seg button:hover{color:var(--ink)}
.seg button:disabled{opacity:.45;font-style:italic;cursor:not-allowed}
.seg button:disabled:hover{color:var(--muted)}
.refnote .tag.warn{color:#b9770e}
@media (prefers-color-scheme:dark){.refnote .tag.warn{color:#d9a441}}
.seg button.on{background:#0072B2;color:#fff;font-weight:600}
@media (prefers-color-scheme:dark){.seg button.on{background:#3a9bdc}}
select{background:var(--bg);color:var(--ink);border:1px solid var(--border);border-radius:9px;padding:6px 10px;font:inherit;font-size:13px}
.chk{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;cursor:pointer;user-select:none}
.refnote{color:var(--muted);font-size:12.5px;margin:12px 2px 0;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center}
.refnote .tag{display:inline-flex;align-items:center;gap:7px}
.refnote .dash{width:22px;border-top:2px dashed var(--muted)}
.refnote .dash.cfg{border-top-style:dotted;border-top-color:#b9770e}
@media (prefers-color-scheme:dark){.refnote .dash.cfg{border-top-color:#d9a441}}
.setup{margin:8px 2px 0}
.setup summary{cursor:pointer;color:var(--muted);font-size:12.5px;font-weight:600;padding:4px 0}
.setup .cfg-list{display:flex;flex-direction:column;gap:5px;margin:8px 0 6px;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:10px}
.setup .cfg-row{display:flex;align-items:baseline;gap:9px;font-size:12.5px;flex-wrap:wrap}
.setup .cfg-row .sw{width:11px;height:11px;border-radius:3px;flex:0 0 auto;align-self:center}
.setup .cfg-row b{min-width:170px}
.setup .cfg-desc{color:var(--muted)}
.setup .cfg-note{color:var(--muted);font-size:12px;margin:2px 4px 0;line-height:1.55}
#chart{width:100%;height:640px;margin-top:8px}
.foot{color:var(--muted);font-size:12px;margin-top:14px}
.hidden{display:none!important}
.summary{margin:12px 2px 2px;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:12px}
.summary .desc{font-size:13.5px;line-height:1.5;margin:0 0 4px}
.summary .dir{font-size:12.5px;color:var(--muted);margin:0 0 10px}
.summary .dir b{color:var(--ink)}
.summary .hd{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin:2px 0 7px}
.summary .row{display:flex;align-items:center;gap:10px;line-height:2.0;font-size:13px}
.summary .row .sw{width:11px;height:11px;border-radius:3px;flex:0 0 auto}
.summary .row .nm{min-width:180px;font-weight:600}
.summary .row .val{min-width:120px;font-variant-numeric:tabular-nums}
.summary .row .bar{height:8px;border-radius:4px;flex:1;max-width:240px;background:var(--border);overflow:hidden}
.summary .row .bar>span{display:block;height:100%;border-radius:4px}
.summary .row .rt{color:var(--muted);min-width:120px}
.summary .row.best .rt{color:#1a9d5a;font-weight:700}
@media (prefers-color-scheme:dark){.summary .row.best .rt{color:#3ec07f}}
"""

_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>/*TITLE*/</title>
<style>/*CSS*/</style>
<script>/*PLOTLY*/</script>
</head><body>
<h1>/*TITLE*/</h1>
<p class="sub" id="subtitle"></p>
<div class="controls" id="controls"></div>
<div class="refnote" id="refnote"></div>
<div class="setup" id="setup"></div>
<div class="summary" id="summary"></div>
<div id="chart"></div>
<p class="foot">Interactive: hover for values · drag to zoom · double-click to reset · click a legend entry to toggle a variant · camera icon exports PNG. Rendered with Plotly (embedded inline — no network needed).</p>
<script>const DATA=/*DATA*/;</script>
<script>/*DASH*/</script>
</body></html>"""

_DASH_JS = r"""
(function(){
  const RUNS=DATA.runs||[];
  const chartEl=document.getElementById('chart');
  if(!RUNS.length){ chartEl.textContent='No runs yet.'; return; }
  const runById=id=>RUNS.find(r=>r.id===id)||RUNS[RUNS.length-1];
  const suitesOf=r=>(r&&r.suites)||{};
  const state={ run:DATA.default_run||RUNS[RUNS.length-1].id, compare:'',
                suite:null, sweep:null, metric:null, group:null, view:'focus', logy:false };

  const FALL=['#0072B2','#009E73','#E69F00','#D55E00','#CC79A7','#56B4E9','#F0E442','#111'];
  function isDark(){ const t=document.documentElement.getAttribute('data-theme'); if(t)return t==='dark'; return !!(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches); }
  function theme(){ return isDark()
    ? {paper:'#0f1216',plot:'#0f1216',font:'#e6eaef',grid:'#2a3038',zero:'#3a424c',muted:'#9aa4af',cfg:'#d9a441',tag:'#171b21'}
    : {paper:'#ffffff',plot:'#ffffff',font:'#1a1f26',grid:'#e6ebef',zero:'#c7ced5',muted:'#5b6570',cfg:'#b9770e',tag:'#f2f5f8'}; }
  function el(tag,attrs,kids){ const e=document.createElement(tag); if(attrs)for(const k in attrs){ if(k==='class')e.className=attrs[k]; else if(k==='text')e.textContent=attrs[k]; else e.setAttribute(k,attrs[k]);} (kids||[]).forEach(k=>e.appendChild(k)); return e; }
  function seg(items,cur,onPick){ const box=el('div',{class:'seg'}); items.forEach(it=>{ const b=el('button',{text:it.label}); if(it.key===cur)b.classList.add('on'); b.onclick=()=>onPick(it.key); box.appendChild(b);}); return box; }
  function ctl(label,node,id){ const c=el('div',{class:'ctl'},[el('span',{class:'lbl',text:label}),node]); if(id)c.id=id; return c; }
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

  // Every suite key seen anywhere in the history — the fallback yardstick a run's
  // coverage is stated against, so a one-suite record is never presented as if
  // it were the whole story.
  const ALL_SUITES=(function(){ const s={}; RUNS.forEach(r=>Object.keys(suitesOf(r)).forEach(k=>{s[k]=1;})); return Object.keys(s).sort(); })();
  function suiteKeysOf(r){ return Object.keys(suitesOf(r)).sort(); }
  // What the WRITER declared this record was supposed to contain. Records
  // archived before that declaration existed carry none, and fall back to the
  // history-wide set — exactly the labeling they have always had.
  function declaredOf(r){ return (r&&r.expected_suites)||[]; }
  // The yardstick: the declaration UNION every suite the history knows about, so
  // neither a suite the record promised nor a suite its peers carry can go
  // unmentioned.
  function yardstickOf(r){ const s={}; ALL_SUITES.forEach(k=>{s[k]=1;}); declaredOf(r).forEach(k=>{s[k]=1;}); return Object.keys(s).sort(); }
  function missingSuites(r){ const ks=suiteKeysOf(r); return yardstickOf(r).filter(k=>ks.indexOf(k)<0); }
  // Declared-but-absent: the record itself says it should have had this suite.
  // That is a broken record, not merely a narrower one.
  function missingDeclared(r){ const ks=suiteKeysOf(r); return declaredOf(r).filter(k=>ks.indexOf(k)<0); }
  function coverageTag(r){ const ks=suiteKeysOf(r); if(!ks.length)return 'no suites';
    return ks.join('+')+(missingSuites(r).length?' only':''); }

  function curSuites(){ return suitesOf(runById(state.run)); }
  function SU(){ return curSuites()[state.suite]||null; }
  function SW(){ const su=SU(); return su?(su.sweeps[state.sweep]||null):null; }
  function baseSW(){ if(!state.compare)return null; const su=(suitesOf(runById(state.compare))||{})[state.suite]; return su?(su.sweeps[state.sweep]||null):null; }
  function metricsOf(){ const su=SU(); return su?su.metrics:[]; }
  // A sweep may declare its own variant list (core's sweep schema carries one):
  // e.g. a connection-model comparison whose variants are two configurations of
  // ONE server. Falling back to the suite's variants keeps every existing suite
  // unchanged, and stops sweep-specific variants from being drawn as empty
  // series (and empty legend entries) on every other sweep.
  function varsOf(su,sw){ return (sw&&sw.variants&&sw.variants.length)?sw.variants:(su?su.variants:[]); }
  function colorFor(su,v){ const i=su.variants.indexOf(v); return (su.colors&&su.colors[v])||FALL[(i<0?0:i)%FALL.length]; }

  function ensureSuite(){ const ks=Object.keys(curSuites()); if(!ks.includes(state.suite)) state.suite=ks[0]||null; }
  function ensureSweep(){ const su=SU(); if(!su){state.sweep=null;return;} const ks=Object.keys(su.sweeps); if(!ks.includes(state.sweep)) state.sweep=ks[0]||null; }
  function availMetrics(){ const su=SU(),sw=SW(); if(!su)return []; if(sw&&sw.metrics&&sw.metrics.length) return su.metrics.filter(m=>sw.metrics.includes(m.key)); return su.metrics; }
  function ensureMetric(){ const ms=availMetrics(); if(!ms.some(m=>m.key===state.metric)) state.metric=ms.length?ms[0].key:null; }
  function ensureGroup(){ const sw=SW(); if(!sw){state.group=null;return;} const gk=sw.groups.map(g=>g.key); if(!gk.includes(state.group)) state.group=gk[0]; }
  function ensureAll(){ ensureSuite(); ensureSweep(); ensureMetric(); ensureGroup(); }

  function metricM(){ return metricsOf().find(m=>m.key===state.metric)||{key:state.metric,label:state.metric,unit:''}; }
  function groupLabel(sw,gk){ const g=sw.groups.find(x=>x.key===gk); return g?(g.label||g.key):gk; }
  function runLabel(r){ const l=r.label?r.label+' · ':''; const t=(r.ts||'').replace('T',' ').slice(0,16); return l+(r.sha||'?')+(t?' · '+t:'')+' · ['+coverageTag(r)+']'; }

  // Controls are built ONCE (buildBar) and thereafter updated in place
  // (syncControls) — never torn down. A value change (compare / metric / group /
  // view / log) only redraws the chart; a structural change (run / suite / sweep,
  // which alters the *available* options) also refreshes dependent controls. This
  // fixes the earlier bug where every interaction rebuilt the whole bar, wiping
  // the very <select> the user was operating.
  const E = {};
  function fillSelect(sel, items, active){
    sel.innerHTML='';
    items.forEach(it=>{ const o=el('option',{value:it.key,text:it.label}); if(it.key===active)o.selected=true; sel.appendChild(o); });
  }
  function fillSeg(box, items, active, onPick){
    box.innerHTML='';
    items.forEach(it=>{ const b=el('button',{text:it.label}); b.dataset.key=it.key;
      if(it.disabled){ b.disabled=true; if(it.title)b.title=it.title; }
      else { if(it.key===active)b.classList.add('on'); b.onclick=()=>onPick(it.key); }
      box.appendChild(b); });
  }
  function setSegActive(box, key){ Array.from(box.children).forEach(b=>b.classList.toggle('on', b.dataset.key===String(key))); }

  function buildBar(){
    ensureAll();
    const c=document.getElementById('controls'); c.innerHTML='';
    E.run=el('select'); E.run.onchange=()=>{ state.run=E.run.value; if(state.compare===state.run)state.compare=''; structural(); };
    c.appendChild(ctl('Run', E.run));
    if(RUNS.length>1){
      E.compare=el('select'); E.compare.onchange=()=>{ state.compare=E.compare.value; draw(); };
      c.appendChild(ctl('Compare vs', E.compare));
    }
    E.suite=el('div',{class:'seg'}); E.suiteCtl=ctl('Suite', E.suite); c.appendChild(E.suiteCtl);
    E.sweep=el('div',{class:'seg'}); c.appendChild(ctl('Sweep', E.sweep));
    E.metric=el('div',{class:'seg'}); E.metricCtl=ctl('Metric', E.metric); c.appendChild(E.metricCtl);
    E.view=el('div',{class:'seg'}); E.viewCtl=ctl('View', E.view); c.appendChild(E.viewCtl);
    E.group=el('select'); E.group.onchange=()=>{ state.group=E.group.value; draw(); };
    E.groupCtl=ctl('Group', E.group, 'grp'); c.appendChild(E.groupCtl);
    E.logy=el('input'); E.logy.type='checkbox'; E.logy.onchange=()=>{ state.logy=E.logy.checked; draw(); };
    const chk=el('label',{class:'chk'}); chk.appendChild(E.logy); chk.appendChild(document.createTextNode(' log scale (Y)'));
    c.appendChild(el('div',{class:'ctl'},[chk]));
    syncControls();
  }

  function syncControls(){
    ensureAll();
    const su=SU(), sw=SW();
    fillSelect(E.run, RUNS.slice().reverse().map(r=>({key:r.id,label:runLabel(r)})), state.run);
    if(E.compare){
      const opts=[{key:'',label:'— none —'}].concat(
        RUNS.slice().reverse().filter(r=>r.id!==state.run).map(r=>({key:r.id,label:runLabel(r)})));
      if(!opts.some(o=>o.key===state.compare)) state.compare='';
      fillSelect(E.compare, opts, state.compare);
    }
    // The Suite selector states this RECORD's coverage: the suites it contains
    // are pickable, the ones it does not are shown disabled rather than omitted —
    // a record covering one suite must never look like the whole battery.
    const suiteKeys=Object.keys(curSuites());
    const curRun=runById(state.run), declMissing=missingDeclared(curRun);
    E.suiteCtl.classList.toggle('hidden', yardstickOf(curRun).length<=1);
    const suiteItems=suiteKeys.map(k=>({key:k,label:curSuites()[k].label||k})).concat(
      missingSuites(curRun).map(k=>declMissing.indexOf(k)>=0
        ? {key:k,label:k+' — declared, not recorded',disabled:true,
           title:'This run DECLARED a '+k+' suite and does not carry one — the record is incomplete, not merely narrower. Re-run the '+k+' suite under the same label so it merges into this record.'}
        : {key:k,label:k+' — not in this run',disabled:true,
           title:'This run recorded no '+k+' suite. Run `make bench-all` for a record covering every suite.'}));
    fillSeg(E.suite, suiteItems, state.suite, k=>{ state.suite=k; structural(); });
    fillSeg(E.sweep, Object.keys(su.sweeps).map(k=>({key:k,label:su.sweeps[k].label})), state.sweep, k=>{ state.sweep=k; structural(); });
    const am=availMetrics();
    E.metricCtl.classList.toggle('hidden', am.length<=1);
    fillSeg(E.metric, am.map(m=>({key:m.key,label:m.label})), state.metric, k=>{ state.metric=k; setSegActive(E.metric,k); draw(); });
    const multi=sw.groups.length>1;
    E.viewCtl.classList.toggle('hidden', !multi);
    fillSeg(E.view, [{key:'focus',label:'Focus'},{key:'grid',label:'Small multiples'}], state.view, k=>{
      state.view=k; setSegActive(E.view,k);
      E.groupCtl.classList.toggle('hidden', !(sw.groups.length>1 && k==='focus')); draw(); });
    fillSelect(E.group, sw.groups.map(g=>({key:g.key,label:g.label||g.key||'(all)'})), state.group);
    E.groupCtl.classList.toggle('hidden', !(multi && state.view==='focus'));
    E.logy.checked=state.logy;
  }

  function structural(){ syncControls(); draw(); }

  function hoverT(v,sfx){ const m=metricM(); const u=(m.unit==='req/s'||m.unit==='msgs/s'||m.unit==='msg/s')?('%{y:,.0f} '+m.unit):(m.unit==='MiB'?'%{y:.0f} MiB':('%{y:.3g} '+m.unit)); return v+(sfx||'')+': '+u+'<extra></extra>'; }
  function traces(su,sw,gk,ax,showlegend,dashed){
    return varsOf(su,sw).map(v=>{
      const arr=(sw.data[v+'|'+gk]||{})[state.metric]||[]; const col=colorFor(su,v);
      return { type:'scatter', mode:'lines+markers', name:v+(dashed?' (base)':''), x:sw.xs, y:arr, connectgaps:false,
        legendgroup:v, showlegend:showlegend&&!dashed,
        line:{color:col,width:dashed?1.8:2.7,dash:dashed?'dot':'solid'},
        marker:{color:col,size:dashed?5:7,symbol:dashed?'circle-open':'circle'},
        opacity:dashed?0.7:1, hovertemplate:hoverT(v,dashed?' (base)':''), xaxis:ax.x, yaxis:ax.y };
    });
  }
  function scan(su,sw,gk,agg){ varsOf(su,sw).forEach(v=>{ ((sw.data[v+'|'+gk]||{})[state.metric]||[]).forEach(x=>{ if(x!=null)agg(x); }); }); }
  function dataMax(su,sw,gk){ let mx=0; scan(su,sw,gk,x=>{if(x>mx)mx=x;}); const b=baseSW(); if(b)scan(su,b,gk,x=>{if(x>mx)mx=x;}); return mx||1; }
  function dataMin(su,sw,gk){ let mn=Infinity; scan(su,sw,gk,x=>{if(x>0&&x<mn)mn=x;}); const b=baseSW(); if(b)scan(su,b,gk,x=>{if(x>0&&x<mn)mn=x;}); return mn===Infinity?1:mn; }

  function refs(sw,xref,yref,showLabels){
    // Labels are anchored ABOVE the plot (yref:paper, y>1) so the y-axis needs no
    // headroom reserved for them — which lets the y-axis use true autorange, so
    // hiding a series via the legend re-scales the chart automatically.
    const th=theme(),shapes=[],annots=[];
    (sw.refs||[]).forEach((r,ri)=>{
      const col=r.kind==='cfg'?th.cfg:th.muted;
      shapes.push({type:'line',xref:xref,yref:yref+' domain',x0:r.v,x1:r.v,y0:0,y1:1,line:{color:col,width:1.6,dash:r.kind==='cfg'?'dot':'dash'},layer:'below'});
      if(showLabels) annots.push({xref:xref,yref:'paper',x:r.v,y:(ri%2?1.004:1.05),yanchor:'bottom',xanchor:'center',text:r.label,showarrow:false,font:{size:10,color:col},bgcolor:th.tag,borderpad:1,opacity:0.96});
    });
    return {shapes,annots};
  }
  function baseLayout(rightLegend){
    const th=theme();
    const L={ paper_bgcolor:th.paper, plot_bgcolor:th.plot, font:{color:th.font,family:'-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif',size:12.5},
      hovermode:'x unified', hoverlabel:{bgcolor:th.paper,bordercolor:th.grid,font:{color:th.font}}, shapes:[], annotations:[] };
    if(rightLegend){ L.margin={l:70,r:158,t:46,b:52}; L.legend={orientation:'v',x:1.02,xanchor:'left',y:1,font:{size:12.5}}; }
    else { L.margin={l:62,r:16,t:58,b:48}; L.legend={orientation:'h',y:1.17,x:0,font:{size:12}}; }
    return L;
  }
  function axis(th,title,islog){ return { gridcolor:th.grid,zerolinecolor:th.zero,linecolor:th.grid,tickcolor:th.grid,title:{text:title,font:{size:12,color:th.muted}},tickfont:{size:11,color:th.muted},tickangle:0,automargin:true,type:islog?'log':'linear' }; }
  function xrange(sw){ const vals=sw.xs.slice(); (sw.refs||[]).forEach(r=>vals.push(r.v)); let lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals); if(sw.xlog)return [Math.log10(lo)-0.06,Math.log10(hi)+0.06]; const pad=(hi-lo)*0.04||1; return [lo-pad,hi+pad]; }
  function setY(ax,su,sw,gk,head){ const mx=dataMax(su,sw,gk); if(state.logy){ ax.range=[Math.log10(Math.max(dataMin(su,sw,gk)*0.8,1e-4)),Math.log10(mx*(head?1.9:1.9))]; } else { ax.range=[0,mx*(head||1.28)]; } }

  function renderFocus(){
    const su=SU(),sw=SW(),th=theme(),m=metricM();
    chartEl.style.height='640px';
    const L=baseLayout(true);
    L.xaxis=Object.assign(axis(th,sw.xtitle,sw.xlog),{tickvals:sw.xs,ticktext:sw.xs.map(String),range:xrange(sw)});
    L.yaxis=axis(th,m.label+(m.unit?' ('+m.unit+')':''),state.logy); L.yaxis.autorange=true; if(!state.logy)L.yaxis.rangemode='tozero';
    const r=refs(sw,'x','y',true); L.shapes=r.shapes; L.annotations=r.annots;
    let ts=traces(su,sw,state.group,{x:'x',y:'y'},true,false);
    const b=baseSW(); if(b) ts=ts.concat(traces(su,b,state.group,{x:'x',y:'y'},false,true));
    Plotly.react('chart',ts,L,CONFIG);
  }
  function renderGrid(){
    const su=SU(),sw=SW(),th=theme(),m=metricM();
    const gs=sw.groups,n=gs.length,cols=Math.min(3,n),rows=Math.ceil(n/cols);
    const L=baseLayout(false);
    L.grid={rows:rows,columns:cols,pattern:'independent',roworder:'top to bottom'};
    L.margin={l:62,r:16,t:58,b:48}; L.height=Math.max(560,rows*300);
    let ts=[],shapes=[],annots=[]; const b=baseSW();
    gs.forEach((g,i)=>{
      const sfx=i===0?'':(i+1),xa='x'+sfx,ya='y'+sfx;
      ts=ts.concat(traces(su,sw,g.key,{x:xa,y:ya},i===0,false));
      if(b) ts=ts.concat(traces(su,b,g.key,{x:xa,y:ya},false,true));
      L['xaxis'+sfx]=Object.assign(axis(th,(i>=n-cols)?sw.xtitle:'',sw.xlog),{nticks:5,range:xrange(sw)});
      L['yaxis'+sfx]=axis(th,(i%cols===0)?m.label:'',state.logy); L['yaxis'+sfx].autorange=true; if(!state.logy)L['yaxis'+sfx].rangemode='tozero';
      annots.push({xref:xa+' domain',yref:ya+' domain',x:0.5,y:1.06,yanchor:'bottom',xanchor:'center',text:'<b>'+esc(g.label||g.key)+'</b>',showarrow:false,font:{size:12,color:th.font}});
      const r=refs(sw,xa,ya,false); shapes=shapes.concat(r.shapes); annots=annots.concat(r.annots);
    });
    L.shapes=shapes; L.annotations=annots;
    chartEl.style.height=L.height+'px';
    Plotly.react('chart',ts,L,CONFIG);
  }

  const CONFIG={responsive:true, displaylogo:false, modeBarButtonsToRemove:['lasso2d','select2d'], toImageButtonOptions:{format:'png',scale:2,filename:'hyperdjango-benchmark'}};

  function updateRefnote(){
    const su=SU(),sw=SW(),run=runById(state.run);
    document.getElementById('subtitle').textContent = su.label+' · '+sw.label+(su.note?' · '+su.note:'');
    const box=document.getElementById('refnote'); box.innerHTML='';
    const ri=el('span',{class:'tag'}); ri.textContent='run: '+runLabel(run); box.appendChild(ri);
    const miss=missingSuites(run), declMiss=missingDeclared(run);
    const cov=el('span',{class:'tag'+(miss.length?' warn':'')});
    // A record that DECLARED a suite it does not carry is incomplete against its
    // own statement of intent — say that, rather than the softer "this record is
    // narrower than its peers".
    cov.textContent = declMiss.length
      ? 'INCOMPLETE record: declared '+declaredOf(run).join(' + ')+' but recorded only '
        +(suiteKeysOf(run).join(' + ')||'nothing')+' — '+declMiss.join(' / ')+' never landed'
      : miss.length
      ? 'record covers '+suiteKeysOf(run).join(' + ')+' — no '+miss.join(' / ')+' in this record (make bench-all records every suite)'
      : 'record covers every suite: '+suiteKeysOf(run).join(' + ');
    box.appendChild(cov);
    // A canonical record is fed by several suite runs — say when THIS suite was
    // measured whenever that differs from the record's own timestamp.
    const prov=(run.provenance||{})[state.suite];
    if(prov && prov.ts && prov.ts!==run.ts){
      const pt=el('span',{class:'tag'});
      pt.textContent=state.suite+' measured '+String(prov.ts).replace('T',' ').slice(0,16)
        +(prov.label?' (fed as '+prov.label+')':'');
      box.appendChild(pt);
    }
    if(state.compare){ const bR=runById(state.compare); const t=el('span',{class:'tag'}); t.appendChild(el('span',{class:'dash'})); t.appendChild(document.createTextNode('dotted = baseline: '+runLabel(bR))); box.appendChild(t); }
    (sw.refs||[]).forEach(r=>{ const tag=el('span',{class:'tag'}); tag.appendChild(el('span',{class:'dash'+(r.kind==='cfg'?' cfg':'')})); tag.appendChild(document.createTextNode(r.label+(r.kind==='cfg'?' — configured limit':' — CPU cores'))); box.appendChild(tag); });
  }
  function updateSetup(){
    const box=document.getElementById('setup'); if(!box)return;
    const su=SU(),run=runById(state.run),cfgs=su.configs||{};
    if(!Object.keys(cfgs).length){ box.innerHTML=''; return; }
    const rows=su.variants.map(v=>'<div class="cfg-row"><span class="sw" style="background:'+colorFor(su,v)+'"></span><b>'+esc(v)+'</b><span class="cfg-desc">'+esc(cfgs[v]||'')+'</span></div>').join('');
    box.innerHTML='<details><summary>⚙ configuration &amp; interpreter</summary><div class="cfg-list">'+rows+'</div>'+
      (su.interpreter?'<p class="cfg-note">Interpreter: <b>'+esc(su.interpreter)+'</b></p>':'')+'</details>';
  }

  function fmtVal(m,x){
    if(x==null) return '—';
    if(m.unit==='req/s'||m.unit==='msgs/s') return Math.round(x).toLocaleString()+' '+m.unit;
    if(m.unit==='MiB') return Math.round(x)+' MiB';
    if(m.unit==='%') return x.toFixed(0)+'%';
    if(m.unit==='ms'||m.unit==='µs') return (x<10?x.toFixed(2):x.toFixed(1))+' '+m.unit;
    return (Math.round(x*100)/100)+(m.unit?' '+m.unit:'');
  }
  // Purpose description + metric direction + an at-a-glance comparison leaderboard
  // (each variant's value at the sweep endpoint, ranked, with its ratio vs best).
  function updateSummary(){
    const box=document.getElementById('summary'); if(!box)return;
    const su=SU(),sw=SW(),m=metricM();
    if(!su||!sw||!m){ box.innerHTML=''; return; }
    const lower=!!m.lower_is_better;
    const gk=(sw.groups.length>1 && state.view==='focus')? state.group : (sw.groups[0]?sw.groups[0].key:'');
    const xs=sw.xs;
    const rows=varsOf(su,sw).map(v=>{
      const arr=(sw.data[v+'|'+gk]||{})[state.metric]||[];
      let xi=-1; for(let i=arr.length-1;i>=0;i--){ if(arr[i]!=null){xi=i;break;} }
      return {v, val: xi>=0?arr[xi]:null, xi};
    });
    const present=rows.filter(r=>r.val!=null), pos=present.filter(r=>r.val>0);
    let html='';
    const desc=sw.desc||sw.note||'';
    if(desc) html+='<p class="desc">'+esc(desc)+'</p>';
    html+='<p class="dir">'+esc(m.label)+(m.unit?' ('+esc(m.unit)+')':'')+' — <b>'+(lower?'lower is better':'higher is better')+'</b>'
      + (state.view==='grid'?' · comparison shown for '+esc(gk||'first group'):'') +'</p>';
    // If the sweep also measures 'served connections', a throughput/latency figure
    // is only honest when the variant actually served ~everyone — otherwise the
    // number reflects a lucky subset (e.g. threaded's low p99 = only the handful
    // of connections it didn't starve). So exclude <90%-served variants from the
    // 'best' award and show each variant's served fraction inline.
    const hasServed=(sw.metrics||[]).includes('served') && state.metric!=='served';
    function servedAt(v){ const a=(sw.data[v+'|'+gk]||{})['served']||[]; for(let i=a.length-1;i>=0;i--){ if(a[i]!=null)return a[i]; } return null; }
    if(pos.length){
      const elig=hasServed? pos.filter(r=>{const s=servedAt(r.v); return s==null||s>=90;}) : pos;
      const pool=elig.length?elig:pos;
      const best=lower?Math.min.apply(null,pool.map(r=>r.val)):Math.max.apply(null,pool.map(r=>r.val));
      const xEnd=xs[Math.max.apply(null,present.map(r=>r.xi))];
      const xname=(sw.xtitle||'').split(' (')[0];
      const ordered=present.slice().sort((a,b)=>{
        if((a.val>0)!==(b.val>0)) return a.val>0?-1:1;
        return lower? a.val-b.val : b.val-a.val;
      });
      html+='<div class="hd">at '+xEnd+' '+esc(xname)+'</div>';
      let anyLow=false;
      ordered.forEach(r=>{
        const sv=hasServed?servedAt(r.v):null, low=sv!=null&&sv<90; if(low)anyLow=true;
        const isBest=r.val>0 && r.val===best && !low;
        let rt='—', frac=0.02;
        if(r.val>0){
          const ratio=lower?(r.val/best):(best/r.val);
          rt=isBest?'★ best':(ratio<10?ratio.toFixed(1):Math.round(ratio))+'× vs best';
          frac=lower?(best/r.val):(r.val/best);
        } else rt='failed / 0';
        if(sv!=null && sv<100) rt+=' <span style="color:'+(low?'#d9772e':'var(--muted)')+'">· '+sv.toFixed(0)+'% served</span>';
        html+='<div class="row'+(isBest?' best':'')+'">'
          +'<span class="sw" style="background:'+colorFor(su,r.v)+'"></span>'
          +'<span class="nm">'+esc(r.v)+'</span>'
          +'<span class="val">'+fmtVal(m,r.val)+'</span>'
          +'<span class="bar"><span style="width:'+Math.max(2,Math.min(100,frac*100))+'%;background:'+colorFor(su,r.v)+'"></span></span>'
          +'<span class="rt">'+rt+'</span></div>';
      });
      if(anyLow) html+='<p class="dir" style="margin-top:9px;color:#d9772e">⚠ '+esc(m.label)+' is measured only over connections that were actually served — a variant serving &lt;100% looks artificially good because its starved connections aren’t counted. Read it alongside <b>Served connections</b>.</p>';
    }
    box.innerHTML=html;
  }

  // draw() only re-renders the chart + captions from current state — it never
  // touches the controls, so interacting with a control can't wipe it.
  function draw(){
    ensureAll();
    const sw=SW();
    if(!sw){ chartEl.textContent='No data for this run/suite.'; return; }
    updateRefnote(); updateSetup(); updateSummary();
    if(state.view==='focus' || sw.groups.length<=1) renderFocus(); else renderGrid();
  }
  if(window.matchMedia){ try{ window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', draw); }catch(e){} }
  buildBar();
  draw();
})();
"""
