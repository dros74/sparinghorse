// Served as a STATIC FILE now, so nothing here is templated: the two server-known values ride
// in on the nonce'd window.SH bootstrap the shell writes (TECH-11).
const SH_READONLY = !!(window.SH && window.SH.readonly);   // public read-only mode
const SH_PRIVATE_URL = (window.SH && window.SH.privateUrl) || "";   // public→private console link
const SH_PAGE = document.body.dataset.page || "dash";   // §RB — "dash" (status) or "runs" (explorer)
const $ = s => document.querySelector(s);
const fmt = (n, d=1) => (n==null ? "—" : Number(n).toFixed(d));
// Per-workout calendar date, e.g. "Jun 23 - Tue" — so the runner can schedule life around the plan.
const sessDate = iso => { if(!iso) return ""; const d=new Date(iso+"T00:00:00");
  return d.toLocaleDateString("en-US",{month:"short",day:"numeric"})+" - "+d.toLocaleDateString("en-US",{weekday:"short"}); };
const getJSON = (url, opts) => fetch(url, opts).then(r => r.json().then(d => {
  if(!r.ok) throw new Error((d && d.error) || ("http " + r.status));   // an error body is a failure, not renderable data
  return d;
}));   // fetch + parse; callers keep their own try/catch
// UX-3 — every loader ends somewhere: data, an honest empty state, or a FAILURE TERMINUS with a retry
// hook. A transport failure (TypeError / !navigator.onLine) is named as offline and stamped with the
// last sync we saw; an answered-with-error read gets the plain "unavailable". Never a parked "Loading…".
let LAST_SYNC = null;
// UX-11 — the last sync is also PERSISTED (private only): a service-worker shell opened offline
// still says how old its data is. tileFail falls back to the stamp; the freshness chip seeds from it.
const storedSync = () => { if(SH_READONLY) return null; try{ return localStorage.getItem("sh-last-sync"); }catch(e){ return null; } };
const noteSync = ts => { if(ts && !SH_READONLY){ try{ localStorage.setItem("sh-last-sync", ts); }catch(e){} } };
// §RB — one footer, one writer. The dashboard learns the sync time from /api/shape and the runs
// explorer from /healthz (the same `meta.last_sync` either way), but both paint the line through
// here, so the two pages cannot word their footer differently.
const footSync = ts => { const el = $("#foot"); if(el && ts)
  el.textContent = `Spares the horse by being the horse · synced ${new Date(ts).toLocaleString()}`; };
function tileFail(host, name, retry, err){
  if(!host) return;
  const off = (err instanceof TypeError) || (typeof navigator !== "undefined" && navigator.onLine === false);
  const since = LAST_SYNC || storedSync();
  const when = since ? ` · data as of your last sync, ${new Date(since).toLocaleString()}` : "";
  host.innerHTML = `<div class="empty tilefail">${off
      ? `Offline — ${esc(name)} loads when you're back${when}.`
      : `${esc(name)} unavailable — the server didn't answer.`} <a href="#" class="tf-retry">Retry</a></div>`;
  host.querySelector(".tf-retry").addEventListener("click", ev => { ev.preventDefault();
    host.innerHTML = `<div class="empty">Loading…</div>`; retry(); });
}
// UX-9 — one place decides how the page jumps, so "reduce motion" is honoured by every scroll
// rather than the ones we remembered. Read per call, not cached: the OS setting can change while the
// page is open, and a runner who turns it on mid-session means it now.
const REDUCE = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
const scrollToEl = (el, block="start") => { if(el) el.scrollIntoView({behavior: REDUCE()?"auto":"smooth", block}); };
const esc = s => String(s==null?"":s).replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));  // HTML-escape before innerHTML

// theme switcher
const themes = $("#themes");
const SH_THEME_BG = window.SH_THEME_BG || {light:"#f4f1ea",dark:"#191a1d",aurora:"#121226"};   // each theme's --bg
function paintTheme(){ const t=document.documentElement.dataset.theme;
  themes.querySelectorAll("button").forEach(b=>b.setAttribute("aria-pressed", b.dataset.theme===t));
  // UX-11 — the browser chrome follows the theme: the tab/PWA title-bar colour, and the manifest
  // link a later install reads (the shell's head script does the same two writes at parse time)
  const mc=document.querySelector('meta[name="theme-color"]');
  if(mc) mc.setAttribute("content", SH_THEME_BG[t]||SH_THEME_BG.light);
  const mf=document.querySelector('link[rel="manifest"]');
  if(mf) mf.setAttribute("href", "/manifest.webmanifest?theme="+t); }
themes.addEventListener("click", e=>{ const b=e.target.closest("button"); if(!b)return;
  document.documentElement.dataset.theme=b.dataset.theme;
  try{localStorage.setItem("sh-theme",b.dataset.theme)}catch(e){} paintTheme(); });
paintTheme();

// `m` is the tile's metric identity ("vo2max"/"fitness"/"fatigue"/"form") — the category palette
// keys its hue off it (0.30.0; never position, which re-hues everything when a tile moves)
function tile(k, v, unit, sub, cls, desc, bg, m){
  return `<div class="tile${bg?' hascap':''}"${m?` data-m="${m}"`:""} ${desc?`title="${desc}"`:""}>
    ${bg?`<div class="tilebg" id="${bg}"></div>`:""}
    <div class="k">${k}${desc?' <span class="info">ⓘ</span>':''}</div>
    <div class="v">${v}${unit?`<small> ${unit}</small>`:""}</div>
    ${sub?`<div class="sub ${cls||""}">${sub}</div>`:""}
    ${bg?`<div class="tilecap" id="${bg}cap"></div>`:""}</div>`;
}
function monYr(iso){ const d=new Date(iso+"T00:00:00");
  return d.toLocaleDateString(undefined,{month:"short",year:"2-digit"}); }
// paint a metric's trend as a tile background + set its timeframe caption (higher = taller)
function paintTrend(id, vals, dates, captionTitle){
  const bg=document.getElementById(id); if(!bg || vals.length<2) return;
  const built=buildProfilePath(vals,{W:1000,H:120}); if(!built) return;
  bg.innerHTML=`<svg viewBox="0 0 1000 120" preserveAspectRatio="none"><path d="${built.path}" class="proffill"/></svg>`;
  bg.classList.add("on");
  const cap=document.getElementById(id+"cap");
  if(cap){ cap.textContent=`6-mo trend · ${monYr(dates[0])}–${monYr(dates[dates.length-1])}`; cap.title=captionTitle; }
}
async function drawVo2maxTrend(){
  if(!document.getElementById("vo2bg")) return;
  try{
    const d=await getJSON("/api/vo2max?months=6");
    const pts=d.points||[];
    paintTrend("vo2bg", pts.map(p=>p.vo2max), pts.map(p=>p.date),
      "Background: your per-run VO₂max estimates over the last 6 months, lightly smoothed.");
  }catch(e){}
}
async function drawShapeTrends(){   // Fitness/Fatigue/Form from the reconstructed CTL/ATL/TSB curve
  if(!document.getElementById("ctlbg")) return;
  try{
    const d=await getJSON("/api/projector?days=180");
    const h=d.history||[], dts=h.map(p=>p.date);
    paintTrend("ctlbg", h.map(p=>p.ctl), dts, "Background: reconstructed Fitness (CTL) over the last 6 months.");
    paintTrend("atlbg", h.map(p=>p.atl), dts, "Background: reconstructed Fatigue (ATL) over the last 6 months.");
    paintTrend("tsbbg", h.map(p=>p.tsb), dts, "Background: reconstructed Form (TSB) over the last 6 months.");
    // CTL ramp — weekly Δ fitness from the reconstructed daily curve (chronic-load tile footer).
    // Build a daily ramp series (ctl[i] − ctl[i−7]) for a background sparkline, like the other tiles.
    const af=document.getElementById("acwrFoot"), aft=document.getElementById("acwrFootTxt");
    if(af && aft){
      const c=h.filter(p=>p.ctl!=null);
      const ramps=[], rdts=[];
      for(let k=7;k<c.length;k++){ ramps.push(c[k].ctl - c[k-7].ctl); rdts.push(c[k].date); }
      if(ramps.length){
        const ramp=ramps[ramps.length-1];
        const cap = ramp>1 ? "fitness building" : ramp<-1 ? "easing — recovering" : "holding steady";
        aft.innerHTML = `<span class="k">CTL ramp · 7-day</span>`+
          `<span class="v">${ramp>=0?"+":""}${ramp.toFixed(1)} <small>/ wk</small></span>`+
          `<span class="cap">${cap}</span>`;
        paintTrend("acwrRampBg", ramps, rdts, "Background: 7-day CTL ramp (weekly Δ fitness) over the last 6 months.");
        af.style.display="";
      } else af.style.display="none";
    }
  }catch(e){}
}

async function loadShape(){
  let d;
  try{ const r = await fetch("/api/shape"); if(!r.ok) throw new Error("http "+r.status); d = await r.json(); }
  catch(e){ tileFail($("#tiles"), "Current shape", loadShape, e); return; }
  if(d.last_sync){ LAST_SYNC = d.last_sync; noteSync(d.last_sync); }
  const s = d.latest;
  HAS_SHAPE = !!s; _frSeen.shape=true; refreshFirstRun();   // first-run: history present?
  const tiles = $("#tiles");
  if(!s){ tiles.innerHTML = `<div class="empty">No shape snapshot yet — hit <b>Sync now</b>.</div>`; return; }
  const prog = s.effective_vo2max_progress;
  const progTxt = prog==null ? "" : `${prog>=0?"▲":"▼"} ${fmt(Math.abs(prog),2)} trend`;
  tiles.innerHTML =
    tile("Effective VO₂max", fmt(s.effective_vo2max,1), "ml/kg/min", progTxt, prog>=0?"up":"down",
      "Maximal oxygen uptake Runalyze estimates from your HR–pace relationship. The single best correlate of endurance performance. Higher is fitter.", "vo2bg", "vo2max") +
    tile("Fitness", fmt(s.fitness,0), "CTL", s.fitness_pct!=null?`${fmt(s.fitness_pct,0)}% of your all-time max`:"",null,
      "CTL (Chronic Training Load): a 42-day weighted average of daily training load (TRIMP). Your built-up aerobic fitness — slow to gain, slow to lose.", "ctlbg", "fitness") +
    tile("Fatigue", fmt(s.fatigue,0), "ATL", "acute load",null,
      "ATL (Acute Training Load): a 7-day weighted average of training load. Your recent fatigue — rises and falls fast.", "atlbg", "fatigue") +
    tile("Form", fmt(s.performance,0), "TSB", "fitness − fatigue",null,
      "TSB (Training Stress Balance) = Fitness − Fatigue. Positive = fresh/tapered; negative = loaded/building. Near a race you want it positive.", "tsbbg", "form");
  // ACWR gauge: value is a RATIO; band 0.8–1.3 on a 0–2 scale. One definition page-wide: ATL ÷ CTL
  // of this same snapshot row — the readiness card's rest-day line divides it the same way, and
  // Runalyze's own ACWR field (computed on another basis; the schema warns "API mixes units!") can
  // sit visibly off the ratio on the same row. The field only remains as the no-fitness fallback.
  const acwr = s.fitness ? s.fatigue/s.fitness : (s.acwr==null ? null : Number(s.acwr));
  const pct = x => Math.max(0,Math.min(100, x/2*100));
  $("#gband").style.left = pct(0.8)+"%"; $("#gband").style.width = (pct(1.3)-pct(0.8))+"%";
  if(acwr!=null){
    const L = pct(acwr);
    $("#gmark").style.left = L+"%";
    // Above the band = the acute spike the engine brakes (danger); below it = tapering/resting/returning —
    // deliberate more often than not, so neutral, not red (0.27.0, log §70).
    const cls = acwr>1.3 ? 'out' : acwr<0.8 ? 'low' : 'inb';
    $("#gyou").style.setProperty("--gx", L+"%");   // CSS clamps the pill inside the gauge's ends
    $("#gyou").innerHTML = `<span class="${cls}">you: ${acwr.toFixed(2)}</span>`;
  }
  footSync(d.last_sync);
  // data-quality: flag likely-duplicate activities (inflate Runalyze's fitness/fatigue too)
  const dq=$("#dqbanner");
  let dqhtml="";
  if(d.duplicate_count>0){
    const dl=d.duplicates||[];
    // direct 🗑 per leftover row, so an OLD dup (not the latest activity) is still reachable
    const del=(SH_READONLY||!dl.length)?"":` Already deleted on Runalyze but this persists? `+
      `Sync never removes the local copy — drop the leftover row: `+
      dl.map(r=>`<a href="#" class="dupdel delact" data-id="${r.id}">🗑 ${r.date||("#"+r.id)}</a>`).join(", ")+`.`;
    dqhtml+=`<div class="dqwarn">⚠ ${d.duplicate_count} likely-duplicate ${d.duplicate_count>1?"activities":"activity"} detected `+
      `(same time, distance &amp; sport — e.g. a watch/Strava double-upload). These inflate Fitness/Fatigue/ACWR `+
      `<b>on Runalyze too</b> — delete the duplicate in Runalyze and re-sync to correct the tiles above. `+
      `(The fitness/fatigue chart already ignores them.)${del}</div>`;
  }
  const ign=d.ignored||[];
  if(ign.length){
    const undo=SH_READONLY?"":ign.map(r=>`<a href="#" class="ignundo" data-id="${r.id}">${r.date||("#"+r.id)}</a>`).join(", ");
    dqhtml+=`<div class="dqnote">⊘ ${ign.length} ${ign.length>1?"activities":"activity"} manually excluded from your stats`+
      (undo?` — restore: ${undo}`:"")+`.</div>`;
  }
  dq.innerHTML=dqhtml;
  dq.querySelectorAll(".dupdel").forEach(el=>el.addEventListener("click", async ev=>{
    ev.preventDefault();
    if(!await confirmDanger({
      title:"Delete this leftover duplicate?",
      intro:"This hard-removes the leftover row from your local copy — for a duplicate you've already deleted on Runalyze that the insert-only sync left behind.",
      lines:[
        "It stops inflating the duplicate count and this banner; the fitness/fatigue chart already excludes it.",
        "If the duplicate still exists on Runalyze it will reappear on the next sync — delete it on Runalyze first.",
        "There's no local undo once it's gone from Runalyze.",
      ],
      alt:"Not sure it's gone upstream? ⊘ Ignore excludes it from the maths reversibly instead.",
      confirmLabel:"Delete locally"})) return;
    await fetch(`/api/activity/${el.dataset.id}/delete`,
      {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    await Promise.all([loadShape(), loadActivity(CURACT), loadProjector()]);
  }));
  dq.querySelectorAll(".ignundo").forEach(el=>el.addEventListener("click", async ev=>{
    ev.preventDefault(); await toggleIgnore(el.dataset.id, false);
  }));
  drawVo2maxTrend();
  drawShapeTrends();
}

function isoWeekMonday(year, wk){
  const simple=new Date(Date.UTC(year,0,1+(wk-1)*7));
  const dow=simple.getUTCDay(); const monday=new Date(simple);
  monday.setUTCDate(simple.getUTCDate()-((dow+6)%7));
  return monday;
}
// Weekly volume: we hold the FULL history and show a fixed window you can grab-drag
// horizontally to pan back/forth through time (bar heights stay on a global scale so weeks
// are comparable across the whole span).
const WEEKLY_WIN=26;
let WEEKLY_ALL=[], WEEKLY_END=0, WEEKLY_MAX=1, WEEKLY_WIRED=false;
function weekLabel(wk){ const [y,w]=wk.split("-W").map(Number); const mon=isoWeekMonday(y,w);
  return mon.toLocaleDateString(undefined,{month:"short",year:"2-digit"}); }
function renderWeekly(){
  const chart=$("#chart");
  if(!WEEKLY_ALL.length){ chart.innerHTML=`<div class="empty">No activities synced yet.</div>`; return; }
  const win=Math.min(WEEKLY_WIN, WEEKLY_ALL.length);
  const start=Math.max(0, WEEKLY_END-win);
  const rows=WEEKLY_ALL.slice(start, WEEKLY_END);
  const bars=rows.map(x=>`<div class="col" title="${x.week}: ${x.km} km">
    <div class="vlbl">${x.km>=1?Math.round(x.km):""}</div>
    <div class="barb" style="height:${Math.round(x.km/WEEKLY_MAX*120)}px"></div>
  </div>`).join("");
  // month/year ruler below: mark where the month changes
  let lastMonth="", ticks="";
  rows.forEach((x,i)=>{
    const [y,w]=x.week.split("-W").map(Number);
    const mon=isoWeekMonday(y,w);
    const key=mon.getUTCFullYear()+"-"+mon.getUTCMonth();
    if(key!==lastMonth){ lastMonth=key;
      const lbl=mon.toLocaleDateString(undefined,{month:"short"})+
        (mon.getUTCMonth()===0?` '${String(mon.getUTCFullYear()).slice(2)}`:"");
      ticks+=`<span class="tick" style="left:${(i/rows.length*100).toFixed(2)}%">${lbl}</span>`;
    }
  });
  chart.innerHTML=`<div class="bars">${bars}</div><div class="ruler">${ticks}</div>`;
  const rng=$("#wkrange");
  if(rng && rows.length){
    const atEnd = WEEKLY_END>=WEEKLY_ALL.length;
    rng.textContent = `— ${weekLabel(rows[0].week)} → ${weekLabel(rows[rows.length-1].week)}`+
      (WEEKLY_ALL.length>win ? (atEnd?" · drag to pan back ‹" : " · ‹ drag ›") : "");
  }
}
function wireWeeklyDrag(){
  if(WEEKLY_WIRED) return; WEEKLY_WIRED=true;
  const chart=$("#chart");
  let startX=0, startEnd=0, dragging=false;
  chart.addEventListener("pointerdown", e=>{
    if(WEEKLY_ALL.length<=WEEKLY_WIN) return;
    dragging=true; startX=e.clientX; startEnd=WEEKLY_END;
    chart.classList.add("grabbing"); chart.setPointerCapture?.(e.pointerId); e.preventDefault();
  });
  chart.addEventListener("pointermove", e=>{
    if(!dragging) return;
    const pxPerWeek=chart.clientWidth/Math.min(WEEKLY_WIN,WEEKLY_ALL.length);
    const dw=Math.round((e.clientX-startX)/pxPerWeek);   // drag right → older weeks
    const lo=Math.min(WEEKLY_WIN, WEEKLY_ALL.length);
    let ne=Math.max(lo, Math.min(WEEKLY_ALL.length, startEnd-dw));
    if(ne!==WEEKLY_END){ WEEKLY_END=ne; renderWeekly(); }
  });
  const end=()=>{ dragging=false; chart.classList.remove("grabbing"); };
  chart.addEventListener("pointerup", end);
  chart.addEventListener("pointercancel", end);
  // mouse wheel / trackpad: scroll down or right → back in time, up or left → forward
  let wheelAcc=0;
  chart.addEventListener("wheel", e=>{
    if(WEEKLY_ALL.length<=WEEKLY_WIN) return;   // nothing to pan; let the page scroll
    e.preventDefault();
    wheelAcc += (Math.abs(e.deltaY)>=Math.abs(e.deltaX) ? e.deltaY : e.deltaX);
    const step=Math.trunc(wheelAcc/40);          // ~one week per notch, accumulate sub-steps
    if(!step) return;
    wheelAcc -= step*40;
    const lo=Math.min(WEEKLY_WIN, WEEKLY_ALL.length);
    const ne=Math.max(lo, Math.min(WEEKLY_ALL.length, WEEKLY_END-step));
    if(ne!==WEEKLY_END){ WEEKLY_END=ne; renderWeekly(); }
  }, {passive:false});
}
async function loadWeekly(){
  let all;
  try{ all = await getJSON("/api/weekly?weeks=0"); }   // full history (tiny)
  catch(e){ tileFail($("#chart"), "Weekly volume", loadWeekly, e); return; }
  WEEKLY_ALL = all;
  WEEKLY_END = WEEKLY_ALL.length;
  WEEKLY_MAX = Math.max(...WEEKLY_ALL.map(x=>x.km), 1);
  renderWeekly();
  wireWeeklyDrag();
}

async function loadWeather(){
  try{ WX = await getJSON("/api/weather"); }catch(e){ WX=null; }
  if(RDY) renderReadiness(RDY);   // fold the three-city forecast into the readiness card footer
}

// Resilient sync POST: a long backfill can exceed the gateway timeout and return an HTML error page,
// which r.json() would choke on with a cryptic "Unexpected token '<'". Catch that and return a clean,
// actionable result. (The heavy part is the activity re-walk; the health-metric pull is ~seconds.)
async function postSync(url){
  let r;
  try{ r=await fetch(url,{method:"POST"}); }
  catch(e){ return {ok:false, error:"network error — "+e}; }
  if(!(r.headers.get("content-type")||"").includes("application/json")){
    return {ok:false, timeout:true, error:
      `the request didn't return JSON (HTTP ${r.status}) — a full backfill can exceed the gateway `+
      `timeout. It may still be finishing on the server, and your recent data syncs fine; a one-off `+
      `full-history pull is best run on the host.`};
  }
  try{ return await r.json(); }catch(e){ return {ok:false, error:"couldn't read the response — "+e}; }
}
$("#syncBtn").addEventListener("click", async ()=>{
  const b=$("#syncBtn"); b.disabled=true; const t=b.textContent; b.textContent="Syncing…";
  try{
    const d=await postSync("/api/sync");
    if(!d.ok) await notice({title:"Sync failed", intro:d.error||"unknown", tone:"danger"});
    await loadShape(); await loadRecent(); await loadProjector(); await loadWeekly(); loadDrift(); loadEffort(); loadZones();
  }finally{ b.disabled=false; b.textContent=t; }
});
$("#backfillBtn").addEventListener("click", async ()=>{
  if(!await confirmDanger({title:"Backfill the full history?",
      intro:"This walks every page back to your first activity.",
      lines:["On a large history it can exceed the gateway timeout.",
             "It is usually only needed on a fresh machine."],
      confirmLabel:"Backfill"})) return;
  const b=$("#backfillBtn"); b.disabled=true; const t=b.textContent; b.textContent="Backfilling…";
  try{
    const d=await postSync("/api/sync?backfill=1");
    if(!d.ok) await notice({title:"Backfill didn't complete", intro:d.error||"unknown", tone:"danger"});
    else await notice({title:"Backfill done",
      intro:`Added ${d.activities?.added ?? 0} activities across ${d.activities?.pages_fetched ?? 0} pages.`});
    await loadShape(); await loadRecent(); await loadProjector(); await loadWeekly(); loadDrift(); loadEffort(); loadZones();
  }finally{ b.disabled=false; b.textContent=t; }
});

// ── Latest activity ─────────────────────────────────────────────────────────
function metric(label, val, unit){
  return `<div class="metric"><div class="ml">${label}</div>
    <div class="mv">${val}${unit?`<small> ${unit}</small>`:""}</div></div>`;
}
function paceStr(minkm){ if(minkm==null) return "—"; const m=Math.floor(minkm), s=Math.round((minkm-m)*60);
  return `${m}:${String(s).padStart(2,"0")}`; }
function durStr(sec){ if(!sec) return "—"; const h=Math.floor(sec/3600), m=Math.round(sec%3600/60);
  return h?`${h}h${String(m).padStart(2,"0")}`:`${m} min`; }
// profile hover: draw an activity's pace/HR/cadence trace as a subtle tile background
let ACTPROFILE=null;
function buildProfilePath(vals, {invert=false, W=1000, H=120}={}){
  const pts=vals.map((v,i)=>[i,v]).filter(p=>p[1]!=null);
  if(pts.length<2) return null;
  const xs=pts.map(p=>p[0]), ys=pts.map(p=>p[1]);
  const hi=Math.max(...ys), lo=Math.min(...ys);
  const x=i=>i/(vals.length-1)*W;
  const y=v=>{ let t=(v-lo)/((hi-lo)||1); if(invert) t=1-t; return H-6-t*(H-18); };
  const poly=pts.map(p=>`${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`);
  const d=`M0,${H} L`+poly.join(" L")+` L${W},${H} Z`;   // filled area (baseline-closed)
  const line="M"+poly.join(" L");                         // stroke-only polyline (no fill/baseline)
  return {path:d, line, hi, lo, y, x, pts, W, H};
}
let LOCKED="pace";  // the profile shown by default / when not hovering
// the channel + chart orientation for a metric (pace inverts: faster = taller)
function profileVals(kind){
  const p=ACTPROFILE; if(!p) return null;
  if(kind==="pace" && p.has_pace) return {vals:p.pace, invert:true};
  if(kind==="hr" && p.has_hr) return {vals:p.hr, invert:false};
  if(kind==="cadence" && p.has_cadence) return {vals:p.cadence, invert:false};
  if(kind==="elevation" && p.has_elevation) return {vals:p.elevation, invert:false};
  return null;
}
function profileLabel(kind){
  const p=ACTPROFILE||{};
  return kind==="pace"?"pace profile (faster = higher)"
    : kind==="hr"?`heart-rate profile · avg ${p.hr_avg||"—"} bpm`
    : kind==="elevation"?"elevation profile (climb)"
    : "cadence profile (higher = quicker)";
}
// red→green by t (0=red, 1=green) for the hover line
// UX-6 — a SINGLE-HUE ramp, not red→green. The old one swept hue 0→120, which is the one pair a
// red-green colourblind reader cannot separate — and it was carrying real information (which part of
// this run was quick). Strength of the house accent carries it instead, so the ordering survives any
// colour vision and any theme. The semantics are unchanged and were never wrong: this is RELATIVE TO
// THIS RUN — the strongest mark is the fastest part of THIS run, not a verdict on the pace itself.
function rg(t){ t=t<0?0:t>1?1:t;
  return `color-mix(in oklab, var(--accent) ${Math.round(18+82*t)}%, var(--surface2))`; }
// 5 HR zones at 60/70/80/90 %HRmax — reconstructed from Runalyze's per-activity zone distribution
// (see runalyze-hr-zones-api). [label, colour, upper %HRmax bound]; single source for line + legend.
// 5 HR-zone colours + labels (Z1→Z5). The BOUNDARIES come from the server's unified hr_zones model
// (LTHR-anchored when confident, %HRmax fallback) — served per-activity as bpm cutoffs, so the chart
// hover, the zone band, and the effort monitor all read ONE definition.
const HRZONE_COLORS=["var(--z1)","var(--z2)","var(--z3)","var(--z4)","var(--z5)"];   // UX-6 — themeable, no literals
const HRZONE_LABELS=["Z1","Z2","Z3","Z4","Z5"];
// per-sample zone index from bpm cutoffs (4 ascending boundaries → 5 zones); -1 when uncolourable.
function hrZoneIdx(v, cutoffs){
  if(v==null || !cutoffs || !cutoffs.length) return -1;
  for(let i=0;i<cutoffs.length;i++) if(v<cutoffs[i]) return i;
  return cutoffs.length;
}
function hrZoneColor(v, cutoffs){ const i=hrZoneIdx(v,cutoffs); return i<0 ? "transparent" : HRZONE_COLORS[i]; }
// A thin discrete strip across the TOP of the activity chart: which HR zone the runner was in at each
// section of the run. Always-on (no hover) — segments coloured by the unified zone model. Empty when
// there's no HR / no zone model (e.g. the public box strips both), so it degrades to nothing.
function hrBandSvg(p, W, bandH){
  const cut=(p.hrzones||{}).cutoffs, hr=p.hr;
  if(!cut || !cut.length || !hr || !hr.length) return "";
  const n=hr.length; let segs="";
  for(let i=0;i<n;i++){
    const c=hrZoneColor(hr[i], cut); if(c==="transparent") continue;
    const x0=i/n*W;
    segs+=`<rect x="${x0.toFixed(1)}" y="0" width="${(W/n+0.6).toFixed(1)}" height="${bandH}" fill="${c}"/>`;
  }
  return segs ? `<g class="hrband" opacity="0.9">${segs}</g>` : "";
}
function hrLegendHtml(){
  const z=(ACTPROFILE||{}).hrzones||{};
  const basis = z.anchor==="lthr" ? ` · LTHR ${z.ref}` : z.anchor==="hrmax" ? ` · %HRmax` : "";
  return `<span class="hrleg">`+HRZONE_LABELS.map((lb,i)=>`<span class="hrz"><i style="background:${HRZONE_COLORS[i]}"></i>${lb}</span>`).join("")
         +(basis?`<span class="hrz" style="opacity:.7">${basis}</span>`:"")+`</span>`;
}
// per-sample colour for the hover line — meaning differs by metric
function metricColor(kind, v, ctx){
  if(v==null) return "transparent";
  if(kind==="hr"){
    if(ctx.cutoffs && ctx.cutoffs.length) return hrZoneColor(v, ctx.cutoffs);  // unified bpm zones
    const hm=ctx.hrmax||ctx.hi||0, f=hm?v/hm:0;                                // defensive %HRmax fallback
    const FB=[0.60,0.70,0.80,0.90]; for(let i=0;i<FB.length;i++) if(f<FB[i]) return HRZONE_COLORS[i];
    return HRZONE_COLORS[4]; }
  if(kind==="pace")    return rg((ctx.hi-v)/((ctx.hi-ctx.lo)||1));   // faster (smaller sec/km) = green
  if(kind==="cadence"){ const d=Math.abs(v-180);                    // 170–190 stays green, then ramps
    return rg(d<=10 ? 1 : 1-(d-10)/15); }                           // to red by ±25 (155 / 205)
  return "var(--accent)";                                            // elevation: neutral single hue
}
// draw the LOCKED metric as a shaded area; if hovering a DIFFERENT metric, trace it as a
// value-coloured line on top (no fill). hoverKind=null → locked layer only.
function renderProfile(hoverKind){
  const p=ACTPROFILE; if(!p) return;
  const bg=$("#actbg"); const W=1000,H=120;
  let svg="";
  const lb=profileVals(LOCKED);
  if(lb){
    const bl=buildProfilePath(lb.vals,{W,H,invert:lb.invert});
    if(bl){
      let avg="";
      if(LOCKED==="hr" && p.hr_avg){ const yv=bl.y(p.hr_avg);
        avg=`<line x1="0" y1="${yv.toFixed(1)}" x2="${W}" y2="${yv.toFixed(1)}" class="avgline"/>`; }
      svg += `<path d="${bl.path}" class="proffill"/>${avg}`;
    }
  }
  const showHover = hoverKind && hoverKind!==LOCKED;
  const hb = showHover ? profileVals(hoverKind) : null;
  if(hb){
    const bh=buildProfilePath(hb.vals,{W,H,invert:hb.invert});
    if(bh){
      const ctx={hi:bh.hi, lo:bh.lo, hrmax:p.hrmax, cutoffs:(p.hrzones||{}).cutoffs};
      const stops=bh.pts.map(pt=>`<stop offset="${(bh.x(pt[0])/W*100).toFixed(2)}%" style="stop-color:${metricColor(hoverKind,pt[1],ctx)}"/>`).join("");
      svg += `<defs><linearGradient id="aclg" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="${W}" y2="0">${stops}</linearGradient></defs>`+
             `<path d="${bh.line}" class="profline" stroke="url(#aclg)"/>`;
    }
  }
  svg += hrBandSvg(p, W, 6);   // always-on zone strip at the top — independent of locked/hover layers
  bg.innerHTML=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${svg}</svg>`;
  bg.classList.toggle("on", !!svg);
  const lblKind = (showHover && hb) ? hoverKind : LOCKED;
  $("#proflbl").textContent = profileLabel(lblKind) + ((showHover && hb) ? " · click to lock" : " · 🔒 locked");
  // The zone legend shows whenever the always-on HR-zone band is present (not just on HR hover) —
  // otherwise the band's colours are uninterpretable. It carries the basis (LTHR vs %HRmax).
  const bandOn = !!((p.hrzones||{}).cutoffs && p.has_hr);
  const leg=$("#hrlegend"); if(leg) leg.innerHTML = bandOn ? hrLegendHtml() : "";
  // yield the hint to the wide HR legend (visibility, not display, so the row height never shifts)
  const hint=document.querySelector("#recent .profhint"); if(hint) hint.style.visibility = bandOn ? "hidden" : "visible";
  document.querySelectorAll("#recent .metric.hovx").forEach(el=>{
    el.classList.toggle("locked", el.dataset.prof===LOCKED);
    el.setAttribute("aria-pressed", el.dataset.prof===LOCKED);   // UX-9: the lock is state, so say so
  });
}
let CURACT=null;   // the activity currently shown in the tile (null = the latest)
function loadRecent(){ return loadActivity(); }   // default tile = latest activity
async function loadActivity(aid){
  CURACT = aid || null;
  const host=$("#recent");
  let a;
  try{ a = await getJSON(aid?`/api/activity/${aid}`:"/api/activity/latest"); }
  catch(e){ tileFail(host, aid ? "That activity" : "Latest activity", ()=>loadActivity(aid), e); return; }
  // a non-run is the most-recent activity → note it (private view only). Its load still counts toward
  // the plan via Runalyze's all-sport fitness/fatigue, so this just explains why an older run shows.
  const cx = (!SH_READONLY && a && a.cross_training) ? a.cross_training : null;
  const cxDate = cx && cx.date ? new Date(cx.date+"T00:00:00").toLocaleDateString(undefined,{day:"numeric",month:"short"}) : "";  // local midnight (no UTC off-by-one)
  const crossNote = cx
    ? `<div class="crossnote">Your latest activity was <b>${esc(cx.sport||"a non-run")}</b>${cxDate?` on ${cxDate}`:""} — not a run, so it isn't shown here. Its training load still counts toward your plan, reflected in your fitness &amp; fatigue markers (Runalyze tracks all sports).</div>`
    : "";
  if(!a || a.empty_run){
    host.innerHTML = (a&&a.empty_run ? `<div class="empty">No running activity logged yet.</div>`
      : `<div class="empty">${aid?"Activity not found.":"No activities synced yet."}</div>`) + crossNote;
    return;
  }
  const dt=new Date(a.date_time);
  const when=dt.toLocaleDateString(undefined,{weekday:"short",day:"numeric",month:"short"})+
    " · "+dt.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"});
  const m=(label,val,unit,hover)=>`<div class="metric ${hover?'hovx':''}" ${hover?`data-prof="${hover}"`:""}>
    <div class="ml">${label}</div><div class="mv">${val}${unit?`<small> ${unit}</small>`:""}</div></div>`;
  const kick = aid
    ? `Activity of ${dt.toLocaleDateString(undefined,{day:"numeric",month:"short",year:"2-digit"})} <a href="#" id="backlatest" class="backlatest">← latest</a>`
    : "Latest running activity";
  host.innerHTML=`<div class="actwrap"><div class="actbg" id="actbg"></div>
    <div class="actfg">
      <div class="rtop">
        <div class="rkick">${kick}</div>
        ${SH_READONLY||!a.id?"":`<div class="dqtools">`+
          (a.ignored
            ? `<span class="muted">⊘ ignored</span> <a href="#" id="igntog" data-id="${a.id}" data-on="0">undo</a>`
            : `<a href="#" id="igntog" data-id="${a.id}" data-on="1" title="Exclude this activity from the fitness/fatigue reconstruction — for a duplicate or mis-tagged upload the auto-detector missed">⊘ ignore</a>`)+
          `<a href="#" id="delact" data-id="${a.id}" class="delact" title="Hard-remove this activity from your local copy — for one you ALREADY deleted on Runalyze (insert-only sync leaves the row behind). Still on Runalyze? It returns next sync — use ⊘ ignore instead.">🗑 delete</a></div>`}
      </div>
      <div class="mrow">
        <span class="ttl">${esc(a.sport||"Activity")}${a.title?` — ${esc(a.title)}`:""}${a.sj?` <span class="sjchip" title="This recording is part ${a.sj.index} of ${a.sj.parts} of a deliberately split session (${a.sj.km} km · ${a.sj.min} min total) — the read-back line below joins the parts. Numbers on this card are THIS part's own.">1+1 · part ${a.sj.index}/${a.sj.parts}</span>`:""}</span>
        ${m("When", when, "")}
        ${m("Distance", fmt(a.distance,2), "km")}
        ${m("Duration", durStr(a.duration), "")}
        ${m("Pace", paceStr(a.pace_min_km), "/km", "pace")}
        ${SH_READONLY?"":m("Avg HR", a.hr_avg||"—", "bpm", "hr")}
        ${SH_READONLY?"":m("Max HR", a.hr_max||"—", "bpm", "hr")}
        ${a.cadence?m("Cadence", a.cadence, "spm", "cadence"):""}
        ${m("TRIMP", a.trimp!=null?Math.round(a.trimp):"—", "")}
        ${a.elevation_up?m("Climb", a.elevation_up, "m", "elevation"):""}
      </div>
      <div class="structline" id="structline"></div>
      <div class="profbar">
        <span class="profhint muted">Background shades the locked trace · hover <b>Pace/HR/Cadence/Climb</b> to overlay it (colour = value), click to lock.</span>
        <span class="profmeta" id="profmeta"><span class="proflbl" id="proflbl"></span><span class="hrlegend" id="hrlegend"></span></span>
      </div>
    </div></div>
    ${SH_READONLY?"":'<div id="actmap" class="actmap"></div>'}` + crossNote;
  // load the profile once, show the default (locked) one, then wire hover-preview + click-lock
  if(a.id){
    // §RD — the detected-structure line fills in async (lazy classification can take a beat on a
    // first view of an old run); absence or an unreadable run just leaves the line empty.
    getJSON(`/api/activity/${a.id}/structure`).then(st=>{
      const el=$("#structline");
      if(!el || !st || !st.ok) return;
      el.innerHTML=`<span class="sdet" title="Detected from the recorded pace profile — grade-adjusted, segmented by sustained pace shifts, and named against your zones as of that day. What the app read back, not what was prescribed.">Read back:</span> `+
        (st.composite?`<span class="sjchip" title="A deliberately split recording (${st.n_parts} parts, saved minutes apart) read back as ONE session — each part analysed on its own clean streams, then joined.">1+1</span> `:"")+
        `<b>${esc(st.kind_label)}</b> — ${esc(st.summary)}`+
        (st.confidence==="rough"?` <span class="muted" title="The pace signal was noisy (GPS gaps or drift) — segment paces are approximate.">·rough signal</span>`:"")+
        (st.sq&&st.sq.line?`<div class="sqline"><span class="sdet" title="Strides execution — count vs the prescribed set band; HR shown as RESPONSE (a 15–20s stride's HR peak lags into the recovery), never as the effort verdict — pace is the trick.">Strides read:</span> ${esc(st.sq.line)}</div>`:"");
    }).catch(()=>{});
    try{ ACTPROFILE = await getJSON(`/api/activity/${a.id}/profile`); }
    catch(e){ ACTPROFILE={}; }
  }
  if(!ACTPROFILE || !ACTPROFILE.has_pace) LOCKED = (ACTPROFILE&&ACTPROFILE.has_hr)?"hr":(ACTPROFILE&&ACTPROFILE.has_cadence)?"cadence":(ACTPROFILE&&ACTPROFILE.has_elevation)?"elevation":"pace";
  // drop the hover affordance from any metric whose channel didn't come through, so the cursor never
  // promises a trace that isn't there (e.g. Climb on a run Runalyze couldn't elevation-correct).
  host.querySelectorAll(".metric.hovx").forEach(el=>{
    const k=el.dataset.prof, P=ACTPROFILE||{};
    const ok=(k==="pace"&&P.has_pace)||(k==="hr"&&P.has_hr)||(k==="cadence"&&P.has_cadence)||(k==="elevation"&&P.has_elevation);
    if(!ok){ el.classList.remove("hovx"); el.removeAttribute("data-prof"); }
  });
  renderProfile(null);
  host.querySelectorAll(".hovx").forEach(el=>{
    // UX-9: focusable exactly where it is clickable. The loop above strips .hovx from a metric this
    // activity has no data for, so binding the button affordance HERE — with the listeners, not in the
    // template — means the role can never outlive the behaviour it announces.
    el.setAttribute("role","button"); el.setAttribute("tabindex","0");
    el.addEventListener("mouseenter", ()=>renderProfile(el.dataset.prof));
    el.addEventListener("mouseleave", ()=>renderProfile(null));
    el.addEventListener("click", ()=>{ LOCKED=el.dataset.prof; renderProfile(null); });
  });
  const tog=$("#igntog");
  if(tog) tog.addEventListener("click", async ev=>{
    ev.preventDefault();
    await toggleIgnore(tog.dataset.id, tog.dataset.on==="1");
  });
  const del=$("#delact");
  if(del) del.addEventListener("click", async ev=>{
    ev.preventDefault();
    if(!await confirmDanger({
      title:"Delete this activity?",
      intro:"This hard-removes the activity from your local copy — only meant for one you've ALREADY deleted on Runalyze that the insert-only sync left behind.",
      lines:[
        "It's dropped from your fitness/fatigue reconstruction, weekly mileage and effort history.",
        "If it still exists on Runalyze it will reappear on the next sync — the delete won't stick.",
        "There's no local undo: if you deleted it on Runalyze too, it's gone (an accidental delete of a live activity recovers with a full backfill).",
      ],
      alt:"Just want to exclude a duplicate or mis-tag from the maths? Use ⊘ Ignore instead — that's reversible.",
      confirmLabel:"Delete locally"})) return;
    await fetch(`/api/activity/${del.dataset.id}/delete`,
      {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    CURACT=null;   // the row is gone → fall back to the latest activity
    await Promise.all([loadShape(), loadActivity(), loadProjector()]);
  });
  const bl=$("#backlatest");
  if(bl) bl.addEventListener("click", ev=>{ ev.preventDefault(); loadActivity(); });
  if(!SH_READONLY && a.id) showActivityMap(a.id);
}
// ── §RB Run browser (the /runs page, private only) ──────────────────────────
// A month calendar where each day with runs carries dots coloured by the run's avg-HR zone (the
// SAME unified grid as the chart band/zones card — the calendar doubles as an intensity map of the
// month). Click a day: one run loads straight into the activity tile below (the existing
// loadActivity pipeline — profile, hover traces, route map); a multi-run day opens a chip popover.
let RCAL={month:null, sel:null};
function rcalDot(z){ return `<span class="rcal-dot" style="${z!=null?`background:${HRZONE_COLORS[z]}`:""}"></span>`; }
function rcalPick(id, date, cell){
  RCAL.sel=date;
  document.querySelectorAll(".rcal-day.sel").forEach(c=>c.classList.remove("sel"));
  if(cell) cell.classList.add("sel");
  loadActivity(id);
  scrollToEl($("#sec-recent"));
}
function rcalCloseAllPops(){ document.querySelectorAll(".rcal-pop").forEach(p=>p.remove()); }
async function loadRunsCal(month){
  const host=$("#runscal"); if(!host) return;
  let d; try{ d=await getJSON("/api/runs"+(month?`?month=${encodeURIComponent(month)}`:"")); }
  catch(e){ host.innerHTML=`<div class="empty">Calendar unavailable.</div>`; return; }
  if(!d || !d.ok){ host.innerHTML=`<div class="empty">${esc((d&&d.error)||"Calendar unavailable.")}</div>`; return; }
  RCAL.month=d.month;
  const [y,m]=d.month.split("-").map(Number);
  const firstDow=(new Date(y,m-1,1).getDay()+6)%7;              // Monday-first grid
  const dim=new Date(y,m,0).getDate();
  const now=new Date(), pad=n=>String(n).padStart(2,"0");
  const todayIso=`${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`;
  const other=new Set(d.other||[]);
  const title=new Date(y,m-1,1).toLocaleDateString(undefined,{month:"long",year:"numeric"});
  const mAdd=k=>{ const t=new Date(y,m-1+k,1); return `${t.getFullYear()}-${pad(t.getMonth()+1)}`; };
  const dows=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].map(w=>`<div class="rcal-dow">${w}</div>`).join("");
  let cells="";
  for(let i=0;i<firstDow;i++) cells+=`<div class="rcal-day blank"></div>`;
  for(let day=1;day<=dim;day++){
    const date=`${d.month}-${pad(day)}`, runs=d.days[date]||[];
    const dots=runs.slice(0,3).map(r=>rcalDot(r.z)).join("")+(runs.length>3?`<span class="rcal-more">+${runs.length-3}</span>`:"")
      +(!runs.length&&other.has(date)?`<span class="rcal-tick" title="Activity, but not a run (counts toward load; the viewer below is run-centric)"></span>`:"");
    const cls=["rcal-day", runs.length?"has":"", date===todayIso?"today":"", date===RCAL.sel?"sel":""].filter(Boolean).join(" ");
    const tip=runs.length?` title="${runs.length===1?`${runs[0].km} km${runs[0].pace?` @ ${runs[0].pace}/km`:""}${runs[0].sj?` (1+1 split recording, ${runs[0].sj} parts)`:""} — view this run`:`${runs.length} runs — pick one`}"`:"";
    // UX-9: only a cell that opens something takes the role — a focus stop that does nothing is
    // worse than no focus stop, and blank/empty days are exactly that.
    const kb = runs.length
      ? ` role="button" tabindex="0" aria-label="${day}: ${runs.length===1?`${runs[0].km} km`:`${runs.length} runs`}"` : "";
    cells+=`<div class="${cls}" data-date="${date}"${tip}${kb}>${day}<div class="rcal-dots">${dots}</div></div>`;
  }
  const zl=["Z1","Z2","Z3","Z4","Z5"].map((z,i)=>`<span><span class="rcal-dot" style="background:${HRZONE_COLORS[i]}"></span>${z}</span>`).join("");
  // roll-up rail: three windows over the same non-dropped run set the calendar shows — the browsed
  // month, the trailing 12 calendar months ending with it (moves with the nav), and all time
  const cols=[d.stats||{}, d.stats12||{}, d.statsAll||{}];
  const since=d.since?d.since.split("-").reverse().join("/"):null;
  // numbers share one right edge: every cell carries a fixed-width unit slot (sized for TRIMP,
  // empty when unitless) and thousands get a narrow space so 121 130 reads at a glance
  const fmtN=v=>{const [i,f]=String(v).split("."); return i.replace(/\B(?=(\d{3})+(?!\d))/g," ")+(f?"."+f:"");};
  const c3=(k,f,u,tip)=>`<div class="rst3"${tip?` title="${tip}"`:""}><span>${k}</span>`+
    cols.map(s=>{const v=s[f]; return `<i>${v==null?"—":(typeof v==="number"?fmtN(v):v)}<small>${v!=null&&u?u:""}</small></i>`;}).join("")+`</div>`;
  const statsHtml =
    `<div class="rcal-stats"><div class="rst-title">Totals &amp; averages</div>`+
    `<div class="rst3 rst3-head"><span></span><i>Month<small></small></i><i title="The 12 calendar months ending with the browsed month">12 mo<small></small></i><i${since?` title="Since ${since}"`:""}>All time<small></small></i></div>`+
    c3("Runs","runs")+
    c3("Distance","km","km")+
    c3("Time on feet","hms")+
    c3("Avg pace","pace","/km")+
    c3("Avg HR","hr_avg","bpm","Duration-weighted across each window's runs — a long easy hour counts for more than a short blast")+
    c3("Load","trimp","TRIMP")+
    c3("Longest run","longest_km","km")+
    (since?`<div class="rst-since">all time = since ${since}</div>`:"")+
    `</div>`;
  host.innerHTML=`<div class="rcal-wrap"><div class="rcal">
      <div class="rcal-head">
        <button class="rcal-nav" id="rcprev" ${d.first&&d.month<=d.first?"disabled":""} aria-label="Previous month">‹</button>
        <b>${esc(title)}</b>
        <button class="rcal-nav" id="rcnext" ${d.last&&d.month>=d.last?"disabled":""} aria-label="Next month">›</button>
      </div>
      <div class="rcal-grid">${dows}${cells}</div>
      <div class="rcal-legend">${zl}<span><span class="rcal-dot"></span>no HR</span><span><span class="rcal-tick"></span>other sport</span>
        <span style="margin-left:auto">dot colour = the run's average-HR zone${qhint("Each dot is one run, coloured by which heart-rate zone its average HR falls in — the same unified zone grid the activity chart band and the zones card use (Friel %LTHR when a trustworthy LTHR exists, %HRmax otherwise). A glanceable intensity map: a healthy polarized month is mostly Z1/Z2 dots with the odd Z4. Grey = no heart-rate data. Faint square = a non-run activity (its load still counts; this viewer is run-centric).")}</span></div>
    </div>${statsHtml}</div>`;
  $("#rcprev").addEventListener("click", ()=>loadRunsCal(mAdd(-1)));
  $("#rcnext").addEventListener("click", ()=>loadRunsCal(mAdd(1)));
  host.querySelectorAll(".rcal-day.has").forEach(cell=>{
    cell.addEventListener("click", ev=>{
      if(ev.target.closest(".rcal-pop")) return;               // clicks inside an open popover: the chips handle it
      rcalCloseAllPops();
      const runs=d.days[cell.dataset.date]||[];
      if(runs.length===1){ rcalPick(runs[0].id, cell.dataset.date, cell); return; }
      const pop=document.createElement("div");
      pop.className="rcal-pop";
      pop.innerHTML=runs.map(r=>`<button type="button" class="rcal-chip" data-id="${r.id}">${rcalDot(r.z)}${esc(r.t||"—")} · ${r.km} km${r.pace?` · ${esc(r.pace)}/km`:""}${r.sj?` · 1+1`:""}</button>`).join("");
      pop.addEventListener("click", ev2=>{
        const c=ev2.target.closest(".rcal-chip"); if(!c) return;
        ev2.stopPropagation(); rcalCloseAllPops();
        rcalPick(+c.dataset.id, cell.dataset.date, cell);
      });
      cell.appendChild(pop);
      ev.stopPropagation();
    });
  });
}
document.addEventListener("click", e=>{ if(!e.target.closest(".rcal-day")) rcalCloseAllPops(); });

// ── Workout route map (private only) ────────────────────────────────────────
// Leaflet is loaded lazily from a CDN and ONLY on the private instance — the public read-only
// container never fetches it, and its /map endpoint 403s, because the routes reveal where the owner
// lives (the whole reason this feature is private). Falls back to a clean empty state with no GPS.
let LEAFLET_READY=null;
function ensureLeaflet(){
  if(window.L) return Promise.resolve();
  if(LEAFLET_READY) return LEAFLET_READY;
  LEAFLET_READY=new Promise((res,rej)=>{
    // Subresource Integrity: this runs in the PRIVATE instance (token + blood markers), so pin the
    // exact 1.9.4 bytes — a compromised/MITM'd CDN can't substitute code. Hashes are version-locked.
    const css=document.createElement("link");
    css.rel="stylesheet"; css.href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    css.integrity="sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H";
    css.crossOrigin="anonymous"; document.head.appendChild(css);
    const js=document.createElement("script");
    js.src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    js.integrity="sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH";
    js.crossOrigin="anonymous";
    js.onload=()=>res(); js.onerror=()=>rej(new Error("leaflet failed to load"));
    document.head.appendChild(js);
  });
  return LEAFLET_READY;
}
let ACTMAP=null;
async function showActivityMap(aid){
  const host=$("#actmap"); if(!host) return;
  let d=null; try{ d=await getJSON(`/api/activity/${aid}/map`); }catch(e){}
  if(ACTMAP){ ACTMAP.remove(); ACTMAP=null; }   // tear down a prior map before re-init
  if(!d || !d.has_gps || !(d.path&&d.path.length>1)){
    host.innerHTML=`<div class="mapempty">No route recorded for this activity.</div>`; return;
  }
  host.innerHTML="";
  try{ await ensureLeaflet(); }catch(e){ host.innerHTML=`<div class="mapempty">Map unavailable (offline?).</div>`; return; }
  const cs=getComputedStyle(document.documentElement);
  const accent=cs.getPropertyValue("--accent").trim()||"#b9542c";   // route line follows the theme
  const okc=cs.getPropertyValue("--ok").trim()||"#4f8c5f";          // start marker = good (semantic)
  const dangerc=cs.getPropertyValue("--danger").trim()||"#b5563f";  // end marker = stop (semantic)
  ACTMAP=L.map(host,{zoomControl:true, scrollWheelZoom:false});
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    {maxZoom:19, attribution:"© OpenStreetMap"}).addTo(ACTMAP);
  L.polyline(d.path,{color:accent, weight:4, opacity:.9}).addTo(ACTMAP);
  L.circleMarker(d.path[0],{radius:5, color:okc, weight:2, fillColor:okc, fillOpacity:1}).addTo(ACTMAP);
  L.circleMarker(d.path[d.path.length-1],{radius:5, color:dangerc, weight:2, fillColor:dangerc, fillOpacity:1}).addTo(ACTMAP);
  ACTMAP.fitBounds(d.bounds,{padding:[18,18]});
}

// Data-quality: exclude (or restore) an activity from the owned reconstruction, then
// refresh the dashboard so the shape tiles + projector reflect it.
async function toggleIgnore(id, on){
  if(SH_READONLY) return;
  await fetch(`/api/activity/${id}/${on?"ignore":"unignore"}`,
    {method:"POST", headers:{"Content-Type":"application/json"},
     body: JSON.stringify({reason:"manual"})});
  // keep the tile on whatever activity is displayed (CURACT) — not always the latest
  await Promise.all([loadShape(), loadActivity(CURACT), loadProjector()]);
}

// ── Readiness gate ──────────────────────────────────────────────────────────
// Phase label for the tile kicker — the plan is no longer re-base-only (an assertive road has no
// re-base at all), so the week tag names its actual phase (same caution-era family as the log fix).
const PHASEN={rebase:"re-base", base:"Base", build:"Build", peak:"Peak", taper:"Taper"};
const KINDT={easy:"Easy run", long:"Long run", long_mp:"Long run · MP finish",
             interval:"Interval session", tempo:"Tempo run"};
function plannedSession(s, easyPace){
  if(!s) return `<div class="planned muted" style="font-size:13px">${SH_READONLY
      ? "No active plan."   /* the public box has no Generate button to point at (UX-11) */
      : "No active plan — generate one below in Training plan."}</div>`;
  if(s.kind==="pre"){
    const d=new Date(s.start+"T00:00:00");
    const fmt=d.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"});
    return `<div class="planned"><div class="rkick">Training plan active</div>
      <div class="mrow"><span class="ttl">Training starts ${fmt}</span><span class="muted" style="font-size:13px">Keep it easy until then — readiness check-ins still count.</span></div></div>`;
  }
  if(s.kind==="post") return `<div class="planned"><div class="rkick">Today's session</div>
    <div class="mrow"><span class="ttl">Road complete</span><span class="muted" style="font-size:13px">Regenerate to periodize the next phase.</span></div></div>`;
  if(s.kind==="rest") return `<div class="planned"><div class="rkick">Today's session</div>
    <div class="mrow"><span class="ttl">${s.optional?"Optional — week complete":"Rest day"}</span><span class="muted" style="font-size:13px">${esc(s.note)}</span></div></div>`;
  const act=s.actual||{};
  const q=s.reps&&s.reps.length;
  const kick=`Today's session · ${PHASEN[s.pk]||"plan"} week ${s.week}`+
    (s.done?` · <span style="color:var(--ok);font-weight:600">done ✓</span>`:"");
  const actLine=s.done
    ? `<div style="font-size:12px;margin-top:6px;color:var(--ok)">✓ Ran ${act.km}k${act.pace?` @ ${act.pace}/km`:""} today — session complete.</div>`
    : "";
  // a structured session carries its own per-segment paces in the card — the single easy pace
  // metric would misprescribe the day, so it only renders for plain easy/long runs.
  const noteParts=(s.note||"").split(" — ");
  const noteTxt=(q&&noteParts.length>1)?noteParts.slice(0,-1).join(" — "):s.note;   // card replaces the cramped structure string
  return `<div class="planned"${s.done?' style="opacity:.85"':''}><div class="rkick">${kick}</div>
    <div class="mrow">
      <span class="ttl">${KINDT[s.kind]||esc(s.kind)+" run"}</span>
      ${metric("Distance", s.km, "km")}
      ${metric("Duration", `~${s.minutes}`, "min")}
      ${q?"":metric("Pace", (s.easy_pace||easyPace||"").replace("/km",""), "/km easy")}
      ${metric("Target load", s.trimp!=null?Math.round(s.trimp):"—", "TRIMP")}
    </div>
    ${actLine}
    <div class="muted" style="font-size:12px;margin-top:6px">${esc(noteTxt)}</div>${q?workoutCard(s,true):""}</div>`;
}
// the configured cities' forecast, folded into the readiness card footer (white on the gradient)
function wxFootHtml(){
  if(!WX||!WX.cities||!WX.cities.length) return "";
  return `<span class="sc-wx" title="Current conditions in the cities you've configured">`+
    WX.cities.map(c=>`<span class="wxc"><span class="wxk">${esc((c.key||"").toUpperCase())}</span> ${c.icon||""} ${c.temp==null?"–":c.temp+"°"}</span>`).join("")+
    `</span>`;
}
// §3 status card — "lead with the verdict": gradient panel (state-coloured), glass pill, big verdict.
// UX-4 — how old is the data behind this verdict? The readiness card was happy to render a confident
// green off a sync that failed two nights ago, and the only hint was a timestamp in the page footer
// nobody scrolls to. The chip reads `last_sync` from /healthz (which TECH-8 records) and turns amber
// past 26 h — the same staleness the nightly catch-up uses, so the number on screen and the number
// the scheduler acts on cannot disagree.
// PRIVATE ONLY. The public box deliberately answers /healthz with booleans and no timestamps, so a
// stranger cannot learn when the household syncs; printing an age here would hand it straight back.
let SYNC_LAST = storedSync();   // seeded from the stored stamp, so an offline shell still dates itself
function paintFreshness(){
  const el = $("#scFresh");
  if(!el) return;
  if(SH_READONLY || !SYNC_LAST){ el.textContent=""; el.classList.remove("stale"); return; }
  const then = new Date(SYNC_LAST), hrs = (Date.now() - then.getTime())/36e5;
  if(!isFinite(hrs)){ el.textContent=""; return; }
  const age = hrs < 1 ? `${Math.max(1, Math.round(hrs*60))} min ago`
            : hrs < 48 ? `${Math.round(hrs)} h ago`
            : `${Math.round(hrs/24)} days ago`;
  el.textContent = `synced ${age}`;
  el.classList.toggle("stale", hrs > 26);
  el.title = `Last successful sync: ${then.toLocaleString()}`;
}
function statusCard(a, foot, wx){
  const v=a.verdict||"green";
  const orbs=`<span class="sc-orb" style="top:-50px;right:-40px;width:180px;height:180px"></span>`+
             `<span class="sc-orb" style="bottom:-60px;left:-30px;width:150px;height:150px;background:color-mix(in oklab,var(--onready),transparent 95%)"></span>`;
  return `<div class="statuscard ${v}">${orbs}
      <div class="sc-top">
        <span class="sc-eyebrow">Today's readiness</span>
        <span class="sc-pill"><span class="dot"></span>${v}${a.halt?" · halted":""}</span>
      </div>
      <div class="sc-verdict">${esc(a.action||v)}</div>
      ${a.halt?`<div class="halt">⚠ Plan halted — clear it with your doctor before resuming.</div>`:""}
      ${(foot||wx||(!SH_READONLY&&SYNC_LAST))?`<div class="sc-foot">${foot?`<span>${esc(foot)}</span>`:""}<span class="sc-fresh" id="scFresh"></span>${wx||""}</div>`:""}
    </div>`;
}
function renderReadiness(d){
  RDY=d;
  setTimeout(paintFreshness, 0);            // the chip outlives each re-render of the card                                   // cache so loadWeather can re-render with the forecast folded in
  if(d.zones) ZONESD=d.zones;              // §W1 — current zones ride along (private), so the card never races the zones fetch
  const a=d.assessment||{};
  if(SH_READONLY || a.public){   // public view: verdict card + planned session only
    $("#readiness").innerHTML = statusCard(a, "", wxFootHtml()) + plannedSession(d.session);
    return;
  }
  const c=d.checkin||{};
  const hrv=a.hrv||{};
  const hrvTxt = hrv.state==null ? "HRV: no data"
    : `HRV ${hrv.baseline} vs ${hrv.band[0]}–${hrv.band[1]} — ${hrv.state}`;
  const sel=(name,val,opts)=>`<label>${name}</label><select id="ci_${name}">`+
    opts.map(([v,t])=>`<option value="${v}" ${v===val?"selected":""}>${t}</option>`).join("")+`</select>`;
  let aiLine="";
  if(a.source && a.source.startsWith("llm"))
    aiLine=`🩺 AI judgment${a.engine_floor&&a.engine_floor!==a.verdict?` · engine floor was ${a.engine_floor}`:""}`;
  else if(a.source && a.source.startsWith("engine ("))
    aiLine=`engine floor held — AI suggested ${a.ai_verdict}`;
  const notePh = LLM_OK ? "Anything else? e.g. “slight cold coming on”, “legs heavy but slept great” — the AI reads this"
                        : "Note (optional)";
  const foot=[(a.reasons||[]).join(" · "), hrvTxt, aiLine].filter(Boolean).join("  ·  ");
  $("#readiness").innerHTML=
    statusCard(a, foot, wxFootHtml()) +
    plannedSession(d.session) +
    `<div class="checkin">
      ${/* §H7 — an unset dropdown reads "—", never a pre-selected "ok". Showing "Legs: ok" on a day
            with no check-in made the form look answered, and saving it then wrote a declaration the
            athlete never made. The blank posts as absent, and the card says so. */""}
      ${sel("energy", c.energy||"", [["","Legs: —"],["good","Legs: fresh"],["ok","Legs: ok"],["heavy","Legs: heavy"]])}
      ${sel("sleep", c.sleep||"", [["","Slept: —"],["good","Slept: well"],["ok","Slept: ok"],["poor","Slept: poorly"]])}
      <input id="ci_note" class="cinote" placeholder="${notePh}" value="${esc(c.note)}">
      <label class="stop" title="Stop-the-run exertional symptom (chest pain, breathlessness, dizziness, fainting) — halts the plan"><input type="checkbox" id="ci_stop" ${c.stop_symptom?"checked":""}> I had to stop / chest symptom</label>
      <button class="primary" id="ciBtn" style="font-size:13px;padding:7px 12px">Save check-in</button>
    </div>`;
  $("#ciBtn").addEventListener("click", async ()=>{
    const btn=$("#ciBtn");
    const body={energy:$("#ci_energy").value, sleep:$("#ci_sleep").value,
      note:$("#ci_note").value, stop_symptom:$("#ci_stop").checked};
    btn.disabled=true; btn.classList.remove("err"); btn.textContent="Saving…";
    try{
      const r=await fetch("/api/readiness",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const d=await r.json().catch(()=>null);
      if(!r.ok || !d || !d.assessment) throw new Error((d&&d.error)||("http "+r.status));
      renderReadiness(d);
    }catch(e){
      btn.disabled=false; btn.textContent="Couldn't save — retry"; btn.classList.add("err");
    }
  });
}
async function loadReadiness(){
  try{ renderReadiness(await getJSON("/api/readiness")); }
  catch(e){ tileFail($("#readiness"), "Readiness", loadReadiness, e); }
}
// ── Durability tracker (§3.3, 0.32.0) ───────────────────────────────────────
// Long-run aerobic decoupling (the pace:HR drift, first→second half) as the resilience proxy,
// displayed MEASURE-FIRST: tracked + trended, never governing. PRIVATE-only (HR-adjacent) — the
// public shell removes the card. The §55d caveats shape the chart: every bar carries its distance
// in the tooltip (on his corpus longer runs decouple LESS — duration must stay visible), and the
// engine itself voids the trend when the recent-vs-prior distance mix shifts.
async function loadEfficiency(){
  const body=$("#effbody"); if(!body) return;              // the card is removed on the public box
  try{ renderEfficiency(await getJSON("/api/efficiency")); }
  catch(e){ tileFail(body, "Efficiency", loadEfficiency, e); }
}
// §EF — TWO panels, one shared time axis, each with its OWN vertical scale, drawn one above the
// other and never overlaid. A twin-axis overlay is the classic way to make two unrelated series
// look like they explain each other, and that is exactly the open question here: heat depresses
// efficiency, and his cool runs are also his most recent and fittest. Stacked panels let the eye
// compare them without the chart having asserted an answer. Both SVGs stretch and NEITHER letters
// itself — every value is HTML around the plot (UX-8, det/axis-legibility).
function effPlot(pts, key, cls, fit){
  if(!pts.length) return "";
  const W=1000,H=cls==="ef"?132:56,padT=6,padB=6;
  const t=d=>Date.parse(d+"T12:00:00Z");
  const t0=t(pts[0].date), t1=t(pts[pts.length-1].date), span=Math.max(1,t1-t0);
  const vals=pts.map(p=>p[key]).filter(v=>v!=null);
  if(!vals.length) return "";
  let lo=Math.min(...vals), hi=Math.max(...vals);
  const padv=(hi-lo)*0.12||1; lo-=padv; hi+=padv;
  const x=d=>4+(W-8)*(t(d)-t0)/span;
  const y=v=>padT+(H-padT-padB)*(1-(v-lo)/(hi-lo||1));
  // ⚠ NOT <circle>: this SVG stretches (preserveAspectRatio="none"), and a circle stretches with it
  // — on a 390px phone the same markup that looked round at 1280px drew tall ellipses. A ZERO-LENGTH
  // line with a round cap and a non-scaling stroke draws a true circle whose diameter is the stroke
  // width in device pixels, so the marks stay round at every width. Same family of defect as UX-8's
  // squeezed <text>, one layer down: the stretch scales marks as well as glyphs.
  const r=cls==="ef"?7:5;
  const dots=pts.filter(p=>p[key]!=null).map(p=>{
    const tip=cls==="ef"
      ? `${p.date} · ${p.ef} · ${p.km} km @ ${p.pace}/km · HR ${p.hr}`
      : `${p.date} · ${p.temp_c}°C`;
    const cx=x(p.date).toFixed(1), cy=y(p[key]).toFixed(1);
    return `<line class="dot" x1="${cx}" y1="${cy}" x2="${cx}" y2="${cy}" stroke-width="${r}"><title>${esc(tip)}</title></line>`;
  }).join("");
  // the fitted line is drawn ONLY on the efficiency panel: it is the claim the chart makes, and the
  // temperature panel is deliberately claim-free — it is context, not a trend we are asserting.
  let line="";
  if(cls==="ef" && fit && fit.per_30d!=null){
    const days=(t1-t0)/86400000, mid=vals.reduce((a,b)=>a+b,0)/vals.length;
    const slope=fit.per_30d/30, y0=mid-slope*days/2, y1=mid+slope*days/2;
    line=`<line class="fit" x1="${x(pts[0].date).toFixed(1)}" y1="${y(y0).toFixed(1)}" x2="${x(pts[pts.length-1].date).toFixed(1)}" y2="${y(y1).toFixed(1)}"/>`;
  }
  const u=cls==="temp"?"°C":"", dp=cls==="ef"?2:0;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line class="grid" x1="0" x2="${W}" y1="${(H-1).toFixed(1)}" y2="${(H-1).toFixed(1)}"/>${line}${dots}</svg>`
       + `<span class="axhi">${hi.toFixed(dp)}${u}</span><span class="axlo">${lo.toFixed(dp)}${u}</span>`;
}
function renderEfficiency(d){
  const host=$("#effbody"); if(!host) return;
  if(!d || d.ok===false){
    host.innerHTML=`<div class="empty">${esc((d&&d.reason)||"No efficiency read yet")} — the chart starts once four aerobic runs land with heart-rate data.</div>`;
    return;
  }
  const pts=d.series||[], first=pts[0], last=pts[pts.length-1];
  const tr=d.trend||{};
  // the fitted change across the drawn window — NOT last-minus-first, which reports whichever
  // single run happens to sit at either end (the chart draws this same line, so they agree)
  const days=(first&&last)?(Date.parse(last.date)-Date.parse(first.date))/86400000:0;
  const gain=(tr.pct_per_30d!=null&&days>0)?tr.pct_per_30d*days/30:null;
  const vcls=d.verdict==="improving"?"ok":d.verdict==="declining"?"danger":"warn";
  const dates=pts.length>1?`${monYr(first.date)}–${monYr(last.date)}`:"";
  const tempTxt=(d.temp&&d.temp.r_with_ef!=null)
    ? `r ${d.temp.r_with_ef>0?"+":""}${d.temp.r_with_ef} with efficiency, ${d.temp.lo}–${d.temp.hi}°C`
    : "temperature not recorded on these runs";
  host.innerHTML=`
    <p class="effhero"><b class="${vcls}">${esc(d.verdict)}</b>${gain!=null?`<span class="d">${gain>0?"+":""}${gain.toFixed(1)}% over ${esc(dates)}</span>`:""}</p>
    <p class="muted" style="font-size:11px;margin:6px 0 0;line-height:1.5">Metres per minute per heartbeat, one point per aerobic run
      (average HR at or below the top of Z2${d.z2_top?`, ${d.z2_top} bpm`:""}) of ${Math.round(d.min_km)} km or more. Running the same pace at a lower
      heart rate <em>is</em> the adaptation — no model in between, just the watch. Measure-first: shown and trended, governing nothing.</p>
    <div class="effrow"><span class="k">Efficiency · ${d.n} runs</span><span class="k">${tr.pct_per_30d!=null?`${tr.pct_per_30d>0?"+":""}${tr.pct_per_30d}%/month${tr.r!=null?` · r ${tr.r}`:""}`:""}</span></div>
    <div class="effplot ef">${effPlot(pts,"ef","ef",tr)}</div>
    <div class="effrow"><span class="k">Temperature</span><span class="k">${esc(tempTxt)}</span></div>
    <div class="effplot temp">${effPlot(pts,"temp_c","temp",null)}</div>
    <div class="effax" style="margin-top:5px"><span>${esc(first?monYr(first.date):"")}</span><span>${esc(last?monYr(last.date):"")}</span></div>
    <p class="muted" style="font-size:11px;margin:12px 0 0;line-height:1.5"><b>Read the two together.</b> Heat costs you efficiency, and
      your cool runs are also your most recent and fittest — so the gain above is flattered by whatever the weather did. The panels are
      separate and share only their time axis on purpose: nothing here subtracts a temperature correction, because how big that correction
      should be is not settled on your data yet. Hover any point for its pace, distance and heart rate.</p>`;
}
async function loadDurability(){
  const body=$("#durbody"); if(!body) return;              // the card is removed on the public box
  try{ renderDurability(await getJSON("/api/durability")); }
  catch(e){ tileFail(body, "Durability", loadDurability, e); }
}
// one bar per long run, height = decoupling %, coloured by its fade band; the dashed lines are the
// engine's own thresholds. preserveAspectRatio="none" stretches the chart to the tile — fine for
// bars, and the threshold dashes carry vector-effect:non-scaling-stroke so they stay hairlines.
function durChartSvg(series, goodPct, highPct){
  const pts=[...series].reverse();                 // chronological, oldest → newest
  if(!pts.length) return "";
  const W=1000,H=96,pad=4;
  const vmax=Math.max(15, ...pts.map(p=>p.decoupling_pct))*1.08;
  const y=v=>H-pad-(H-2*pad)*Math.min(v,vmax)/vmax;
  const slot=(W-2*pad)/pts.length, bw=Math.max(2,slot*0.52);
  const band=v=>v<goodPct?"ok":v<highPct?"warn":"danger";
  const bars=pts.map((p,i)=>{
    const x=pad+slot*i+(slot-bw)/2, top=y(p.decoupling_pct);
    return `<rect class="${band(p.decoupling_pct)}" x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${(H-pad-top).toFixed(1)}">`+
      `<title>${esc(p.date)} · ${p.km} km · ${p.decoupling_pct}%</title></rect>`;
  }).join("");
  const dash=(v,cls)=>vmax>v?`<line class="thresh ${cls}" x1="${pad}" x2="${W-pad}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>`:"";
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${dash(goodPct,"ok")}${dash(highPct,"danger")}${bars}</svg>`;
}
function renderDurability(d){
  const host=$("#durbody"); if(!host) return;
  if(!d || d.ok===false){
    host.innerHTML=`<div class="empty">No long runs with decoupling yet — the tracker starts once a run of ${d&&d.min_km?Math.round(d.min_km):16}&nbsp;km or more lands with HR data.</div>`;
    return;
  }
  const SCALE=15;                                    // % full-scale — gauge, scale and chart agree (the one-definition rule)
  const good=d.good_below_raw/100, high=d.high_above_raw/100, med=d.recent_median_pct;
  const pct=x=>Math.max(0,Math.min(100, x/SCALE*100));
  const pillCls=d.verdict==="durable"?"inb":d.verdict==="high fade"?"out":"mid";
  const wordCls=d.verdict==="durable"?"ok":d.verdict==="high fade"?"danger":"warn";
  const pts=d.series||[];
  const vTrend=d.trend==null?"collecting":d.trend.startsWith("distance mix")?"mix shifted":d.trend;
  const trendCap={improving:"economy decay shrinking across your recent long runs — getting more durable",
    steady:"holding steady across your recent long runs",
    declining:"economy decay growing across your recent long runs — watch the fade",
    collecting:"the trend reads once about twelve long runs are banked (recent six vs prior six)",
    "mix shifted":"your recent long-run distances changed, so the trend is unreliable — compare like distances"}[vTrend]||"";
  const datesTxt=pts.length>1?` · ${monYr(pts[pts.length-1].date)}–${monYr(pts[0].date)}`:"";
  const priorTxt=d.prior_median_raw==null?"":`; prior 6-run median ${(d.prior_median_raw/100).toFixed(1)}%`;
  host.innerHTML=`
    <div class="gauge"><div class="band" style="left:0;width:${pct(good)}%"></div>
      <div class="band danger" style="left:${pct(high)}%;width:${100-pct(high)}%"></div>
      <div class="mark" style="left:${pct(med)}%"></div>
      <div class="gyou" style="--gx:${pct(med)}%"><span class="${pillCls}">you: ${med.toFixed(1)}%</span></div></div>
    <div class="gauge-scale"><span>0%</span><span><b>${good}%</b></span><span><b>${high}%</b></span><span>${SCALE}%</span></div>
    <p class="durverdict"><b class="${wordCls}">${esc(d.verdict)}</b> — median of your last 6 long runs
      (≥${Math.round(d.min_km)} km)${esc(priorTxt)} · ${d.n_long} tracked</p>
    <p class="muted" style="font-size:11px;margin:10px 0 0;line-height:1.5">Aerobic decoupling — how much your
      pace:HR drifts from the first half of a long run to the second. Low means your running economy holds up over
      the distance; high or rising means it decays. Measure-first: tracked and trended, governing nothing. Compare
      like distances — decoupling drifts with distance, so the trend voids itself when the long-run mix changes.</p>
    <div class="acwr-foot">
      <span class="k">Long-run tracker · ${pts.length} runs ≥${Math.round(d.min_km)} km${datesTxt}</span>
      <div class="durchart">${durChartSvg(pts,good,high)}</div>
      <span class="v">${esc(vTrend)}</span>
      <span class="cap">${esc(trendCap)} · dashes at ${good}% / ${high}% · hover a bar for its distance</span>
    </div>`;
}
// Opportunistic page-load sync (private only): pull any activities synced to Runalyze since the
// last sync — throttled server-side — so a run finished earlier today lands without the manual
// button, and the readiness tile flips to "done ✓". Silent + non-blocking. A real (non-skipped)
// sync always re-reads readiness: the HRV snapshot can move with zero new activities (the morning
// pull after the watch uploads last night's reading), and the tile must not keep a verdict computed
// from pre-sync data. The full page refresh still fires only when an activity actually arrived,
// so a quiet load never flickers the heavier tiles.
async function touchSync(){
  try{
    const d = await getJSON("/api/sync?auto=1",{method:"POST"});
    if(!d || !d.ok || d.skipped) return;
    loadReadiness();
    if(d.activities && d.activities.added>0){
      loadPlan(); loadShape(); loadRecent(); loadProjector(); loadWeekly(); loadEffort(); loadZones(); loadDurability(); loadEfficiency();
    }
  }catch(e){ /* offline / token issue — the tile just keeps its last-known state */ }
}

// ── Training plan ───────────────────────────────────────────────────────────
function acBadge(a){
  if(a==null) return "";
  const cls = a<=1.18 ? "lo" : "mid";
  return `<span class="acbadge ${cls}">ACWR →${a.toFixed(2)}</span>`+
    qhint("Projected acute:chronic load at this week's end — rolled forward from today's fitness if you run the plan. The engine plans every week under the 1.30 ceiling and trims volume when a week would breach it; a taper or down week reads low on purpose.");
}
// House-styled confirmation for destructive actions — spells out the consequences before anything is
// removed. opts: {title, intro, lines:[…consequences], alt, confirmLabel}. Returns a Promise<bool>
// (true = proceed). Falls back to a native confirm() if the dialog element isn't present.
function confirmDanger(opts){
  const o = opts||{};
  return new Promise(resolve=>{
    const dlg=$("#confirmDialog");
    if(!dlg || typeof dlg.showModal!=="function"){
      resolve(confirm([o.intro, ...(o.lines||[]), o.alt].filter(Boolean).join("\n\n"))); return;
    }
    if(dlg.open){ resolve(false); return; }   // re-entrant guard — never stack/showModal-throw while one is up
    $("#cfTitle").textContent = o.title || "Are you sure?";
    $("#cfIntro").textContent = o.intro || "";
    $("#cfList").innerHTML = (o.lines||[]).map(l=>`<li>${esc(l)}</li>`).join("");
    $("#cfAlt").textContent = o.alt || "";
    const ok=$("#cfOk"); ok.textContent = o.confirmLabel || "Delete";
    ok.classList.add("danger"); $("#cfCancel").hidden = false;   // undo any notice() that ran before
    let done=false;
    const finish=v=>{ if(done) return; done=true; try{ dlg.close(); }catch(e){} resolve(v); };
    ok.onclick=()=>finish(true);
    $("#cfCancel").onclick=()=>finish(false);
    dlg.onclick=e=>{ if(e.target===dlg) finish(false); };           // backdrop click ⇒ cancel (onclick = no accrual)
    dlg.addEventListener("close", ()=>finish(false), {once:true});   // Esc ⇒ cancel
    dlg.showModal();
    $("#cfCancel").focus();   // default focus on the safe action, not Delete
  });
}
// A house NOTICE — the same dialog, one button, no Cancel. This replaces window.alert(), which is
// modal to the whole browser, unstyled, unthemed, unreadable on a phone (where it reads like a
// browser error rather than something this app is telling you) and impossible to assert on. The
// native call survives only as the no-<dialog> fallback, same as confirmDanger's.
function notice(opts){
  const o = opts||{};
  return new Promise(resolve=>{
    const dlg=$("#confirmDialog");
    if(!dlg || typeof dlg.showModal!=="function"){
      alert([o.title, o.intro, ...(o.lines||[])].filter(Boolean).join("\n\n")); resolve(); return;
    }
    if(dlg.open){ resolve(); return; }            // never stack a second modal on an open one
    $("#cfTitle").textContent = o.title || "Heads up";
    $("#cfIntro").textContent = o.intro || "";
    $("#cfList").innerHTML = (o.lines||[]).map(l=>`<li>${esc(l)}</li>`).join("");
    $("#cfAlt").textContent = o.alt || "";
    const ok=$("#cfOk"), cancel=$("#cfCancel");
    cancel.hidden = true;                          // a notice has nothing to cancel
    ok.textContent = o.confirmLabel || "OK";
    ok.classList.toggle("danger", o.tone === "danger");
    let done=false;
    // Put the shared dialog back the way confirmDanger expects to find it, whichever way this closes.
    const finish=()=>{ if(done) return; done=true; cancel.hidden=false; ok.classList.add("danger");
      try{ dlg.close(); }catch(e){} resolve(); };
    ok.onclick=finish;
    dlg.onclick=e=>{ if(e.target===dlg) finish(); };
    dlg.addEventListener("close", finish, {once:true});
    dlg.showModal();
    ok.focus();
  });
}
// Reusable click-to-open help bubble (Runalyze-style "?"). Delegated, so it works on dynamic content;
// positioned as a fixed bubble so an overflow:hidden ancestor can't clip it.
function qhint(text){
  return `<span class="qhint" tabindex="0" role="button" aria-label="Explanation">?<span class="qtip" role="tooltip">${esc(text)}</span></span>`;
}
document.addEventListener("click", e=>{
  if(e.target.closest(".qtip")) return;                       // clicks inside the bubble: leave it open
  const h = e.target.closest(".qhint");
  document.querySelectorAll(".qhint.open").forEach(n=>{ if(n!==h) n.classList.remove("open"); });
  if(!h) return;
  e.stopPropagation(); e.preventDefault();                    // don't let the week row's click fire
  const opening = !h.classList.contains("open");
  h.classList.toggle("open");
  if(opening){
    const t=h.querySelector(".qtip"), r=h.getBoundingClientRect(), w=Math.min(240, window.innerWidth-16);
    t.style.width=w+"px"; t.style.top=(r.bottom+6)+"px";
    t.style.left=Math.max(8, Math.min(r.right-w, window.innerWidth-w-8))+"px";
  }
});
// UX-9 — Enter/Space activate anything the app has dressed as a button. Driven off the ARIA itself
// rather than a list of class names: a <div role="button" tabindex="0"> that no key reaches is a
// worse lie than a plain div, because it tells a screen reader "button" and then ignores the only way
// that reader's user can press it. Every element that takes the role takes the keys, with nothing to
// remember and nothing to re-bind — which matters here, where most of them re-render constantly.
document.addEventListener("keydown", e=>{
  if(e.key==="Escape"){ document.querySelectorAll(".qhint.open").forEach(n=>n.classList.remove("open")); return; }
  if(e.key!=="Enter" && e.key!==" ") return;
  const t = e.target.closest && e.target.closest('[role="button"][tabindex="0"]');
  if(!t) return;
  e.preventDefault();      // Space would otherwise scroll the page out from under the press
  t.click();
});
// §W1 — toggle a quality session's instruction card (delegated: week rows re-render). Cards ride
// only NOT-done lines, which never carry the click-to-map affordance, so the two can't collide.
document.addEventListener("click", e=>{
  const t=e.target.closest(".wsx"); if(!t) return;
  const sl=t.closest(".sline"), c=sl&&sl.querySelector(".wcard");
  if(!c) return;
  e.stopPropagation();
  t.classList.toggle("open", c.classList.toggle("open"));
});
let OBJECTIVES=[], LASTDIFF=null, LLM_OK=false, LOG=null, WX=null, RDY=null,
    ZONESD=null;   // §W1 — current training_zones payload (rides /api/readiness + /api/zones); null on public
let TOKEN_OK=false, HAS_SHAPE=false;   // first-run signals (from /healthz + /api/shape)
const _frSeen={tok:false, shape:false, obj:false};   // gate the card until all 3 report once
// First-run guided setup: walks a brand-new instance from nothing to a first plan. Three
// signals decide the active step — token connected, history pulled, a race added — and the
// card removes itself once all three are satisfied (so a configured instance never sees it).
// Private only: the #firstrun host is stripped on the public read-only view.
function refreshFirstRun(){
  const host=$("#firstrun"); if(!host) return;
  // Wait until token, shape AND objectives have each reported once — otherwise the card flashes a
  // wrong/early step on a configured instance as the three async boot signals resolve out of order.
  if(!(_frSeen.tok && _frSeen.shape && _frSeen.obj)) return;
  // §RL — a resolved (done/lapsed) race still counts as "has added a race": resolution must not
  // resurrect the first-run card on a veteran instance. Only 'removed' means the user took it back.
  const hasObj = OBJECTIVES.some(o=>['upcoming','done','lapsed'].includes(o.status));
  const steps=[
    {label:"Connect Runalyze", done: TOKEN_OK || HAS_SHAPE,
     // the token is a secret, but it's now settable in the private Settings window (stored off the
     // public box, applied live) — so this step opens Settings instead of asking for a file edit
     desc:"Add your Runalyze API token in Settings — it's stored privately and takes effect right away, "+
          "no restart needed.", act:{id:"fr_keys", text:"Open Settings"}},
    {label:"Pull your training history", done: HAS_SHAPE,
     desc:"Fetch your activities from Runalyze to build your current shape.",
     act:{id:"fr_sync", text:"Sync now"}},
    {label:"Add your first race", done: hasObj,
     desc:"Tell the engine what you are training for, and it builds the plan around it.",
     act:{id:"fr_race", text:"Add a race"}},
  ];
  if(steps.every(s=>s.done)){ host.innerHTML=""; return; }   // configured → vanish
  const active=steps.findIndex(s=>!s.done);
  const li=steps.map((s,i)=>{
    const state=s.done?"done":(i===active?"active":"todo");
    const body=(i===active)
      ? `<div class="fr-desc">${s.desc}</div>`+(s.act?`<button class="primary fr-act" id="${s.act.id}">${s.act.text}</button>`:"")
      : "";
    return `<li class="fr-step ${state}"><span class="fr-num" aria-hidden="true">${s.done?"✓":(i+1)}</span>`+
      `<div><div class="fr-label">${s.label}</div>${body}</div></li>`;
  }).join("");
  host.innerHTML=`<div class="firstrun"><div class="fr-head"><b>Welcome to Sparing Horse.</b> `+
    `Three steps to your first plan.</div><ol class="fr-steps">${li}</ol></div>`;
  const kb=$("#fr_keys"); if(kb) kb.addEventListener("click", ()=>{ const dlg=$("#settingsDialog"); if(dlg){ if(!$("#setform")) loadSettings(); loadSecrets(true); dlg.showModal(); } });
  const sb=$("#fr_sync"); if(sb) sb.addEventListener("click", ()=>$("#syncBtn")&&$("#syncBtn").click());
  const rb=$("#fr_race"); if(rb) rb.addEventListener("click", async ()=>{
    scrollToEl(document.getElementById("plan"));
    // The race form lives inside #plan's objManager, which only renders once a plan exists. On a
    // fresh instance (history pulled, no plan yet) generate one first so the form is there to focus.
    let inp=$("#ao_label");
    if(!inp){ const gen=$("#planBtn"); if(gen) gen.click();
      for(let i=0; i<20 && !inp; i++){ await new Promise(r=>setTimeout(r,150)); inp=$("#ao_label"); } }
    if(inp){ scrollToEl(inp, "center"); inp.focus(); }
  });
}
function objManager(p){
  // Priority chip: static on the public view; an inline A|B|C selector on the private console
  // (clicking a letter POSTs /priority and re-periodizes — same path as the adjudication "Set B").
  const priBadge = o => SH_READONLY
    ? `<span class="pr ${o.priority}">${o.priority}</span>`
    : `<span class="prsel" role="group" aria-label="priority for ${esc(o.label)}">${['A','B','C'].map(x=>
        `<button type="button" class="prseg ${x===o.priority?'on':''}" data-oid="${o.id}" data-pri="${x}" title="Set priority ${x}">${x}</button>`).join("")}</span>`;
  const rows = OBJECTIVES.filter(o=>o.status==='upcoming').map(o=>{
    const isAnchor = p.objective && o.label===p.objective.label && o.date===p.objective.date;
    return `<div class="obj ${isAnchor?'anchor':''}">
      ${priBadge(o)}
      <span>${esc(o.label)}${isAnchor?' <span class="muted mono" style="font-size:10px">· anchor</span>':''}</span>
      <span class="od">${esc(o.date)} · ${esc(o.type)} · ${esc(o.target)}</span>
      <button class="x" data-oid="${o.id}">remove</button>
    </div>`;}).join("") || `<div class="muted" style="font-size:13px">No objectives — maintenance mode.</div>`;
  if(SH_READONLY) return `<div class="objs">${rows}</div>`;   // public: list only, no controls
  // §RL — past races (private-only: results are personal, same posture as the §6s reckoning).
  const pastChip = o => {
    let oc = {}; try{ oc = JSON.parse(o.outcome||"{}"); }catch(e){}
    if(o.status==='lapsed') return `<span class="muted">lapsed — no race-day run</span>`;
    if(oc.status==='dnf') return `<span>DNF at ${oc.dnf_km} km</span>`;
    if(oc.status==='finished'){
      const vs = oc.beat===true ? ' · beat the goal' : oc.beat===false ? ` · goal ${esc(oc.goal)} missed` : '';
      return `<span>✓ ${esc(oc.actual||'finished')}${vs}</span>`;
    }
    return `<span class="muted">run — result unverified</span>`;
  };
  const past = OBJECTIVES.filter(o=>o.status==='done'||o.status==='lapsed')
    .sort((a,b)=>b.date.localeCompare(a.date)).slice(0,5).map(o=>
      `<div class="obj past"><span class="pr ${o.priority}">${o.priority}</span>
        <span>${esc(o.label)}</span>
        <span class="od">${esc(o.date)} · ${esc(o.type)} · ${pastChip(o)}</span></div>`).join("");
  const pastRow = past ? `<div class="muted" style="font-size:11px;margin-top:8px">Past races</div>
    <div class="objs">${past}</div>` : "";
  const aCount = OBJECTIVES.filter(o=>o.status==='upcoming' && o.priority==='A').length;
  const conflictRow = aCount>=2 ? `
    <div class="conflictrow">
      <button id="adjBtn" ${LLM_OK?'':'disabled'}>⚖ ${aCount} A-races compete — get advice${LLM_OK?'':' (add a Claude API key in Settings)'}</button>
      <div id="objAdjudicate"></div>
    </div>` : "";
  const nlRow = `
    <div class="nlobj">
      <input id="ao_nl" ${LLM_OK?'':'disabled'} placeholder="Describe a goal — e.g. &quot;sub-45 10k in October&quot;, &quot;spring marathon, want to BQ&quot;" style="flex:1">
      <button id="ao_parse" ${LLM_OK?'':'disabled'} style="font-size:12px;padding:6px 11px">✨ Parse</button>
      <span id="ao_interp" class="nlinterp${LLM_OK?'':' guess'}">${LLM_OK?'':'⚙ Add a Claude API key in Settings to enable AI parsing'}</span>
    </div>`;
  return `<div class="objs">${rows}</div>
    ${conflictRow}
    ${nlRow}
    <div class="addobj">
      <input id="ao_label" placeholder="race name" style="width:130px">
      <select id="ao_type"><option>5k</option><option>10k</option><option>half</option><option selected>marathon</option><option>custom</option></select>
      <input id="ao_date" type="date">
      <select id="ao_pri"><option value="A">A</option><option value="B">B</option><option value="C">C</option></select>
      <input id="ao_target" placeholder="goal (finish / 3:55)" style="width:120px">
      <button class="primary" id="ao_add" style="font-size:12px;padding:6px 11px">Add objective</button>
    </div>
    ${pastRow}`;
}
function diffBanner(diff){
  if(!diff || diff.first) return "";
  return `<div class="diff"><div class="dh">Re-planned — ${esc(diff.summary)}</div>
    <ul>${(diff.changes||[]).map(c=>`<li>${esc(c)}</li>`).join("")}</ul></div>`;
}
function renderAdjudicate(d){
  const host=$("#objAdjudicate"); if(!host) return;
  if(!d.ok){ host.innerHTML=`<div class="adjclamp err">⚠ ${esc(d.error||'could not adjudicate')}</div>`; return; }
  const recs=(d.recommendations||[]).map(r=>{
    const cur=OBJECTIVES.find(o=>o.id===r.id);
    const changed=cur && cur.priority!==r.suggested_priority;
    return `<li><b>${esc(r.label)}</b> → <span class="pr ${r.suggested_priority}">${r.suggested_priority}</span>${r.id===d.primary_id?' <span class="muted mono" style="font-size:10px">· peak</span>':''}
      <div class="adjmeta">${esc(r.reason)}</div>
      ${changed?`<button class="applyrec" data-id="${r.id}" data-pri="${r.suggested_priority}" style="font-size:11px;padding:3px 9px;margin-top:4px">Set ${r.suggested_priority}</button>`:''}</li>`;
  }).join("");
  host.innerHTML=`<div class="adjudbox">
    <div class="exh">${esc(d.summary||'')}</div>
    <ul class="expts">${recs}</ul>
    <div class="exfoot">Claude advises; the engine periodizes from the priorities you keep.</div>
  </div>`;
  host.querySelectorAll(".applyrec").forEach(b=>b.addEventListener("click", async ()=>{
    const r=await fetch(`/api/objectives/${b.dataset.id}/priority`,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({priority:b.dataset.pri})});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  }));
}
function wireObjActions(){
  const adj=$("#adjBtn");
  if(adj) adj.addEventListener("click", async ()=>{
    const t=adj.textContent; adj.disabled=true; adj.textContent="Weighing…";
    const host=$("#objAdjudicate"); if(host) host.innerHTML=`<div class="adjclamp">thinking…</div>`;
    try{ renderAdjudicate(await getJSON("/api/objectives/adjudicate",{method:"POST"})); }
    catch(e){ if(host) host.innerHTML=`<div class="adjclamp err">⚠ ${esc(String(e))}</div>`; }
    finally{ adj.disabled=false; adj.textContent=t; }
  });
  const parse=$("#ao_parse");
  if(parse) parse.addEventListener("click", async ()=>{
    const text=$("#ao_nl").value.trim(); if(!text){ $("#ao_nl").focus(); return; }
    const interp=$("#ao_interp"); const t=parse.textContent;
    parse.disabled=true; parse.textContent="Parsing…"; interp.textContent="";
    try{
      const r=await fetch("/api/objectives/parse",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
      const d=await r.json();
      if(!d.ok){ interp.textContent="⚠ "+(d.error||"couldn't parse"); interp.className="nlinterp err"; return; }
      // fill the structured form — the owner reviews, then clicks Add objective
      $("#ao_label").value=d.label||""; $("#ao_type").value=d.type||"marathon";
      $("#ao_date").value=d.date||""; $("#ao_pri").value=d.priority||"A";
      $("#ao_target").value=d.target||"finish";
      interp.className="nlinterp"+(d.confident?"":" guess");
      interp.textContent=(d.confident?"":"⚠ guessed — check the date · ")+(d.interpretation||"Review and click Add.");
    }catch(e){ interp.textContent="⚠ "+e; interp.className="nlinterp err"; }
    finally{ parse.disabled=false; parse.textContent=t; }
  });
  const add=$("#ao_add");
  if(add) add.addEventListener("click", async ()=>{
    const body={label:$("#ao_label").value||"Race", type:$("#ao_type").value,
      date:$("#ao_date").value, priority:$("#ao_pri").value, target:$("#ao_target").value||"finish"};
    if(!body.date){ await notice({title:"Pick a date", intro:"A race needs a day before it can be planned for."}); return; }
    const r=await fetch("/api/objectives",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  });
  document.querySelectorAll(".obj .x").forEach(btn=>btn.addEventListener("click", async ()=>{
    const r=await fetch(`/api/objectives/${btn.dataset.oid}/remove`,{method:"POST"});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  }));
  // inline A|B|C priority selector — set a priority and re-periodize (no-op if already that letter)
  document.querySelectorAll(".obj .prseg").forEach(b=>b.addEventListener("click", async ()=>{
    if(b.classList.contains("on")) return;
    const r=await fetch(`/api/objectives/${b.dataset.oid}/priority`,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({priority:b.dataset.pri})});
    const p=await r.json();
    if(!p.ok){ await notice({title:"Could not set priority", intro:p.error||"unknown", tone:"danger"}); return; }
    LASTDIFF=p.diff; await refreshPlan(p);
  }));
}
// ── Qualitative adjustment (§6c) — LLM proposes, engine clamps ───────────────
function adjEffect(a){
  const med = a.medical || a.medical_flag;
  if(med) return "Plan halted — full rest until you've seen your doctor";
  if(a.volume_multiplier===0) return "Full rest over the window";
  if(a.volume_multiplier>=1) return a.easy_only ? "Easy effort only over the window" : "On plan — no load change";
  return `Load eased to ${Math.round(a.volume_multiplier*100)}% of plan${a.easy_only?", easy effort only":""}`;
}
function adjustmentUI(p){
  if(SH_READONLY) return "";   // public: no qualitative-adjustment controls or (medical) banner
  const a=p.adjustment;
  const banner = a ? `<div class="${(a.medical||a.medical_flag)?'adjmed':'adjbox'}">
      <div class="adjh">${(a.medical||a.medical_flag)?'⚠ Medical flag':'Active adjustment'} — ${adjEffect(a)}</div>
      <div class="adjmeta">“${esc(a.note)}” · ${esc(a.applies_from)}→${esc(a.applies_until)}${a.summary?` · ${esc(a.summary)}`:''}</div>
      ${a.clamp?`<div class="adjclamp">engine clamp: ${esc(a.clamp)}</div>`:''}
      ${(a.medical||a.medical_flag)?`<div class="adjclamp">Sparing Horse tracks &amp; flags — it never diagnoses. Clear this once your doctor signs off.</div>`:''}
      <button id="adj_clear" style="font-size:11px;padding:4px 9px;margin-top:8px">Clear adjustment</button>
    </div>` : "";
  const ask = `<div class="adjask">
      <input id="adj_text" ${LLM_OK?'':'disabled'} placeholder="How'd it go / how are you? e.g. “felt great today”, “knee’s a bit sore”, “travelling Mon–Fri”">
      <button id="adj_propose" ${LLM_OK?'':'disabled'}>💬 Tell the horse</button>
      <div class="adjhint">A run that's done → I'll <b>log it</b> · something ahead (a niggle, travel, a cold) → I'll <b>propose easing</b> the plan. I only ever ease or hold — never push harder.</div>
      <div id="adj_preview" class="adjpreview">${LLM_OK?'':'<div class="adjclamp">⚙ Add a Claude API key in Settings to enable — the engine still clamps every suggestion.</div>'}</div>
    </div>`;
  return banner + ask;
}
async function renderAdjPreview(d){
  const host=$("#adj_preview"); if(!host) return;
  if(!d.ok){ host.innerHTML=`<div class="adjclamp err">⚠ ${esc(d.error||'could not read that')}</div>`; return; }
  // A reflection ('felt great') changes nothing about the plan — journal it against today and
  // show the reply. Only a real ease/hold/medical signal becomes a confirmable adjustment.
  if(d.kind==="log"){
    await fetch("/api/log/note",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({note:d.note})});
    const t=$("#adj_text"); if(t) t.value="";
    await refreshPlan();   // rebuilds #plan (and a fresh, empty #adj_preview) — do this FIRST
    const h2=$("#adj_preview");   // then write the reply into the NEW node, or it gets clobbered
    if(h2) h2.innerHTML=`<div class="adjprop">
      <div class="adjreply">${esc(d.reply||"Noted — keeping you on plan.")}</div>
      <div class="adjmeta">📓 logged to today's run · plan unchanged</div></div>`;
    return;
  }
  const a=d.directive;
  host.dataset.note=d.note||""; host.dataset.directive=JSON.stringify(a);
  host.innerHTML=`<div class="adjprop">
    ${d.reply?`<div class="adjreply">${esc(d.reply)}</div>`:''}
    <div>${(a.medical_flag)?'⚠ ':''}${adjEffect(a)} · ${esc(a.applies_from)}→${esc(a.applies_until)}</div>
    ${a.summary?`<div class="adjmeta">${esc(a.summary)}</div>`:''}
    ${d.clamp?`<div class="adjclamp">engine clamp: ${esc(d.clamp)}</div>`:''}
    <div style="margin-top:6px">
      <button id="adj_apply" class="primary" style="font-size:11px;padding:4px 10px">Apply</button>
      <button id="adj_dismiss" style="font-size:11px;padding:4px 10px">Dismiss</button></div>
  </div>`;
  $("#adj_apply").addEventListener("click", async ()=>{
    const r=await fetch("/api/adjustment/apply",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({note:host.dataset.note, directive:JSON.parse(host.dataset.directive)})});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  });
  $("#adj_dismiss").addEventListener("click", ()=>{ host.innerHTML=""; });
}
function renderExplain(d){
  const host=$("#planExplain"); if(!host) return;
  if(!d.ok){ host.innerHTML=`<div class="adjclamp err">⚠ ${esc(d.error||'could not explain')}</div>`; return; }
  host.innerHTML=`<div class="explainbox">
    <div class="exh">${esc(d.headline||'')}</div>
    <ul class="expts">${(d.points||[]).map(p=>`<li>${esc(p)}</li>`).join("")}</ul>
    ${d.change_note?`<div class="exchange"><b>Latest change:</b> ${esc(d.change_note)}</div>`:""}
    <div class="exfoot">Claude explains the engine's numbers — it doesn't set them.</div>
  </div>`;
}
function wireAdjust(){
  const exp=$("#explainBtn");
  if(exp) exp.addEventListener("click", async ()=>{
    const t=exp.textContent; exp.disabled=true; exp.textContent="Explaining…";
    const host=$("#planExplain"); if(host) host.innerHTML=`<div class="adjclamp">thinking…</div>`;
    try{
      const r=await fetch("/api/plan/explain",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({diff:LASTDIFF})});
      renderExplain(await r.json());
    }catch(e){ if(host) host.innerHTML=`<div class="adjclamp err">⚠ ${esc(String(e))}</div>`; }
    finally{ exp.disabled=false; exp.textContent=t; }
  });
  const clr=$("#adj_clear");
  if(clr) clr.addEventListener("click", async ()=>{
    const r=await fetch("/api/adjustment/clear",{method:"POST"});
    const p=await r.json(); LASTDIFF=p.diff; await refreshPlan(p);
  });
  const prop=$("#adj_propose");
  if(prop) prop.addEventListener("click", async ()=>{
    const text=$("#adj_text").value.trim(); if(!text){ $("#adj_text").focus(); return; }
    const t=prop.textContent; prop.disabled=true; prop.textContent="Thinking…";
    try{
      const r=await fetch("/api/adjustment/propose",{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
      renderAdjPreview(await r.json());
    }catch(e){ const h=$("#adj_preview"); if(h) h.innerHTML=`<div class="adjclamp err">⚠ ${esc(String(e))}</div>`; }
    finally{ prop.disabled=false; prop.textContent=t; }
  });
}
// §T2 — the four-component fitness model: display names + the science tooltip per component.
const COMPL={vo2max:"VO₂max",ssmax:"SSmax/LT2",economy:"economy",resilience:"resilience"};
const COMPT={
  vo2max:"Builds VO₂max — central aerobic power (blood volume + cardiac output drive most of the variance). Developed by ≥2-min reps near 5k pace; red-cell persistence keeps it for months once built, so the plan develops it EARLY and then merely maintains it.",
  ssmax:"Builds SSmax/LT2 — the steady-state ceiling (mitochondrial respiratory power, capillaries, lactate shuttling), raised by threshold and high-end aerobic work.",
  economy:"Builds running economy — the energy-per-km skill. Grows with accumulated mileage and race-pace specificity over years; the long easy run is its main lever here.",
  resilience:"Builds physiological resilience — how little your VO₂max, SSmax and economy decay over 42 km (the marathon's modern fourth component). Built by long runs with marathon-pace segments, which the assertive plan extends at CONSTANT speed — longer, not faster."};
function compChip(s){
  return s.component?` <span class="scomp" title="${COMPT[s.component]||''}">${COMPL[s.component]||s.component}</span>`:"";
}
// One LOG-enriched session line — planned label + done/missed mark, actual km@pace, reflection,
// doubles breakdown. Shared by the re-base journal renderer and (via weekHtml) every other phase's
// elapsed weeks: the assertive regime skips the re-base, so the log overlay must not live only there.
function logLine(s,today,planLabel){
  const mark = s.unplanned ? '<span class="smk extra" title="unplanned — bonus volume">+</span>'
    : s.done ? '<span class="smk done">✓</span>'
    : s.missed ? '<span class="smk missed">✕</span>'
    : (s.date===today ? '<span class="smk today">•</span>' : '<span class="smk">○</span>');
  const act = s.actual ? `<span class="sact">${s.actual.km}k${s.actual.pace?` @ ${s.actual.pace}`:''}</span>` : "";
  const refl = s.reflection ? `<div class="srefl">📓 ${esc(s.reflection)}</div>` : "";
  const clk = !SH_READONLY && (s.done||s.unplanned) && s.activity_id;   // a completed/extra run → view its run + map
  const plan = s.unplanned ? '<span class="splan exu">unplanned</span>' : planLabel;
  // a DOUBLE (≥2 runs that day): break the combined actual into its per-run halves, each map-linkable
  const brk = (s.runs && s.runs.length>1)
    ? `<div class="srun" title="${s.runs.length} runs this day">↳ ${s.runs.map(r=>{
          const rc = !SH_READONLY && r.activity_id;
          return `<span class="brkrun"${rc?` data-act-id="${r.activity_id}" role="button" tabindex="0" title="View this run on the map"`:''}>${r.km}k${r.pace?` @ ${r.pace}`:''}</span>`;
        }).join(" · ")}</div>`
    : "";
  return `<div class="sline ${s.date===today?'stoday':''}${s.unplanned?' unplanned':''}${clk?' sclick':''}"${clk?` data-act-id="${s.activity_id}" role="button" tabindex="0" title="View this run on the map"`:""}>${mark}<span class="sdate">${sessDate(s.date)}</span>${plan}${act?' → '+act:''}${refl}${brk}</div>`;
}
// §W1 — structured-workout instruction card: the execution plan for a quality session, one row per
// segment (interval reps grouped), each with duration, ~distance, the pace window and the HR band.
// Pace/HR read the CURRENT unified zones (training_zones — the same det-locked grid the effort
// monitor, chart band and zones card share, so this card can't disagree with the verdicts); without
// them (public view, or zones not loaded yet) it falls back to the plan's own generation-time pace
// strings and shows no HR (H7 — heart-rate data never reaches the public read).
function zrow(zone){ return ((ZONESD&&ZONESD.rows)||[]).find(r=>r.key===zone)||null; }
function segPace(zone, rep){
  const r=zrow(zone);
  if(r&&r.pace_slower_than&&r.pace_slower_than.fmt) return `slower than ${r.pace_slower_than.fmt}/km`;
  if(r&&r.pace_target&&r.pace_target.fmt) return `≈ ${r.pace_target.fmt}/km`;
  const p=((rep&&rep.pace_zone)||"").split("/km")[0];   // fallback: the plan's own pace at generation time
  return p?`≈ ${p}/km`:"—";
}
function segHR(zone){
  const h=(zrow(zone)||{}).hr;
  if(!h) return "";
  if(h.lo!=null&&h.hi!=null) return `${h.lo}–${h.hi} bpm`;
  if(h.hi!=null) return `≤ ${h.hi} bpm`;
  if(h.lo!=null) return `≥ ${h.lo} bpm`;
  return "";
}
function workoutCard(s, open){
  const reps=s.reps||[]; if(!reps.length) return "";
  const sum=(a,k)=>a.reduce((t,r)=>t+(r[k]||0),0);
  const grp=e=>reps.filter(r=>r.effort===e);
  const wu=grp("warmup"), cd=grp("cooldown"), base=grp("easy_base"),
        work=grp("work"), rec=grp("recovery");
  const rows=[], cue=[];
  const row=(what,zone,min,km,rep)=>rows.push(
    `<tr><td>${esc(what)}</td><td>${min}′</td><td>${km?`~${km.toFixed(1)}k`:"—"}</td>`+
    `<td>${esc(segPace(zone,rep))}</td><td>${esc(segHR(zone))||"—"}</td></tr>`);
  if(wu.length){ row("Warm-up · easy","easy",sum(wu,"minutes"),sum(wu,"km"),wu[0]); cue.push(`${sum(wu,"minutes")}′ easy`); }
  if(base.length){ row("Easy aerobic base","easy",sum(base,"minutes"),sum(base,"km"),base[0]); cue.push(`${sum(base,"minutes")}′ easy base`); }
  if(work.length>1){
    const rm=work[0].minutes, rz=work[0].zone, rcm=rec.length?rec[0].minutes:0;
    row(`${work.length} × ${rm}′ @ ${rz}`,rz,sum(work,"minutes"),sum(work,"km"),work[0]);
    if(rec.length) row(`${rec.length} × ${rcm}′ jog between reps`,"easy",sum(rec,"minutes"),sum(rec,"km"),rec[0]);
    cue.push(`${work.length} × ${rm}′ @ ${rz} pace${rcm?` (${rcm}′ easy jog between)`:""}`);
  }else if(work.length===1){
    const w0=work[0];
    row(s.kind==="long_mp"?`${w0.minutes}′ @ marathon pace to finish`:`${w0.minutes}′ steady @ ${w0.zone}`,
        w0.zone,w0.minutes,w0.km,w0);
    cue.push(s.kind==="long_mp"?`finish with ${w0.minutes}′ @ marathon pace`:`${w0.minutes}′ steady @ ${w0.zone} pace`);
  }
  if(cd.length){ row("Cool-down · easy","easy",sum(cd,"minutes"),sum(cd,"km"),cd[0]); cue.push(`${sum(cd,"minutes")}′ easy`); }
  return `<div class="wcard${open?" open":""}"><div class="wcue">${esc(cue.join("  →  "))}</div>
    <div class="wctbl-wrap"><table class="wctbl"><thead><tr><th>Segment</th><th>Time</th><th>Dist</th><th>Pace</th><th>HR</th></tr></thead>
    <tbody>${rows.join("")}</tbody></table></div>
    <div class="wctot">Total ${s.km} km · ~${s.minutes}′ · load ${s.trimp!=null?Math.round(s.trimp):"—"} TRIMP${ZONESD?"":" · paces from plan"+(SH_READONLY?"":"; HR bands load with zones")}</div></div>`;
}
// §6f Step F — compact label for a planned session. Structured quality sessions (intervals / MP
// long run / tempo, carried as `reps`) read their structure; plain runs show distance.
function sessSummary(s){
  if(s.reps&&s.reps.length){
    const work=s.reps.filter(r=>r.effort==='work');
    if(s.kind==='interval'&&work.length) return `${work.length}×${work[0].minutes}′ ${work[0].zone}`;
    if(s.kind==='long_mp'){ const mp=work.find(r=>r.zone==='marathon'); return `long ${s.km}k +${mp?mp.minutes:0}′ MP`; }
    if(s.kind==='tempo'&&work.length) return `${s.km}k · ${work.reduce((a,r)=>a+r.minutes,0)}′ ${work[0].zone}`;
  }
  // strides ride a specific run (the week's first short easy one) — say so on THAT line, where
  // they're executed, instead of the old orphaned "strides×2" note floating under the week.
  // Plans stored before the s.strides flag carry the fact only in the engine-authored note text,
  // so fall back to that until the nightly re-plan refreshes the JSON.
  const st = s.strides || +(((s.note||"").match(/(\d+)×4–6 strides/)||[])[1]||0);
  if(st) return `${s.km}k <span class="strid" title="Finish this run with ${st} × 4–6 strides: ~20-second relaxed-fast accelerations with full walk-back recovery. A neuromuscular touch-up, not a workout — it adds no training load.">+ ${st}× strides</span>`;
  return `${s.km}k`;
}
// One plan week (used for every phase but the re-base, which carries the journal/actuals overlay).
// Tags down weeks (3:1 mesocycle), frozen weeks (§6f Step E — completed, carried verbatim), and the
// The Sunday a Monday-anchored week ends. ALL-UTC on purpose: the obvious `new Date(iso)` +
// `setDate(getDate()+6)` + `toISOString()` mixes a UTC instant with LOCAL date arithmetic, and in a
// zone whose DST starts on a Sunday at 02:00 — Auckland, Sydney, Chatham — that returns the end one
// day EARLY, once a year (verified: week 2026-09-21 ends 09-26 instead of 09-27 in Pacific/Auckland).
// The plan then stops treating the current week as current on its own final Sunday. Northern zones
// never see it, because their switch falls outside the window this arithmetic walks.
const weekEndIso = mon => { const d=new Date(mon+"T00:00:00Z"); d.setUTCDate(d.getUTCDate()+6);
  return d.toISOString().slice(0,10); };
// week containing today. Quality sessions are accented; the ACWR badge rides the right rail.
function weekHtml(w,p,today){
  const down=/down/i.test(w.intent||'');
  let cur=false;
  if(w.start) cur = !w.frozen && w.start<=today && today<=weekEndIso(w.start);
  // LOG-enriched sessions (merged in by renderPlan for elapsed/current weeks) get the journal line
  // (done/missed mark, actual, reflection, doubles); plain future weeks keep the compact summary.
  const sess=w.sessions.map(s=>{
    // §W1 — a quality session expands to its instruction card on click (skip done sessions:
    // their line is already the click-to-map affordance, and the prescription moment is past)
    const q=s.reps&&s.reps.length, card=(q&&!s.done)?workoutCard(s):"";
    const label=`<span class="wsi${q?' qs':''}${card?' wsx':''}"${card?' tabindex="0" role="button" title="Show the workout instructions"':''}>${sessSummary(s)}${card?' <span class="wsxc" aria-hidden="true">▸</span>':''}</span>${compChip(s)}${card}`;
    return ('done' in s) ? logLine(s,today,label)
      : `<div class="sline"><span class="sdate">${sessDate(s.date)}</span>${label}</div>`;
  }).join("");
  const flags=[w.frequency_met?'<span class="wfz" title="You’ve already run this week’s prescribed count and volume — today’s remaining run is optional, not forced.">✓ frequency met — today optional</span>'
                 // §6o-B — volume charged: the week's km intent is already run (even if the run count is short) — no more sessions are laid on the remaining days
                 :(w.volume_met?'<span class="wfz" title="You’ve already run this week’s planned km — the remaining days aren’t re-prescribed just to hit a run count. Rest is prescribed; an easy run is fine if you feel good.">✓ volume run — today optional</span>':''),
               w.fatigue_capped?'<span class="down" title="A building week, but recent fatigue left no ACWR headroom — the long run was held back. Load capped for safety, not silently degraded.">⚠ build intent capped by recent fatigue</span>'
                 :(w.clipped?'<span class="down">clipped to fit ACWR</span>':''),
               // §PRO9 — long-run progression cap (Aarhus injury lever); §3.1 — biomechanical (eq_km) load ease
               w.long_step_capped?`<span class="wfz" title="Long-run progression cap: this week's long run was held to +10% over your longest run of the last 4 weeks — the strongest single injury lever (a sharp long-run jump predicts injury more than mileage jumps). Freed volume went to easy runs; weekly load unchanged.">long-run held (+10%)</span>`:'',
               w.bio_capped?`<span class="wfz" title="Biomechanical load ease: this week's pace-weighted 'damage-equivalent' load (fast km count for more) jumped too far above your recent weeks, so the fast work was eased to easy — aerobic volume kept, only the high-damage fast km reduced. The biomechanical/tissue-damage axis (informed by John Davis), invisible to heart-rate load.">fast load eased</span>`:'',
               // §AV — laid around declared away days (private payload only: the public box strips av_dates)
               w.av_dates?`<span class="eased" title="Away days: the week is laid around the days you declared unavailable — runs relocated to the nearest legal day, spacing kept${w.av_shed?'; too few legal days remained, so '+w.av_shed+' run'+(w.av_shed>1?'s were':' was')+' shed and the load went with them (a travel week is lighter, never crammed)':''}.">✈ away ${w.av_dates.map(sessDate).join(', ')}${w.av_shed?' · −'+w.av_shed+' run'+(w.av_shed>1?'s':''):''}</span>`:'',
               w.adjusted?'<span class="eased">eased</span>':'',
               w.frozen?'<span class="wfz">✓ done</span>':''].filter(Boolean).join(" · ");
  // §3.1 — surface the biomechanical eq_km on quality weeks (where it exceeds raw km); calm/muted.
  const eqkm=(w.eq_km&&w.eq_km>w.km+0.5)?` · <span class="muted mono" title="Biomechanical load: pace-weighted damage-equivalent km (fast running does several times more tissue damage per km than easy). The biomechanical/tissue-damage axis (informed by John Davis), which heart-rate load can't see.">${w.eq_km} eq-km</span>`:'';
  // §CARD2 — the straddling week's total = actuals so far + prescription ahead; show the split so
  // the number explains itself (the old prescription-sum header made an over-run week LOOK smaller).
  const kmSplit=(w.partial&&w.km_done!=null)?` · <span class="muted mono" title="This week straddles today, so its total counts days you've already run at their REAL distance (${w.km_done} km in ${w.runs_done} run${w.runs_done===1?'':'s'}) plus what's still prescribed ahead (${w.km_ahead} km in ${w.runs_ahead}). Days you out-run no longer shrink the week's headline.">${w.km_done} run + ${w.km_ahead} ahead</span>`:'';
  return `<div class="wk ${down?'wdown':''} ${w.frozen?'wfrozen':''} ${cur?'wcur':''}">
      <div class="wn">${w.wk}${w.frozen?'<span class="wlock" title="completed — carried verbatim">🔒</span>':''}</div>
      <div class="wbody">
        <div><span class="wkm">${w.km} km</span>${eqkm} · ${w.runs} runs${kmSplit}${flags?' · '+flags:''}</div>
        <div class="wintent">${w.intent}</div>
        <div class="wsesslog">${sess}</div>
      </div>
      <div>${acBadge(w.proj_acwr)}</div>
    </div>`;
}
// Map a phase's display name → the stable key shared by its bar segment and its weeks panel.
function phaseKey(name){
  if(/^re-?base/i.test(name)) return "rebase";
  if(/^base/i.test(name))     return "base";
  if(/^build/i.test(name))    return "build";
  if(/^peak/i.test(name))     return "peak";
  if(/^taper/i.test(name))    return "taper";
  return "";
}
// True when `today` falls inside the week starting at w.start (Mon–Sun).
function weekHoldsToday(w,today){
  if(!w||!w.start) return false;
  return w.start<=today && today<=weekEndIso(w.start);
}
// Which week is open by default in a phase's strip: the one holding `today`, else the first.
function defaultWeek(weeks,today){ return ((weeks.find(w=>weekHoldsToday(w,today))||weeks[0]||{}).wk); }
// A second-level selector that echoes the phase bar: one segment per week, the selected one active.
// Shared by every phase (re-base + future) — only the *detail* below it differs per phase, so the
// LOG-enriched vs plain week renderers stay untouched (we wrap their output, never merge them).
function weekStrip(weeks,pk,sel){
  return `<div class="weekstrip" data-pk="${pk}">`+weeks.map(w=>{
    const down=/down/i.test(w.intent||'')||w.wk===4;
    return `<div class="weekseg${w.wk===sel?' active':''}${down?' wsdown':''}" data-pk="${pk}" data-wk="${w.wk}"
       role="button" tabindex="0" aria-pressed="${w.wk===sel}"
       aria-label="W${w.wk}: ${w.km} km, ${w.runs} runs${w.frozen?', done':''}"
       title="Week ${w.wk} · ${w.km} km · ${w.runs} runs${w.frozen?' · done':''}">W${w.wk}${w.frozen?'<span class="wlock">🔒</span>':''}</div>`;
  }).join("")+`</div>`;
}
// Wrap a week's already-rendered detail so the strip can show/hide it (scoped by phase key + week).
function weekDetail(inner,pk,wk,sel){ return `<div class="weekdetail${wk===sel?' active':''}" data-pk="${pk}" data-wk="${wk}">${inner}</div>`; }
// A collapsible-free phase section: header (weeks, calendar, projected end CTL/ATL, Σ km, frozen
// count) + a week strip; only the selected week's detail shows. Renders nothing if the phase
// wasn't generated (short runways drop late phases).
function phaseSection(title,block,p,today,pk){
  if(!block||!block.weeks||!block.weeks.length) return "";
  pk = pk || phaseKey(title);   // §6q — explicit key (chain segments share base/peak/taper names)
  const km=block.weeks.reduce((s,w)=>s+(w.km||0),0);
  const froz=block.weeks.filter(w=>w.frozen).length;
  const fz=froz?` · <span class="wfz">${froz} done</span>`:"";
  const sel=defaultWeek(block.weeks,today);
  // §T2 — what this phase builds, summed from the session component tags (can't drift from the plan)
  const builds=(block.builds&&block.builds.length)
    ?` · <span class="muted" style="font-size:11px">builds ${block.builds.map(c=>COMPL[c]||c).join(" + ")}</span>`+
      qhint("Marathon fitness decomposes into four components: VO₂max × running economy × SSmax/LT2 (the classic three-factor model) plus physiological resilience — how little the first three decay over 42 km. Each quality session is tagged with the component it chiefly builds (hover a session's chip); this line sums what the phase is FOR. The assertive plan periodizes them: VO₂max developed early then merely maintained (red-cell persistence makes it cheap to hold), marathon-pace work growing through the build, and the peak pivoting to resilience — the long run's MP segment extends at constant speed. Framing follows John Davis; the fractions are our operationalization.")
    :"";
  return `<h3 class="phasehdr">
      <span>${title} <span class="muted mono" style="font-size:11px">(${block.weeks.length}w · start ${block.start} · ends CTL ${block.end_ctl}/ATL ${block.end_atl})${fz}</span>${qhint("CTL = chronic load (your fitness), a slow ~42-day average of training; ATL = acute load (recent fatigue), a fast ~7-day average. Shown here is each value projected to this phase's end.")}${builds}</span>
      <span class="muted mono" style="font-size:12px;font-weight:600;white-space:nowrap" title="Total planned distance across the phase">Σ ${km.toFixed(0)} km</span>
    </h3>
    ${weekStrip(block.weeks,pk,sel)}
    <div class="weekdetails">${block.weeks.map(w=>weekDetail(weekHtml(w,p,today),pk,w.wk,sel)).join("")}</div>`;
}
function renderPlan(p){
  const host=$("#plan");
  // the public box removes the Generate button — its empty state must not point at it (UX-11)
  if(!p){ host.innerHTML=`<div class="empty">${SH_READONLY
      ? "No plan published yet."
      : `No plan yet — hit <b>Generate plan</b>.`}</div>`; return; }
  const o=p.objective, rb=p.rebase;
  // A-race pill bar at the top of the page (mockup Almanac) — driven by the plan's objective
  const ob=$("#objbar");
  if(ob) ob.innerHTML = o ? `<span class="objlabel">Current main objective</span>`+
      `<span class="arace">${esc(o.priority||'A')}-race</span>`+
      `<span class="oname">${esc(o.label)}</span>`+
      `<span class="owhen">${esc(o.date)} · ${o.weeks_away} weeks out</span>`+
      ((p.feasibility&&(p.feasibility.verdict||o.target))?`<span class="overdict">Verdict — <b style="color:var(--text)">${esc(p.feasibility.verdict||o.target)}</b></span>`:"")
    : "";
  const totalw=p.phases.reduce((s,x)=>s+x.weeks,0);
  const zoneChips=Object.entries(p.pace_zones).map(([k,v])=>
    `<span class="zone ${k==='easy_top'?'hl':''}">${k.replace('_',' ')} <b>${v}</b></span>`).join("");
  // Prefer the log's weeks (sessions enriched with done/actual/reflection); fall back to the
  // raw plan weeks when the log isn't loaded yet. The log weeks are a superset of plan weeks.
  const today = (LOG&&LOG.today) || new Date().toISOString().slice(0,10);
  // Log weeks are pk-tagged per phase: the re-base group keeps its dedicated journal renderer;
  // every other phase's weeks get the enriched sessions merged into its block (assertive skips the
  // re-base entirely, so elapsed weeks must carry their actuals wherever they live).
  const planWeeks = (LOG&&LOG.weeks&&LOG.weeks.filter(w=>!w.pk||w.pk==="rebase")) || rb.weeks;
  const LOGW={};
  ((LOG&&LOG.weeks)||[]).forEach(w=>{ if(w.pk&&w.pk!=="rebase"){ (LOGW[w.pk]=LOGW[w.pk]||{})[w.wk]=w; } });
  const enrich=(k,blk)=>(blk&&blk.weeks&&LOGW[k])
    ? {...blk, weeks: blk.weeks.map(w=>LOGW[k][w.wk]?{...w,sessions:LOGW[k][w.wk].sessions}:w)}
    : blk;
  // Which phase owns "today" — the one whose weeks bracket it. Default selection for the Plan tile:
  // the bar shows the whole road, but only the live phase's weeks are open underneath. Fallbacks:
  // before the plan starts → the first phase; after it ends → the last.
  // §6q — phase groups drive "which phase owns today". Re-base weeks come from the LOG-enriched
  // planWeeks; every other segment (base/build/peak/taper + any chain bridge/peak/taper) comes from
  // its own block keyed by p.phases[].key, so a multi-A chain opens the right segment under the bar.
  const phaseGroups=[{key:"rebase",weeks:planWeeks}]
    .concat((p.phases||[]).filter(x=>x.key&&x.key!=="rebase")
            .map(x=>({key:x.key,weeks:(p[x.key]&&p[x.key].weeks)||[]})))
    .filter(g=>g.weeks.length);
  let curPhase=(phaseGroups.find(g=>g.weeks.some(w=>weekHoldsToday(w,today)))||{}).key;
  if(!curPhase && phaseGroups.length){
    const first=phaseGroups[0];
    curPhase=(first.weeks[0].start && today<first.weeks[0].start)
      ? first.key : phaseGroups[phaseGroups.length-1].key;
  }
  curPhase=curPhase||"rebase";
  const phaseBar=p.phases.filter(x=>x.weeks>0).map((x,i)=>{
    const mix=20+i*16, k=x.key||phaseKey(x.phase);   // §6q — explicit per-segment key
    return `<div class="phaseseg${k===curPhase?' active':''}" data-pk="${k}" title="${esc(x.phase)}"
      role="button" tabindex="0" aria-pressed="${k===curPhase}" style="flex:${x.weeks};--mix:${mix}%">
      <b>${x.phase.split(" ")[0]}</b>${x.weeks}w</div>`;}).join("");
  // Glanceable block total — Phase 0 is all easy running, so planned time = distance × easy pace.
  const phaseKm = planWeeks.reduce((s,w)=>s+(w.km||0),0);
  const easySec = (m=>m?(+m[1]*60+ +m[2]):0)(/(\d+):(\d+)/.exec(p.pace_zones.easy_top||""));
  const fmtDur = m => m>=60 ? `${Math.floor(m/60)}h${String(m%60).padStart(2,"0")}m` : `${m}m`;
  const phaseMin = easySec ? Math.round(phaseKm*easySec/60) : 0;
  const phaseTot = `Σ ${phaseKm.toFixed(0)} km${phaseMin?` · ~${fmtDur(phaseMin)} easy`:""}`;
  const ran = LOG&&LOG.ran;   // actually run across the block so far (dups excluded)
  const ranTot = ran&&ran.km>0 ? `▸ ran ${ran.km.toFixed(0)} km${ran.min?` · ${fmtDur(ran.min)}`:""}` : "";
  // Last refreshed = when the plan was regenerated (auto-replan runs nightly; paces & totals are
  // recomputed from that snapshot's VO₂max, so this is how fresh the pills below are).
  const refreshed = p.generated_at
    ? `<div class="legend" style="margin-top:6px;opacity:.85">↻ Last refreshed ${new Date(p.generated_at).toLocaleString()}</div>` : "";
  // The re-base journal line = the shared logLine with a plain-distance plan label (Phase 0 is
  // pure easy running; quality summaries only matter in the later phases' weekHtml path).
  const sessHtml=s=>logLine(s,today,`<span class="splan">${sessSummary(s)}</span>`);
  // Re-base weeks are LOG-enriched (done/missed/unplanned/doubles via sessHtml) — kept as their own
  // renderer; we only wrap each in a week-detail and front it with the shared strip (selector below).
  const rbSel=defaultWeek(planWeeks,today);
  const weeks=weekStrip(planWeeks,'rebase',rbSel)+`<div class="weekdetails">`+planWeeks.map(w=>{
    const hasLog = w.sessions.some(s=>'done' in s);
    const sess = hasLog ? `<div class="wsesslog">${w.sessions.map(sessHtml).join("")}</div>`
      : `<div class="wsesslog">${w.sessions.map(s=>`<div class="sline"><span class="sdate">${sessDate(s.date)}</span><span class="splan">${sessSummary(s)}</span></div>`).join("")}<div class="muted mono" style="margin-top:3px">@ easy ${p.pace_zones.easy_top}</div></div>`;
    const inner = `<div class="wk ${w.wk===4?'wdown':''}">
      <div class="wn">${w.wk}</div>
      <div class="wbody">
        <div><span class="wkm">${w.km} km</span> · ${w.runs} runs${w.clipped?' · <span class="down">clipped to fit ACWR</span>':''}${w.adjusted?' · <span class="eased">eased</span>':''}</div>
        <div class="wintent">${w.intent}</div>
        ${sess}
      </div>
      <div>${acBadge(w.proj_acwr)}</div>
    </div>`;
    return weekDetail(inner,'rebase',w.wk,rbSel);}).join("")+`</div>`;
  const adh = (LOG&&LOG.adherence&&LOG.adherence.scheduled)
    ? `<span class="muted mono" style="font-size:11px"> · done ${LOG.adherence.done}/${LOG.adherence.scheduled} so far</span>` : "";
  // (§FORM1 2026-08-18 — the graduation, earned-build, and 6th-run banners are gone with the
  // banked-week machinery: nothing is unlocked by adherence bookkeeping any more. The regime bar
  // above carries the posture story; §PRO5's ride note carries the responsiveness story.)
  const grad = "", earnedNote = "", freqNote = "";
  const tuneTxt = (p.tune_ups&&p.tune_ups.length)
    ? `<div class="legend" style="margin-top:8px">Tune-ups before the peak: ${p.tune_ups.map(t=>`${esc(t.label)} (${t.date}, ${t.priority})`).join(" · ")}</div>` : "";
  // §6q/#2 — multi-A chain strip: when the build chains ≥2 A-races, surface each race's projected
  // race-day fitness (peak CTL carried into the taper) + its own feasibility verdict, so the roadmap reads "where does
  // each race land", not just the final peak. Single-A skips it (the objline + headline verdict cover one).
  const verdTone = v => v==="too soon" ? "warn" : (v==="finish"||v==="maintain") ? "ok" : "muted";
  const roleName = r => r==="goal" ? "Goal" : r==="coequal" ? "Co-equal" : r==="subordinate" ? "Tune-up" : (r||"");
  const chainRaces = p.chain||[];
  const chainStrip = chainRaces.length>1
    ? `<div class="chainstrip">
        <div class="legend" style="margin-bottom:5px">Race chain — projected fitness &amp; verdict at each A-race${qhint("Each A-race in the build, with the chronic load (CTL — your fitness) the engine projects you'll carry into it at the end of that race's taper, and whether that supports a healthy finish on its runway. Re-read every block as real fitness returns.")}</div>
        ${chainRaces.map(c=>{
          const ctl = (c.proj_ctl!=null) ? `CTL ≈ ${Math.round(c.proj_ctl)}` : "—";
          const v = c.feasibility;
          return `<div class="chainrace">
            <span class="crole">${esc(roleName(c.role))}</span>
            <span class="cname">${esc(c.label)}</span>
            <span class="cwhen">${esc(c.date)}</span>
            <span class="cctl" title="Projected race fitness — peak CTL carried into the taper (ACWR-capped)">${ctl}</span>
            ${v?`<span class="cverd ${verdTone(v)}">${esc(v)}</span>`:""}
          </div>`;
        }).join("")}
      </div>` : "";
  const header = o
    ? `<div class="objline">
        <span class="race">${esc(o.label)}</span>
        <span class="away">${o.weeks_away} weeks away · ${o.date}</span>
        <span class="away" style="color:var(--muted)">goal: ${o.target} · priority ${o.priority||'A'}</span>
      </div>`
    : `<div class="objline"><span class="race">Maintenance</span>
        <span class="away" style="color:var(--muted)">no objective — holding fitness</span></div>`;
  // §PRO7/§FT6 — finish-time strip. It used to read now→+4w→+8w, which prices a LATER RACE DATE —
  // a counterfactual nobody asked about, and one that reads like a timeline ("am I training for the
  // next 8 weeks?"). It now answers the question a runner actually has: what does this BLOCK buy?
  // Today's measured shape → the race-day projection, with the gain named. The runway curve is not
  // lost — it moves into the hover, labelled as the what-if it always was.
  const FT = p.feasibility && p.feasibility.finish_time;
  // §FT3 — the RANGE is the headline (owner-decided: a point invites anchoring on false precision);
  // the median rides as the trend detail.
  const FTB = FT && FT.band, FTT = FT && FT.today;
  // §FT7 — a plan SAVED by an EARLIER engine renders through today's UI: /api/plan serves the last
  // stored row, and deploying new code does not regenerate it (plans are versioned artifacts —
  // §6f Step E freezes the past on purpose). Such a payload carries no band, no today, and a
  // pre-§FT1 FROZEN curve — so the strip silently showed a superseded prediction as current, and
  // the §FT6 runway hover printed three identical times immediately after promising "more runway →
  // faster". Say it is stale, and never assert a trend the numbers don't show.
  const fcHms = (FT&&FT.curve||[]).map(c=>c.hms);
  // §PRO14 — the GENERAL staleness signal: this artifact's engine stamp vs the engine now running.
  // Any mismatch means regenerate, in either direction (an upgrade, or a rollback) — comparing for
  // equality rather than ordering keeps that honest without pretending to understand version order.
  // A pre-§PRO14 plan carries no stamp, so it reads stale, which is correct.
  const planStale = !!(p.engine_running && p.engine_version !== p.engine_running);
  // §56 — the DAY moved under the plan. `moved` is computed server-side against live state; a plan
  // that predates §PRO20, or came from the cold-start path, yields seed_now:null and says NOTHING
  // rather than guessing. Deliberately makes no claim about the sessions — only that the seed moved.
  const SN = p.seed_now;
  const seedStale = !!(SN && SN.moved);
  const ftStale = !!FT && !FTB && !FTT;
  const ftFrozen = fcHms.length>1 && new Set(fcHms).size===1;
  // §FT9 — the speed anchor is older than the window in which it could describe TODAY (a layoff).
  // The engine withheld the "off today's shape" read rather than decaying a number it cannot
  // calibrate a decay for; say WHY it is missing, or the strip just looks like the pre-§FT6 one.
  const ftAnchor = FT && FT.anchor_stale;
  const ftPts = FTT
    ? [{lab:"off today's shape", hms:FTT.hms}, {lab:"by race day", hms:FT.hms, now:true}]
    : (FT&&FT.curve||[]).map(c=>({lab: c.plus_weeks===0?(FTB?'median now':'now'):'+'+c.plus_weeks+'w',
                                  hms: c.hms, now: c.plus_weeks===0}));
  const fcMin=s=>{const m=Math.round(Math.abs(s)/60);
    return m>=60?`${Math.floor(m/60)}h ${String(m%60).padStart(2,"0")}min`:`${m} min`};
  // Named honestly in BOTH directions — a taper-only or detraining runway must not be dressed up
  // as a gain, and a flat one says so rather than implying movement.
  const gain = FTT && FTT.gain_seconds;
  const gainTxt = !FTT ? "" : gain>60 ? `the build buys ${fcMin(gain)}`
    : gain<-60 ? `this runway costs ${fcMin(gain)}` : "holds today's shape";
  const runway = ftFrozen ? "" : (FT&&FT.curve||[]).filter(c=>c.plus_weeks>0)
    .map(c=>`+${c.plus_weeks}w → ${c.hms}`).join(" · ");
  const fcHint = !FT ? "" : (FT.note||"")
    + (runway?` If the race were later (same training): ${runway}.`:"")
    + (ftStale?" This prediction was saved by an earlier version of the engine — regenerate the plan to re-read it on the current model.":"")
    + (ftAnchor?` Your last measured speed is ${ftAnchor.age_days} days old (${esc(ftAnchor.as_of)}), so there is no honest read of today's shape to compare against — run, and it re-anchors on its own.`:"");
  const finishCurve = FT ? `<div class="finishcurve">
      <span class="fclabel">Projected ${esc(FT.distance)} finish
        ${FTB?`<b>${esc(FTB.lo_hms)}–${esc(FTB.hi_hms)}</b>`:`<b>${esc(FT.hms)}</b>`}</span>
      ${ftPts.map(c=>`<span class="fcpt${c.now?' now':''}">${esc(c.lab)}: <b>${esc(c.hms)}</b></span>`)
        .join('<span class="fcsep">→</span>')}
      ${gainTxt?`<span class="fcgain">${esc(gainTxt)}</span>`:''}
      ${(ftStale||planStale)?`<span class="fcstale">⟳ saved by an earlier engine — regenerate to re-read</span>`:''}
      ${ftAnchor?`<span class="fcstale">◷ last measured ${ftAnchor.age_days}d ago — today's shape unread</span>`:''}
      ${qhint(fcHint)}
    </div>` : "";
  // §PRO3/§PRO5 — training-regime posture: which regime drove the plan + why, and (assertive) how hard
  // it's riding the load ceiling given the measured-vs-projected response. Surfaced so the auto-gate is
  // never silent. The ceiling is named for what binds (0.27.0, log §70): `ride_cap` is the PLANNING cap —
  // the settled end-of-week target (≤ ACWR_SOFT, judged on the §PRO8-floored denominator) — while the
  // in-week raw peak rides to the HARD cap; "hard cap 1.30" = ACWR_HARD (det/copy-posture pins the literal).
  const RG = p.regime || {}, SR = p.shape_response || {};
  const rideTxt = (RG.mode==='assertive' && SR.ride_cap)
    ? (SR.factor>=1 ? `riding the full load ceiling (ACWR planning cap ${SR.ride_cap}, hard cap 1.30)`
                    : `easing the push — measured fitness ${Math.round((SR.ratio||0)*100)}% of projection (ACWR planning cap ${SR.ride_cap})`)
    : "";
  const regimeNote = RG.mode ? `<div class="regimebar ${RG.mode}">
      <span class="rgbadge">${RG.mode==='assertive'?'⟢ Assertive build':'◇ Conservative'}</span>
      <span class="rgreason">${esc(RG.reason||'')}</span>
      ${rideTxt?`<span class="rgride">${rideTxt}</span>`:''}
    </div>` : "";
  // Per-phase week lists, each behind its bar segment. Only the active phase's panel is shown;
  // clicking a segment swaps which one is open (wired below). Empty phases render no panel.
  // §6q — one panel per non-rebase phase in p.phases (single-A = base/build/peak/taper; a chain adds
  // its bridge/peak/taper segments), keyed by the phase's own key so segments never collide.
  const panel=(k,inner)=>inner?`<div class="phasepanel${k===curPhase?' active':''}" data-pk="${k}">${inner}</div>`:'';
  const phasePanels = o ? (p.phases||[]).filter(x=>x.key&&x.key!=="rebase")
      .map(x=>panel(x.key, phaseSection(x.phase, enrich(x.key, p[x.key]), p, today, x.key))).join("") : '';
  const rebaseInner=`
    <h3 style="font-family:var(--serif);font-weight:600;font-size:16px;margin:18px 0 2px;display:flex;justify-content:space-between;align-items:baseline;gap:12px">
      <span>Phase 0 — the re-base block <span class="muted mono" style="font-size:11px">(start ${rb.start}, ends CTL ${rb.end_ctl}/ATL ${rb.end_atl})${adh}</span>${qhint("CTL = chronic load (your fitness), a slow ~42-day average of training; ATL = acute load (recent fatigue), a fast ~7-day average. Shown here is each value projected to this phase's end.")}</span>
      <span style="text-align:right;line-height:1.4">
        <div class="mono" style="font-size:12px;font-weight:600;color:var(--muted);white-space:nowrap" title="Total planned distance and easy-run time across the re-base block">${phaseTot}</div>
        ${ranTot?`<div class="mono" style="font-size:11px;font-weight:600;color:var(--ok);white-space:nowrap" title="Distance and time you've actually run during this block so far (duplicates excluded)">${ranTot}</div>`:""}
      </span>
    </h3>
    ${weeks}`;
  host.innerHTML=`
    ${planStale?`<div class="stalebanner">⟳ <b>This plan was built by an earlier version of the engine.</b>
      Updating the app does not rebuild saved plans — the weeks below, and every number derived from
      them, are exactly as they were generated${p.engine_version?` by v${esc(p.engine_version)}`:''}.
      ${SH_READONLY?"":`Hit <b>Generate plan</b> to re-read them on v${esc(p.engine_running)}.`}</div>`:''}
    ${seedStale?`<div class="stalebanner">⟳ <b>This plan was built from an older reading of your shape.</b>
      Generated ${p.generated_at?esc(new Date(p.generated_at).toLocaleString()):'earlier'}, seeded from
      your ${esc(SN.was_from||'earlier')} state (CTL ${esc(SN.was.ctl)} · ATL ${esc(SN.was.atl)}). Your
      settled ${esc(SN.from)} state${SN.bridged_days?`, rolled forward ${SN.bridged_days}
      day${SN.bridged_days>1?'s':''} by measurement,`:''} now reads CTL ${esc(SN.ctl)} · ATL
      ${esc(SN.atl)}. ${SH_READONLY?"":`Hit <b>Generate plan</b> to re-read today off it.`}</div>`:''}
    ${diffBanner(LASTDIFF)}
    ${objManager(p)}
    ${header}
    ${adjustmentUI(p)}
    ${SH_READONLY?'':`<div class="explainrow">
      <button id="explainBtn" ${LLM_OK?'':'disabled'}>📖 Explain this plan${LLM_OK?'':' — add a Claude API key in Settings'}</button>
    </div>
    <div id="planExplain"></div>`}
    <p class="feas">${esc(p.feasibility.note).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')}</p>
    ${finishCurve}
    ${regimeNote}
    <div class="phases">${phaseBar}</div>
    <div class="legend" style="margin-top:4px">${o?`Full periodization, re-base → race week (${totalw} weeks; ${o.weeks_away} still ahead) · tap a phase to open its weeks`:'Re-base, then hold'}</div>
    ${chainStrip}
    ${tuneTxt}
    <div class="zones">${zoneChips}</div>
    ${refreshed}
    <p class="feas" style="border-color:var(--warn)">${esc(p.note).replace(/THRESHOLD/,'<b>THRESHOLD</b>')}</p>
    ${grad}${earnedNote}${freqNote}
    <div class="phasepanels">
      ${panel('rebase',rebaseInner)}
      ${phasePanels}
    </div>`;
  // The phase bar acts as a selector: clicking a segment opens that phase's weeks and closes the rest.
  host.querySelectorAll(".phaseseg").forEach(seg=>seg.addEventListener("click",()=>{
    const pk=seg.dataset.pk;
    if(!host.querySelector(`.phasepanel[data-pk="${pk}"]`)) return;  // no weeks for this phase
    host.querySelectorAll(".phaseseg").forEach(s=>{
      s.classList.toggle("active",s===seg); s.setAttribute("aria-pressed", s===seg); });
    host.querySelectorAll(".phasepanel").forEach(pn=>pn.classList.toggle("active",pn.dataset.pk===pk));
  }));
  // The week strip is the second-level selector: clicking a week shows only its detail, scoped to
  // its own phase (data-pk + data-wk) so e.g. W1 of Base doesn't toggle W1 of Build.
  host.querySelectorAll(".weekseg").forEach(seg=>seg.addEventListener("click",()=>{
    const pk=seg.dataset.pk, wk=seg.dataset.wk;
    host.querySelectorAll(`.weekstrip[data-pk="${pk}"] .weekseg`).forEach(s=>{
      s.classList.toggle("active",s===seg); s.setAttribute("aria-pressed", s===seg); });
    host.querySelectorAll(`.weekdetail[data-pk="${pk}"]`).forEach(d=>d.classList.toggle("active",d.dataset.wk===wk));
  }));
  // a completed session → load that day's run into the activity tile + its route map
  host.querySelectorAll(".sline.sclick").forEach(el=>el.addEventListener("click",()=>{
    loadActivity(+el.dataset.actId);
    scrollToEl(document.getElementById("recent"));
  }));
  // a double's per-run links → that specific run's map (stop bubbling so the line's own click doesn't fire)
  host.querySelectorAll(".brkrun[data-act-id]").forEach(el=>el.addEventListener("click",ev=>{
    ev.stopPropagation();
    loadActivity(+el.dataset.actId);
    scrollToEl(document.getElementById("recent"));
  }));
  wireObjActions();
  wireAdjust();
}
async function refreshPlan(p){
  OBJECTIVES = await getJSON("/api/objectives");
  _frSeen.obj=true; refreshFirstRun();   // first-run: an upcoming race added?
  try{ LOG = await getJSON("/api/log"); }catch(e){ LOG=null; }
  renderPlan(p || await getJSON("/api/plan"));
  loadDrift();   // the plan (or its history) may have moved — refresh the drift view
}
async function loadPlan(){ try{ await refreshPlan(); }catch(e){ tileFail($("#plan"), "Training plan", loadPlan, e);
  // refreshPlan() is what normally fires loadDrift(), so a failed plan load would leave #drift parked on
  // "Loading…" for the life of the page. Terminate it HERE rather than also calling loadDrift() at boot:
  // that fired a second /api/plandrift on every healthy load (the plan's heaviest read, twice per page).
  tileFail($("#drift"), "Plan drift", loadDrift, e); } }
$("#planBtn").addEventListener("click", async ()=>{
  const b=$("#planBtn"); b.disabled=true; const t=b.textContent; b.textContent="Generating…";
  try{
    const r=await fetch("/api/plan/generate",{method:"POST"}); const p=await r.json();
    if(!p.ok){ await notice({title:"Plan failed", intro:p.error||"unknown", tone:"danger"}); }
    else { LASTDIFF=p.diff; await refreshPlan(p); }
  }catch(e){ await notice({title:"Plan error", intro:String(e), tone:"danger"}); }
  finally{ b.disabled=false; b.textContent=t; }
});

// ── Chart axis labels ───────────────────────────────────────────────────────
// The trend charts are drawn with preserveAspectRatio="none" so the trace always fills its box.
// That stretch scales glyphs too: an SVG <text> at 9px inside a 1000-unit viewBox came out about
// 3px wide on a 340px phone — drawn, present in the DOM, and unreadable. The labels are HTML over
// the chart now, so the trace keeps its stretch and the type keeps its width.
//   `top` is the label's old SVG baseline in pixels — both charts are drawn 1:1 vertically (a
//   200-unit viewBox in a 200px box, 170 in 170), so a user unit IS a CSS pixel down the page.
//   `left` is a percentage, because across the box a user unit is whatever the width says it is.
// A label is {x, y, t}, plus `mid` (centre-anchored) and `tick` (a bottom x-tick, thinnable).
// Deliberately NOT aria-hidden: these are still the chart's only text, exactly as the <text> nodes
// were. What a screen reader should make of a chart is UX-9's question, not a layout fix's to answer.
function axisLayer(labels, W){
  return `<div class="axlbl">`+labels.map(l=>
    `<span class="ax${l.tick?" x":""}${l.mid?" mid":""}" `+
    `style="left:${(l.x/W*100).toFixed(3)}%;top:${l.y.toFixed(1)}px">${esc(l.t)}</span>`).join("")+
    `</div>`;
}
// Real type collides where 3px-wide type merely looked like dust: drop any tick that would run into
// the one before it. MEASURED rather than estimated from a character count — the face is whatever
// the theme's --mono resolves to on the device. A hidden host measures zero (a phone shows one tab
// at a time, and the section is a <details>) — leave those alone until the observer reports a real
// width, rather than thinning against a box of nothing.
//   This shipped with edge guards either side too, on the reasoning that a label hanging off a
//   full-width card would widen the page. Measured across 320/390/500/1280px at 3/6/12/24/30-month
//   spans, they NEVER fired once: month ticks are evenly spaced and centre-anchored, so the last one
//   always clears padR, and the collision rule had already removed anything that could reach an edge.
//   Both are gone — a branch that cannot be made to run, defended by a claim nothing demonstrated,
//   is worse than no branch at all.
function thinAxis(host){
  const layer = host && host.querySelector(".axlbl"); if(!layer) return;
  const box = layer.getBoundingClientRect(); if(!box.width) return;
  const xs = [...layer.querySelectorAll(".ax.x")];
  if(!xs.length) return;
  xs.forEach(el=>el.classList.remove("hide"));
  // every box READ before any class is WRITTEN: hiding a tick cannot move the others (they are all
  // absolutely positioned), so interleaving the two only bought a forced layout per label per frame
  const rects = xs.map(el=>el.getBoundingClientRect());
  let right = -1e9;
  xs.forEach((el,i)=>{
    const r = rects[i];
    if(r.left < right+6) el.classList.add("hide");
    else right = r.right;
  });
}
// One observer for every chart, keyed on the HOST. It fires on a window resize, a rotation, a mobile
// tab switch and a <details> toggle alike: all the same event as far as the labels are concerned.
// Toggling .hide touches only absolutely-positioned descendants, so it can't resize the host back.
// ⚠ "the host survives a re-render" is true of #ffchart and FALSE of the drift charts: loadDrift()
// rebuilds #drift's innerHTML, which destroys all five #drift-* divs. An observer holds a STRONG
// reference and a detached box never fires again, so each reload would strand the last set for the
// life of the page. Drop the disconnected ones before taking on a new one.
const AXOBS = window.ResizeObserver ? new ResizeObserver(es=>es.forEach(e=>thinAxis(e.target))) : null;
const AXSEEN = new Set();
function mountAxis(host){
  thinAxis(host);
  if(!AXOBS) return;
  for(const el of AXSEEN) if(!el.isConnected){ AXOBS.unobserve(el); AXSEEN.delete(el); }
  AXOBS.observe(host); AXSEEN.add(host);
}
// The mono face is a Google webfont loaded with display=swap: the first paint measures a FALLBACK,
// and the real face lands afterwards with different advances. Without this the thinning is decided
// against type the reader never sees, and only a resize would ever correct it.
if(document.fonts && document.fonts.ready) document.fonts.ready.then(()=>AXSEEN.forEach(thinAxis));

// ── Fitness & fatigue trend (projector) ─────────────────────────────────────
async function loadProjector(){
  let d;
  try{ d = await getJSON("/api/projector?days=180"); }
  catch(e){ tileFail($("#ffchart"), "Fitness & fatigue", loadProjector, e); return; }
  const h = d.history||[];
  const host = $("#ffchart");
  if(!h.length){ host.innerHTML = `<div class="empty">No activities synced yet.</div>`; return; }
  const W=1000, H=200, pad=24;
  const ctls=h.map(p=>p.ctl), atls=h.map(p=>p.atl);
  const hi=Math.max(...ctls,...atls), lo=Math.min(0,...ctls,...atls);
  const x=i=>pad+i*(W-2*pad)/(h.length-1);
  const y=v=>H-pad-(v-lo)/(hi-lo)*(H-2*pad);
  const path=key=>h.map((p,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  const labels=[{x:2, y:y(hi), t:hi.toFixed(0)}];
  let lastMonth="";
  h.forEach((p,i)=>{ const m=p.date.slice(0,7); if(m!==lastMonth){ lastMonth=m;
    labels.push({x:x(i), y:H-6, t:p.date.slice(2,7), tick:true}); }});
  host.innerHTML = `<svg class="ff" id="ffsvg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line class="zero" x1="${pad}" y1="${y(0).toFixed(1)}" x2="${W-pad}" y2="${y(0).toFixed(1)}"/>
    <path class="atl" d="${path('atl')}"/>
    <path class="ctl" d="${path('ctl')}"/>
    <g id="ffcross" style="display:none">
      <line class="cross" id="ffline" y1="0" y2="${H-14}"/>
      <circle class="ffdot ctl" id="ffdc" r="3.5"/><circle class="ffdot atl" id="ffda" r="3.5"/>
    </g>
  </svg>
  ${axisLayer(labels, W)}
  <div class="fftip" id="fftip" style="display:none"></div>`;
  mountAxis(host);
  const v=d.validation;
  if(v){
    const gap=Math.abs(v.modeled.atl-v.runalyze.atl);
    if(d.duplicate_count>0 && gap>8){
      $("#ffvalid").innerHTML =
        `<b style="color:var(--warn)">Heads-up:</b> your training history models to CTL <b>${v.modeled.ctl}</b> / `+
        `ATL <b>${v.modeled.atl}</b>, but Runalyze currently reports ${v.runalyze.ctl}/${v.runalyze.atl}. `+
        `That gap lines up with ${d.duplicate_count} detected duplicate(s) — Runalyze's figure looks inflated by them. `+
        `This chart uses the de-duplicated, history-consistent values; fix the duplicate on Runalyze to reconcile `+
        `(or 🗑 Delete from local copy if you already removed it on Runalyze).`;
    } else {
      $("#ffvalid").innerHTML =
        `Model validated against Runalyze — today: CTL <b>${v.modeled.ctl}</b> vs ${v.runalyze.ctl}, `+
        `ATL <b>${v.modeled.atl}</b> vs ${v.runalyze.atl} (τ ${v.tau_ctl}/${v.tau_atl}d). `+
        `Reconstructed so you have a full curve from one daily snapshot.`;
    }
  }
  // hover crosshair: map cursor x → nearest day, draw line + dots + tooltip
  const svg=$("#ffsvg"), g=$("#ffcross"), tip=$("#fftip");
  function onMove(e){
    const rect=svg.getBoundingClientRect();
    const px=(e.clientX-rect.left)/rect.width*W;
    let i=Math.round((px-pad)/((W-2*pad)/(h.length-1)));
    i=Math.max(0,Math.min(h.length-1,i)); const p=h[i];
    $("#ffline").setAttribute("x1",x(i)); $("#ffline").setAttribute("x2",x(i));
    $("#ffdc").setAttribute("cx",x(i)); $("#ffdc").setAttribute("cy",y(p.ctl));
    $("#ffda").setAttribute("cx",x(i)); $("#ffda").setAttribute("cy",y(p.atl));
    g.style.display="block";
    const dt=new Date(p.date+"T00:00");
    tip.style.display="block";
    tip.style.left=Math.min(rect.width-150,(e.clientX-rect.left)+12)+"px";
    tip.innerHTML=`<b>${dt.toLocaleDateString(undefined,{day:"numeric",month:"short",year:"2-digit"})}</b>`+
      `<span class="t-ctl">Fitness ${p.ctl.toFixed(1)}</span>`+
      `<span class="t-atl">Fatigue ${p.atl.toFixed(1)}</span>`+
      `<span class="muted">Form ${p.tsb.toFixed(1)} · ACWR ${p.acwr??"—"}</span>`;
  }
  svg.addEventListener("mousemove",onMove);
  svg.addEventListener("mouseleave",()=>{g.style.display="none";tip.style.display="none";});
}

// ── Plan drift — the original road vs the road as it stands (§6b, visible) ───
const ISO2T = iso => new Date(iso+"T00:00:00").getTime();
function driftLegend(lines){
  return `<div class="legend">`+lines.filter(l=>l.pts&&l.pts.length).map(l=>
    `<span style="color:${l.color}"><i style="${l.dash?'border-top-style:dashed':''}"></i>${l.label}</span>`
  ).join("")+`</div>`;
}
function mkChart(host, lines, {fmt=v=>v.toFixed(0), zeroBase=false, nowT=null, dots=false}={}){
  const all=lines.flatMap(l=>l.pts||[]);
  if(all.length<2){ host.innerHTML=`<div class="empty">Not enough history yet — the road will visibly move as results come in.</div>`; return; }
  const W=1000, H=170, padL=34, padR=14, padT=10, padB=18;
  const xs=all.map(p=>ISO2T(p.date)), x0=Math.min(...xs), x1=Math.max(...xs);
  let lo=Math.min(...all.map(p=>p.val)), hi=Math.max(...all.map(p=>p.val));
  if(zeroBase) lo=0;
  if(hi===lo) hi=lo+1;
  const m=(hi-lo)*0.08; hi+=m; if(!zeroBase) lo-=m;
  const X=t=>padL+(x1===x0?0.5:(t-x0)/(x1-x0))*(W-padL-padR);
  const Y=v=>H-padB-(v-lo)/(hi-lo)*(H-padT-padB);
  const dpath=pts=>pts.map((p,i)=>`${i?"L":"M"}${X(ISO2T(p.date)).toFixed(1)},${Y(p.val).toFixed(1)}`).join(" ");
  // month gridlines (SVG) + their labels (HTML, see axisLayer)
  const labels=[{x:2, y:Y(hi)+9, t:fmt(hi)}, {x:2, y:Y(lo), t:fmt(lo)}];
  let grid="", gd=new Date(x0); gd.setDate(1); gd.setHours(0,0,0,0);
  if(gd.getTime()<x0) gd.setMonth(gd.getMonth()+1);
  for(let i=0; gd.getTime()<=x1 && i<40; i++){
    const gx=X(gd.getTime());
    grid+=`<line class="grid" x1="${gx.toFixed(1)}" y1="2" x2="${gx.toFixed(1)}" y2="${H-padB}"/>`;
    labels.push({x:gx, y:H-5, t:gd.toLocaleDateString(undefined,{month:"short"}), mid:true, tick:true});
    gd.setMonth(gd.getMonth()+1);
  }
  const nowln = (nowT!=null && nowT>=x0 && nowT<=x1)
    ? `<line class="now" x1="${X(nowT).toFixed(1)}" y1="2" x2="${X(nowT).toFixed(1)}" y2="${H-padB}"/>` : "";
  const paths=lines.map(l=>(l.pts&&l.pts.length)
    ? `<path class="dl ${l.cls}" ${l.dash?'stroke-dasharray="5 4"':''} d="${dpath(l.pts)}"/>` : "").join("");
  // dots: everywhere when asked, plus any single-point series (an invisible zero-length path)
  const dotsm=lines.flatMap(l=>(dots||(l.pts||[]).length===1?(l.pts||[]):[]).map(p=>
    `<circle class="ddot" cx="${X(ISO2T(p.date)).toFixed(1)}" cy="${Y(p.val).toFixed(1)}" r="3" style="fill:${l.color}"/>`)).join("");
  host.innerHTML=`<div class="driftwrap"><svg class="drift" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    ${grid}
    ${nowln}${paths}${dotsm}
    <g class="dcross" style="display:none"><line class="cross" y1="2" y2="${H-padB}"/></g>
  </svg>${axisLayer(labels, W)}<div class="drifttip"></div></div>`;
  mountAxis(host);
  const svg=host.querySelector("svg"), g=host.querySelector(".dcross"),
        cl=g.querySelector("line"), tip=host.querySelector(".drifttip");
  const dates=[...new Set(all.map(p=>p.date))].sort();
  svg.addEventListener("mousemove",e=>{
    const rect=svg.getBoundingClientRect(), px=(e.clientX-rect.left)/rect.width*W;
    let best=dates[0], bd=1e18;
    for(const dt of dates){ const dx=Math.abs(X(ISO2T(dt))-px); if(dx<bd){bd=dx;best=dt;} }
    const gx=X(ISO2T(best)); cl.setAttribute("x1",gx); cl.setAttribute("x2",gx); g.style.display="block";
    const rows=lines.map(l=>{ const p=(l.pts||[]).find(q=>q.date===best);
      return p?`<span style="color:${l.color}">${l.label}: ${fmt(p.val)}</span>`:""; }).filter(Boolean).join("");
    const dt=new Date(best+"T00:00:00");
    tip.style.display="flex"; tip.style.left=Math.min(rect.width-160,(e.clientX-rect.left)+12)+"px";
    tip.innerHTML=`<b>${dt.toLocaleDateString(undefined,{day:"numeric",month:"short",year:"2-digit"})}</b>${rows}`;
  });
  svg.addEventListener("mouseleave",()=>{g.style.display="none";tip.style.display="none";});
}
// scorecard — the four series synthesized into one 'who's winning' read (§6j). Engine owns the
// numbers AND the templated headline; this only formats them. ahead→ok, behind→danger, level→muted.
function scoreRow(k, main, sub, cls){
  return `<div class="sc-row"><span class="sc-k">${esc(k)}</span>`+
    `<span class="sc-v ${cls}">${main}${sub?` <span class="sub">${sub}</span>`:""}</span></div>`;
}
function scorecardHTML(sc, r){
  if(!sc) return "";
  const sign=g=>(g>0?"+":"")+g;
  // §6s — once the race is run (settled) the scorecard RECKONS instead of projecting: fitness
  // arrived vs the plan's projection, and the finish vs the goal. The verdict headline carries the story.
  if(sc.reckoning){
    const rf=sc.reckoning.fitness, rr=sc.reckoning.result;
    const fitMain = rf.gap==null ? "—"
      : Math.abs(rf.gap)<=2 ? `CTL ${Math.round(rf.arrived)} · on target`
      : `CTL ${Math.round(rf.arrived)} · ${Math.abs(rf.gap)} ${rf.gap<0?"short":"above"}`;
    let resMain, resCls="level", resSub="";
    if(rr.status==="dnf"){ resMain=`${rr.dnf_km} km`; resCls="behind"; resSub="DNF"; }
    else if(!rr.found){ resMain="—"; resCls="unknown"; resSub="not synced yet"; }
    else if(rr.goal_seconds==null){ resMain=esc(rr.actual||"finished"); resSub="finished"; }
    else { resMain=`${esc(rr.goal)} → ${esc(rr.actual)}`; resCls=rr.beat?"ahead":"behind";
           resSub=rr.beat?"beat the goal":"missed the goal"; }
    const rp=sc.reckoning.prediction;   // §FT4 — the engine's own settled bet, when a scorable plan exists
    const predRow = rp ? scoreRow("Engine's call",
        rp.lo_hms?`${esc(rp.lo_hms)}–${esc(rp.hi_hms)}`:esc(rp.p50_hms||"—"),
        rp.in_band==null?`median ${esc(rp.p50_hms||"—")} · ${rp.err_pct>0?"+":""}${rp.err_pct}%`
                        :(rp.in_band?`inside the band · median ${rp.err_pct>0?"+":""}${rp.err_pct}%`
                                    :`outside the band · median ${rp.err_pct>0?"+":""}${rp.err_pct}%`),
        rp.in_band==null?"level":(rp.in_band?"ahead":"behind")) : "";
    return `<div class="scorecard"><div class="sc-head">How the race went</div>`+
      `<div class="sc-rows">`+
        scoreRow("Fitness arrived", fitMain, "vs the plan's projection", rf.state)+
        scoreRow(r&&r.label?r.label:"Result", resMain, resSub, resCls)+
        predRow+
      `</div><div class="sc-verdict">${esc(sc.headline)}</div></div>`;
  }
  const v=sc.volume, f=sc.fitness, rc=sc.race;
  const volMain = v.state==="unknown"?"—" : v.state==="level"?"on the road" : `${sign(v.gap)} km ${v.state}`;
  const fitMain = f.state==="unknown"?"—" : f.state==="level"?"CTL on track" : `CTL ${sign(f.gap)} ${f.state}`;
  let raceMain, raceCls, raceSub="";
  if(rc.now==null){ raceMain="—"; raceCls="unknown"; }
  else if(rc.caveat){ raceMain=`proj ${Math.round(rc.now)}`; raceCls="unknown"; raceSub="duplicate caveat"; }
  else { raceCls = rc.trend==="gaining"?"ahead":rc.trend==="slipping"?"behind":"level";
         raceMain = `${Math.round(rc.founding)} → ${Math.round(rc.now)}`; raceSub = rc.trend; }
  const sub = s => s.state==="unknown"?"":"vs founding road";
  const wks = sc.weeks_to_go!=null?`<span class="wks"> — ${sc.weeks_to_go}w to go</span>`:"";
  // §6q/#3 — multi-peak awareness: when the build chains ≥2 A-races, the single "Race day" row above
  // tracks only the final peak; this breaks out each peak's founding→now projection + its own trend
  // (and marks ones already run). Single-A omits it (sc.chain is null server-side).
  const chainHTML = (sc.chain && sc.chain.length>1)
    ? `<div class="sc-chain"><div class="sc-k" style="margin-bottom:5px">Each A-race peak (projected CTL)</div>`+
      sc.chain.map(c=>{
        const cls = c.passed ? "level"
          : c.trend==="gaining"?"ahead":c.trend==="slipping"?"behind":c.trend==="steady"?"level":"unknown";
        const proj = (c.founding!=null&&c.now!=null) ? `${Math.round(c.founding)} → ${Math.round(c.now)}`
          : (c.now!=null ? `proj ${Math.round(c.now)}` : "—");
        const tag = c.passed ? "run" : (c.trend!=="unknown"?c.trend:"");
        return `<div class="sc-crow"><span class="sc-cname">${esc(c.label||c.date)}</span>`+
          `<span class="sc-cwhen">${esc(c.date)}</span>`+
          `<span class="sc-cv ${cls}">${proj}${tag?` <span class="sub">${esc(tag)}</span>`:""}</span></div>`;
      }).join("")+`</div>`
    : "";
  return `<div class="scorecard"><div class="sc-head">The road vs the road as it stands</div>`+
    `<div class="sc-rows">`+
      scoreRow("Volume", volMain, sub(v), v.state)+
      scoreRow("Fitness", fitMain, sub(f), f.state)+
      scoreRow(r&&r.label?r.label:"Race day", raceMain, raceSub, raceCls)+
    `</div>${chainHTML}<div class="sc-verdict">${esc(sc.headline)}${wks}</div></div>`;
}
// §6m — effort discipline: HR-led "are your easy days actually easy?" Judged by heart rate (terrain
// & heat already live in HR), TE corroborates, GAP shown as terrain-fair pace.
const EFFP = s => s ? `${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}` : "—";
const EFFV = {on:["var(--ok)","on target"], hot:["var(--warn)","hot"],
              too_hard:["var(--danger)","too hard"], too_easy:["var(--muted)","too easy"],
              unknown:["var(--muted)","—"]};
async function loadEffort(){
  const host=$("#effort"); if(!host) return;
  let d; try{ d=await getJSON("/api/effort-discipline"); }catch(e){ tileFail(host, "Effort discipline", loadEffort, e); return; }
  if(!d || d.easy_score==null){
    host.innerHTML=`<div class="empty">Not enough runs in the last ${d&&d.window_days||21} days yet${d&&d.public?" to score easy-pace discipline.":" — this reads your synced runs' HR."}</div>`; return; }
  const c=d.easy_counts, score=d.easy_score;
  const tone = score>=80?"var(--ok)":score>=50?"var(--warn)":"var(--danger)";
  const verdict = score>=80?"dialed in":score>=50?"drifting hard":"easy days are threshold days";
  const scoreHint = qhint(
    `The share of your easy & long runs that actually stayed easy — judged on ${d.public?"grade-adjusted pace":"heart rate"}. `+
    `100% is the target: every easy day run easy. 80%+ reads as "dialed in", 50–79% "drifting", below 50% means easy days have crept up to threshold. `+
    `Hard sessions are meant to be hard, so they're never counted against this score.`);
  let rows, headCols, capLine, note;
  if(d.public){
    // sanitized public read — pace-judged, no HR / TE / feeling
    rows=(d.runs||[]).map(r=>{
      const [col,lbl]=EFFV[r.verdict]||EFFV.unknown;
      return `<tr><td class="mono">${esc(r.date.slice(5))}</td><td>${esc(r.kind)}</td>`+
        `<td class="mono">${r.km}k</td><td class="mono">${EFFP(r.gap_pace)}</td>`+
        `<td style="color:${col};font-weight:600">${lbl}</td></tr>`;
    }).join("");
    headCols=`<th>date</th><th>session</th><th>dist</th>`+
      `<th>GAP ${qhint("Grade Adjusted Pace — pace corrected for hills so efforts compare fairly across terrain (from Runalyze).")}</th>`+
      `<th>vs easy ${qhint("Whether the grade-adjusted pace stayed at or below the easy-pace ceiling. The public read judges on pace; heart rate stays private.")}</th>`;
    capLine=`Last ${d.window_days} days · <b>${c.on}/${c.judged}</b> easy runs stayed at easy pace (≤ <b>${esc(d.easy_pace_ceiling||"—")}</b>/km, grade-adjusted).${c.too_hard?` <b style="color:var(--danger)">${c.too_hard}</b> ran quicker than easy.`:""}`;
    note=`Judged on grade-adjusted pace, not heart rate (HR stays private). Pace is a rough proxy — a run that looks easy on pace can still have been hard on heart rate (heat, hills, fatigue it can't see), so a verdict here can differ from the owner's HR-led console on the same run.`;
  }else{
    // private read — full HR-led detail
    rows=(d.runs||[]).map(r=>{
      const [col,lbl]=EFFV[r.verdict]||EFFV.unknown;
      // "low" confidence is the VERDICT's own trust level (a structured session's whole-run avg HR
      // blends reps with recovery), NOT the runner's compliance — say so, don't abbreviate to ambiguity
      const tag = r.seg_read
        ? ` <span class="muted" style="font-weight:400;border-bottom:1px dotted var(--muted);cursor:help" title="Graded on the DETECTED work reps only (§RD) — warm-up, floats and cool-down excluded${r.seg?`: ${r.seg.n_work} rep${r.seg.n_work>1?"s":""}${r.seg.work_hr?` @ ~${r.seg.work_hr} bpm`:` @ ${r.seg.work_pace}/km`} vs the ${r.seg.zone} band`:""}.">·reps read</span>`
        : (r.confidence==="low"?` <span class="muted" style="font-weight:400;border-bottom:1px dotted var(--muted);cursor:help" title="Low confidence in this VERDICT — not in your effort: a structured session's whole-run average heart rate blends work reps with warm-up, recovery jogs and cool-down, so the 'did you hit it' read is approximate (once this run's structure is detected, the verdict upgrades to a per-rep read). Quality sessions never count toward the easy-run discipline score.">·rough read</span>`:"");
      return `<tr><td class="mono">${esc(r.date.slice(5))}</td><td>${esc(r.kind)}</td>`+
        `<td class="mono">${r.km}k</td><td class="mono col-sec">${EFFP(r.gap_pace)}</td>`+
        `<td class="mono">${r.hr_avg}<span class="muted hrpct"> ${r.hr_pct}%</span></td>`+
        `<td class="mono col-sec">${r.te==null?"—":r.te}</td>`+
        `<td class="mono col-sec">${r.feeling==null?"—":r.feeling+"/5"}</td>`+
        `<td style="color:${col};font-weight:600">${lbl}${tag}</td></tr>`;
    }).join("");
    headCols=`<th>date</th><th>session</th><th>dist</th>`+
      `<th class="col-sec">GAP ${qhint("Grade Adjusted Pace — your pace corrected for hills so efforts compare fairly across terrain (from Runalyze). Runs here are judged on heart rate, not this.")}</th>`+
      `<th>avg HR</th>`+
      `<th class="col-sec">TE ${qhint("Training Effect — Runalyze/Firstbeat's 1–5 aerobic-stress rating (intensity × duration). It only corroborates the heart-rate read here, it never overrides it.")}</th>`+
      `<th class="col-sec">feel</th>`+
      `<th>verdict ${qhint("How this run's effort compared to its prescription — graded by heart rate (terrain and heat already live in your HR), not pace. A '·reps read' tag means a quality session was graded on its DETECTED work reps only (warm-up, floats and cool-down excluded — the sharper §RD read); '·rough read' means the whole-run average HR had to stand in (reps blend with recovery), never a judgment of your compliance. Quality sessions are excluded from the easy-discipline score either way.")}</th>`;
    const basis = d.anchor==="lthr" ? `(85% of LTHR ${d.lthr})`
                : d.anchor==="lt1_pace" ? `(pace-anchored LT1)` : `(78% of HRmax ${d.hrmax})`;
    const ceil = d.anchor==="lt1_pace"
      ? `easy-pace ceiling ≈ <b>${esc(d.easy_pace_ceiling||"—")}</b>/km ${basis}`
      : `easy-HR ceiling ≈ <b>${d.easy_hr_ceiling}</b> bpm ${basis}`;
    capLine=`Last ${d.window_days} days · <b>${c.on}/${c.judged}</b> easy runs stayed aerobic · ${ceil}.${c.too_hard?` <b style="color:var(--danger)">${c.too_hard}</b> ran at threshold effort.`:""}`;
    note=`Judged by heart rate, not pace — terrain &amp; heat already live in your HR. Pace shown is grade-adjusted (GAP). Each run is matched to its nearest prescribed session within a couple of days, so an anticipated or postponed session is judged against its real prescription, not the day it landed on; a run with no session in range falls back to the easy default. One mismatch is an observation, not a verdict.`;
  }
  let cohLine="";   // pace↔HR coherence — private diagnostic; surfaces the two-model divergence, never the plan
  if(!d.public){
    try{ const co=await getJSON("/api/pace-hr-coherence");
      if(co && co.ok && co.verdict!=="insufficient"){
        const bad = co.verdict==="pace_ahead_of_hr";
        cohLine=`<div class="muted" style="font-size:11px;margin-top:4px${bad?";color:var(--warn)":""}">`+
          `Pace↔HR ${qhint("Your plan prescribes effort by PACE (from VO₂max); this monitor judges it by HEART RATE (from your threshold HR). They should agree — running at the easy-pace ceiling should keep HR under the easy-HR ceiling. They drift apart most when you're detrained (a given easy pace runs hot on HR). This is a read-only check; it never changes your plan.")}: ${esc(co.note)}</div>`;
      }
    }catch(e){}
  }
  // §3.4 — the fitness-tracking LT1 (aerobic-threshold pace = the easy bar). Private (bundles the HR cross-check).
  let lt1Line="";
  const L=d.lt1;
  if(!d.public && L && L.ok){
    const manualBadge=(L.hr&&L.hr.source==="manual")?` <span class="muted">(manual LTHR${L.hr.age_days!=null?`, set ${Math.round(L.hr.age_days/7)}wk ago`:""})</span>`:"";
    lt1Line=`<div class="muted" style="font-size:11px;margin-top:4px${L.detrained?";color:var(--warn)":""}">`+
      `LT1 ${qhint("Your aerobic-threshold pace, which we set at ≈80% of your 5k pace (a pace-first anchor informed by John Davis) — the ceiling your easy days should stay under. It's derived from your CURRENT fitness, so it moves as you get fitter or detrain, and heart rate is the cross-check.")}: easy ceiling ≈ <b>${esc(L.lt1_pace_fmt)}</b>/km`+
      `${(L.hr&&L.hr.easy_ceiling)?` · HR cross-check ≤ <b>${L.hr.easy_ceiling}</b> bpm${manualBadge}`:""}.`+
      `${L.detrained?" You're running easy a touch faster than LT1 for the heart rate it costs right now — normal on a rebuild, it self-corrects, so we don't police easy pace here.":""}</div>`;
    // §HR slice #2 — the 30-min-TT suggestion, shown ONLY when every clearance holds (assertive regime,
    // green readiness, no medical hold, anchor actually improvable). The gate lives server-side.
    if(L.tt_offer && L.tt_offer.offer){
      lt1Line+=`<div class="muted" style="font-size:11px;margin-top:4px">Your threshold anchor is `+
        `${esc(L.tt_offer.confidence)}-confidence and you're cleared to push ${qhint("A 30-minute solo time trial sharpens your LTHR: run 30 min all-out alone on flat ground; your average HR over the FINAL 20 minutes is your LTHR. Enter it in Settings → Manual LTHR. The app only suggests this when your plan is assertive, readiness is green and there's no medical hold — never during a rebuild. Details in the manual (Settings section).")} — `+
        `a 30-min field test would sharpen it (see the manual).</div>`;
    }
  }
  host.innerHTML=`
    <div class="effort-head">
      <div class="effort-score" style="color:${tone}">${score}<span class="pct">%</span></div>
      <div class="effort-cap">
        <div class="big">Easy discipline ${scoreHint} — <b style="color:${tone}">${verdict}</b></div>
        <div class="muted">${capLine}</div>
        <div class="muted" style="font-size:11px;margin-top:4px">${note}</div>
        ${lt1Line}
        ${cohLine}
      </div>
    </div>
    <div class="efftbl-wrap"><table class="efftbl"><thead><tr>${headCols}</tr></thead><tbody>${rows}</tbody></table></div>${d.public?"":'<div class="efftbl-rot">↻ Rotate to landscape for pace, training effect &amp; feel</div>'}`;
}
// §TR — the scorecard the engine keeps on ITSELF. Deliberately the same on both boxes, minus the
// finish TIMES: the public payload carries whether a band contained the race, never what was run
// (a race result is withheld — publishing the prediction beside its error hands it back).
async function loadTrack(){
  const host=$("#track"); if(!host) return;
  let d; try{ d = await getJSON("/api/track-record"); }
  catch(e){ tileFail(host, "Track record", loadTrack, e); return; }
  const c=d.ctl||{}, races=d.races||[], sm=d.summary||{};
  if(!c.n && !races.length){
    host.innerHTML=`<div class="empty">Nothing has settled yet. A weekly fitness forecast is scored
      once it is ${sm.lead_days||28} days old — old enough that scoring it isn't hindsight — and a race
      band is scored when the race is run.</div>`;
    return;
  }
  const sign=v=> (v>0?"+":"")+v;
  const ctlLine = c.n ? `<div class="trrow"><b>${c.n}</b> weekly fitness checkpoint${c.n===1?"":"s"} ·
      average miss <b>${c.mae}</b> CTL point${c.mae===1?"":"s"} ·
      bias <b>${sign(c.bias)}</b> <span class="muted">(${c.bias>0?"came out fitter than projected":c.bias<0?"came out short of projection":"even"})</span>
      ${c.close_rate!=null?` · <b>${Math.round(c.close_rate*100)}%</b> landed within ${c.close_within}`:""}</div>` : "";
  const bandLine = sm.banded ? `<div class="trrow"><b>${sm.in_band}</b> of <b>${sm.banded}</b> finish
      band${sm.banded===1?"":"s"} contained the race</div>` : "";
  const rows = races.map(r=>`<tr><td>${esc(r.label||r.type||"race")}</td><td class="mono">${esc(r.date||"")}</td>
      <td>${r.kind==="race_t8"?`T−${Math.round((r.horizon_days||0)/7)} weeks`:"final call"}</td>
      <td class="${r.in_band?"ok":"bad"}">${r.in_band==null?"—":r.in_band?"inside the band":"outside"}</td>
      ${r.err_pct!=null?`<td class="mono">${sign(r.err_pct)}%</td>`:"<td></td>"}</tr>`).join("");
  host.innerHTML = ctlLine + bandLine + (rows?`<table class="trtbl"><thead><tr><th>Race</th><th>Date</th>
      <th>Scored at</th><th>Band</th><th>Error</th></tr></thead><tbody>${rows}</tbody></table>`:"")
    + `<div class="help">Every row was written when the outcome settled and is never rewritten — a
       forecast that can be re-scored after the fact is not a forecast.</div>`;
}

async function loadDrift(){
  const host=$("#drift"); if(!host) return;
  let d; try{ d=await getJSON("/api/plandrift"); }catch(e){ tileFail(host, "Plan drift", loadDrift, e); return; }
  if(!d || !d.ok){ host.innerHTML=`<div class="empty">${(d&&d.error)||"No plan history yet."}</div>`; return; }
  const MUTED="var(--muted)", ACC="var(--accent)";
  // 1 — cumulative distance
  const dc=d.distance.current||[];
  const act=dc.filter(p=>p.kind==="actual").map(p=>({date:p.date,val:p.cum}));
  const prj=dc.filter(p=>p.kind==="proj").map(p=>({date:p.date,val:p.cum}));
  const distLines=[
    {pts:(d.distance.initial||[]).map(p=>({date:p.date,val:p.cum})), cls:"init", color:MUTED, label:"Original road"},
    {pts:act, cls:"actual", color:ACC, label:"Run so far"},
    {pts:act.length?[act[act.length-1],...prj]:prj, cls:"proj", dash:true, color:ACC, label:"Projected"},
  ];
  // 2 — weekly training load (effort / intensity)
  const effLines=[
    {pts:(d.effort.initial||[]).map(p=>({date:p.date,val:p.trimp})), cls:"init", color:MUTED, label:"Original load"},
    {pts:(d.effort.actual||[]).map(p=>({date:p.date,val:p.trimp})), cls:"actual", color:ACC, label:"Done so far"},
    {pts:(d.effort.current||[]).map(p=>({date:p.date,val:p.trimp})), cls:"proj", dash:true, color:ACC, label:"Prescribed now"},
  ];
  // 3 — fitness trajectory (CTL)
  const ctlLines=[
    {pts:(d.ctl.initial||[]).map(p=>({date:p.date,val:p.ctl})), cls:"init", color:MUTED, label:"Original projection"},
    {pts:(d.ctl.actual||[]).map(p=>({date:p.date,val:p.ctl})), cls:"actual", color:ACC, label:"Actual fitness"},
    {pts:(d.ctl.current||[]).map(p=>({date:p.date,val:p.ctl})), cls:"proj", dash:true, color:ACC, label:"Projected now"},
  ];
  // 4 — projected race-day fitness, version over version
  const outLines=[{pts:(d.outcome||[]).map(p=>({date:p.date,val:p.ctl})), cls:"actual", color:ACC, label:"Projected race CTL"}];
  // 5 — §FT4 the prediction ledger: predicted finish per regen, P50 + the 80% band envelope
  const fd=d.finish_drift||[];
  const fmtT=v=>{const tm=Math.round(v/60);return Math.floor(tm/60)+":"+String(tm%60).padStart(2,"0")};
  const finLines=[
    {pts:fd.filter(p=>p.hi!=null).map(p=>({date:p.date,val:p.hi})), cls:"init", dash:true, color:MUTED, label:"80% band"},
    {pts:fd.filter(p=>p.lo!=null).map(p=>({date:p.date,val:p.lo})), cls:"init", dash:true, color:MUTED, label:"80% band"},
    {pts:fd.map(p=>({date:p.date,val:p.p50})), cls:"actual", color:ACC, label:"Predicted finish (median)"},
  ];
  const finLegend=[finLines[2],finLines[0]];
  const a=d.anchor, r=d.race;
  let cap=`Baseline: plan of <b style="color:var(--text)">${a.for_date}</b>`+
    (a.is_current?` — just sealed (the first complete road); drift accrues from here`:"")+
    ` · ${a.versions} version${a.versions>1?"s":""} on record`+
    (r.label?` · ${esc(r.label)}${r.weeks_away!=null?` (${r.weeks_away}w out)`:""}`:"");
  if(d.duplicate_count>0) cap+=`<br><span class="warn">⚠ ${d.duplicate_count} duplicate activity is inflating the snapshot the plan seeds from — fix it in Runalyze to clean the projection (actuals here already ignore it; if you already removed it on Runalyze, use 🗑 Delete from local copy to drop the leftover row).</span>`;
  host.innerHTML=`${scorecardHTML(d.scorecard, r)}<div class="driftcap">${cap}</div>
    <div class="driftseg" role="group" aria-label="Drift comparison">
      <button type="button" data-dm="founding" class="on" aria-pressed="true" title="Your original plan for this goal vs where it stands now — how the road has moved over time">Original → Now</button>
      <button type="button" data-dm="compare" aria-pressed="false" title="Your current road vs the road the other regime would prescribe from today — the assertive build your data could unlock, or the conservative floor the engine would fall back to">Conservative vs Assertive</button></div>
    <div id="drift-caveat" class="note driftcaveat" style="display:none"></div>
    <div class="driftblock"><h3>Cumulative distance</h3>
      <p class="note">The founding road versus what you've actually run plus the current plan's projection forward. The widening gap is volume drift — ahead of, or behind, the original plan.</p>
      <span id="lg-dist">${driftLegend(distLines)}</span><div id="drift-dist"></div></div>
    <div class="driftblock"><h3>Weekly training load · effort</h3>
      <p class="note">The intensity half of the road: each week's prescribed load (TRIMP), original versus now, against what you actually did. When the plan eases — sickness, missed weeks, low readiness — this line drops below the founding plan; when results earn it, it rises.</p>
      <span id="lg-eff">${driftLegend(effLines)}</span><div id="drift-eff"></div></div>
    <div class="driftblock"><h3>Fitness trajectory · CTL</h3>
      <p class="note">What the original plan projected your fitness would do, against your real reconstructed CTL continued by today's projection. The engine's true currency — distance is the volume view, this is the fitness view.</p>
      <span id="lg-ctl">${driftLegend(ctlLines)}</span><div id="drift-ctl"></div></div>
    <div class="driftblock"><h3 id="h-out">Projected race-day fitness</h3>
      <p class="note" id="note-out">The race-day CTL the engine projected at each re-plan. Is your goal race getting more or less reachable as your results come in?</p>
      <span id="lg-out">${driftLegend(outLines)}</span><div id="drift-out"></div></div>
    ${fd.length?`<div class="driftblock"><h3>Predicted finish · the ledger</h3>
      <p class="note">The finish the engine predicted at each re-plan — the product watching itself. Lower is faster; the dashed envelope is the 80% band (it starts where plans began carrying one) and should narrow as the race nears. Steps in the line are model upgrades or your shape moving, both honest.</p>
      <span id="lg-fin">${driftLegend(finLegend)}</span><div id="drift-fin"></div></div>`:""}`;
  const setLg=(id,lines)=>{const e=$(id); if(e) e.innerHTML=driftLegend(lines);};
  const FOUND_OUT_NOTE="The race-day CTL the engine projected at each re-plan. Is your goal race getting more or less reachable as your results come in?";
  const drawFounding=()=>{
    setLg("#lg-dist",distLines); setLg("#lg-eff",effLines); setLg("#lg-ctl",ctlLines); setLg("#lg-out",outLines);
    if(fd.length) setLg("#lg-fin",finLegend);
    const nout=$("#note-out"); if(nout) nout.textContent=FOUND_OUT_NOTE;
    const hout=$("#h-out"); if(hout) hout.textContent="Projected race-day fitness";
    const ob=$("#drift-out"); if(ob) ob.innerHTML="";
    mkChart($("#drift-dist"), distLines, {zeroBase:true, fmt:v=>v.toFixed(0)+" km", nowT:ISO2T(d.today)});
    mkChart($("#drift-eff"), effLines, {zeroBase:true, fmt:v=>v.toFixed(0), nowT:ISO2T(d.today)});
    mkChart($("#drift-ctl"), ctlLines, {fmt:v=>v.toFixed(0), nowT:ISO2T(d.today)});
    mkChart($("#drift-out"), outLines, {fmt:v=>v.toFixed(0), dots:true});
    if(fd.length) mkChart($("#drift-fin"), finLines, {fmt:fmtT, dots:true});
    const cav=$("#drift-caveat"); if(cav) cav.style.display="none";
  };
  let cfData=null, cfTried=false;
  const REGNAME={caution:"Conservative",assertive:"Assertive"};   // one word per regime, matches the plan badge
  const rn=s=>REGNAME[s]||(s?s.charAt(0).toUpperCase()+s.slice(1):s);
  const drawCompare=async()=>{
    if(!cfTried){ cfTried=true; try{ cfData=(await getJSON("/api/plandrift?compare=1")).counterfactual; }catch(e){} }
    const cf=cfData, cav=$("#drift-caveat");
    if(!cf){ if(cav){ cav.textContent="Comparison needs an objective set."; cav.style.display=""; } return; }
    const ACC2="var(--accent2, var(--accent))", ACC="var(--accent)";
    const lbl=rn(cf.regime), nlbl=rn(cf.vs);
    // direction matters (Duarte's 2026-07-04 catch): from a CONSERVATIVE plan the assertive road is
    // the upper envelope you can EARN; from an ASSERTIVE plan the conservative road is the FLOOR the
    // engine would hold you to if the gate re-closed — "what earning it unlocks" reads as nonsense there.
    const upside=cf.regime==="assertive";
    const aEff=(d.effort.actual||[]).map(p=>({date:p.date,val:p.trimp})), aCtl=(d.ctl.actual||[]).map(p=>({date:p.date,val:p.ctl}));
    const cfDist=(cf.distance||[]).map(p=>({date:p.date,val:p.cum}));
    const dl=[
      {pts:act, cls:"actual", color:ACC, label:"Run so far"},
      {pts:act.length?[act[act.length-1],...prj]:prj, cls:"proj", dash:true, color:ACC, label:nlbl+" — your road now"},
      {pts:act.length&&cfDist.length?[act[act.length-1],...cfDist]:cfDist, cls:"cf", dash:true, color:ACC2, label:lbl+(upside?" — what earning it unlocks":" — the floor if the gate re-closes")},
    ];
    const el=[
      {pts:aEff, cls:"actual", color:ACC, label:"Done so far"},
      {pts:(d.effort.current||[]).map(p=>({date:p.date,val:p.trimp})), cls:"proj", dash:true, color:ACC, label:nlbl+" — now"},
      {pts:(cf.effort||[]).map(p=>({date:p.date,val:p.trimp})), cls:"cf", dash:true, color:ACC2, label:lbl},
    ];
    const cl=[
      {pts:aCtl, cls:"actual", color:ACC, label:"Actual fitness"},
      {pts:(d.ctl.current||[]).map(p=>({date:p.date,val:p.ctl})), cls:"proj", dash:true, color:ACC, label:nlbl+" — now"},
      {pts:(cf.ctl||[]).map(p=>({date:p.date,val:p.ctl})), cls:"cf", dash:true, color:ACC2, label:lbl},
    ];
    setLg("#lg-dist",dl); setLg("#lg-eff",el); setLg("#lg-ctl",cl); setLg("#lg-out",[]);
    const fb=$("#drift-fin"); if(fb) fb.innerHTML=""; setLg("#lg-fin",[]);   // §FT4 ledger is founding-mode only
    mkChart($("#drift-dist"), dl, {zeroBase:true, fmt:v=>v.toFixed(0)+" km", nowT:ISO2T(d.today)});
    mkChart($("#drift-eff"), el, {zeroBase:true, fmt:v=>v.toFixed(0), nowT:ISO2T(d.today)});
    mkChart($("#drift-ctl"), cl, {fmt:v=>v.toFixed(0), nowT:ISO2T(d.today)});
    const o=cf.outcome||{};
    const hout=$("#h-out"); if(hout) hout.textContent="Projected race outcome";
    const nout=$("#note-out"); if(nout) nout.textContent="Race-day fitness and projected finish: the road you're on vs the road the other regime would give.";
    const ob=$("#drift-out");
    if(ob) ob.innerHTML=`<div class="cfout">`+
      `<div><span class="k">${nlbl} now</span> — peak CTL <b>${o.now_peak_ctl==null?"–":o.now_peak_ctl}</b>, finish <b>${o.now_finish||"–"}</b></div>`+
      `<div><span class="k" style="color:var(--accent2, var(--accent))">${lbl}</span> — peak CTL <b>${o.peak_ctl==null?"–":o.peak_ctl}</b>, finish <b>${o.finish||"–"}</b></div>`+
      (o.curve&&o.curve.length?`<div class="note">With more runway: ${o.curve.map(c=>"+"+c.plus_weeks+"w → "+c.hms).join(" · ")}</div>`:"")+
      `</div>`;
    if(cav){ cav.innerHTML=upside
      ? `<b>What earning ${esc(lbl.toLowerCase())} unlocks.</b> Upper envelope — assumes you clear the gate${cf.reason?" ("+esc(cf.reason)+")":""} and absorb the load; the engine re-reads it every block.`
      : `<b>What ${esc(nlbl.toLowerCase())} is buying you.</b> The ${esc(lbl.toLowerCase())} line is the floor the engine would hold you to if the gate re-closed — low readiness, symptoms or unabsorbed weeks can re-close it${cf.reason?" ("+esc(cf.reason)+")":""}; the engine re-reads it every block.`;
      cav.style.display=""; }
  };
  const paint=(mode)=>{
    host.querySelectorAll(".driftseg button").forEach(b=>{
      b.classList.toggle("on", b.dataset.dm===mode); b.setAttribute("aria-pressed", b.dataset.dm===mode); });
    if(mode==="compare") drawCompare(); else drawFounding();
  };
  host.querySelectorAll(".driftseg button").forEach(b=>b.addEventListener("click",()=>paint(b.dataset.dm)));
  drawFounding();
}

// ── Health markers ──────────────────────────────────────────────────────────
let MARKERS = {};
let HSERIES = {};   // marker -> full ascending series, kept so range toggles re-render locally
// §HS — per-marker freshness from the server. A chart whose feed died does not LOOK dead: it just
// stops, and the last point reads like today's. This is what says otherwise.
let HSTALE = {markers:{}, summary:null};
// per-marker window choice, remembered across visits. "all" | "365" | "180" (days)
const HRANGE = (()=>{ try{ return JSON.parse(localStorage.getItem("hrange"))||{}; }catch(e){ return {}; } })();
const HRANGES = [["180","6m"],["365","1y"],["all","All"]];
function sparkline(points, ref){
  // points: [{date, value}] ascending. ref:[low,high] (either may be null).
  // Returns the markup plus the x/y mappers so the crosshair can snap to real readings.
  const W=240, H=54, pad=6;
  const vals = points.map(p=>p.value);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  [ref[0], ref[1]].forEach(v=>{ if(v!=null){ lo=Math.min(lo,v); hi=Math.max(hi,v); }});
  if(hi===lo) hi=lo+1;
  const x = i => pad + (points.length<2 ? (W-2*pad)/2 : i*(W-2*pad)/(points.length-1));
  const y = v => H-pad - (v-lo)/(hi-lo)*(H-2*pad);
  let band="";
  if(ref[0]!=null || ref[1]!=null){
    const top = ref[1]!=null ? y(ref[1]) : pad;
    const bot = ref[0]!=null ? y(ref[0]) : H-pad;
    band = `<rect class="refband" x="0" y="${Math.min(top,bot).toFixed(1)}" width="${W}" height="${Math.abs(bot-top).toFixed(1)}"/>`;
    if(ref[1]!=null) band += `<line class="refline" x1="0" y1="${y(ref[1]).toFixed(1)}" x2="${W}" y2="${y(ref[1]).toFixed(1)}"/>`;
    if(ref[0]!=null) band += `<line class="refline" x1="0" y1="${y(ref[0]).toFixed(1)}" x2="${W}" y2="${y(ref[0]).toFixed(1)}"/>`;
  }
  const d = points.map((p,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const last = points[points.length-1];
  const dot = `<circle class="sparkdot" cx="${x(points.length-1).toFixed(1)}" cy="${y(last.value).toFixed(1)}" r="3"/>`;
  const svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${band}<path class="spark" d="${d}"/>${dot}`+
    `<g class="hcross" style="display:none"><line class="hcrossln" y1="${pad}" y2="${H-pad}"/><circle class="hcrossdot" r="3"/></g></svg>`;
  return {svg, x, y, W};
}
function inRange(v, ref){ return (ref[0]==null||v>=ref[0]) && (ref[1]==null||v<=ref[1]); }
// A range toggle only earns its place when there is history to fold away:
// more than 6 months of span AND more than 50 readings.
function hrangeEligible(pts){
  return pts.length>50 && (ISO2T(pts[pts.length-1].date)-ISO2T(pts[0].date)) > 183*86400000;
}
function hslice(pts, r){
  if(r==="all") return pts;
  // Anchor the window to the LAST READING, not today: a series that stopped (e.g. a marker the
  // watch no longer reports) still has a meaningful "last 6m/1y of data" — cutting from today
  // made every window empty and silently fell back to the full span with the button still lit.
  const cut = new Date(ISO2T(pts[pts.length-1].date)); cut.setDate(cut.getDate()-(+r));
  const iso = cut.toISOString().slice(0,10);
  const out = pts.filter(p=>p.date>=iso);
  return out.length>=2 ? out : pts;   // window nearly empty → fall back to everything
}
// §HS — one banner above the cards when a nightly feed has gone quiet. Named feeds and a date, not
// a vague "some data is old": the 2026-08-15 stall was invisible for eight days precisely because
// every chart still drew a full line. It says nothing at all when everything is current.
function staleBannerHTML(){
  const st = HSTALE.summary;
  if(!st) return "";
  const names = st.markers.map(k=>(MARKERS[k]||{}).label||k).join(", ");
  const d = st.days;
  return `<div class="dqwarn" role="status">⚠ <b>Nightly data has stopped.</b> Nothing since
    ${esc(st.last)} — ${d} day${d===1?"":"s"} ago — for: ${esc(names)}. The lab markers below are
    unaffected. Check that the watch has synced and that wellness sharing is still on in its app.</div>`;
}
function hcardHTML(k){
  const m = MARKERS[k]||{label:k,unit:"",ref:[null,null],good:"band"};
  const full = HSERIES[k];
  const eligible = hrangeEligible(full);
  const r = eligible ? (HRANGE[k]||"all") : "all";
  const pts = eligible ? hslice(full, r) : full;
  const last = full[full.length-1];   // headline = latest reading, whatever the window
  const ok = m.good==="band" ? inRange(last.value,m.ref)
           : m.good==="low"  ? (m.ref[1]==null||last.value<=m.ref[1])
           :                    (m.ref[0]==null||last.value>=m.ref[0]);
  const refTxt = m.ref[0]!=null&&m.ref[1]!=null ? `${m.ref[0]}–${m.ref[1]}`
               : m.ref[1]!=null ? `&lt; ${m.ref[1]}` : m.ref[0]!=null ? `&gt; ${m.ref[0]}` : "";
  const range = eligible
    ? `<div class="hrange" role="group" aria-label="${esc(m.label)} range">${HRANGES.map(([v,l])=>
        `<button type="button" data-r="${v}" class="${r===v?"on":""}" aria-pressed="${r===v}">${l}</button>`).join("")}</div>`
    : "";
  const counts = pts.length===full.length ? `${full.length} readings`
               : `${pts.length} of ${full.length} readings`;
  const fresh = HSTALE.markers[k]||{};      // §HS — age on the card that owns the dead feed
  const age = fresh.stale ? ` · <span class="stalen">last reading ${fresh.days} days ago</span>` : "";
  return `<div class="hcard" data-k="${esc(k)}">
    <div class="hhead"><div class="hk">${m.label}</div>${range}</div>
    <div class="hv">${fmt(last.value, last.value%1?1:0)}<small> ${m.unit}</small>
      ${refTxt?`<span class="flag ${ok?"ok":"bad"}">${ok?"ok":"watch"} ${refTxt}</span>`:""}</div>
    <div class="hchart">${sparkline(pts, m.ref).svg}<div class="htip"></div></div>
    <div class="hk" style="margin-top:6px;text-transform:none;letter-spacing:0">
      ${counts} · ${pts[0].date} → ${last.date}${age}</div>
  </div>`;
}
function wireHCard(card){
  const k = card.dataset.k;
  const m = MARKERS[k]||{label:k,unit:"",ref:[null,null],good:"band"};
  const full = HSERIES[k];
  const pts = hrangeEligible(full) ? hslice(full, HRANGE[k]||"all") : full;
  const sp = sparkline(pts, m.ref);   // recompute mappers for the slice actually drawn
  card.querySelectorAll(".hrange button").forEach(b=>b.addEventListener("click",()=>{
    HRANGE[k]=b.dataset.r;
    try{ localStorage.setItem("hrange", JSON.stringify(HRANGE)); }catch(e){}
    const tmp=document.createElement("div"); tmp.innerHTML=hcardHTML(k);
    const nu=tmp.firstElementChild; card.replaceWith(nu); wireHCard(nu);
  }));
  // crosshair — snap to the nearest reading, show its date + value
  const svg=card.querySelector(".hchart svg"), g=svg.querySelector(".hcross"),
        ln=g.querySelector("line"), cd=g.querySelector("circle"),
        wrap=card.querySelector(".hchart"), tip=card.querySelector(".htip");
  svg.addEventListener("mousemove", e=>{
    const rect=svg.getBoundingClientRect();
    const px=(e.clientX-rect.left)/rect.width*sp.W;
    let best=0, bd=1e18;
    for(let i=0;i<pts.length;i++){ const dx=Math.abs(sp.x(i)-px); if(dx<bd){bd=dx;best=i;} }
    const p=pts[best], gx=sp.x(best).toFixed(1);
    ln.setAttribute("x1",gx); ln.setAttribute("x2",gx);
    cd.setAttribute("cx",gx); cd.setAttribute("cy",sp.y(p.value).toFixed(1));
    g.style.display="block";
    const dt=new Date(p.date+"T00:00:00");
    tip.innerHTML=`<b>${dt.toLocaleDateString(undefined,{day:"numeric",month:"short",year:"2-digit"})}</b>`+
      `${fmt(p.value, p.value%1?1:0)} ${m.unit}`;
    tip.style.display="block";
    const wr=wrap.getBoundingClientRect();
    tip.style.left=Math.max(0, Math.min(wr.width-tip.offsetWidth, e.clientX-wr.left+10))+"px";
  });
  svg.addEventListener("mouseleave", ()=>{ g.style.display="none"; tip.style.display="none"; });
}

async function loadHealth(){
  let d;
  try{ d = await getJSON("/api/health"); }
  catch(e){ tileFail($("#health"), "Health markers", loadHealth, e); return; }
  MARKERS = d.markers;
  HSERIES = d.series||{};
  HSTALE = d.staleness||{markers:{}, summary:null};
  // populate the add-form marker dropdown once
  const sel = $("#hmarker");
  if(!sel.options.length){
    sel.innerHTML = Object.entries(MARKERS).map(([k,m])=>`<option value="${k}">${m.label} (${m.unit})</option>`).join("");
  }
  const hd=$("#hdate"); if(hd && !hd.value){   // UX-11 — prefill today (local): a reading is almost always "now"
    const n=new Date();
    hd.value=`${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,"0")}-${String(n.getDate()).padStart(2,"0")}`; }
  const host = $("#health");
  const present = Object.keys(HSERIES);
  if(!present.length){ host.innerHTML = `<div class="empty">No markers yet — add one below.</div>`; return; }
  host.innerHTML = staleBannerHTML() + present.map(hcardHTML).join("");
  host.querySelectorAll(".hcard").forEach(wireHCard);
}

const _hform=$("#hform");
if(_hform) _hform.addEventListener("submit", async e=>{
  e.preventDefault();
  const body = {marker:$("#hmarker").value, date:$("#hdate").value, value:$("#hvalue").value, source:"manual"};
  const r = await fetch("/api/health",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json();
  if(!d.ok){ await notice({title:"Could not add that marker", intro:d.error||"unknown", tone:"danger"}); return; }
  $("#hvalue").value=""; await loadHealth();
});

// ── Settings (private-only, in a modal) — edit the non-secret personalization that otherwise comes
// from SH_* env. Values are stored in the DB (meta) and override env; secrets are never shown/settable.
// "Name,lat,lon[,CODE];…" ⇄ [{name,lat,lon,code}] for the weather city picker (backend format unchanged).
function parseCities(str){
  return (str||"").split(";").map(s=>s.trim()).filter(Boolean).map(p=>{
    const b=p.split(",").map(x=>x.trim());
    return {name:b[0], lat:b[1], lon:b[2], code:((b[3]||(b[0]||"").slice(0,3))||"").toUpperCase()};
  }).filter(c=>c.name && c.lat && c.lon);
}
function serializeCities(arr){ return arr.map(c=>`${c.name},${c.lat},${c.lon},${c.code}`).join(";"); }

async function loadSettings(){
  const host=$("#settings"); if(!host) return;
  let d; try{ d=await getJSON("/api/settings"); }catch(e){ host.innerHTML=`<div class="empty">Could not load settings.</div>`; return; }
  if(!d.ok){ host.innerHTML=`<div class="empty">${esc(d.error||"unavailable")}</div>`; return; }
  // data-orig = the value as loaded, so saveSettings posts ONLY the fields the user changed —
  // posting an untouched env/default-sourced value would persist it to meta and shadow the env.
  const cityField=s=>`<div class="setrow">
      <label>${esc(s.label)}<span class="src">${esc(s.source)}</span></label>
      <input type="hidden" id="set_weather_cities" data-key="weather_cities" data-orig="${esc(s.value||"")}" value="${esc(s.value||"")}">
      <div class="wxchips" id="wxchips"></div>
      <div class="wxsearchrow">
        <input id="wxsearch" type="text" placeholder="Search a city — e.g. Lisbon" autocomplete="off">
        <button type="button" class="ghost" id="wxsearchbtn">Search</button>
      </div>
      <div class="err" id="wxcap"></div>
      <ul class="wxresults" id="wxresults"></ul>
      <div class="help">Type a city and pick it — the coordinates are resolved for you. Up to 5; no cities = the widget is hidden.</div>
      <div class="err" id="err_weather_cities"></div>
    </div>`;
  const field=s=>{
    if(s.key==="weather_cities") return cityField(s);
    const id="set_"+s.key;
    const ctl = s.kind==="text"
      ? `<textarea id="${id}" data-key="${s.key}" data-orig="${esc(s.value||"")}">${esc(s.value||"")}</textarea>`
      : `<input id="${id}" data-key="${s.key}" data-orig="${esc(s.value||"")}" type="text" value="${esc(s.value||"")}">`;
    return `<div class="setrow">
      <label for="${id}">${esc(s.label)}<span class="src">${esc(s.source)}</span></label>
      ${ctl}
      <div class="help">${esc(s.help)}</div>
      <div class="err" id="err_${s.key}"></div>
    </div>`;
  };
  host.innerHTML=`<form class="setform" id="setform">
    ${d.settings.map(field).join("")}
    <div class="setbar"><button class="primary" type="submit">Save settings</button>
      <span class="ok" id="setok"></span></div>
  </form>
  <div class="setrow" style="margin-top:14px">
    <label>Backup &amp; export</label>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <a class="ghost" href="/api/backup/db" download style="text-decoration:none;padding:6px 11px;font-size:12px">⬇ Database snapshot (.db)</a>
      <a class="ghost" href="/api/export/json" download style="text-decoration:none;padding:6px 11px;font-size:12px">⬇ Data export (.json)</a>
    </div>
    <div class="help">The snapshot is a complete, consistent copy of the database — restore by stopping the app
      and dropping it into ./data as sparinghorse.db. The JSON export carries only what can't be rebuilt
      (objectives, check-ins, reflections, adjustments, lab markers, plans, settings) — restore into a fresh
      instance with: python SparingHorse.py import &lt;file&gt;. API keys are never included in either.</div>
  </div>
  <div class="setrow" style="margin-top:14px">
    <label>Away / can't run</label>
    <ul class="avlist" id="avlist" style="list-style:none;margin:0 0 8px;padding:0"></ul>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <input id="avfrom" type="date" style="max-width:150px">
      <span class="muted">→</span>
      <input id="avto" type="date" style="max-width:150px">
      <input id="avnote" type="text" placeholder="note (optional) — e.g. flight" style="flex:1;min-width:120px">
      <button type="button" class="ghost" id="avadd">Add</button>
    </div>
    <div class="help">Days you can't run (travel, life). The plan lays around them: runs slide to the
      nearest sensible day, spacing rules hold, and a heavily blocked week gets honestly lighter —
      never crammed. Single day: leave the second date empty. Private — away days never appear on
      the public page.</div>
    <div class="err" id="averr"></div>
  </div>`;
  $("#setform").addEventListener("submit", saveSettings);
  wireCityPicker();
  wireAway();
}

// §AV — the away-days list: add/remove re-lay the plan immediately (the response is the replan
// payload, same contract as objectives/adjustment writes).
async function wireAway(){
  const list=$("#avlist"); if(!list) return;
  const err=$("#averr");
  const fmt=d=>{ const x=new Date(d+"T00:00:00");
    return x.toLocaleDateString(undefined,{weekday:"short",day:"numeric",month:"short"}); };
  async function render(){
    let rows=[]; try{ rows=await getJSON("/api/availability"); }catch(e){ rows=[]; }
    list.innerHTML = rows.length ? rows.map(a=>`<li style="display:flex;gap:8px;align-items:center;padding:2px 0">
        <span class="mono">${fmt(a.date_from)}${a.date_to!==a.date_from?" → "+fmt(a.date_to):""}</span>
        ${a.note?`<span class="muted">${esc(a.note)}</span>`:""}
        <button type="button" class="ghost" data-av="${a.id}" title="Clear — the plan re-lays" style="margin-left:auto;padding:1px 8px">✕</button>
      </li>`).join("") : `<li class="muted" style="padding:2px 0">none — you're fully available</li>`;
    list.querySelectorAll("[data-av]").forEach(b=>b.addEventListener("click",async()=>{
      b.disabled=true;
      const r=await fetch(`/api/availability/${b.dataset.av}/remove`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
      const p=await r.json(); if(p.ok){ LASTDIFF=p.diff; await refreshPlan(p); } await render();
    }));
  }
  $("#avadd").addEventListener("click",async()=>{
    err.textContent="";
    const f=$("#avfrom").value, t=$("#avto").value||f;
    if(!f){ err.textContent="pick at least the first date"; return; }
    const r=await fetch("/api/availability",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({date_from:f,date_to:t,note:$("#avnote").value})});
    const p=await r.json();
    if(!p.ok){ err.textContent=p.error||"could not save"; return; }
    $("#avfrom").value=$("#avto").value=$("#avnote").value="";
    LASTDIFF=p.diff; await refreshPlan(p); await render();
  });
  await render();
}

const MAX_CITIES=5;   // mirrors the server's MAX_WEATHER_CITIES (validated there too)
function wireCityPicker(){
  const hidden=$("#set_weather_cities"); if(!hidden) return;
  let cities=parseCities(hidden.value);
  const chips=$("#wxchips"), results=$("#wxresults"), search=$("#wxsearch"),
        searchBtn=$("#wxsearchbtn"), cap=$("#wxcap");
  const sync=()=>{ hidden.value=serializeCities(cities); renderChips(); };
  function renderChips(){
    chips.innerHTML = cities.length
      ? cities.map((c,i)=>`<span class="wxchip"><b>${esc(c.code)}</b> ${esc(c.name)} <button type="button" data-i="${i}" aria-label="Remove ${esc(c.name)}">✕</button></span>`).join("")
      : `<span class="muted" style="font-size:12px">No cities — the widget is hidden.</span>`;
    chips.querySelectorAll("button[data-i]").forEach(b=>b.addEventListener("click",()=>{ cities.splice(+b.dataset.i,1); sync(); }));
    const full = cities.length>=MAX_CITIES;   // at the cap → block adding, prompt a removal
    search.disabled=searchBtn.disabled=full;
    cap.textContent = full ? `Maximum ${MAX_CITIES} cities — remove one to add another.` : "";
    if(full) results.innerHTML="";
  }
  async function doSearch(){
    const q=search.value.trim(); if(q.length<2){ results.innerHTML=""; return; }
    results.innerHTML=`<li class="sub">searching…</li>`;
    let out; try{ out=await getJSON("/api/geocode?q="+encodeURIComponent(q)); }catch(e){ results.innerHTML=`<li class="sub">search failed</li>`; return; }
    if(!out.ok){ results.innerHTML=`<li class="sub">search unavailable</li>`; return; }
    const rs=out.results||[];
    results.innerHTML = rs.length
      ? rs.map((r,i)=>`<li data-i="${i}">${esc(r.name)} <span class="sub">${esc([r.admin1,r.country].filter(Boolean).join(", "))}</span></li>`).join("")
      : `<li class="sub">no matches</li>`;
    results.querySelectorAll("li[data-i]").forEach(li=>li.addEventListener("click",()=>{
      const r=rs[+li.dataset.i];
      const name=(r.name||"").replace(/[,;]/g," ").trim();   // ',' and ';' are delimiters in the stored format
      if(name && cities.length<MAX_CITIES && !cities.some(c=>c.lat==r.lat && c.lon==r.lon)){   // cap + skip dup
        cities.push({name, lat:r.lat, lon:r.lon, code:name.slice(0,3).toUpperCase()});
      }
      search.value=""; results.innerHTML=""; sync();
    }));
  }
  $("#wxsearchbtn").addEventListener("click", doSearch);
  search.addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); doSearch(); } });
  renderChips();
}
async function saveSettings(e){
  e.preventDefault();
  document.querySelectorAll("#setform .err").forEach(n=>n.textContent="");
  $("#setok").textContent="";
  const payload={};
  document.querySelectorAll("#setform [data-key]").forEach(n=>{
    if(n.value !== n.dataset.orig) payload[n.dataset.key]=n.value;   // changed fields only
  });
  if(Object.keys(payload).length===0){ $("#setok").textContent="No changes"; return; }
  const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
  const d=await r.json();
  if(!d.ok){
    const errs=d.errors||{};
    Object.keys(errs).forEach(k=>{ const n=$("#err_"+k); if(n) n.textContent="⚠ "+errs[k]; });
    return;
  }
  $("#setok").textContent="Saved ✓";
  loadSettings();          // refresh provenance badges (env → saved)
  if(typeof loadWeather==="function") loadWeather();   // weather cities take effect live
}
// Keys block — the two secrets (Runalyze token / Claude key). WRITE-ONLY: the value is never sent back,
// so the field is always empty and shows status only. Private console only (#secretsBox is removed on
// the public view). Saving applies live — no .env edit, no restart.
// §SG — `justSaved` is the key whose field should come back EMPTY; every other field keeps whatever
// the owner had typed into it. Re-rendering the whole block used to wipe unsaved siblings, which with
// five keys means pasting three Suunto credentials and losing two of them to the first Save.
async function loadSecrets(probe, justSaved){
  const host=$("#secretsBox"); if(!host) return;
  const drafts={}; host.querySelectorAll("input[id^='sec_']").forEach(i=>{ if(i.value) drafts[i.id]=i.value; });
  let d; try{ d=await getJSON("/api/secrets"); }catch(e){ host.innerHTML=""; return; }
  if(!d.ok){ host.innerHTML=""; return; }
  // Initial badge = configured-or-not + where it came from. When `probe` is set we then live-check
  // each configured key against its provider and rewrite the badge to valid / rejected (see validateSecrets).
  const srcWord=s=> s.source==="env" ? "from environment" : "configured";
  const badge=s=> s.configured
        ? `<span class="src ok" id="secbadge_${s.key}">✓ ${srcWord(s)}${probe?" · checking…":""}</span>`
        : `<span class="src warn" id="secbadge_${s.key}">not set</span>`;
  // §SG — WHICH value is in the box. "configured" reads the same before and after a rotation, so a
  // save that silently failed looks exactly like one that worked; eight hex of sha256 tells them
  // apart without describing the key. Sits OUTSIDE the badge span, which validateSecrets rewrites.
  const fp=s=> s.fingerprint
        ? ` <code class="fp" title="First 8 hex of sha256 of the stored value — one-way. Check it yourself: printf %s &quot;$KEY&quot; | sha256sum | cut -c1-8">fp ${esc(s.fingerprint)}</code>`
        : "";
  const row=s=>`<div class="setrow">
      <label for="sec_${s.key}">${esc(s.label)} ${badge(s)}${fp(s)}</label>
      <div class="secinput">
        <input id="sec_${s.key}" type="password" autocomplete="new-password"
               placeholder="${s.configured?"•••• — paste a new value to replace":"Paste your key to enable"}">
        <button type="button" class="primary" data-sec="${s.key}">Save</button>
        ${s.source==="saved"?`<button type="button" class="ghost" data-clr="${s.key}">Clear</button>`:""}
      </div>
      <div class="help">${esc(s.help)}</div>
      <div class="err" id="secerr_${s.key}"></div>
    </div>`;
  host.innerHTML=`<div class="secblock"><div class="sectitle">Connections &amp; keys</div>
    ${d.secrets.map(row).join("")}<div id="suuntoBox"></div>
    <div class="help" style="margin-top:2px">Keys are write-only — the value never comes back to this
      page. <code>fp</code> is the first 8 hex of its sha256, so you can confirm which value is stored:
      <code>printf %s "$KEY" | sha256sum | cut -c1-8</code>.</div></div>`;
  // put back what was being typed (minus the field just saved) BEFORE anything can steal focus
  Object.entries(drafts).forEach(([id,v])=>{ if(id==="sec_"+justSaved) return;
    const el=document.getElementById(id); if(el) el.value=v; });
  host.querySelectorAll("button[data-sec]").forEach(b=>b.addEventListener("click",()=>saveSecret(b.dataset.sec,false)));
  host.querySelectorAll("button[data-clr]").forEach(b=>b.addEventListener("click",()=>saveSecret(b.dataset.clr,true)));
  loadSuunto();
  if(probe) validateSecrets(d.secrets);
}
// §SG — the Suunto watch link: a one-time OAuth "Connect" (full-page bounce to Suunto and back),
// then the nightly re-plan pushes the coming week to the watch as SuuntoPlus Guides. The three app
// credentials above must be saved first; tokens never reach the browser (status only).
async function loadSuunto(){
  const box=$("#suuntoBox"); if(!box) return;
  let d; try{ d=await getJSON("/api/suunto/status"); }catch(e){ box.innerHTML=""; return; }
  if(!d.ok){ box.innerHTML=""; return; }
  const badge = d.connected
    ? `<span class="src ok">✓ connected${d.user?" as "+esc(String(d.user)):""}</span>`
    : (d.configured ? `<span class="src warn">not connected</span>`
                    : `<span class="src warn">needs the 3 Suunto keys above</span>`);
  box.innerHTML=`<div class="setrow">
      <label>Suunto watch (Guides push) ${badge}</label>
      <div class="secinput">
        ${d.connected
          ? `<button type="button" class="primary" id="suuntoPush">Push week to watch</button>
             <button type="button" class="ghost" id="suuntoRepush">Rebuild on watch</button>
             <button type="button" class="ghost" id="suuntoDisc">Disconnect</button>`
          : `<button type="button" class="primary" id="suuntoConn" ${d.configured?"":"disabled"}>Connect Suunto</button>`}
      </div>
      <div class="help">Sends the plan's next days to your watch as SuuntoPlus Guides (steps, pace
        and HR bands on the wrist). Auto-pushes nightly after the re-plan once connected.
        <b>Rebuild</b> deletes each guide and sends it again under a new id — use it after deleting
        guides on the watch itself, because an ordinary push only <i>updates</i> them and the watch
        never re-downloads a guide it already knows.
        Register the redirect URI <code>${esc(d.redirect_uri||"")}</code> on your Suunto OAuth app.</div>
      <div class="err" id="suuntoErr"></div>
    </div>`;
  const err=$("#suuntoErr");
  const conn=$("#suuntoConn"); if(conn) conn.addEventListener("click",()=>{ location.href="/api/suunto/connect"; });
  const disc=$("#suuntoDisc"); if(disc) disc.addEventListener("click",async()=>{
    try{ await fetch("/api/suunto/disconnect",{method:"POST"}); }catch(e){}
    loadSuunto();
  });
  // One handler, two doors. The plain push UPDATES the guides already on the account, which is
  // right for the nightly re-plan and useless once a guide has been deleted on the wrist — the id
  // never changes, so the watch has no reason to fetch it again. Rebuild deletes first (§SG2).
  const runPush=async(btn,label,recreate)=>{
    err.textContent=""; btn.disabled=true; btn.textContent=recreate?"Rebuilding…":"Pushing…";
    let r; try{ r=await (await fetch("/api/suunto/push",{method:"POST",headers:{"Content-Type":"application/json"},
                                                        body:JSON.stringify({recreate:!!recreate})})).json(); }
    catch(e){ r={ok:false,error:"network error"}; }
    btn.disabled=false; btn.textContent=label;
    const rebuilt=(r.recreated||[]).length;
    if(r.ok) err.innerHTML=`<span class="src ok">✓ ${r.pushed||0} session${(r.pushed===1)?"":"s"} pushed`
      +`${rebuilt?" ("+rebuilt+" rebuilt from scratch)":""}${r.note?" — "+esc(r.note):""}</span>`;
    else err.textContent="⚠ "+(r.error||"push failed");
  };
  const push=$("#suuntoPush"); if(push) push.addEventListener("click",()=>runPush(push,"Push week to watch",false));
  const repush=$("#suuntoRepush"); if(repush) repush.addEventListener("click",()=>runPush(repush,"Rebuild on watch",true));
}
// Live key check — fires when the Settings window opens (or after a save), not on every page load (the
// Anthropic probe is a network round-trip). A configured key resolves to ✓ in use · valid, ✗ key rejected,
// or — if the provider couldn't be reached — falls back to the plain configured badge.
async function validateSecrets(secrets){
  const configured=(secrets||[]).filter(s=>s.configured);
  if(!configured.length) return;
  let d; try{ d=await getJSON("/api/secrets/validate"); }catch(e){ d=null; }
  configured.forEach(s=>{
    const el=$("#secbadge_"+s.key); if(!el) return;
    const v = (d&&d.ok) ? d.results[s.key] : "unknown";
    if(v==="valid"){ el.className="src ok"; el.textContent="✓ in use · valid";
      el.title="Verified against the provider just now."; }
    else if(v==="invalid"){ el.className="src bad"; el.textContent="✗ key rejected";
      el.title="A key is set but the provider rejected it — paste a fresh one to fix."; }
    else { el.className="src ok"; el.textContent="✓ "+(s.source==="env"?"from environment":"configured");
      el.title="A key is set; couldn't reach the provider to verify it right now."; }
  });
}
async function saveSecret(key, clear){
  const inp=$("#sec_"+key), errEl=$("#secerr_"+key); if(errEl) errEl.textContent="";
  const value = clear ? "" : (inp ? inp.value : "");
  if(!clear && !value){ if(errEl) errEl.textContent="⚠ paste a value first"; return; }
  let d; try{
    const r=await fetch("/api/secrets",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,value})});
    d=await r.json();
  }catch(e){ if(errEl) errEl.textContent="⚠ could not save"; return; }
  if(!d.ok){ if(errEl) errEl.textContent="⚠ "+(d.error||"could not save"); return; }
  if(inp) inp.value="";
  loadSecrets(true, key);   // refresh badges + re-validate; siblings keep their unsaved drafts
  // a freshly-set token/key changes what the app can do — refresh the affected surfaces live
  fetch("/healthz").then(r=>r.json()).then(h=>{ LLM_OK=!!h.llm; TOKEN_OK=!!h.token_configured; SYNC_LAST=h.last_sync||null; paintFreshness(); refreshFirstRun(); }).catch(()=>{});
}

// learn whether the LLM layer is configured (§6c) before the plan/objectives render.
// §RB — the /runs explorer boots only what it shows: the calendar + the activity tile; the
// dashboard's status loaders (plan, drift, shape, health…) stay dashboard-only.
if(SH_PAGE==="runs"){
  loadRunsCal(); loadRecent();
  // the explorer shows the same footer as the dashboard, so it needs the same sync time; /healthz
  // is the one read it already has a reason to make (private-only page, so `last_sync` is served).
  fetch("/healthz").then(r=>r.json()).then(d=>{ SYNC_LAST=d.last_sync||null; noteSync(d.last_sync);
    footSync(d.last_sync); }).catch(()=>{});
}else{
fetch("/healthz").then(r=>r.json()).then(d=>{ LLM_OK=!!d.llm; TOKEN_OK=!!d.token_configured; SYNC_LAST=d.last_sync||null; noteSync(d.last_sync); paintFreshness(); _frSeen.tok=true; refreshFirstRun(); loadPlan(); }).catch(()=>{ _frSeen.tok=true; refreshFirstRun(); loadPlan(); });
loadShape(); loadRecent(); loadProjector(); loadWeekly(); loadWeather(); loadEffort(); loadZones(); loadTrack();
}
// §HR — the 'Current zones' card: training-intent rows, pace (VDOT, prescription anchor) + HR
// (unified hr_zones grid, monitoring cross-check), both fitness-tracking. Private-only (section is
// removed on the public view; the endpoint 403s there too).
async function loadZones(){
  const host=$("#zones"); if(!host || SH_READONLY) return;
  let d; try{ d=await getJSON("/api/zones"); }catch(e){ tileFail(host, "Current zones", loadZones, e); return; }
  if(!d || !d.ok){ host.innerHTML=`<div class="empty">Not enough data yet — zones appear once a fitness (VO₂max) snapshot or heart-rate history is in.</div>`; return; }
  ZONESD=d;   // §W1 — same payload shape as the readiness rider; workout cards read it
  const paceHint=qhint("Pace targets are fixed fractions of vVO₂max — the velocity at VO₂max, solved from the Daniels–Gilbert oxygen-cost curve VO₂(v) = −4.60 + 0.182258·v + 0.000104·v² — using your CURRENT effective VO₂max: marathon 0.81, threshold 0.88, interval 0.97 of vVO₂max. The easy bar is LT1 (aerobic threshold), operationalized as 80% of 5k pace with v5k ≈ 0.95·vVO₂max (a pace-first anchor informed by John Davis). Every value moves as your VO₂max moves — zones are never stale.");
  const hrHint=qhint("HR bands are the Friel run grid as fractions of LTHR: Z1 <0.85, Z2 0.85–0.90, Z3 0.90–0.95, Z4 0.95–1.00, Z5 ≥ LTHR. LTHR is estimated streamlessly — the whole-run average HR of your sustained hard efforts (20–70 min at ≥85% of robust HRmax), robust-high percentile, recency-weighted confidence — or taken from your manual field-test entry while fresh (Settings → Manual LTHR). Without a trustworthy LTHR the grid falls back to %HRmax (60/70/80/90). These are the SAME cutoffs the effort monitor and the activity chart band read — one grid, so the card can't disagree with the verdicts.");
  const noteHint=qhint("Pace and HR are two INDEPENDENT estimators of the same fitness (VDOT from VO₂max vs the LTHR grid), deliberately not averaged into one number. On a rebuild they can visibly disagree — cardiac decoupling: a given easy pace costs more HR than VDOT predicts. The Pace↔HR line under Effort discipline tracks that divergence; when they disagree on an easy day, heart rate is the honest read of how easy it really was. On short intervals the opposite caveat applies: HR lags the effort, so pace leads there.");
  const paceCell=r=>{
    const st=r.pace_slower_than, tg=r.pace_target;
    if(st && st.fmt) return `slower than <b>${esc(st.fmt)}</b>/km`;
    if(tg && tg.fmt) return `≈ <b>${esc(tg.fmt)}</b>/km`;
    return "—"; };
  const hrCell=r=>{
    const h=r.hr||{};
    if(h.lo!=null && h.hi!=null) return `${h.lo}–${h.hi} bpm`;
    if(h.hi!=null) return `≤ ${h.hi} bpm`;
    if(h.lo!=null) return `≥ ${h.lo} bpm`;
    return "—"; };
  const dot=i=>`<i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${HRZONE_COLORS[i]};margin-right:6px;vertical-align:baseline"></i>`;
  const rows=(d.rows||[]).map(r=>
    `<tr><td style="white-space:nowrap">${dot(r.zone_idx)}${esc(r.label)}</td>`+
    `<td>${paceCell(r)}</td><td style="white-space:nowrap">${hrCell(r)}</td></tr>`).join("");
  const pa=d.pace_anchor||{}, ha=d.hr_anchor||{};
  const hrAnchorTxt = ha.anchor==="lthr"
    ? `LTHR <b>${ha.ref}</b> (${ha.source==="manual"?`manual${ha.age_days!=null?`, set ${Math.round(ha.age_days/7)}wk ago`:""}`:`derived, ${esc(ha.confidence||"")} confidence`})`
    : ha.anchor==="hrmax" ? `%HRmax of <b>${ha.ref}</b> (no trustworthy LTHR yet)` : "no HR model yet";
  const paceAnchorTxt = pa.vo2max!=null
    ? `VO₂max <b>${pa.vo2max}</b>${pa.p5k_fmt?` (5k-equiv ${esc(pa.p5k_fmt)}/km)`:""}` : "no VO₂max snapshot";
  host.innerHTML=`
    <div class="efftbl-wrap"><table class="efftbl">
      <thead><tr><th>Intent</th><th>Pace ${paceHint}</th><th>HR ${hrHint}</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="muted" style="font-size:11px;margin-top:6px">Pace from ${paceAnchorTxt} · HR from ${hrAnchorTxt}.
      Two independent reads of the same fitness ${noteHint} — both move as you train.</div>`;
}

// Settings modal open/close (private only — the button is removed on the public view below).
const _setBtn=$("#settingsBtn"), _setDlg=$("#settingsDialog");
if(_setBtn && _setDlg){
  _setBtn.addEventListener("click", ()=>{ if(!$("#setform")) loadSettings(); loadSecrets(true); _setDlg.showModal(); });  // (re)load settings if the initial fetch failed; refresh + live-validate keys each open
  const _x=$("#settingsClose"); if(_x) _x.addEventListener("click", ()=>_setDlg.close());
  _setDlg.addEventListener("click", e=>{ if(e.target===_setDlg) _setDlg.close(); });  // backdrop click
  // §SG — landing back from the Suunto OAuth bounce (/?suunto=connected|denied|…): reopen Settings
  // so the fresh connection status (or the failure) is right there; scrub the flag from the URL.
  const _su=new URLSearchParams(location.search).get("suunto");
  if(_su){ history.replaceState(null,"",location.pathname);
    if(!$("#setform")) loadSettings(); loadSecrets(true); _setDlg.showModal(); }
}
if(SH_READONLY){
  // public view: health markers stay private; the readiness VERDICT tile stays (the server
  // redacts its inputs/HRV/note). Drop the write controls; surface read-only + the Log-in link.
  // §RB — the run browser (route geo + HR calendar) is private-only: drop its section + links too.
  ["sec-health","sec-zones","settingsDialog","firstrun","sec-runs","runsLink","durcard","effcard"].forEach(id=>{const e=$("#"+id); if(e) e.remove();});
  ["syncBtn","backfillBtn","planBtn","settingsBtn"].forEach(id=>{const e=$("#"+id); if(e) e.remove();});
  loadReadiness();
  const cluster=document.querySelector(".topctl");
  if(cluster){
    let extra='<span class="ro-badge" title="Read-only public view">read-only</span>';
    if(SH_PRIVATE_URL) extra+=`<a class="adminlink" href="${esc(SH_PRIVATE_URL)}" title="Private console">🔒 Log in</a>`;
    cluster.insertAdjacentHTML("afterbegin", extra);
  }
}else if(SH_PAGE==="runs"){
  // §RB — explorer chrome: the header link points back home; the mobile Runs tab reads active.
  document.title="Sparing Horse — run browser";
  const l=$("#runsLink"); if(l){ l.textContent="← Dashboard"; l.href="/"; l.title="Back to the status dashboard"; }
  const mr=$("#mnavruns"); if(mr) mr.setAttribute("aria-current","page");
}else{
  loadReadiness(); loadHealth(); loadSettings(); loadSecrets(); loadDurability(); loadEfficiency();
  touchSync();   // pull today's run if it's already on Runalyze, then refresh "done ✓"
}

// ── Mobile bottom-nav wiring. The CSS shows .mobnav and the per-tab view only ≤760px; this just keeps
// <body data-mtab> and the buttons' aria-current in sync, and snaps to the top on a tab change. A no-op
// shell on desktop (the nav is display:none, clicks never happen). ──────────────────────────────────
(function(){
  const nav = document.querySelector(".mobnav");
  if(!nav) return;
  const btns = nav.querySelectorAll(".mnav-btn");
  const TABS = [].slice.call(btns).map(b => b.dataset.goto);   // whatever the server emitted (public drops Body)
  function go(tab, push){
    if(!TABS.includes(tab)) tab = "today";
    document.body.dataset.mtab = tab;
    btns.forEach(b => b.setAttribute("aria-current", b.dataset.goto===tab ? "page" : "false"));
    if(push) history.replaceState(null, "", "#"+tab);   // deep-link/refresh keeps the tab; no history spam
    window.scrollTo(0, 0);
  }
  nav.addEventListener("click", e => {
    const b = e.target.closest(".mnav-btn");
    if(!b || !b.dataset.goto) return;                   // the Runs tab is a real <a> — let it navigate
    if(SH_PAGE==="runs"){ location.href = "/#"+b.dataset.goto; return; }   // §RB — tabs live on the dashboard
    go(b.dataset.goto, true);
  });
  if(SH_PAGE!=="runs") go((location.hash || "").replace("#",""), false);   // restore tab from the URL (defaults to today)
})();
