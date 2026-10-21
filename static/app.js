// Served as a STATIC FILE now, so nothing here is templated: the two server-known values ride
// in on the nonce'd window.SH bootstrap the shell writes (TECH-11).
const SH_READONLY = !!(window.SH && window.SH.readonly);   // public read-only mode
const SH_DEMO = !!(window.SH && window.SH.demo);   // §DEMO — synthetic athlete, shared, self-resetting
const SH_PRIVATE_URL = (window.SH && window.SH.privateUrl) || "";   // public→private console link
const SH_STALE_HOURS = (window.SH && window.SH.staleHours) || 26;    // scheduler + /healthz source of truth
// 0.58.0 (U5) — distance units are a DISPLAY choice: the engine and every API stay metric, and the page
// converts at the edge. `U.d` converts a distance, `U.p` a pace string ("4:35" per km → per mile), `U.t`
// a server-baked string ("4:35/km easy", "12.0 km") — the three shapes the API emits. `U.k` is the short
// suffix ("12k" → "7.5mi"). The setting is read once at boot; a save says "reload".
const SH_UNITS = ((window.SH && window.SH.units) === "mi") ? "mi" : "km";
const KM_PER_MI = 1.609344;
const U = {
  mi: SH_UNITS === "mi", unit: SH_UNITS, k: SH_UNITS === "mi" ? "mi" : "k",
  d: (km, dec = 1) => (km == null || isNaN(km)) ? km : +(SH_UNITS === "mi" ? km / KM_PER_MI : +km).toFixed(dec),
  pmin: (minPerKm) => (minPerKm == null) ? minPerKm : (SH_UNITS === "mi" ? minPerKm * KM_PER_MI : minPerKm),
  p: (str) => {
    if (SH_UNITS !== "mi" || !str) return str;
    return String(str).replace(/(\d+):(\d{2})/g, (_, m, sec) => {
      const t = Math.round((+m * 60 + +sec) * KM_PER_MI);
      return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; });
  },
  t: (str) => {
    if (SH_UNITS !== "mi" || !str) return str;
    return String(str)
      .replace(/(\d+):(\d{2})\s*\/km\b/g, (_, m, sec) => U.p(`${m}:${sec}`) + "/mi")
      .replace(/(\d+(?:\.\d+)?)\s*km\b/g, (_, n) => `${U.d(+n, /\./.test(n) ? 1 : 0)} mi`);
  },
};
const SH_PAGE = document.body.dataset.page || "dash";   // §RB — "dash" (status) or "runs" (explorer)
const $ = s => document.querySelector(s);
const fmt = (n, d=1) => (n==null ? "—" : Number(n).toFixed(d));
// Per-workout calendar date, e.g. "Jun 23 - Tue" — so the runner can schedule life around the plan.
const sessDate = iso => { if(!iso) return ""; const d=new Date(iso+"T00:00:00");
  return d.toLocaleDateString(undefined,{month:"short",day:"numeric"})+" - "+d.toLocaleDateString(undefined,{weekday:"short"}); };   // 0.56.1: the one hard-coded en-US literal (review U5) — the browser's locale, like every other date on the page
const getJSON = (url, opts) => fetch(url, opts).then(r => r.json().catch(()=>null).then(d => {
  if(r.status===401 && !SH_READONLY && !SH_DEMO){   // 0.56.0 — the session ended (or none yet): go and log in
    location.href = (d && d.setup) || ("/login?next=" + encodeURIComponent(location.pathname + location.search));
    return new Promise(()=>{});                       // never resolves; the navigation takes over
  }
  if(!r.ok) throw new Error((d && d.error) || ("http " + r.status));   // an error body is a failure, not renderable data — a non-JSON one still names its status
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

// theme switcher — 0.57.0: one button cycling auto (follow the system) → dark → light. "auto" stores
// nothing and re-reads prefers-color-scheme; a saved choice wins until the cycle returns to auto.
const SH_THEME_BG = window.SH_THEME_BG || {light:"#f4f1ea",dark:"#191a1d"};   // each theme's --bg
const themeBtn = $("#themeBtn");
const systemTheme = () => (window.matchMedia&&matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
function themeChoice(){ try{ const s=localStorage.getItem("sh-theme"); return (s==="dark"||s==="light") ? s : "auto"; }catch(e){ return "auto"; } }
function paintTheme(){
  const choice=themeChoice(), t=choice==="auto" ? systemTheme() : choice;
  document.documentElement.dataset.theme=t;
  if(themeBtn){ themeBtn.textContent = "Theme · "+(choice==="auto"?"auto":choice==="dark"?"charcoal":"daylight");
    themeBtn.setAttribute("aria-label", choice==="auto" ? "Theme: follows the system" : `Theme: ${choice==="dark"?"Charcoal":"Daylight"}`); }
  // UX-11 — the browser chrome follows the theme: the tab/PWA title-bar colour, and the manifest
  // link a later install reads (the shell's head script does the same two writes at parse time)
  const mc=document.querySelector('meta[name="theme-color"]');
  if(mc) mc.setAttribute("content", SH_THEME_BG[t]||SH_THEME_BG.light);
  const mf=document.querySelector('link[rel="manifest"]');
  if(mf) mf.setAttribute("href", "/manifest.webmanifest?theme="+t); }
if(themeBtn) themeBtn.addEventListener("click", ()=>{
  const next = {auto:"dark", dark:"light", light:"auto"}[themeChoice()];
  try{ if(next==="auto") localStorage.removeItem("sh-theme"); else localStorage.setItem("sh-theme",next); }catch(e){}
  paintTheme(); });
if(window.matchMedia) matchMedia("(prefers-color-scheme: dark)").addEventListener("change", ()=>{ if(themeChoice()==="auto") paintTheme(); });
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
// Weekly volume: we hold the FULL history and show a fixed window the user can grab-drag
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
  const bars=rows.map(x=>`<div class="col" title="${x.week}: ${U.d(x.km)} ${U.unit}">
    <div class="vlbl">${x.km>=1?Math.round(U.d(x.km)):""}</div>
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
  // §A11Y-PAN — the keyboard equivalent of the drag/wheel pan above: same clamp, same step sizes.
  chart.addEventListener("keydown", e=>{
    if(WEEKLY_ALL.length<=WEEKLY_WIN) return;   // nothing to pan
    const lo=Math.min(WEEKLY_WIN, WEEKLY_ALL.length);
    let ne=WEEKLY_END;
    if(e.key==="ArrowLeft") ne=WEEKLY_END-1;
    else if(e.key==="ArrowRight") ne=WEEKLY_END+1;
    else if(e.key==="PageDown") ne=WEEKLY_END-WEEKLY_WIN;
    else if(e.key==="PageUp") ne=WEEKLY_END+WEEKLY_WIN;
    else if(e.key==="Home") ne=lo;
    else if(e.key==="End") ne=WEEKLY_ALL.length;
    else return;
    e.preventDefault();
    ne=Math.max(lo, Math.min(WEEKLY_ALL.length, ne));
    if(ne!==WEEKLY_END){ WEEKLY_END=ne; renderWeekly(); }
  });
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
function paceStr(minkm){ if(minkm==null) return "—"; minkm=U.pmin(minkm); const m=Math.floor(minkm), s=Math.round((minkm-m)*60);
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
  if(kind==="gct" && p.has_gct) return {vals:p.gct, invert:true};   // shorter contact = taller, as pace
  if(kind==="vo" && p.has_vo) return {vals:p.vo, invert:false};
  return null;
}
function profileLabel(kind){
  const p=ACTPROFILE||{};
  return kind==="pace"?"pace profile (faster = higher)"
    : kind==="hr"?`heart-rate profile · avg ${p.hr_avg||"—"} bpm`
    : kind==="elevation"?"elevation profile (climb)"
    : kind==="gct"?"ground-contact profile (shorter = higher)"
    : kind==="vo"?"vertical-oscillation profile (bounce)"
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
  if(kind==="gct")     return rg((ctx.hi-v)/((ctx.hi-ctx.lo)||1));   // shorter contact = the fast colour, as pace
  if(kind==="cadence"){ const d=Math.abs(v-180);                    // 170–190 stays green, then ramps
    return rg(d<=10 ? 1 : 1-(d-10)/15); }                           // to red by ±25 (155 / 205)
  return "var(--accent)";                                            // elevation, vo: neutral single hue (no good/bad end)
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
let ACT_SEQ=0;   // §ACT-SEQ — a fast day-switch fires a newer request before the older one lands
let TODAY_LINES="";   // §TODAY-LINES — the plan's "Binding limit"/"Deload" lines, built once and re-appended across renderReadiness's own innerHTML rewrites
function loadRecent(){ return loadActivity(); }   // default tile = latest activity
async function loadActivity(aid){
  CURACT = aid || null;
  const seq = ++ACT_SEQ;
  const host=$("#recent");
  let a;
  try{ a = await getJSON(aid?`/api/activity/${aid}`:"/api/activity/latest"); }
  catch(e){ if(seq!==ACT_SEQ) return; tileFail(host, aid ? "That activity" : "Latest activity", ()=>loadActivity(aid), e); return; }
  if(seq!==ACT_SEQ) return;   // a newer request superseded this one
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
  // 0.55.1 — the public box serves the DATE only (the time of day is the household routine, §PV
  // S3), so the card reads "Tue 1 Sept" there and "Tue 1 Sept · 18:30" on the private console.
  const dt=a.date_time ? new Date(a.date_time) : new Date((a.date||"")+"T00:00:00");
  const when=dt.toLocaleDateString(undefined,{weekday:"short",day:"numeric",month:"short"})+
    (a.date_time ? " · "+dt.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"}) : "");
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
            ? `<span class="muted">⊘ ignored</span> <button type="button" class="linkbtn" id="igntog" data-id="${a.id}" data-on="0">undo</button>`
            : `<button type="button" class="linkbtn" id="igntog" data-id="${a.id}" data-on="1" title="Exclude this activity from the fitness/fatigue reconstruction — for a duplicate or mis-tagged upload the auto-detector missed">⊘ ignore</button>`)+
          `<button type="button" id="delact" data-id="${a.id}" class="linkbtn delact" title="Hard-remove this activity from your local copy — for one you ALREADY deleted on Runalyze (insert-only sync leaves the row behind). Still on Runalyze? It returns next sync — use ⊘ ignore instead.">🗑 delete</button></div>`}
      </div>
      <div class="mrow">
        <span class="ttl">${esc(a.sport||"Activity")}${a.title?` — ${esc(a.title)}`:""}${a.sj?` <span class="sjchip" title="This recording is part ${a.sj.index} of ${a.sj.parts} of a session recorded in ${a.sj.parts} parts (${a.sj.km} km · ${a.sj.min} min total) — the read-back line below joins the parts. Numbers on this card are THIS part's own.">1+1 · part ${a.sj.index}/${a.sj.parts}</span>`:""}</span>
        ${m("When", when, "")}
        ${m("Distance", fmt(U.d(a.distance,2),2), U.unit)}
        ${m("Duration", durStr(a.duration), "")}
        ${m("Pace", paceStr(a.pace_min_km), "/"+U.unit, "pace")}
        ${SH_READONLY?"":m("Avg HR", a.hr_avg||"—", "bpm", "hr")}
        ${SH_READONLY?"":m("Max HR", a.hr_max||"—", "bpm", "hr")}
        ${a.cadence?m("Cadence", a.cadence, "spm", "cadence"):""}
        ${a.gct_ms?m("Ground contact", a.gct_ms, "ms", "gct"):""}
        ${a.vo_cm?m("Vert. osc.", fmt(a.vo_cm,1), "cm", "vo"):""}
        ${m("TRIMP", a.trimp!=null?Math.round(a.trimp):"—", "")}
        ${a.elevation_up?m("Climb", a.elevation_up, "m", "elevation"):""}
      </div>
      <div class="structline" id="structline"></div>
      <div class="profbar">
        <span class="profhint muted">Background shades the locked trace · hover a metric tile to overlay it (colour = value), click to lock.</span>
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
        (st.composite?`<span class="sjchip" title="A recording saved in ${st.n_parts} parts minutes apart, read back as ONE session — each part analysed on its own clean streams, then joined.">1+1</span> `:"")+
        `<b>${esc(st.kind_label)}</b> — ${esc(st.summary)}`+
        (st.confidence==="rough"?` <span class="muted" title="The pace signal was noisy (GPS gaps or drift) — segment paces are approximate.">·rough signal</span>`:"")+
        (st.sq&&st.sq.line?`<div class="sqline"><span class="sdet" title="Strides execution — count vs the prescribed set band; HR shown as RESPONSE (a 15–20s stride's HR peak lags into the recovery), never as the effort verdict — pace is the trick.">Strides read:</span> ${esc(st.sq.line)}</div>`:"");
    }).catch(()=>{});
    try{ ACTPROFILE = await getJSON(`/api/activity/${a.id}/profile`); }
    catch(e){ ACTPROFILE={}; }
    if(seq!==ACT_SEQ) return;   // §ACT-SEQ — the same guard on the profile fetch, or a stale profile draws on the newer card
  }
  if(!ACTPROFILE || !ACTPROFILE.has_pace) LOCKED = (ACTPROFILE&&ACTPROFILE.has_hr)?"hr":(ACTPROFILE&&ACTPROFILE.has_cadence)?"cadence":(ACTPROFILE&&ACTPROFILE.has_elevation)?"elevation":"pace";
  // drop the hover affordance from any metric whose channel didn't come through, so the cursor never
  // promises a trace that isn't there (e.g. Climb on a run Runalyze couldn't elevation-correct).
  host.querySelectorAll(".metric.hovx").forEach(el=>{
    const k=el.dataset.prof, P=ACTPROFILE||{};
    const ok=(k==="pace"&&P.has_pace)||(k==="hr"&&P.has_hr)||(k==="cadence"&&P.has_cadence)||(k==="elevation"&&P.has_elevation)||(k==="gct"&&P.has_gct)||(k==="vo"&&P.has_vo);
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
    const tip=runs.length?` title="${runs.length===1?`${U.d(runs[0].km)} ${U.unit}${runs[0].pace?` @ ${U.p(runs[0].pace)}/${U.unit}`:""}${runs[0].sj?` (1+1 split recording, ${runs[0].sj} parts)`:""} — view this run`:`${runs.length} runs — pick one`}"`:"";
    // UX-9: only a cell that opens something takes the role — a focus stop that does nothing is
    // worse than no focus stop, and blank/empty days are exactly that.
    const kb = runs.length
      ? ` role="button" tabindex="0" aria-label="${day}: ${runs.length===1?`${U.d(runs[0].km)} ${U.unit}`:`${runs.length} runs`}"` : "";
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
    cols.map(s=>{const v0=s[f]; const v=(u==="km"&&typeof v0==="number")?U.d(v0):(u==="/km"&&typeof v0==="string")?U.p(v0):v0;
      const uu=u==="km"?U.unit:u==="/km"?"/"+U.unit:u;
      return `<i>${v==null?"—":(typeof v==="number"?fmtN(v):v)}<small>${v!=null&&uu?uu:""}</small></i>`;}).join("")+`</div>`;
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
      pop.innerHTML=runs.map(r=>`<button type="button" class="rcal-chip" data-id="${r.id}">${rcalDot(r.z)}${esc(r.t||"—")} · ${U.d(r.km)} ${U.unit}${r.pace?` · ${esc(U.p(r.pace))}/${U.unit}`:""}${r.sj?` · 1+1`:""}</button>`).join("");
      pop.addEventListener("click", ev2=>{
        const c=ev2.target.closest(".rcal-chip"); if(!c) return;
        ev2.stopPropagation(); rcalCloseAllPops();
        rcalPick(+c.dataset.id, cell.dataset.date, cell);
      });
      cell.appendChild(pop);
      pop.querySelector(".rcal-chip")?.focus();   // §A11Y-POP — a keyboard user gets somewhere to land, not just an open popover
      ev.stopPropagation();
    });
  });
}
document.addEventListener("click", e=>{ if(!e.target.closest(".rcal-day")) rcalCloseAllPops(); });
// §A11Y-POP — Escape closes an open popover and gives focus back to the day cell that opened it,
// so a keyboard user isn't left stranded on a chip that just vanished.
document.addEventListener("keydown", e=>{
  if(e.key!=="Escape") return;
  const pops=document.querySelectorAll(".rcal-pop");
  if(!pops.length) return;
  const cell=pops[0].parentElement;
  rcalCloseAllPops();
  cell?.focus();
});

// ── Workout route map (private only) ────────────────────────────────────────
// Leaflet is loaded lazily from a CDN and ONLY on the private instance — the public read-only
// container never fetches it, and its /map endpoint 403s, because the routes reveal where the user
// lives (the whole reason this feature is private). Falls back to a clean empty state with no GPS.
let LEAFLET_READY=null;
function ensureLeaflet(){
  if(window.L) return Promise.resolve();
  if(LEAFLET_READY) return LEAFLET_READY;
  LEAFLET_READY=new Promise((res,rej)=>{
    // 0.55.2 — Leaflet 1.9.4 is served from THIS box (static/vendor/, the exact unpkg bytes), so the
    // CSP names no third-party script host at all and the map works with the outside world cut off.
    // The Subresource Integrity pins stay: same-origin or not, the browser refuses bytes that are
    // not the ones this file was written against. Hashes are version-locked.
    const css=document.createElement("link");
    css.rel="stylesheet"; css.href="/static/vendor/leaflet-1.9.4/leaflet.css";
    css.integrity="sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H";
    css.crossOrigin="anonymous"; document.head.appendChild(css);
    const js=document.createElement("script");
    js.src="/static/vendor/leaflet-1.9.4/leaflet.js";
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
             interval:"Interval session", tempo:"Tempo run", race_pace:"Race-pace touch", race:"Race day"};   // §RACE · §TT3
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
    <div class="mrow"><span class="ttl">${s.optional?"Optional — week complete":"Rest day"}</span><span class="muted" style="font-size:13px">${esc(U.t(s.note))}</span></div></div>`;
  const act=s.actual||{};
  const q=s.reps&&s.reps.length;
  // §RACE — race day carries the RACE's pace, and the engine publishes it as `pace_zone`
  // ("4:28/km marathon"). The easy-pace metric below reads `easyPace`, so before this the card
  // prescribed the marathon at the week's easy pace and labelled it "easy" — a misprescription on
  // the one day of the block that cannot be re-run. A race has no `reps`, so it took the plain-run
  // branch and nothing flagged it.
  const pz=(s.pace_zone||"").match(/^(\S+)\/km\s+(.+)$/);   // the API is metric; U.p converts the pace
  const paceMetric = s.race
    ? metric("Race pace", pz?esc(U.p(pz[1])):"—", pz?`/${U.unit} ${esc(pz[2])}`:`/${U.unit}`)
    : metric("Pace", U.p((s.easy_pace||easyPace||"").replace("/km","")), `/${U.unit} easy`);
  const kick=`Today's session · ${PHASEN[s.pk]||"plan"} week ${s.week}`+
    (s.done?` · <span style="color:var(--ok);font-weight:600">done ✓</span>`:"");
  const actLine=s.done
    ? `<div style="font-size:12px;margin-top:6px;color:var(--ok)">✓ Ran ${U.d(act.km)}${U.k}${act.pace?` @ ${U.p(act.pace)}/${U.unit}`:""} today — session complete.</div>`
    : "";
  // a structured session carries its own per-segment paces in the card — the single easy pace
  // metric would misprescribe the day, so it only renders for plain easy/long runs.
  const noteParts=(s.note||"").split(" — ");
  const noteTxt=U.t((q&&noteParts.length>1)?noteParts.slice(0,-1).join(" — "):s.note);   // card replaces the cramped structure string
  // 0.60.2 — a note that only restates the title ("easy run" under "Easy run") is not a line
  const ttlTxt=String(KINDT[s.kind]||s.kind+" run").replace(/<[^>]+>/g,"");
  const noteLine=(noteTxt&&String(noteTxt).trim().toLowerCase()!==ttlTxt.toLowerCase()) ? `<div class="muted" style="font-size:12px;margin-top:6px">${esc(noteTxt)}</div>` : "";
  return `<div class="planned"${s.done?' style="opacity:.85"':''}><div class="rkick">${kick}</div>
    <div class="mrow">
      <span class="ttl">${KINDT[s.kind]||esc(s.kind)+" run"}</span>
      ${metric("Distance", U.d(s.km), U.unit)}
      ${metric("Duration", `~${s.minutes}`, "min")}
      ${q?"":paceMetric}
      ${metric("Target load", s.trimp!=null?Math.round(s.trimp):"—", "TRIMP")}
    </div>
    ${actLine}
    ${noteLine}${q?workoutCard(s,true):""}</div>`;
}// §3 status card — "lead with the verdict": gradient panel (state-coloured), glass pill, big verdict.
// UX-4 — how old is the data behind this verdict? The readiness card was happy to render a confident
// green off a sync that failed two nights ago, and the only hint was a timestamp in the page footer
// nobody scrolls to. The chip reads `last_sync` from /healthz (which TECH-8 records) and turns amber
// past the server-provided boundary — the same staleness the nightly catch-up uses, so the number on screen and the number
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
  el.classList.toggle("stale", hrs > SH_STALE_HOURS);
  el.title = `Last successful sync: ${then.toLocaleString()}`;
}
function statusCard(a, foot){
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
      ${(foot||(!SH_READONLY&&SYNC_LAST))?`<div class="sc-foot">${foot?`<span>${esc(foot)}</span>`:""}<span class="sc-fresh" id="scFresh"></span></div>`:""}
    </div>`;
}
// 0.57.0 (§5.1) — one line saying why THIS session, rendered from the plan's own fields: the phase and
// week the session sits in, what it is, and the fitness component it builds. Only on the Today page.
const PHASE_NAMES={rebase:"Re-base",base:"Base",build:"Build",peak:"Peak",taper:"Taper",race:"Race week",maintenance:"Maintenance"};
function whyLine(s){
  if(!s) return `<div class="whyline"><b>Why nothing:</b> no plan covers today — add a race in the Objectives panel and generate a plan.</div>`;
  const pk=String(s.pk||""), phase=PHASE_NAMES[pk] || (pk.startsWith("bridge")?"Bridge":pk.startsWith("peak")?"Peak":pk.startsWith("taper")?"Taper":pk.startsWith("recovery")?"Recovery":pk.startsWith("base")?"Base":pk.startsWith("build")?"Build":pk?pk:"");  // §CHAIN2 — chain-segment recovery/base/build keys (recovery1, base1, build1…) fall to their phase name too
  const where = phase ? `${phase}${s.week?`, week ${esc(String(s.week))}`:""}` : "";
  const what = s.kind==="rest" ? "a rest day" : `${esc(String(s.kind||"run").replace(/_/g," "))}${s.km?` · ${esc(String(U.d(s.km)))} ${U.unit}`:""}${s.pace_zone?` at ${esc(U.t(String(s.pace_zone)))}`:""}`;
  const comp = s.component && COMPT[s.component] ? COMPT[s.component].split(/(?<=\.)\s/)[0] : (s.kind==="rest" ? "Recovery is part of the plan — adaptation happens between the sessions." : "");
  // 0.60.2 — the session card above already prints the note; echoing it here read the same sentence twice
  return `<div class="whyline"><b>Why this session:</b> ${where?where+" — ":""}${what}.${comp?" "+esc(comp):""}</div>`;
}
function renderReadiness(d){
  RDY=d;
  setTimeout(paintFreshness, 0);            // the chip outlives each re-render of the card
  if(d.zones) ZONESD=d.zones;              // §W1 — current zones ride along (private), so the card never races the zones fetch
  const a=d.assessment||{};
  if(SH_READONLY || a.public){   // public view: verdict card + planned session only
    $("#readiness").innerHTML = statusCard(a, "") + plannedSession(d.session)
      + (SH_PAGE==="today" ? whyLine(d.session) + (TODAY_LINES ? `<div data-today-lines="1">${TODAY_LINES}</div>` : "") : "");   // §TODAY-LINES — the public day reads the limit line too
    return;
  }
  const c=d.checkin||{};
  const hrv=a.hrv||{};
  const hrvTxt = hrv.state==null ? "HRV: no data"
    : `HRV ${hrv.baseline} vs ${hrv.band[0]}–${hrv.band[1]} — ${hrv.state}`;
  const sel=(name,val,opts)=>`<label for="ci_${name}">${name}</label><select id="ci_${name}">`+
    opts.map(([v,t])=>`<option value="${v}" ${v===val?"selected":""}>${t}</option>`).join("")+`</select>`;
  let aiLine="";
  if(a.source && a.source.startsWith("llm"))
    aiLine=`🩺 AI judgment${a.engine_floor&&a.engine_floor!==a.verdict?` · engine floor was ${a.engine_floor}`:""}`;
  else if(a.source && a.source.startsWith("engine ("))
    aiLine=`engine floor held — AI suggested ${a.ai_verdict}`;
  const notePh = (LLM_OK&&AI.judgment) ? "Anything else? e.g. “slight cold coming on”, “legs heavy but slept great” — the AI reads this"
                        : "Note (optional)";
  const foot=[(a.reasons||[]).join(" · "), hrvTxt, aiLine].filter(Boolean).join("  ·  ");
  $("#readiness").innerHTML=
    statusCard(a, foot) +
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
  if(SH_PAGE==="today") $("#readiness").insertAdjacentHTML("beforeend", whyLine(d.session));   // 0.57.0 — the why line, on the Today page only
  if(SH_PAGE==="today" && TODAY_LINES) $("#readiness").insertAdjacentHTML("beforeend", `<div data-today-lines="1">${TODAY_LINES}</div>`);   // §TODAY-LINES — survive this rewrite even if the plan fetch landed first
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
// in the tooltip (on my corpus longer runs decouple LESS — duration must stay visible), and the
// engine itself voids the trend when the recent-vs-prior distance mix shifts.
async function loadEfficiency(){
  const body=$("#effbody"); if(!body) return;              // the card is removed on the public box
  try{ renderEfficiency(await getJSON("/api/efficiency")); }
  catch(e){ tileFail(body, "Efficiency", loadEfficiency, e); }
}
// §EF — TWO panels, one shared time axis, each with its OWN vertical scale, drawn one above the
// other and never overlaid. A twin-axis overlay is the classic way to make two unrelated series
// look like they explain each other, and that is exactly the open question here: heat depresses
// efficiency, and my cool runs are also my most recent and fittest. Stacked panels let the eye
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
      ? `${p.date} · ${p.ef} · ${U.d(p.km)} ${U.unit} @ ${U.p(p.pace)}/${U.unit} · HR ${p.hr}`
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
    <p class="explain" style="margin-top:6px">Metres per minute per heartbeat, one point per aerobic run
      (average HR at or below the top of Z2${d.z2_top?`, ${d.z2_top} bpm`:""}) of ${Math.round(U.d(d.min_km))} ${U.unit} or more. Running the same pace at a lower
      heart rate <em>is</em> the adaptation — no model in between, just the watch. Measure-first: shown and trended, governing nothing.</p>
    <div class="effrow"><span class="k">Efficiency · ${d.n} runs</span><span class="k">${tr.pct_per_30d!=null?`${tr.pct_per_30d>0?"+":""}${tr.pct_per_30d}%/month${tr.r!=null?` · r ${tr.r}`:""}`:""}</span></div>
    <div class="effplot ef">${effPlot(pts,"ef","ef",tr)}</div>
    <div class="effrow"><span class="k">Temperature</span><span class="k">${esc(tempTxt)}</span></div>
    <div class="effplot temp">${effPlot(pts,"temp_c","temp",null)}</div>
    <div class="effax" style="margin-top:5px"><span>${esc(first?monYr(first.date):"")}</span><span>${esc(last?monYr(last.date):"")}</span></div>
    <p class="explain" style="margin-top:12px"><b>Read the two together.</b> Heat costs you efficiency, and
      your cool runs are also your most recent and fittest — so the gain above is flattered by whatever the weather did. The panels are
      separate and share only their time axis on purpose: nothing here subtracts a temperature correction, because how big that correction
      should be is not settled on your data yet. Hover any point for its pace, distance and heart rate.</p>`;
}
async function loadDurability(){
  const body=$("#durbody"); if(!body) return;              // the card is removed on the public box
  try{ renderDurability(await getJSON("/api/durability")); }
  catch(e){ tileFail(body, "Durability", loadDurability, e); }
}
// ── Durability, build vs build (SH-94) ─────────────────────────────────────
// One card, a Chart/Table switch. CHART: decoupling % (y) against distance km (x) — the current
// build in accent with a solid fit line, ONE past build (chips) in blue dashed, the other past
// builds small and pale; race runs as rings; dashes at 5 % / 10 %; a "5 % horizon" tile per build.
// TABLE: distance-band medians × builds, the race's own decoupling as the last row. The headline
// reads the current build's horizon. PRIVATE-only, like before. View remembered in sh.durView.
let DUR = null;                                 // the last /api/durability payload (clicks re-render)
const durState = {view: null, cmp: null};       // view "chart"|"table"; cmp = the compared past build's key

function durViewPref(){
  try{ const v = localStorage.getItem("sh.durView"); if(v === "chart" || v === "table") return v; }catch(e){}
  return (window.matchMedia && matchMedia("(max-width: 640px)").matches) ? "table" : "chart";
}
function durSetView(v){
  durState.view = v;
  try{ localStorage.setItem("sh.durView", v); }catch(e){}
  drawDurability();
}
// one inline SVG for the marks, HTML for every label (det/axis-legibility's flat rule: no <text>
// in an app.js SVG). The SVG scales by viewBox ONLY (uniform — the dots stay round), so the label
// layer positions in % of the plot box and the 11px type keeps its size at any width. Axes fixed at
// 18–44 km / −4–18 % (the canvas's frame); runs under −4 % clamp onto the axis, counted in the footnote.
function durChartHtml(B, cmpKey){
  const X = km => 44 + (Math.min(Math.max(km, 18), 44) - 18) / 26 * 486;
  const Y = p  => (18 - Math.min(Math.max(p, -4), 18)) / 22 * 260;
  const builds = [...B.past, B.current];
  let clamped = 0;
  const ticksY = [15, 10, 5, 0].map(v =>
    `<span class="durtick y" style="top:${(Y(v) / 300 * 100).toFixed(2)}%">${v} %</span>`).join("");
  const ticksX = [18, 22, 26, 30, 34, 38, 42].map(v =>
    `<span class="durtick x" style="left:${(X(v) / 540 * 100).toFixed(2)}%">${v}</span>`).join("");
  const fitLine = (b, cls) => {
    if(!b.fit) return "";
    const train = b.runs.filter(r => !r.race);
    if(!train.length) return "";
    const a = Math.max(18, Math.min(...train.map(r => r.km))), z = Math.min(44, Math.max(...train.map(r => r.km)));
    return `<line class="${cls}" x1="${X(a).toFixed(1)}" y1="${Y(b.fit.slope * a + b.fit.intercept).toFixed(1)}" x2="${X(z).toFixed(1)}" y2="${Y(b.fit.slope * z + b.fit.intercept).toFixed(1)}"/>`;
  };
  const dots = ["pale", "cmp", "now"].map(role => builds.filter(b =>
    role === "now" ? b.is_current : role === "cmp" ? b.key === cmpKey : !b.is_current && b.key !== cmpKey
  ).map(b => b.runs.map(r => {
    if(r.pct < -4) clamped++;
    const rad = role === "now" ? 5.5 : role === "cmp" ? 4.5 : 3.5;
    const cy = Math.max(Y(r.pct), rad + 0.5);         // over-frame runs pin to the ceiling, not half off it
    return `<circle class="durdot ${role}${r.race ? " ring" : ""}" cx="${X(r.km).toFixed(1)}" cy="${cy.toFixed(1)}" r="${rad}">` +
      `<title>${esc(b.label)} · ${r.km.toFixed(1)} km · ${r.pct.toFixed(1)} %</title></circle>`;
  }).join("")).join("")).join("");
  const cmp = B.past.find(b => b.key === cmpKey);
  const svg = `<svg viewBox="0 0 540 300" aria-hidden="true">`
    + `<line class="durzero" x1="44" x2="530" y1="${Y(0).toFixed(1)}" y2="${Y(0).toFixed(1)}"/>`
    + `<line class="durband5" x1="44" x2="530" y1="${Y(5).toFixed(1)}" y2="${Y(5).toFixed(1)}"/>`
    + `<line class="durband10" x1="44" x2="530" y1="${Y(10).toFixed(1)}" y2="${Y(10).toFixed(1)}"/>`
    + `<line class="duraxis" x1="44" x2="44" y1="0" y2="260"/><line class="duraxis" x1="44" x2="530" y1="260" y2="260"/>`
    + (cmp ? fitLine(cmp, "durfit-cmp") : "") + fitLine(B.current, "durfit-now")
    + dots + `</svg>`;
  return {clamped, html: `<div class="durplot">${svg}${ticksY}${ticksX}<span class="durtick km">km</span></div>`};
}
function durTableHtml(B){
  const builds = [...B.past, B.current];
  const head = builds.map(b =>
    `<div class="h${b.is_current ? " cur" : ""}">${esc(b.label)}${b.pb ? " · PB" : ""}</div>`).join("");
  const cell = (b, v, sub, tint) =>
    `<div class="durcell${b.is_current ? " cur" : ""}${tint ? " " + tint : ""}"><div class="v">${v}</div><div class="n">${sub}</div></div>`;
  const tintOf = v => v < 5 ? "ok" : v < 10 ? "warn" : "danger";
  const rows = B.bands.map((label, i) => `<div class="bandlabel">${esc(label)}</div>` + builds.map(b => {
    const c = b.bands[i];
    return c ? cell(b, `${c.median.toFixed(1)} %`, `${c.n} run${c.n === 1 ? "" : "s"}`, tintOf(c.median))
             : cell(b, "—", "none yet", "empty");
  }).join("")).join("");
  const race = `<div class="bandlabel">Race</div>` + builds.map(b => {
    if(b.race_pct != null) return cell(b, `${b.race_pct.toFixed(1)} %`, "race", tintOf(b.race_pct));
    if(b.is_current && b.race_date){
      const d = new Date(b.race_date + "T00:00:00");
      return cell(b, esc(d.toLocaleDateString(undefined, {day: "numeric", month: "short"})), "race day", "empty");
    }
    return cell(b, "—", "none yet", "empty");
  }).join("");
  return `<div class="durtblwrap"><div class="durtbl" style="grid-template-columns:84px repeat(${builds.length},minmax(0,1fr))">`
    + `<div></div>${head}${rows}${race}</div></div>`
    + `<div class="durtints"><span><i class="sw" style="background:color-mix(in oklab,var(--ok),var(--surface) 84%)"></i> under 5 %</span>`
    + `<span><i class="sw" style="background:color-mix(in oklab,var(--warn),var(--surface) 84%)"></i> 5–10 %</span>`
    + `<span><i class="sw" style="background:color-mix(in oklab,var(--danger),var(--surface) 86%)"></i> over 10 %</span></div>`;
}
function drawDurability(){
  const host = $("#durbody"); if(!host || !DUR || !DUR.builds) return;
  const B = DUR.builds, cur = B.current, past = B.past || [];
  const sw = $("#durswitch");
  if(sw) sw.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === durState.view)));
  const cmp = past.find(b => b.key === durState.cmp) || past[past.length - 1] || null;
  const nTrain = cur.runs.filter(r => !r.race).length;
  const below = b => b.horizon_km != null && b.horizon_km < B.min_km;   // 5 % already passed on the shortest runs
  const headline = below(cur)
    ? `This build passes 5 % before ${Math.round(B.min_km)} km`
    : cur.horizon_km != null
    ? `This build holds under 5 % to about ${cur.horizon_km} km`
    : `${nTrain} long run${nTrain === 1 ? "" : "s"} this build — the line needs 4`;
  let body = "", clamped = 0;
  if(durState.view === "table"){
    body = `<p class="durintro">Median fade of the long runs in each distance band, build by build.
      Read across a row: same distance, different year.</p>` + durTableHtml(B);
  }else{
    const chart = durChartHtml(B, cmp && cmp.key);
    clamped = chart.clamped;
    const chips = past.length ? `<div class="durchips"><span>Compare with</span>` + past.map(b =>
      `<button type="button" data-k="${esc(b.key)}" aria-pressed="${String(cmp && b.key === cmp.key)}">${esc(b.label)}${b.pb ? " (PB)" : ""}</button>`).join("") + `</div>` : "";
    const legend = `<div class="durlegend"><span><i class="sw" style="background:var(--accent)"></i> This build</span>`
      + (cmp ? `<span><i class="sw" style="background:var(--durcmp)"></i> ${esc(cmp.label)} build</span>` : "")
      + `<span><i class="sw ring"></i> race</span><span>dashed: 5 % and 10 %</span></div>`;
    const tiles = [cur, ...past].map(b => {
      const v = below(b) ? `before ${Math.round(B.min_km)} km` : b.horizon_km != null
        ? (b.extrapolated && b.longest_km != null ? `beyond ${Math.round(b.longest_km)} km` : `${b.horizon_km} km`) : "—";
      const nt = b.is_current ? `${nTrain} run${nTrain === 1 ? "" : "s"} so far` : b.pb ? "PB build" : "";
      return `<div class="durhz-t${b.is_current ? " now" : cmp && b.key === cmp.key ? " cmp" : ""}">`
        + `<div class="yr">${esc(b.label)}</div><div class="km">${v}</div><div class="nt">${nt}</div></div>`;
    }).join("");
    body = `<p class="durintro">Fade (aerobic decoupling) against the distance of each run of
      ${Math.round(B.min_km)} km or more. Longer runs fade more, so compare at the same distance:
      a line further right holds longer.</p>`
      + chips + chart.html + legend
      + `<div class="durhz" style="grid-template-columns:repeat(${past.length + 1},minmax(0,1fr))">${tiles}</div>`;
  }
  // the canvas footnote, numbers computed: the current line's base, the effort context against the
  // oldest past build (median HR decides "easier"/"harder"), and the count clamped off the chart.
  let foot = `Runs of ${Math.round(B.min_km)} km or more in the 20 weeks before each race.`;
  if(nTrain) foot += ` The ${esc(cur.label.replace(" · now", ""))} line rests on ${nTrain} run${nTrain === 1 ? "" : "s"}`;
  const hrs = b => b.runs.filter(r => !r.race && r.hr).map(r => r.hr);
  const old = past[0], ch = hrs(cur), oh = old ? hrs(old) : [];
  if(nTrain && old && ch.length && oh.length){
    const med = a => a.slice().sort((x, y) => x - y)[Math.floor((a.length - 1) / 2)];
    foot += `, run ${med(ch) <= med(oh) ? "easier" : "harder"} (HR ${Math.min(...ch)}–${Math.max(...ch)}) than ${esc(old.label)}'s (${Math.min(...oh)}–${Math.max(...oh)})`;
  }
  if(nTrain) foot += ".";
  if(clamped) foot += ` ${clamped} run${clamped === 1 ? "" : "s"} below −4 % sit${clamped === 1 ? "s" : ""} on the chart's floor.`;
  host.innerHTML = `<h2 class="durhead">${esc(headline)}</h2>${body}<p class="durfoot">${foot}</p>`;
  host.querySelectorAll(".durchips button[data-k]").forEach(b =>
    b.addEventListener("click", () => { durState.cmp = b.dataset.k; drawDurability(); }));
}
function renderDurability(d){
  const host = $("#durbody"); if(!host) return;
  if(!d || d.ok===false){
    host.innerHTML=`<div class="empty">No long runs with decoupling yet — the tracker starts once a run of ${Math.round(U.d(d&&d.min_km?d.min_km:16))}&nbsp;${U.unit} or more lands with HR data.</div>`;
    return;
  }
  DUR = d;
  if(!durState.view) durState.view = durViewPref();
  if(!durState.cmp){
    const past = (d.builds && d.builds.past) || [];
    durState.cmp = ((past.find(b => b.pb) || past[past.length - 1] || {}).key) || null;
  }
  // the Chart/Table switch rides in the card's title row (index.html owns the title; built once)
  const title = document.querySelector("#durcard .acwr-title");
  if(title && !$("#durswitch")){
    const swEl = document.createElement("span");
    swEl.id = "durswitch"; swEl.setAttribute("role", "group"); swEl.setAttribute("aria-label", "View");
    swEl.innerHTML = `<button type="button" data-v="chart" aria-pressed="true">Chart</button><button type="button" data-v="table" aria-pressed="false">Table</button>`;
    swEl.addEventListener("click", e => {
      const b = e.target.closest("button[data-v]"); if(b) durSetView(b.dataset.v);
    });
    title.appendChild(swEl);
  }
  drawDurability();
}
// Opportunistic page-load sync (private only): pull any activities synced to Runalyze since the
// last sync — throttled server-side — so a run finished earlier today lands without the manual
// button, and the readiness tile flips to "done ✓". Silent + non-blocking. A real (non-skipped)
// sync always re-reads readiness: the HRV snapshot can move with zero new activities (the morning
// pull after the watch uploads last night's reading), and the tile must not keep a verdict computed
// from pre-sync data. The full page refresh still fires only when an activity actually arrived,
// so a quiet load never flickers the heavier tiles.
async function touchSync(){
  // §DEMO — there is no account to sync from, and the demo box refuses the route anyway. Calling it
  // would 403 on every page load, which a visitor sees in the console and, worse, in the sync tile.
  // Skipping it is the honest no-op: the synthetic history is already complete.
  if (SH_DEMO) return;
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
    qhint("Projected acute:chronic load at this week's end — rolled forward from today's fitness if you run the plan. The engine sizes every planned week against 1.25, but it applies that bound to a shape-neutral reading of the ratio; this number is the raw end-of-week sample, which lands on the long-run day and reads higher — so above 1.30 on a hard building week is normal, not a ceiling that moved. The governed number is published beside it. A taper or down week reads low on purpose, and race week reads high on purpose: it carries the race itself, a fixed distance at a fixed pace that the governor sizes the training around rather than trimming.");
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
// browser error rather than something this app is telling the user) and impossible to assert on. The
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
let AI={narration:true,parsing:true,judgment:false};   // 0.56.0 §S5 — the switches, from /healthz
let OBJECTIVES=[], LASTDIFF=null, LLM_OK=false, LOG=null, RDY=null,
    ZONESD=null;   // §W1 — current training_zones payload (rides /api/readiness + /api/zones); null on public
let TOKEN_OK=false, HAS_SHAPE=false;   // first-run signals (from /healthz + /api/shape)
let LAST_PLAN=null;   // §CHAIN3 — the last /api/plan payload, so a race-selector click can re-render
                      // the header locally instead of re-fetching the plan
let RACE_SEL = (()=>{ try{ return localStorage.getItem("sh.raceSel"); }catch(e){ return null; } })();
// (§CHAIN3 — a date, or null for the headline. Validity against the CURRENT plan's own chain is
// checked at render time (renderPlan), never here: a selection from a since-regenerated plan whose
// chain moved on must fall back to the headline, not dangle.)
// SH-86 (slice 2) — the plan page's own view state, kept across re-renders: the week the road and
// the week pane show, the races drawer's open state, which source panel is open, and the limit row
// to render open on the next pass (cleared after use).
let WEEK_SEL=null, RACES_OPEN=false, PLAN_OPEN=null, LIM_OPEN=null;
// §CHAIN4 — which chain leg the console shows: the stored selection when it names a leg of THIS
// plan's chain, else the NEXT leg still ahead, else null (= the plan's own headline objective).
// The engine's headline is the chain's terminal race — the periodization's anchor; the athlete is
// training for the race that comes first. Single-race plans always answer null: untouched.
function viewRaceOf(p, today){
  const chain = (p && p.chain) || [];
  if(chain.length < 2) return null;
  return chain.find(c=>c.date===RACE_SEL) || chain.find(c=>c.date>=today) || null;
}
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
    // The race form lives in the races drawer (#racesDrawer) inside #plan's objManager, which only
    // renders once a plan exists. On a fresh instance (history pulled, no plan yet) generate one
    // first so the form is there to focus; then open the drawer so the form is VISIBLE to focus.
    let inp=$("#ao_label");
    if(!inp){ const gen=$("#planBtn"); if(gen) gen.click();
      for(let i=0; i<20 && !inp; i++){ await new Promise(r=>setTimeout(r,150)); inp=$("#ao_label"); } }
    if(!RACES_OPEN){ const rbtn=$("#racesBtn"); if(rbtn) rbtn.click(); }
    if(inp){ scrollToEl(inp, "center"); inp.focus(); }
  });
}
function objManager(p){
  // Priority chip: static on the public view; an inline A|B|C selector on the private console
  // (clicking a letter POSTs /priority and re-periodizes — same path as the adjudication "Set B").
  const priBadge = o => SH_READONLY
    ? `<span class="pr ${esc(o.priority)}">${esc(o.priority)}</span>`
    : `<span class="prsel" role="group" aria-label="priority for ${esc(o.label)}">${['A','B','C'].map(x=>
        `<button type="button" class="prseg ${x===o.priority?'on':''}" data-oid="${o.id}" data-pri="${x}" title="Set priority ${x}">${x}</button>`).join("")}</span>`;
  // 0.55.1 (review U1) — on the public box the row is TEXT: the date picker and the remove button
  // used to render there too (the early return below came after the template had already built
  // them), so a visitor's first click on the showcase was a control that answered 403.
  const dateCell = o => SH_READONLY
    ? `<span class="odate-ro mono">${esc(o.date)}</span>`
    : `<input type="date" class="odate" data-oid="${o.id}" value="${esc(o.date)}"
        aria-label="date of ${esc(o.label)}" title="Move this race — the plan re-periodizes around the new day">`;
  // §GUN — the gun time is display-only (the engine periodizes to the DAY), so editing it is a
  // light write: POST /time, update the local row, re-render — no re-plan. Public: text, like the
  // date (0.55.1 — no dead controls on the read-only box).
  const timeCell = o => SH_READONLY
    ? (o.start_time ? ` · <span class="otime-ro mono">${esc(o.start_time)}</span>` : "")
    : ` · <input type="time" class="otime" data-oid="${o.id}" value="${esc(o.start_time||"")}"
        aria-label="gun time of ${esc(o.label)}" title="Gun time (optional) — the countdown in the bar above targets it; leave empty for midnight">`;
  const rows = OBJECTIVES.filter(o=>o.status==='upcoming').map(o=>{
    // §CHAIN4b — the highlighted row is the race the console is SHOWING (viewRaceOf: the tapped leg,
    // else the next race ahead), not the engine's terminal anchor; it re-renders with every strip tap.
    const cur = viewRaceOf(p, (LOG&&LOG.today)||new Date().toISOString().slice(0,10)) || p.objective;
    const isCurrent = !!(cur && o.label===cur.label && o.date===cur.date);
    return `<div class="obj ${isCurrent?'current':''}">
      ${priBadge(o)}
      <span>${esc(o.label)}${isCurrent?' <span class="muted mono" style="font-size:11px">· current</span>':''}</span>
      <span class="od">${dateCell(o)}${timeCell(o)} · ${esc(o.type)} · ${esc(o.target)}</span>
      ${SH_READONLY?'':`<button class="x" data-oid="${o.id}">remove</button>`}
    </div>`;}).join("") || `<div class="muted" style="font-size:13px">No objectives — maintenance mode.</div>`;
  if(SH_READONLY) return `<div class="objs">${rows}</div>`;   // public: list only, no controls
  // §RL — past races (private-only: results are personal, same posture as the §6s reckoning).
  const pastChip = o => {
    let oc = {}; try{ oc = JSON.parse(o.outcome||"{}"); }catch(e){}
    if(o.status==='lapsed') return `<span class="muted">lapsed — no race-day run</span>`;
    if(oc.status==='dnf') return `<span>DNF at ${U.d(oc.dnf_km)} ${U.unit}</span>`;
    if(oc.status==='finished'){
      const vs = oc.beat===true ? ' · beat the goal' : oc.beat===false ? ` · goal ${esc(oc.goal)} missed` : '';
      return `<span>✓ ${esc(oc.actual||'finished')}${vs}</span>`;
    }
    return `<span class="muted">run — result unverified</span>`;
  };
  const past = OBJECTIVES.filter(o=>o.status==='done'||o.status==='lapsed')
    .sort((a,b)=>b.date.localeCompare(a.date)).slice(0,5).map(o=>
      `<div class="obj past"><span class="pr ${esc(o.priority)}">${esc(o.priority)}</span>
        <span>${esc(o.label)}</span>
        <span class="od">${esc(o.date)} · ${esc(o.type)} · ${pastChip(o)}</span></div>`).join("");
  const pastRow = past ? `<div class="muted" style="font-size:11px;margin-top:8px">Past races</div>
    <div class="objs">${past}</div>` : "";
  // §OBJ3 — a race removed in the last 24 h stays listed with a put-back control (the undo of a slip)
  const today=(LOG&&LOG.today)||new Date().toISOString().slice(0,10);
  const recent=OBJECTIVES.filter(o=>o.status==='removed' && o.removed_at && o.date>=today
      && (Date.now()-Date.parse(o.removed_at)) < 24*3600*1000)
    .sort((a,b)=>String(b.removed_at).localeCompare(String(a.removed_at)));
  const removedRow = recent.length ? `<div class="muted" style="font-size:11px;margin-top:8px">Removed in the last 24 hours</div>
    <div class="objs">${recent.map(o=>
      `<div class="obj removed"><span class="pr ${esc(o.priority)}">${esc(o.priority)}</span><span>${esc(o.label)}</span><span class="od">${esc(o.date)} · ${esc(o.type)} · removed</span><button class="putback" data-oid="${o.id}" title="Put this race back on the calendar — the plan re-anchors around it">put back</button></div>`
    ).join("")}</div>` : "";
  // SH-86 (slice 2) — the races drawer keeps the calendar mechanics (rows, dates, gun times,
  // priority, remove/put-back, past races, the add form); the AI goal-parse row and the A-race
  // adjudication advice no longer render (SH-88 removes them with the backend).
  return `<div class="objs">${rows}</div>
    ${removedRow}
    <div class="addobj">
      <input id="ao_label" placeholder="race name" style="width:130px">
      <select id="ao_type" aria-label="Race type"><option>5k</option><option>10k</option><option>half</option><option selected>marathon</option><option>custom</option></select>
      <input id="ao_date" type="date" aria-label="Race date">
      <input id="ao_time" type="time" aria-label="Start time (optional)" title="Gun time (optional) — the countdown targets it">
      <select id="ao_pri" aria-label="Priority"><option value="A">A</option><option value="B">B</option><option value="C">C</option></select>
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
    return `<li><b>${esc(r.label)}</b> → <span class="pr ${r.suggested_priority}">${r.suggested_priority}</span>${r.id===d.primary_id?' <span class="muted mono" style="font-size:11px">· peak</span>':''}
      <div class="adjmeta">${esc(r.reason)}</div>
      ${changed?`<button class="applyrec" data-id="${r.id}" data-pri="${r.suggested_priority}" style="font-size:11px;padding:3px 9px;margin-top:4px">Set ${r.suggested_priority}</button>`:''}</li>`;
  }).join("");
  host.innerHTML=`<div class="adjudbox">
    <div class="exh">${esc(d.summary||'')}</div>
    <ul class="expts">${recs}</ul>
    <div class="exfoot">Claude advises; the engine periodizes from the priorities you keep.</div>
  </div>`;
  host.querySelectorAll(".applyrec").forEach(b=>b.addEventListener("click", async ()=>{
    if(b.disabled) return;   // §OBJ2
    const row=b.closest(".obj");   // the adjudication list isn't .obj rows — no-op when absent
    b.disabled=true; if(row) row.setAttribute("aria-busy","true");
    try{
      const r=await fetch(`/api/objectives/${b.dataset.id}/priority`,{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({priority:b.dataset.pri})});
      const p=await r.json();
      if(!p.ok){ await notice({title:"Could not set priority", intro:p.error||"unknown", tone:"danger"}); return; }
      LASTDIFF=p.diff; await refreshPlan(p);
    } finally{ b.disabled=false; if(row) row.removeAttribute("aria-busy"); }
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
      // fill the structured form — the user reviews, then click Add objective
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
    if(add.disabled) return;   // §OBJ2 — a second click while a re-plan is in flight is a no-op
    const body={label:$("#ao_label").value||"Race", type:$("#ao_type").value,
      date:$("#ao_date").value, priority:$("#ao_pri").value, target:$("#ao_target").value||"finish",
      start_time:$("#ao_time").value||null};   // §GUN — optional gun time; empty counts to midnight
    if(!body.date){ await notice({title:"Pick a date", intro:"A race needs a day before it can be planned for."}); return; }
    const t=add.textContent; add.disabled=true; add.textContent="Planning…";
    try{
      const r=await fetch("/api/objectives",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const p=await r.json();
      if(!p.ok){ await notice({title:"Could not add the race", intro:p.error||"unknown", tone:"danger"}); return; }
      LASTDIFF=p.diff; await refreshPlan(p);
    } finally{ add.disabled=false; add.textContent=t; }
  });
  // §GM — move a race to a new day. This is an EDIT, not add-then-remove: the objective keeps its
  // row (and with it its id, its history and, once run, its result), and the banked plans keep
  // pointing at the same race, so the founding road and the §FT4 prediction ledger survive the move.
  document.querySelectorAll(".obj .odate").forEach(inp=>{
    const was=inp.value;
    inp.addEventListener("change", async ()=>{
      if(!inp.value || inp.value===was){ inp.value=was; return; }
      inp.disabled=true;
      const r=await fetch(`/api/objectives/${inp.dataset.oid}/date`,{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({date:inp.value})});
      const p=await r.json();
      if(!p.ok){ inp.value=was; inp.disabled=false;
        await notice({title:"Could not move the race", intro:p.error||"unknown", tone:"danger"}); return; }
      LASTDIFF=p.diff; await refreshPlan(p);
    });
  });
  // §GUN — set/clear a race's gun time. DISPLAY-ONLY (no re-plan happens server-side), so unlike
  // the date-mover this skips refreshPlan: update the local OBJECTIVES row and re-render off the
  // plan already in hand — the bar's countdown re-arms onto the new target in the same pass.
  document.querySelectorAll(".obj .otime").forEach(inp=>{
    const was=inp.value;
    inp.addEventListener("change", async ()=>{
      if(inp.value===was) return;
      inp.disabled=true;
      let p;
      try{
        const r=await fetch(`/api/objectives/${inp.dataset.oid}/time`,{method:"POST",
          headers:{"Content-Type":"application/json"},body:JSON.stringify({start_time:inp.value||null})});
        p=await r.json();
      }catch(e){ p={ok:false, error:"the server could not be reached — try again"}; }
      if(!p.ok){ inp.value=was; inp.disabled=false;
        await notice({title:"Could not set the gun time", intro:p.error||"unknown", tone:"danger"}); return; }
      const o=OBJECTIVES.find(x=>String(x.id)===inp.dataset.oid);
      if(o) o.start_time=p.start_time||null;
      renderPlan(LAST_PLAN);
    });
  });
  document.querySelectorAll(".obj .x").forEach(btn=>btn.addEventListener("click", async ()=>{
    if(btn.disabled) return;   // §OBJ2
    const o=OBJECTIVES.find(x=>String(x.id)===btn.dataset.oid);
    if(!await confirmDanger({title:`Remove ${o?o.label:"this race"}?`,
        intro:"It comes off the calendar and the plan re-anchors around the races that remain (maintenance if none are left).",
        lines: o?[[o.date,o.type,o.target].join(" · ")]:[],   // confirmDanger escapes each line itself
        alt:"It stays listed under the calendar for 24 hours with a put-back control, in case this was a slip.",
        confirmLabel:"Remove"})) return;
    const row=btn.closest(".obj");
    btn.disabled=true; if(row) row.setAttribute("aria-busy","true");
    try{
      const r=await fetch(`/api/objectives/${btn.dataset.oid}/remove`,{method:"POST"});
      const p=await r.json();
      if(!p.ok){ await notice({title:"Could not remove the race", intro:p.error||"unknown", tone:"danger"}); return; }
      LASTDIFF=p.diff; await refreshPlan(p);
    } finally{ btn.disabled=false; if(row) row.removeAttribute("aria-busy"); }
  }));
  // §OBJ3 — put a removed race back: the undo of remove, no confirm.
  document.querySelectorAll(".obj .putback").forEach(btn=>btn.addEventListener("click", async ()=>{
    if(btn.disabled) return;   // §OBJ2
    const row=btn.closest(".obj");
    btn.disabled=true; if(row) row.setAttribute("aria-busy","true");
    try{
      const r=await fetch(`/api/objectives/${btn.dataset.oid}/restore`,{method:"POST"});
      const p=await r.json();
      if(!p.ok){ await notice({title:"Could not put the race back", intro:p.error||"unknown", tone:"danger"}); return; }
      LASTDIFF=p.diff; await refreshPlan(p);
    } finally{ btn.disabled=false; if(row) row.removeAttribute("aria-busy"); }
  }));
  // inline A|B|C priority selector — set a priority and re-periodize (no-op if already that letter)
  document.querySelectorAll(".obj .prseg").forEach(b=>b.addEventListener("click", async ()=>{
    if(b.classList.contains("on") || b.disabled) return;   // §OBJ2
    const row=b.closest(".obj");
    b.disabled=true; if(row) row.setAttribute("aria-busy","true");
    try{
      const r=await fetch(`/api/objectives/${b.dataset.oid}/priority`,{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({priority:b.dataset.pri})});
      const p=await r.json();
      if(!p.ok){ await notice({title:"Could not set priority", intro:p.error||"unknown", tone:"danger"}); return; }
      LASTDIFF=p.diff; await refreshPlan(p);
    } finally{ b.disabled=false; if(row) row.removeAttribute("aria-busy"); }
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
function adjBanner(p){
  if(SH_READONLY) return "";   // public: no qualitative-adjustment (medical) banner
  const a=p.adjustment;
  return a ? `<div class="${(a.medical||a.medical_flag)?'adjmed':'adjbox'}">
      <div class="adjh">${(a.medical||a.medical_flag)?'⚠ Medical flag':'Active adjustment'} — ${adjEffect(a)}</div>
      <div class="adjmeta">“${esc(a.note)}” · ${esc(a.applies_from)}→${esc(a.applies_until)}${a.summary?` · ${esc(a.summary)}`:''}</div>
      ${a.clamp?`<div class="adjclamp">engine clamp: ${esc(a.clamp)}</div>`:''}
      ${(a.medical||a.medical_flag)?`<div class="adjclamp">Sparing Horse tracks &amp; flags — it never diagnoses. Clear this once your doctor signs off.</div>`:''}
      <button id="adj_clear" style="font-size:11px;padding:4px 9px;margin-top:8px">Clear adjustment</button>
    </div>` : "";
}
function adjustmentUI(p){
  if(SH_READONLY) return "";   // public: no qualitative-adjustment controls or (medical) banner
  const ask = `<div class="adjask">
      <input id="adj_text" ${(LLM_OK&&AI.judgment)?'':'disabled'} placeholder="How'd it go / how are you? e.g. “felt great today”, “knee’s a bit sore”, “travelling Mon–Fri”">
      <button id="adj_propose" ${(LLM_OK&&AI.judgment)?'':'disabled'}>💬 Tell the horse</button>
      <div class="adjhint">A run that's done → I'll <b>log it</b> · something ahead (a niggle, travel, a cold) → I'll <b>propose easing</b> the plan. I only ever ease or hold — never push harder.</div>
      <div id="adj_preview" class="adjpreview">${LLM_OK?(AI.judgment?'':'<div class="adjclamp">⚙ Free-text adjustments are switched off in Settings → AI features — your words stay on this box</div>'):'<div class="adjclamp">⚙ Add a Claude API key in Settings to enable — the engine still clamps every suggestion.</div>'}</div>
    </div>`;
  return adjBanner(p) + ask;
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
  resilience:"Builds physiological resilience — how little your VO₂max, SSmax and economy decay over "+U.d(42.2,U.mi?1:0)+" "+U.unit+" (the marathon's modern fourth component). Built by long runs with marathon-pace segments, which the assertive plan extends at CONSTANT speed — longer, not faster."};
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
  const act = s.actual ? `<span class="sact">${U.d(s.actual.km)}${U.k}${s.actual.pace?` @ ${U.p(s.actual.pace)}`:''}</span>` : "";
  const refl = s.reflection ? `<div class="srefl">📓 ${esc(s.reflection)}</div>` : "";
  const clk = !SH_READONLY && (s.done||s.unplanned) && s.activity_id;   // a completed/extra run → view its run + map
  const plan = s.unplanned ? '<span class="splan exu">unplanned</span>' : planLabel;
  // a DOUBLE (≥2 runs that day): break the combined actual into its per-run halves, each map-linkable
  const brk = (s.runs && s.runs.length>1)
    ? `<div class="srun" title="${s.runs.length} runs this day">↳ ${s.runs.map(r=>{
          const rc = !SH_READONLY && r.activity_id;
          return `<span class="brkrun"${rc?` data-act-id="${r.activity_id}" role="button" tabindex="0" title="View this run on the map"`:''}>${U.d(r.km)}${U.k}${r.pace?` @ ${U.p(r.pace)}`:''}</span>`;
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
  if(r&&r.pace_slower_than&&r.pace_slower_than.fmt) return `slower than ${U.p(r.pace_slower_than.fmt)}/${U.unit}`;
  if(r&&r.pace_target&&r.pace_target.fmt) return `≈ ${U.p(r.pace_target.fmt)}/${U.unit}`;
  const p=((rep&&rep.pace_zone)||"").split("/km")[0];   // fallback: the plan's own pace at generation time
  return p?`≈ ${U.p(p)}/${U.unit}`:"—";
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
    `<tr><td>${esc(what)}</td><td>${min}′</td><td>${km?`~${U.d(km).toFixed(1)}${U.k}`:"—"}</td>`+
    `<td>${esc(segPace(zone,rep))}</td><td>${esc(segHR(zone))||"—"}</td></tr>`);
  if(wu.length){ row("Warm-up · easy","easy",sum(wu,"minutes"),sum(wu,"km"),wu[0]); cue.push(`${sum(wu,"minutes")}′ easy`); }
  if(base.length){ row("Easy aerobic base","easy",sum(base,"minutes"),sum(base,"km"),base[0]); cue.push(`${sum(base,"minutes")}′ easy base`); }
  // 0.63.3 — "N × M′ @ zone" is the shape of an INTERVAL session: every work rep the same length at
  // the same zone. A progression run (§PROG) carries three work reps at three zones — settle in
  // easy, cruise at marathon pace, close at threshold — and the same row built from work[0] read
  // "3 × 20′ @ easy": a session the athlete would have run entirely easy. Reps that differ get a
  // row each, in order, with the engine's own detail as the label.
  const homogeneous=work.length>1&&work.every(r=>r.zone===work[0].zone&&r.minutes===work[0].minutes);
  if(work.length>1&&!homogeneous){
    work.forEach(r=>{ row(`${r.minutes}′ ${r.detail||`@ ${r.zone}`}`,r.zone,r.minutes,r.km,r); });   // row() escapes
    cue.push(work.map(r=>`${r.minutes}′ @ ${r.zone}`).join("  →  "));
  }else if(work.length>1){
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
    <div class="wctot">Total ${U.d(s.km)} ${U.unit} · ~${s.minutes}′ · load ${s.trimp!=null?Math.round(s.trimp):"—"} TRIMP${ZONESD?"":" · paces from plan"+(SH_READONLY?"":"; HR bands load with zones")}</div></div>`;
}
// §6f Step F — compact label for a planned session. Structured quality sessions (intervals / MP
// long run / tempo, carried as `reps`) read their structure; plain runs show distance.
function sessSummary(s){
  // A session with no prescribed distance has nothing to summarise — say so rather than
  // interpolating the absence. Reached when `km` is null (an unplanned run's row) and the flag
  // that would have routed the caller elsewhere is missing; the public allowlist withheld exactly
  // that flag once, and the line rendered "nullk". A label is not the place to learn about it.
  if(s.km==null) return "—";
  if(s.reps&&s.reps.length){
    const work=s.reps.filter(r=>r.effort==='work');
    if(s.kind==='interval'&&work.length) return `${work.length}×${work[0].minutes}′ ${work[0].zone}`;
    if(s.kind==='progression'&&work.length) return `${U.d(s.km)}${U.k} · progression to ${work[work.length-1].zone}`;   // 0.63.3
    if(s.kind==='long_mp'){ const mp=work.find(r=>r.zone==='marathon'); return `long ${U.d(s.km)}${U.k} +${mp?mp.minutes:0}′ MP`; }
    if((s.kind==='tempo'||s.kind==='race_pace')&&work.length) return `${U.d(s.km)}${U.k} · ${work.reduce((a,r)=>a+r.minutes,0)}′ ${work[0].zone}`;
  }
  // strides ride a specific run (the week's first short easy one) — say so on THAT line, where
  // they're executed, instead of the old orphaned "strides×2" note floating under the week.
  // Plans stored before the s.strides flag carry the fact only in the engine-authored note text,
  // so fall back to that until the nightly re-plan refreshes the JSON.
  const st = s.strides || +(((s.note||"").match(/(\d+)×4–6 strides/)||[])[1]||0);
  if(st) return `${U.d(s.km)}${U.k} <span class="strid" title="Finish this run with ${st} × 4–6 strides: ~20-second relaxed-fast accelerations with full walk-back recovery. A neuromuscular touch-up, not a workout — it adds no training load.">+ ${st}× strides</span>`;
  // §RACE — the race is a session now, and it must READ as one. Without this the row printed a bare
  // distance, indistinguishable from a long run, on the day the whole block points at; the flag is
  // published for exactly this (§PV `sessions[].race`) and nothing was reading it.
  if(s.race) return `${U.d(s.km)}${U.k} <span class="racemk" title="${esc(s.note||"Race day")} — the race itself, at ${esc(U.t(s.pace_zone||"race pace"))}. A fixed prescription: the distance and the pace are the race's, so the governor sizes the training around it rather than trimming it.">· race day</span>`;
  return `${U.d(s.km)}${U.k}`;
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
// The 7 ISO dates of the week, same all-UTC arithmetic (the rest-day fill below walks them).
const weekDates = mon => { const d=new Date(mon+"T00:00:00Z"), out=[];
  for(let i=0;i<7;i++){ out.push(d.toISOString().slice(0,10)); d.setUTCDate(d.getUTCDate()+1); }
  return out; };
// week containing today. Quality sessions are accented; the ACWR badge rides the right rail.
// §LIMITS (0.58.0) — the body's limits, one row per week: each axis with its ceiling, what the
// week was laid at, and which one HELD the week. `basis` says where the ceiling comes from
// (L literature · A fitted · S structural). SH-86 turns each row into the reason and the source:
// a plain sentence about THIS week, and on click the named study or guideline behind the number.
const LIMIT_NAMES={acwr:"load ratio",long_step:"long-run step",eq_week:"impact load, week",eq_session:"impact load, single run",tissue:"weeks near the ceiling",chronic:"fitness gain per week"};
const BASIS={
  L:{label:"Literature", desc:"Literature: published science or an established coaching convention."},
  A:{label:"Fitted to one runner", desc:"Fitted: measured on the history of the athlete this engine was calibrated on. If that is not you, treat the number as a starting point."},
  S:{label:"Structural", desc:"Structural: a definition or a window, not a safety threshold."}};
const limV=(v,u)=>(v==null)?v:(u==="km"||u==="eq-km")?U.d(v):v;            // a limit's number, in the display unit
const limU=u=>u==="km"?U.unit:u==="eq-km"?"eq-"+U.unit:u;
// SH-86 — the source behind each number the plan shows: what it is, the rule, where the number comes
// from, how the engine uses it. Generic on purpose: every install reads it, so nothing here names one
// runner's history as the reader's own.
const PLAN_SOURCES={
  chronic:{title:"Fitness gain per week", basis:"L",
    what:"How much fitness may rise in one week. Fitness here is CTL: the average training load of the last 42 days.",
    rule:"At most +5 CTL points a week.",
    refs:[{cite:"Friel J. Why ramp rate is an important training metric. TrainingPeaks.", finding:"About 5 to 8 CTL points a week suits most athletes; more than that starts to become crash training."},
          {cite:"Couzens A. CTL ramp rates, TSB floors and loading patterns. alancouzens.com.", finding:"Over years of coaching, 5 to 8 a week proved too aggressive for long blocks; 3 to 5 is the recommendation instead."}],
    use:"The app holds 5, where the two guidelines meet. It catches a slow, steady over-build that the load ratio, which compares this week with the base, does not see.",
    caveat:"Coaching guidance from experience, not a controlled study."},
  acwr:{title:"Load ratio", basis:"L",
    what:"This week's training load divided by the base: the 7-day average of the load (ATL) over the 42-day average (CTL).",
    rule:"The plan sizes each week so that a shape-neutral reading of the ratio stays at or under 1.25. The raw reading at the end of a hard week lands on the long-run day and can print above 1.30; that is normal. Race week carries the race, which is fixed.",
    refs:[{cite:"Gabbett TJ. The training-injury prevention paradox: should athletes be training smarter and harder? Br J Sports Med 2016;50(5):273-280.", finding:"In team sports, ratios of about 0.8 to 1.3 went with the fewest injuries and 1.5 or more with the most. The data came from cricket, Australian football and rugby league; the author advises caution for individual-sport athletes."},
          {cite:"Impellizzeri FM et al. Acute:chronic workload ratio: conceptual issues and fundamental pitfalls. Int J Sports Physiol Perform 2020;15(6):907-913.", finding:"No study has shown that changing the ratio changes injury rates, and the ratio has statistical flaws."},
          {cite:"Frandsen et al. Br J Sports Med 2025;59(17):1203-1210.", finding:"In 5,200 runners followed for 18 months, the ratio showed no link with injury."}],
    use:"A ceiling, never a target: the plan stays under it with a margin and does not push the load up to it.",
    caveat:"Contested evidence, drawn mostly from team sports."},
  long_step:{title:"Long-run step", basis:"L",
    what:"How much longer this week's longest run may be than the longest run of the last four weeks.",
    rule:"At most 10 % longer. A longer run is never laid; the kilometres go to the easy runs instead.",
    refs:[{cite:"Frandsen et al. How much running is too much? Identifying high-risk running sessions in a 5200-person cohort study. Br J Sports Med 2025;59(17):1203-1210.", finding:"A single run more than 10 % longer than the longest run of the previous 30 days raised the rate of overuse injury. Week-to-week volume measures did not predict it."}],
    use:"A hard cap, chosen 1 Jul 2026. The week's total load stays the same; only the single longest run is held.",
    caveat:"One observational study; injuries were self-reported."},
  eq_week:{title:"Impact load, week", basis:"A",
    what:"The week's kilometres weighted by speed, because a fast kilometre loads tendons and bones more than an easy one: marathon pace counts 1.4×, threshold 2.5×, intervals 3.5×.",
    rule:"At most 1.3× the heaviest week of the last four, counting the runs actually run.",
    refs:[{cite:"Davis JJ. Marathon Excellence for Everyone; Running Writings.", finding:"Running injuries are mechanical: the damage a run does rises steeply with speed, which heart-rate load does not see."},
          {cite:"Fitted: a replay of the calibration athlete's 1,078 runs over 4.7 years (2 Jul 2026).", finding:"These weights and the 1.3 step catch the one known overload in that history (March 2022, calf and hip) and would have braked 3 weeks that were absorbed well; Davis's published weights braked 7."}],
    use:"When it binds, fast running is eased to easy pace and the aerobic kilometres stay.",
    caveat:"Fitted on a single runner. For anyone else these weights are a starting point."},
  eq_session:{title:"Impact load, single run", basis:"S",
    what:"The same speed-weighted load, read on a single run instead of the whole week.",
    rule:"One run may weigh at most 1.3× the heaviest single run of the last four weeks.",
    refs:[{cite:"The same step as the weekly impact load (Davis's framing, fitted).", finding:"One number read at two scales, so a single big session cannot hide inside a normal week."}],
    use:"It stops one session from carrying a jump the weekly total would average away.",
    caveat:""},
  tissue:{title:"Weeks near the ceiling", basis:"L",
    what:"How many weeks in a row have run close to the load ceiling (a load ratio of 1.20 or more).",
    rule:"After 4 such weeks in a row, a down week is forced. The peak is exempt: the taper that follows is its recovery.",
    refs:[{cite:"Periodisation convention: three loading weeks, then one recovery week (3:1).", finding:"A named source is still to be added."}],
    use:"A safety net behind the planned down weeks: at 4, the plan's own down weeks normally come first. Set to 4 on 6 Sep 2026.",
    caveat:"A convention, not a measured threshold."},
  finish:{title:"Finish-time range", basis:"A",
    what:"The time range the engine expects for this race: 8 races in 10 should land inside it.",
    rule:"Race pace from the speed current fitness supports, adjusted for the fitness carried on race day and for how the long runs compare with the race distance.",
    refs:[{cite:"Daniels J. Daniels' Running Formula.", finding:"Race pace as a fixed share of the speed VO₂max supports."},
          {cite:"Fitted to the calibration athlete's four marathons (fitness CTL 67, 92, 98 and 109 on race day).", finding:"Below a fitness floor, pace fades about 0.8 % per CTL point; three of the four races land within 1.5 % of the model."}],
    use:"The range is the headline and the median the trend. It narrows as runs land and races calibrate it.",
    caveat:"Race day also rides on fuelling, pacing, weather and the day's legs."},
  paces:{title:"Training paces", basis:"L",
    what:"The training paces, each a share of the speed current VO₂max supports.",
    rule:"Easy up to 72 % of that speed, marathon pace 81 %, 5K pace 95 %; LT1 marks the top of truly easy running.",
    refs:[{cite:"Daniels J. Daniels' Running Formula.", finding:"Training zones as fixed fractions of the speed at VO₂max."},
          {cite:"Davis JJ on the first lactate threshold (Running Writings).", finding:"Easy running belongs below the first lactate threshold."}],
    use:"Paces follow fitness and are re-read every night; the Effort discipline panel shows how the easy runs measure up.",
    caveat:""},
  posture:{title:"Posture: assertive or conservative", basis:"S",
    what:"Which of two postures built the plan. Conservative holds the load well under the ceilings; assertive rides them.",
    rule:"Assertive once 56 days pass with no symptom or medical event; back to conservative at the next one.",
    refs:[{cite:"An app rule, not a published threshold.", finding:"A clean window long enough to show the work is being absorbed."}],
    use:"When measured fitness lags the projection, the ceiling eases before anything else does.",
    caveat:""},
  components:{title:"What the sessions build", basis:"L",
    what:"Marathon fitness splits into four parts: VO₂max, running economy, threshold (the fastest pace held for about an hour) and resilience (how little the first three fade late in the race).",
    rule:"VO₂max is built early and then held; marathon-pace work grows through the build; the peak turns to resilience by extending the fast finish of the long run.",
    refs:[{cite:"Davis JJ. Marathon Excellence for Everyone.", finding:"The four-component model of marathon performance and how a build periodises it."}],
    use:"Each quality session carries the part it mainly builds; the phase line sums them.",
    caveat:"The share of each part per phase is the app's own reading of Davis."}
};
// one-line credit per limit, for the tile (SH-86)
const SOURCE_SHORT={chronic:"Friel; Couzens (ramp-rate guidance)", acwr:"Gabbett 2016 (contested)", long_step:"Frandsen et al. 2025, 5,200 runners", eq_week:"Davis; fitted", eq_session:"the weekly impact-load step", tissue:"3:1 periodisation convention"};
// the source panel for one limit (SH-86): every text escaped; an unknown key is no panel.
// SH-86c — in three parts (top / left / right) so a panel can lay the sections in two columns
// (.srccols): the week line, the what and the rule on the left; the named sources, the use, the
// caveat and the basis on the right. The race panel embeds the same source flat, one column.
function sourceParts(key, weekLine, lineLabel="This week"){
  const s=PLAN_SOURCES[key]; if(!s) return null;
  const bas=BASIS[s.basis];
  const lbl=t=>`<div class="srclbl">${esc(t)}</div>`;
  return {
    top:`<div class="srctop"><span class="srctitle">${esc(s.title)}</span>${bas?`<span class="basis ${s.basis}">${esc(bas.label)}</span>`:""}</div>`,
    left:(weekLine?`${lbl(lineLabel)}<p>${esc(weekLine)}</p>`:"")
      +`${lbl("What it is")}<p>${esc(s.what)}</p>`
      +`${lbl("The rule")}<p>${esc(s.rule)}</p>`,
    right:lbl("Where the number comes from")
      +s.refs.map(r=>`<div class="srcref"><div class="srccite">${esc(r.cite)}</div><div>${esc(r.finding)}</div></div>`).join("")
      +`${lbl("How the app uses it")}<p>${esc(s.use)}</p>`
      +(s.caveat?`<p class="srccaveat">Caveat: ${esc(s.caveat)}</p>`:"")
      +(bas?`<div class="srcbasis">${esc(bas.desc)}</div>`:"") };
}
function sourceHtml(key, weekLine, lineLabel="This week"){
  const p=sourceParts(key, weekLine, lineLabel); if(!p) return "";
  return p.top+`<div class="srccols"><div>${p.left}</div><div>${p.right}</div></div>`;
}
// the plain sentence a limit row leads with (SH-86): this week's own numbers, in the display unit
function limitSentence(k, ax, w, L){
  const sized = L.binding===k ? " This is the limit that sized the week." : "";
  const v=(x,d=1)=>Number(limV(x,ax.unit)).toFixed(d);
  if(k==="chronic"){
    const laid=v(ax.laid), ceiling=v(ax.ceiling);
    if(ax.laid<0){
      const why=w&&(w.role==="taper"||w.role==="race")?"the taper"
              :w&&w.role==="recovery"?"recovery after the race":"a down week";
      return `Fitness eases by ${v(-ax.laid)} this week (${why}), well inside the limit.`;
    }
    if(ax.laid>ax.ceiling+0.05)
      return `Fitness rose ${laid}, above the ${ceiling} limit: the week as run carried more than it was laid with.`;
    return L.binding===k ? `Fitness rises ${laid} this week, the most allowed.${sized}`
                         : `Fitness rises ${laid} of the ${ceiling} allowed.`;
  }
  if(k==="acwr")
    return `This week's load is ${v(ax.laid,2)}× the base on the shape-neutral reading; the plan sizes it at or under ${v(ax.ceiling,2)}.${sized}`;
  if(k==="long_step"){
    const prev=v(ax.ceiling/1.10);
    const at=Math.abs(ax.laid-ax.ceiling)<=0.15;
    return (at?`Longest run ${v(ax.laid)} ${limU(ax.unit)}: right at the step, 10 % over the longest of the last four weeks (${prev} ${limU(ax.unit)}).`
             :`Longest run ${v(ax.laid)} ${limU(ax.unit)}; the step allows ${v(ax.ceiling)} ${limU(ax.unit)}, 10 % over the longest of the last four weeks (${prev} ${limU(ax.unit)}).`)+sized;
  }
  if(k==="eq_week")
    return `${v(ax.laid)} speed-weighted ${limU(ax.unit)} against a ceiling of ${v(ax.ceiling)}, 1.3× the heaviest week of the last four.${sized}`;
  if(k==="eq_session")
    return w&&w.role==="race"
      ?`The race itself is the heaviest run (${v(ax.laid)} speed-weighted ${limU(ax.unit)}); race week is laid around it.${sized}`
      :`The heaviest single run weighs ${v(ax.laid)} speed-weighted ${limU(ax.unit)}; one run may weigh up to ${v(ax.ceiling)}.${sized}`;
  if(k==="tissue")
    return ax.exempt
      ?`${ax.streak} weeks in a row near the ceiling. The peak is exempt: the taper that follows is its recovery.`
      :`${ax.streak} of ${ax.limit} weeks in a row near the load ceiling; at ${ax.limit}, a down week is forced.${sized}`;
  return "";
}
function limitsHtml(L, w){
  if(!L) return "";
  const order=["chronic","acwr","long_step","eq_week","eq_session","tissue"].filter(k=>L[k]);
  if(!order.length) return "";
  order.sort((a,b)=>(a===L.binding?0:1)-(b===L.binding?0:1));   // the one that held the week first
  const rows=order.map(k=>{
    const ax=L[k], sentence=limitSentence(k,ax,w,L);
    const binding=L.binding===k;
    const tol=k==="acwr"?0.005:0.05;
    const over=!ax.exempt&&ax.ceiling!=null&&ax.laid!=null&&ax.laid>ax.ceiling+tol;
    const tag=binding?`<span class="limtag hold">holds the week</span>`
            :over?`<span class="limtag over">ran over</span>`
            :ax.exempt?`<span class="limtag">exempt in the peak</span>`:"";
    let right, ratio;
    if(k==="tissue"){ right=`${ax.streak} of ${ax.limit} weeks`; ratio=ax.limit?ax.streak/ax.limit:0; }
    else if(k==="chronic"){ right=`${ax.laid>=0?"+":""}${Number(ax.laid).toFixed(1)} of ${ax.ceiling>=0?"+":""}${Number(ax.ceiling).toFixed(1)} CTL`;
      ratio=ax.ceiling?Math.max(0,ax.laid)/ax.ceiling:0; }
    else { right=`${limV(ax.laid,ax.unit)} of ${limV(ax.ceiling,ax.unit)} ${esc(limU(ax.unit))}`;
      ratio=ax.ceiling?Math.max(0,ax.laid)/ax.ceiling:0; }
    const pct=Math.round(Math.min(1,Math.max(0,ratio))*100);
    const name=LIMIT_NAMES[k]||k;
    const bas=BASIS[ax.basis];
    return `<div class="limrow">
      <button type="button" class="limhd" aria-expanded="false" data-lim="${k}">
        <span class="limtop"><span class="limname">${esc(name.charAt(0).toUpperCase()+name.slice(1))}</span>${tag}<span class="limval mono">${right}</span></span>
        <span class="limbar${binding?" hold":over?" over":""}"><i style="width:${pct}%"></i></span>
        <span class="limsent">${esc(sentence)}</span>
        <span class="limmeta">${bas?`<span class="basis ${ax.basis}">${esc(bas.label)}</span>`:""}<span class="limcta">Why, and the source →</span></span>
      </button>
      <div class="limsrc" hidden>${sourceHtml(k, sentence)}</div>
    </div>`;
  });
  return `<div class="limits"><div class="limitslbl">Why this week looks like this</div>${rows.join("")}</div>`;
}
function weekHtml(w,p,today){
  const down=isDownWeek(w);
  let cur=false;
  if(w.start) cur = !w.frozen && w.start<=today && today<=weekEndIso(w.start);
  // LOG-enriched sessions (merged in by renderPlan for elapsed/current weeks) get the journal line
  // (done/missed mark, actual, reflection, doubles); plain future weeks keep the compact summary.
  const lines=w.sessions.map(s=>{
    // §W1 — a quality session expands to its instruction card on click (skip done sessions:
    // their line is already the click-to-map affordance, and the prescription moment is past)
    const q=s.reps&&s.reps.length, card=(q&&!s.done)?workoutCard(s):"";
    const label=`<span class="wsi${q?' qs':''}${card?' wsx':''}"${card?' tabindex="0" role="button" title="Show the workout instructions"':''}>${sessSummary(s)}${card?' <span class="wsxc" aria-hidden="true">▸</span>':''}</span>${compChip(s)}${card}`;
    return {d:s.date, html:(('done' in s) ? logLine(s,today,label)
      : `<div class="sline"><span class="sdate">${sessDate(s.date)}</span>${label}</div>`)};
  });
  // §REST — the engine lists run sessions only; a rest day has no entry, so every week card read
  // as an unbroken wall of runs — the athlete reasonably counted "14 days of consecutive running"
  // on a plan that did hold rest days, and the wall also HID the streaks when the spacing really
  // broke. Fill each weekday with no session entry with a muted Rest line. Display-only: the
  // engine's plan JSON is untouched, and a day with an explicit rest card is already covered.
  if(w.start){
    const have=new Set(w.sessions.map(s=>s.date));
    for(const d of weekDates(w.start))
      if(!have.has(d))
        lines.push({d, html:`<div class="sline srest"><span class="sdate">${sessDate(d)}</span><span class="wsi">Rest</span></div>`});
    lines.sort((a,b)=>a.d<b.d?-1:a.d>b.d?1:0);
  }
  const sess=lines.map(l=>l.html).join("");
  // §C (0.59.0) — the deload governor's read on the judged down week: retired, offered, or kept (and why)
  const dl=w.deload;
  const dlChip = (dl&&dl.judged) ? (dl.retired
      ? `<span class="eased" title="${esc(dl.why||"")}">deload retired · block ${dl.absorbed_frac!=null?Math.round(dl.absorbed_frac*100)+" %":"—"} absorbed</span>`
      : (dl.offer&&!SH_READONLY)
        ? `<span class="wfz" title="${esc(dl.why||"")}">deload not owed · <button type="button" class="linkbtn" data-deload="${esc(dl.week||w.start)}" data-retire="1">retire it</button> · <button type="button" class="linkbtn" data-deload="${esc(dl.week||w.start)}" data-retire="0">keep it</button></span>`
        : `<span class="muted" title="${esc(dl.why||"")}">deload ${dl.source==="athlete"&&!dl.retired?"kept (your call)":"kept"}</span>`) : "";
  const flags=[dlChip, w.frequency_met?'<span class="wfz" title="You’ve already run this week’s prescribed count and volume — today’s remaining run is optional, not forced.">✓ frequency met — today optional</span>'
                 // §6o-B — volume charged: the week's km intent is already run (even if the run count is short) — no more sessions are laid on the remaining days
                 :(w.volume_met?'<span class="wfz" title="You’ve already run this week’s planned km — the remaining days aren’t re-prescribed just to hit a run count. Rest is prescribed; an easy run is fine if you feel good.">✓ volume run — today optional</span>':''),
               w.fatigue_capped?'<span class="down" title="A building week, but recent fatigue left no ACWR headroom — the long run was held back. Load capped for safety, not silently degraded.">⚠ build intent capped by recent fatigue</span>'
                 :(w.clipped?'<span class="down">clipped to fit ACWR</span>':''),
               // §PRO9 — long-run progression cap (Aarhus injury lever); §3.1 — biomechanical (eq_km) load ease
               w.long_step_capped?`<span class="wfz" title="Long-run progression cap: this week's long run was held to +10% over your longest run of the last 4 weeks — the strongest single injury lever (a sharp long-run jump predicts injury more than mileage jumps). Freed volume went to easy runs; weekly load unchanged.">long-run held (+10%)</span>`:'',
               w.long_held?`<span class="wfz" title="Long run kept at its ladder rung (${U.d(w.long_held.rung_km)} ${U.unit}): your easy days ran over this week's sheet, so the sheet runs over the bar and the long run keeps its distance. Only the load ceilings can shorten it.">long run kept</span>`:'',
               // §REST — the day-spacing gate: the week wanted another run day but every open day
               // would have chained a run streak, so the spacing held and the week is honestly lighter
               // §REST2 — …and how much it cost, when the engine can measure it. A boolean says the
               // spacing held; the km says what the week gave up for it, which is the number worth arguing with.
               w.rest_gated?`<span class="wfz" title="Day-spacing guard: this week kept its run days instead of adding another — every open day would have chained too many running days in a row. The volume that won't fit on legal days is held back, not crammed into longer easy runs. Your rest days and the +10% long-run ceiling both stand.">held to rest days — spacing kept${w.rest_shed_km?' · −'+w.rest_shed_km+'km':''}</span>`:'',
               w.bio_capped?`<span class="wfz" title="Biomechanical load ease: this week's pace-weighted 'damage-equivalent' load (fast km count for more) jumped too far above your recent weeks, so the fast work was eased to easy — aerobic volume kept, only the high-damage fast km reduced. The biomechanical/tissue-damage axis (informed by John Davis), invisible to heart-rate load.">fast load eased</span>`:'',
               // §AV — laid around declared away days (private payload only: the public box strips av_dates)
               w.av_dates?`<span class="eased" title="Away days: the week is laid around the days you declared unavailable — runs relocated to the nearest legal day, spacing kept${w.av_shed?'; too few legal days remained, so '+w.av_shed+' run'+(w.av_shed>1?'s were':' was')+' shed and the load went with them (a travel week is lighter, never crammed)':''}.">✈ away ${w.av_dates.map(sessDate).join(', ')}${w.av_shed?' · −'+w.av_shed+' run'+(w.av_shed>1?'s':''):''}</span>`:'',
               w.adjusted?'<span class="eased">eased</span>':'',
               w.frozen?'<span class="wfz">✓ done</span>':''].filter(Boolean).join(" · ");
  // §3.1 — surface the biomechanical eq_km on quality weeks (where it exceeds raw km); calm/muted.
  const eqkm=(w.eq_km&&w.eq_km>w.km+0.5)?` · <span class="muted mono" title="Biomechanical load: pace-weighted damage-equivalent km (fast running does several times more tissue damage per km than easy). The biomechanical/tissue-damage axis (informed by John Davis), which heart-rate load can't see.">${U.d(w.eq_km)} eq-${U.unit}</span>`:'';
  // §CARD2 — the straddling week's total = actuals so far + prescription ahead; show the split so
  // the number explains itself (the old prescription-sum header made an over-run week LOOK smaller).
  // §P2 (0.59.0) — where the two denominators diverge, the card says so (the note is the reason)
  const barNote=(w.bar&&w.bar.diverge&&w.bar.intent_km!=null)?` · <span class="muted mono" title="${esc(w.bar.note||"")}">bar ${U.d(w.bar.intent_km)} · sheet ${U.d(w.bar.sheet_km)} ${U.unit}</span>`:'';
  const kmSplit=(w.partial&&w.km_done!=null)?` · <span class="muted mono" title="This week straddles today, so its total counts days you've already run at their REAL distance (${U.d(w.km_done)} ${U.unit} in ${w.runs_done} run${w.runs_done===1?'':'s'}) plus what's still prescribed ahead (${U.d(w.km_ahead)} ${U.unit} in ${w.runs_ahead}). Days you out-run no longer shrink the week's headline.">${U.d(w.km_done)} run + ${U.d(w.km_ahead)} ahead</span>`:'';
  return `<div class="wk ${down?'wdown':''} ${w.frozen?'wfrozen':''} ${cur?'wcur':''}">
      <div class="wn">${w.wk}${w.frozen?'<span class="wlock" title="completed — carried verbatim">🔒</span>':''}</div>
      <div class="wbody">
        <div><span class="wkm">${U.d(w.km)} ${U.unit}</span>${eqkm} · ${w.runs} runs${kmSplit}${barNote}${flags?' · '+flags:''}</div>
        <div class="wintent">${w.intent}</div>
        <div class="wsesslog">${sess}</div>
      </div>
      <div>${acBadge(w.proj_acwr)}</div>
    ${limitsHtml(w.limits, w)}</div>`;
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
// A recovery (down) week is the one the engine ROLED so — never a fixed position. Until §C
// (0.59.0) the down week was always the block's fourth, and the strip still greyed W4 by number;
// once the deload moved (a build block whose down week is its third), W4 greyed as a building
// week. The intent text is the fallback for plans saved before weeks carried a role.
function isDownWeek(w){ return w.role==='down' || /^down/i.test(w.intent||''); }
// Which week is open by default in a phase's strip: the one holding `today`, else the first.
function defaultWeek(weeks,today){ return ((weeks.find(w=>weekHoldsToday(w,today))||weeks[0]||{}).wk); }
// A second-level selector that echoes the phase bar: one segment per week, the selected one active.
// Shared by every phase (re-base + future) — only the *detail* below it differs per phase, so the
// LOG-enriched vs plain week renderers stay untouched (we wrap their output, never merge them).
function weekStrip(weeks,pk,sel){
  return `<div class="weekstrip" data-pk="${pk}">`+weeks.map(w=>{
    const down=isDownWeek(w);
    return `<div class="weekseg${w.wk===sel?' active':''}${down?' wsdown':''}" data-pk="${pk}" data-wk="${w.wk}"
       role="button" tabindex="0" aria-pressed="${w.wk===sel}"
       aria-label="W${w.wk}: ${U.d(w.km)} ${U.unit}, ${w.runs} runs${w.frozen?', done':''}"
       title="Week ${w.wk} · ${U.d(w.km)} ${U.unit} · ${w.runs} runs${w.frozen?' · done':''}">W${w.wk}${w.frozen?'<span class="wlock">🔒</span>':''}</div>`;
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
      qhint("Marathon fitness decomposes into four components: VO₂max × running economy × SSmax/LT2 (the classic three-factor model) plus physiological resilience — how little the first three decay over "+U.d(42.2,U.mi?1:0)+" "+U.unit+". Each quality session is tagged with the component it chiefly builds (hover a session's chip); this line sums what the phase is FOR. The assertive plan periodizes them: VO₂max developed early then merely maintained (red-cell persistence makes it cheap to hold), marathon-pace work growing through the build, and the peak pivoting to resilience — the long run's MP segment extends at constant speed. Framing follows John Davis; the fractions are our operationalization.")
    :"";
  return `<h3 class="phasehdr">
      <span>${title} <span class="muted mono" style="font-size:11px">(${block.weeks.length}w · start ${block.start} · ends CTL ${block.end_ctl}/ATL ${block.end_atl})${fz}</span>${qhint("CTL = chronic load (your fitness), a slow ~42-day average of training; ATL = acute load (recent fatigue), a fast ~7-day average. Shown here is each value projected to this phase's end.")}${builds}</span>
      <span class="muted mono" style="font-size:12px;font-weight:600;white-space:nowrap" title="Total planned distance across the phase">Σ ${U.d(km,0)} ${U.unit}</span>
    </h3>
    ${weekStrip(block.weeks,pk,sel)}
    <div class="weekdetails">${block.weeks.map(w=>weekDetail(weekHtml(w,p,today),pk,w.wk,sel)).join("")}</div>`;
}
// ── §GUN — the live countdown in the objective bar ─────────────────────────
// Ticks every second toward the race the console is SHOWING (the §CHAIN4 view race), at its gun
// time when the objective carries one and at midnight when it doesn't. A self-rescheduling
// setTimeout (the music.js pollJob pattern), aligned to the wall-clock second so the digits don't
// drift; the element is re-queried every tick because renderPlan's innerHTML re-renders wipe it —
// the next renderPlan re-arms. No aria-live: a per-second live region is hostile to screen readers.
let CD_TIMER=null;
function fmtCountdown(ms){
  const s=Math.max(0,Math.round(ms/1000)), d=Math.floor(s/86400), h=Math.floor(s%86400/3600),
        m=Math.floor(s%3600/60), sec=s%60;
  const pl=(n,w)=>`${n} ${w}${n===1?"":"s"}`;
  return d>0 ? `${pl(d,"day")}, ${pl(h,"hour")}, ${pl(m,"minute")}, ${pl(sec,"second")}`
             : `${pl(h,"hour")}, ${pl(m,"minute")}, ${pl(sec,"second")}`;
}
function armCountdown(dateIso, timeHM){
  clearTimeout(CD_TIMER); CD_TIMER=null;
  if(!dateIso) return;
  const target=new Date(dateIso+"T"+(timeHM||"00:00")+":00");   // local time — a gun time IS local
  if(isNaN(target)) return;   // an unreadable date/time shows no countdown, never a false "race day"
  const tick=()=>{
    const el=document.getElementById("objcount");
    if(!el){ CD_TIMER=null; return; }   // a re-render wiped it; the next renderPlan re-arms
    const ms=target-Date.now();
    el.textContent=ms>0 ? " · in "+fmtCountdown(ms) : " · race day 🏁";
    CD_TIMER=setTimeout(tick, 1000-(Date.now()%1000)+5);   // align to the second
  };
  tick();
}
function renderPlan(p){
  const host=$("#plan");
  // the public box removes the Generate button — its empty state must not point at it (UX-11)
  if(!p){ armCountdown(null); host.innerHTML=`<div class="empty">${SH_READONLY
      ? "No plan published yet."
      : `No plan yet — hit <b>Generate plan</b>.`}</div>`; return; }
  LAST_PLAN=p;   // §CHAIN3 — the race selector re-renders off this without re-fetching /api/plan
  const o=p.objective, rb=p.rebase;
  const today0 = (LOG&&LOG.today) || new Date().toISOString().slice(0,10);
  const vr = viewRaceOf(p, today0);                       // §CHAIN4
  const vrOverride = !!(vr && !(o && vr.date===o.date));  // looking at a leg other than the headline
  const vrObj = vrOverride ? (OBJECTIVES.find(x=>x.label===vr.label && x.date===vr.date) || {}) : null;
  // §GUN — the shown race's own objective row carries the gun time (the plan payload doesn't know
  // it); the headline resolves through the same label+date match as the §CHAIN4 leg above.
  const cdRace = vrOverride ? vr : o;
  const cdObj = cdRace ? (vrOverride ? vrObj : (OBJECTIVES.find(x=>x.label===o.label && x.date===o.date) || {})) : null;
  // A-race pill bar at the top of the page (mockup Almanac) — driven by the plan's objective, or by
  // §CHAIN4's view race (the next leg still ahead) when the chain has more than one A-race.
  // `.owhen` stays static (the ticker must never rewrite the date); `#objcount` is the tick's own.
  const ob=$("#objbar");
  if(ob) ob.innerHTML = vrOverride ? `<span class="objlabel">Current main objective</span>`+
      `<span class="arace">${esc(vrObj.priority||'A')}-race</span>`+
      `<span class="oname">${esc(vr.label)}</span>`+
      `<span class="owhen">${esc(vr.date)} · ${vr.weeks_away} weeks out</span>`+
      `<span class="ocount" id="objcount"></span>`+
      ((vr.feasibility||vrObj.target)?`<span class="overdict">Verdict — <b style="color:var(--text)">${esc(vr.feasibility||vrObj.target)}</b></span>`:"")
    : o ? `<span class="objlabel">Current main objective</span>`+
      `<span class="arace">${esc(o.priority||'A')}-race</span>`+
      `<span class="oname">${esc(o.label)}</span>`+
      `<span class="owhen">${esc(o.date)} · ${o.weeks_away} weeks out</span>`+
      `<span class="ocount" id="objcount"></span>`+
      ((p.feasibility&&(p.feasibility.verdict||o.target))?`<span class="overdict">Verdict — <b style="color:var(--text)">${esc(p.feasibility.verdict||o.target)}</b></span>`:"")
    : "";
  // arm AFTER the innerHTML above lands the span; no shown race (or no bar) disarms the tick.
  // Works identically on the public box — date + start_time are allowlisted and #objbar exists there.
  if(ob && cdRace) armCountdown(cdRace.date, (cdObj&&cdObj.start_time)||null);
  else armCountdown(null);
  const totalw=p.phases.reduce((s,x)=>s+x.weeks,0);
  const zoneChips=Object.entries(p.pace_zones).map(([k,v])=>
    `<span class="zone ${k==='easy_top'?'hl':''}">${k.replace('_',' ')} <b>${U.t(v)}</b></span>`).join("");
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
  // Glanceable block total — Phase 0 is all easy running, so planned time = distance × easy pace.
  const phaseKm = planWeeks.reduce((s,w)=>s+(w.km||0),0);
  const easySec = (m=>m?(+m[1]*60+ +m[2]):0)(/(\d+):(\d+)/.exec(p.pace_zones.easy_top||""));
  const fmtDur = m => m>=60 ? `${Math.floor(m/60)}h${String(m%60).padStart(2,"0")}m` : `${m}m`;
  const phaseMin = easySec ? Math.round(phaseKm*easySec/60) : 0;
  const phaseTot = `Σ ${U.d(phaseKm,0)} ${U.unit}${phaseMin?` · ~${fmtDur(phaseMin)} easy`:""}`;
  const ran = LOG&&LOG.ran;   // actually run across the block so far (dups excluded)
  const ranTot = ran&&ran.km>0 ? `▸ ran ${U.d(ran.km,0)} ${U.unit}${ran.min?` · ${fmtDur(ran.min)}`:""}` : "";
  // Last refreshed = when the plan was regenerated (auto-replan runs nightly; paces & totals are
  // recomputed from that snapshot's VO₂max, so this is how fresh the chips on the strip below are).
  const refreshed = p.generated_at
    ? `<span class="muted mono" style="font-size:11px">↻ Plan rebuilt ${new Date(p.generated_at).toLocaleString()}</span>` : "";
  // The re-base journal line = the shared logLine with a plain-distance plan label (Phase 0 is
  // pure easy running; quality summaries only matter in the later phases' weekHtml path).
  const sessHtml=s=>logLine(s,today,`<span class="splan">${sessSummary(s)}</span>`);
  // The re-base week card (SH-86: it lives in the week pane like every other week now) —
  // LOG-enriched (done/missed/unplanned/doubles via sessHtml).
  const rebaseCard=w=>{
    const hasLog = w.sessions.some(s=>'done' in s);
    const sess = hasLog ? `<div class="wsesslog">${w.sessions.map(sessHtml).join("")}</div>`
      : `<div class="wsesslog">${w.sessions.map(s=>`<div class="sline"><span class="sdate">${sessDate(s.date)}</span><span class="splan">${sessSummary(s)}</span></div>`).join("")}<div class="muted mono" style="margin-top:3px">@ easy ${p.pace_zones.easy_top}</div></div>`;
    return `<div class="wk ${isDownWeek(w)?'wdown':''}">
      <div class="wn">${w.wk}</div>
      <div class="wbody">
        <div><span class="wkm">${U.d(w.km)} ${U.unit}</span> · ${w.runs} runs${w.clipped?' · <span class="down">clipped to fit ACWR</span>':''}${w.adjusted?' · <span class="eased">eased</span>':''}</div>
        <div class="wintent">${w.intent}</div>
        ${sess}
      </div>
      <div>${acBadge(w.proj_acwr)}</div>
    </div>`;
  };
  const adh = (LOG&&LOG.adherence&&LOG.adherence.scheduled)
    ? ` · <span class="muted mono" style="font-size:11px">done ${LOG.adherence.done}/${LOG.adherence.scheduled} so far</span>` : "";
  const verdTone = v => v==="too soon" ? "warn" : (v==="finish"||v==="maintain") ? "ok" : "muted";
  const chainRaces = p.chain||[];
  // §CHAIN4 — which race the athlete is LOOKING AT. The default is the NEXT race still ahead, not
  // the plan's own headline (the chain's terminal race): the resolver above (viewRaceOf) applies
  // RACE_SEL when it still names an entry of THIS plan's own chain (a stale selection from a
  // since-regenerated plan just falls back to the next race ahead — no dangling reference), then
  // falls back to the next leg whose date hasn't passed. "Override" = a leg other than the plan's
  // own objective (the final race); landing on the headline itself, or a single-race plan (where
  // the resolver always answers null), leaves every downstream read exactly as it was pre-§CHAIN3.
  const selRace = vr;   // §CHAIN4 — the resolver above already applies RACE_SEL, then the next race ahead
  const selOverride = !!(selRace && !(o && selRace.date===o.date));
  // §PRO7/§FT6 — finish-time strip. It used to read now→+4w→+8w, which prices a LATER RACE DATE —
  // a counterfactual nobody asked about, and one that reads like a timeline ("am I training for the
  // next 8 weeks?"). It now answers the question a runner actually has: what does this BLOCK buy?
  // Today's measured shape → the race-day projection, with the gain named. The runway curve is not
  // lost — it moves into the hover, labelled as the what-if it always was.
  // §CHAIN3 — the picked race's own finish-time band, falling back to the plan's headline one only
  // when the selected leg carries none of its own (a pre-§CHAIN3 plan, or a leg too far out to price).
  const FT = selOverride ? (selRace.finish_time || (p.feasibility && p.feasibility.finish_time))
                        : (p.feasibility && p.feasibility.finish_time);
  // §FT3 — the RANGE is the headline (my decision: a point invites anchoring on false precision);
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
  const fcMin=s=>{const m=Math.round(Math.abs(s)/60);
    return m>=60?`${Math.floor(m/60)}h ${String(m%60).padStart(2,"0")}min`:`${m} min`};
  // Named honestly in BOTH directions — a taper-only or detraining runway must not be dressed up
  // as a gain, and a flat one says so rather than implying movement.
  const gain = FTT && FTT.gain_seconds;
  const gainTxt = !FTT ? "" : gain>60 ? `the build buys ${fcMin(gain)}`
    : gain<-60 ? `this runway costs ${fcMin(gain)}` : "holds today's shape";
  const runway = ftFrozen ? "" : (FT&&FT.curve||[]).filter(c=>c.plus_weeks>0)
    .map(c=>`+${c.plus_weeks}w → ${c.hms}`).join(" · ");
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
  // SH-86 (slice 2) — the plan page: three tiles (this week · what holds it · races), the road of
  // week bars, one week pane, and a strip of sources. The phase bar, the per-phase panels, the
  // chain strip and the standalone finish/regime/paces lines are gone from the output; their
  // content moved into the tiles and the pane. The AI explain/adjudicate/parse handlers stay wired
  // (and guarded) below, but their controls no longer render — SH-88 removes them with the backend.
  // SH-86c — a race's source panel opens as #tilePanel, a full-width sibling BELOW the tiles.
  const phaseNameOf=k=>k==="rebase"?"Re-base":(((p.phases||[]).find(x=>x.key===k)||{}).phase||k);
  // every week in road order, pk-tagged, with the phase's display name alongside
  const allWeeks=[];
  phaseGroups.forEach(g=>{
    const blk = g.key==="rebase" ? {weeks:g.weeks} : enrich(g.key, p[g.key]);
    ((blk&&blk.weeks)||[]).forEach(w=>allWeeks.push({pk:g.key, w, phase:phaseNameOf(g.key)}));
  });
  // the shown week: the remembered pick while it still names a week, else the week holding today,
  // else the first week ahead, else the last on the road
  let selIt = (WEEK_SEL && allWeeks.find(x=>x.pk+":"+x.w.wk===WEEK_SEL)) || null;
  if(!selIt) selIt = allWeeks.find(x=>weekHoldsToday(x.w, today))
    || allWeeks.find(x=>x.w.start && x.w.start>today) || allWeeks[allWeeks.length-1] || null;
  if(selIt) WEEK_SEL=selIt.pk+":"+selIt.w.wk;
  const curIt = allWeeks.find(x=>weekHoldsToday(x.w, today)) || selIt;   // the tiles read THIS week
  const hmm=s=>{ s=Math.round(Math.max(0,s)); return Math.floor(s/3600)+":"+String(Math.floor(s%3600/60)).padStart(2,"0"); };
  // a typed goal in seconds: "3:45" reads as h:mm on a marathon and "45:00" as mm:ss on a 10K, so a
  // two-part time takes the reading nearer the race's own median
  const goalSec=(t,ref)=>{ const m=/^(\d+):(\d{2})(?::(\d{2}))?$/.exec(t||""); if(!m) return null;
    if(m[3]) return +m[1]*3600 + +m[2]*60 + +m[3];
    const hm=+m[1]*3600 + +m[2]*60, ms=+m[1]*60 + +m[2];
    return (ref && Math.abs(ms-ref)<Math.abs(hm-ref)) ? ms : hm; };
  const gainWord=g=>g>60?`the build buys ${fcMin(g)}`:g<-60?`this runway costs ${fcMin(g)}`:"holds today's shape";

  // — tile 1: this week at a glance (mini bars Mon–Sun, height ∝ the session's km) —
  const weekTile = curIt ? (()=>{ const w=curIt.w;
    const maxkm=Math.max(1,...(w.sessions||[]).map(s=>s.km||0));
    const days=weekDates(w.start).map((d,i)=>{
      const s=(w.sessions||[]).find(x=>x.date===d);
      const h=s?Math.max(6,Math.round((s.km||0)/maxkm*44)):2;   // a rest day is a 2px stub
      const cls=!s?"rest":s.race?"race":(s.reps&&s.reps.length)?"qual":(/^(long|long_mp)/.test(s.kind||"")?"long":"easy");
      return `<span class="wkd ${cls}${d===today?" today":""}"><i style="height:${h}px"></i><b>${"MTWTFSS"[i]}</b></span>`;
    }).join("");
    return `<div class="plantile">
      <div class="tilelbl">This week · ${esc(curIt.phase.split(" ")[0])} week ${w.wk}</div>
      <div class="tilebig">${U.d(w.km)} ${U.unit} <span class="muted">· ${w.runs} runs</span></div>
      <div class="muted mono" style="font-size:12px">${sessDate(w.start)} – ${sessDate(weekEndIso(w.start))}${w.proj_ctl!=null?` · fitness at week end CTL ${Math.round(w.proj_ctl)}`:""}</div>
      <div class="wkdays">${days}</div>
      <div class="wklegend"><span><i class="easy"></i>easy</span><span><i class="qual"></i>quality</span><span><i class="long"></i>long</span></div>
      <div class="wintent">${w.intent||""}</div>
      <button type="button" class="linkbtn" id="openWeek">Open the week, day by day ↓</button>
    </div>`;})() : "";

  // — tile 2: what holds this week (the binding limit, then the rest compact) —
  const LW=curIt&&curIt.w.limits;
  const limOrder=["chronic","acwr","long_step","eq_week","eq_session","tissue"].filter(k=>LW&&LW[k]);
  const limVal=(k,ax)=>k==="tissue"?`${ax.streak} of ${ax.limit} weeks`
    :k==="chronic"?`${ax.laid>=0?"+":""}${Number(ax.laid).toFixed(1)} of ${ax.ceiling>=0?"+":""}${Number(ax.ceiling).toFixed(1)} CTL`
    :`${limV(ax.laid,ax.unit)} of ${limV(ax.ceiling,ax.unit)} ${limU(ax.unit)}`;
  const limPct=(k,ax)=>{ const r=k==="tissue"?(ax.limit?ax.streak/ax.limit:0):(ax.ceiling?Math.max(0,ax.laid)/ax.ceiling:0);
    return Math.round(Math.min(1,Math.max(0,r))*100); };
  const leadKey=limOrder.length?(LW.binding&&LW[LW.binding]?LW.binding:limOrder[0]):null;
  const limitsTile=!curIt?"":`<div class="plantile">
    <div class="tilelbl">What holds this week</div>
    ${leadKey?(()=>{const ax=LW[leadKey];return `
      <div class="limname" style="font-family:var(--serif);font-size:17px">${esc((LIMIT_NAMES[leadKey]||leadKey).replace(/^./,c=>c.toUpperCase()))}</div>
      <span class="limbar${LW.binding===leadKey?" hold":""}" style="height:12px"><i style="width:${limPct(leadKey,ax)}%"></i></span>
      <div class="mono" style="font-size:12px">${limVal(leadKey,ax)}</div>
      <div class="limsent" style="margin-top:4px">${esc(limitSentence(leadKey,ax,curIt.w,LW))}</div>
      <div class="muted" style="font-size:12px;margin-top:4px">Source: ${esc(SOURCE_SHORT[leadKey]||"")} ${BASIS[ax.basis]?`<span class="basis ${ax.basis}">${esc(BASIS[ax.basis].label)}</span>`:""}</div>
      <button type="button" class="linkbtn limgo" data-lim="${leadKey}">Why, and where the number comes from ↓</button>`;})():""}
    ${limOrder.filter(k=>k!==leadKey).length?`<div class="limothers"><div class="muted mono" style="font-size:11px;margin:10px 0 2px;border-top:1px solid var(--line);padding-top:8px">The other limits</div>${limOrder.filter(k=>k!==leadKey).map(k=>`
      <button type="button" class="limgo limmini" data-lim="${k}">
        <span class="lk2">${esc(LIMIT_NAMES[k]||k)}</span>
        <span class="limbar" style="height:6px;margin:0"><i style="width:${limPct(k,LW[k])}%"></i></span>
        <span class="mono lv2">${limVal(k,LW[k])}</span>
      </button>`).join("")}</div>`:""}
    ${!limOrder.length?`<div class="muted" style="font-size:13px">No limit binds this week.</div>`:""}
  </div>`;

  // — tile 3: the races, one finish range each, on a shared axis —
  const raceRows=(chainRaces.length>1 ? chainRaces.map(c=>({
      label:c.label, date:c.date, weeks_away:c.weeks_away,
      target:(OBJECTIVES.find(x=>x.label===c.label && x.date===c.date)||{}).target,   // a chain entry carries none
      proj_ctl:c.proj_ctl, verdict:c.feasibility, note:c.feasibility_note, ft:c.finish_time,
      headline:!!(o && c.date===o.date)}))
    : (o ? [{label:o.label, date:o.date, weeks_away:o.weeks_away, target:o.target,
      proj_ctl:p.feasibility&&p.feasibility.projected_ctl, verdict:p.feasibility&&p.feasibility.verdict,
      note:p.feasibility&&p.feasibility.note, ft:p.feasibility&&p.feasibility.finish_time,
      headline:true}] : []));
  const banded=raceRows.filter(r=>r.ft&&r.ft.band);
  // the shared axis spans every range AND every goal, so a goal's tick never lands off the bar
  const bandAxis=banded.length?(()=>{ const goals=banded.map(r=>goalSec(r.target, r.ft.seconds)).filter(g=>g!=null);
    const lo=Math.min(...banded.map(r=>r.ft.band.lo_seconds),...goals)-300,
    hi=Math.max(...banded.map(r=>r.ft.band.hi_seconds),...goals)+300;
    return {lo:Math.floor(lo/600)*600, hi:Math.ceil(hi/600)*600}; })():null;
  const bandBar=r=>{
    const b=r.ft&&r.ft.band; if(!b||!bandAxis) return "";
    const span=bandAxis.hi-bandAxis.lo, pc=x=>((x-bandAxis.lo)/span*100);
    const goal=goalSec(r.target, r.ft.seconds);
    return `<div class="bandbar">
      <span class="bandrange" style="left:${pc(b.lo_seconds).toFixed(2)}%;width:${(pc(b.hi_seconds)-pc(b.lo_seconds)).toFixed(2)}%"></span>
      ${r.ft.seconds?`<span class="bandmed" style="left:${pc(r.ft.seconds).toFixed(2)}%"></span>`:""}
      ${goal?`<span class="bandgoal" style="left:${pc(goal).toFixed(2)}%"></span>`:""}
      <span class="bandtick">${hmm(bandAxis.lo)}</span><span class="bandtick mid">${hmm((bandAxis.lo+bandAxis.hi)/2)}</span><span class="bandtick end">${hmm(bandAxis.hi)}</span>
    </div>`;
  };
  const racePanel=r=>{
    const verdict=r.note
      ? esc(r.note).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
      : `<span class="cverd ${verdTone(r.verdict)}">${esc(r.verdict||'—')}</span> — projected verdict for ${esc(r.label)}.`;
    const ft=r.ft, t=ft&&ft.today;
    const todayLine=(t&&ft.hms)?`Off today's shape ${esc(t.hms)} → by race day ${esc(ft.hms)}${t.gain_seconds!=null?` · ${esc(gainWord(t.gain_seconds))}`:""}`:"";
    const fcH=(ft&&(ft.curve||[]).map(c=>c.hms))||[];
    const rw=(fcH.length>1&&new Set(fcH).size===1)?"":(ft&&(ft.curve||[]).filter(c=>c.plus_weeks>0).map(c=>`+${c.plus_weeks}w → ${c.hms}`).join(" · "));
    const rwLine=rw?`<div class="adjmeta">If the race were later (same training): ${esc(rw)}.</div>`:"";
    const marks=`${(planStale||(ft&&!ft.band&&!ft.today))?`<div class="fcstale">⟳ saved by an earlier engine — regenerate to re-read</div>`:""}${(ft===FT&&ftAnchor)?`<div class="fcstale">◷ last measured ${ftAnchor.age_days}d ago — today's shape unread</div>`:""}`;
    // SH-86c — the panel opens below the tiles (#tilePanel), in two columns at ≥980px: the verdict
    // side left (with the "Now" line), the finish-range source flat on the right
    const sp=sourceParts("finish");
    return { left:`<p class="feas" style="margin-top:0">${verdict}</p>${rwLine}${marks}${todayLine?`<div class="srclbl">Now</div><p>${esc(todayLine)}</p>`:""}`,
             right:sp?sp.top+sp.left+sp.right:"" };
  };
  const racesTile=raceRows.length?`<div class="plantile racetile${chainRaces.length>1?' chainstrip':''}">
    <div class="tilelbl">Races</div>
    ${raceRows.map(r=>{
      const open=PLAN_OPEN==="race:"+r.date;
      const on=selRace?selRace.date===r.date:!!(o&&r.date===o.date);   // the §CHAIN4 pressed contract
      const head=chainRaces.length>1
        ?`<div class="chainrace${on?' on':''}" role="button" tabindex="0" aria-pressed="${on?'true':'false'}" data-race="${esc(r.date)}">
            <span class="cname">${esc(r.label)}</span>
            <span class="cwhen">${esc(r.date)} · ${r.weeks_away} w · goal ${esc(r.target||'—')}</span></div>`
        :`<div class="racename">${esc(o.label)}</div>
          <div class="muted mono" style="font-size:12px">${esc(o.date)} · ${o.weeks_away} weeks · goal ${esc(o.target||'—')}</div>`;
      return `<div class="racerow">${head}
        ${bandBar(r)}
        ${r.ft?`<div class="racenum mono">${r.ft.band?`${esc(r.ft.band.lo_hms)}–${esc(r.ft.band.hi_hms)} · median ${esc(r.ft.hms)}`:`median ${esc(r.ft.hms)}`}${goalSec(r.target, r.ft.seconds)?` · goal ${esc(r.target)}`:""}</div>`:""}
        <div class="muted" style="font-size:12px">Fitness on race day: ${r.proj_ctl!=null?`CTL ${Math.round(r.proj_ctl)}`:"—"}${r.verdict?` · verdict: ${esc(r.verdict)}`:""}</div>
        ${r.ft?`<button type="button" class="linkbtn plansrc" data-open="race:${esc(r.date)}" aria-expanded="${open}" aria-controls="tilePanel">${open?"Hide the source ↑":"Why this range, and the source →"}</button>`:""}
      </div>`;}).join("")}
  </div>`:`<div class="plantile racetile">
    <div class="tilelbl">Races</div>
    <div class="muted" style="font-size:13px">No race: the plan holds fitness.</div>
    <button type="button" class="linkbtn" id="racesOpen">Edit races →</button>
  </div>`;
  // SH-86c — a race's source opens BELOW the tiles, full width on its own grid row, so the three
  // tiles keep their own heights beside it. The header names the race; the close button is the
  // same toggle as the tile's (one PLAN_OPEN slot), so it closes the same way.
  const openRace=raceRows.find(r=>PLAN_OPEN==="race:"+r.date)||null;
  const tilePanel=openRace?(()=>{ const rp=racePanel(openRace);
    return `<section class="plantile tilepanel" id="tilePanel">
      <div class="tilepanel-hd"><h3>${esc(openRace.label)} · why this range</h3>
        <button type="button" class="linkbtn plansrc" data-open="race:${esc(openRace.date)}" aria-controls="tilePanel">Hide ↑</button></div>
      <div class="srccols"><div>${rp.left}</div><div>${rp.right}</div></div>
    </section>`;})():"";

  // — the road: one bar per week (height ∝ km), race flags above, the phase band below —
  const maxWkm=Math.max(1,...allWeeks.map(x=>x.w.km||0));
  const flagAt=date=>{ const i=allWeeks.findIndex(x=>x.w.start&&x.w.start<=date&&date<=weekEndIso(x.w.start)); return i<0?null:i; };
  // SH-86d — a flag sits in its week's column at the race's own day, with a dashed line down to the axis
  const dayIn=(wkStart,date)=>Math.max(0,Math.min(6,Math.round((new Date(date+"T00:00:00Z")-new Date(wkStart+"T00:00:00Z"))/864e5)));
  const flagCell=(i,date,cls,title)=>`<span class="roadflagcell${cls}" style="grid-column:${i+1}"><span class="roadflag${cls}" style="left:${((dayIn(allWeeks[i].w.start,date)+0.5)/7*100).toFixed(2)}%" title="${esc(title)} · ${esc(date)}">🏁</span><i class="roadline" style="left:${((dayIn(allWeeks[i].w.start,date)+0.5)/7*100).toFixed(2)}%"></i></span>`;
  const roadFlags=raceRows.map(r=>{ const i=flagAt(r.date); return i==null?"":flagCell(i,r.date,"",r.label); }).join("")
    +(p.tune_ups||[]).map(t=>{ const i=flagAt(t.date); return i==null?"":flagCell(i,t.date," small",t.label+" (tune-up)"); }).join("");
  const roadBars=allWeeks.map(x=>{
    const w=x.w, sel=!!(selIt&&x.pk===selIt.pk&&w.wk===selIt.w.wk);
    const h=Math.max(4,Math.round((w.km||0)/maxWkm*150));
    const cls=["weekseg",w.frozen||(w.start&&weekEndIso(w.start)<today)?"wsdone":"",weekHoldsToday(w,today)?"wscur":"",
      isDownWeek(w)||["taper","recovery","race"].includes(w.role)?"wsdown":"",sel?"on":""].filter(Boolean).join(" ");
    // SH-86c — colour names the phase (--ph), the FILL says run vs planned: the straddling week
    // splits, its run share solid from the baseline; a week with no km_done yet reads all pale
    const run=(w.partial&&w.km_done!=null&&w.km>0)?Math.min(100,Math.round(w.km_done/w.km*100)):0;
    return `<button type="button" class="${cls}" data-pk="${x.pk}" data-wk="${w.wk}" aria-pressed="${sel?'true':'false'}"
      aria-label="${esc(x.phase.split(" ")[0])} week ${w.wk}, ${sessDate(w.start)} – ${sessDate(weekEndIso(w.start))}: ${U.d(w.km)} ${U.unit}, ${w.runs} runs"
      style="height:${h}px">${run?`<i style="height:${run}%"></i>`:""}</button>`;
  }).join("");
  const roadBand=phaseGroups.map((g,i)=>{
    // one mix for every segment: the old phase bar stepped it per index, which washed a chain's
    // second cycle (segment 6 on) out to plain surface; the hue alone tells the phases apart
    return `<div class="phaseseg" data-pk="${g.key}" title="${esc(phaseNameOf(g.key))}" style="grid-column:span ${g.weeks.length};--mix:40%"><b>${esc(phaseNameOf(g.key).split(" ")[0])}</b></div>`;
  }).join("");
  const todayIdx=allWeeks.findIndex(x=>weekHoldsToday(x.w,today));
  // SH-86c — the today marker ALWAYS shows, on its own line above the axis's dates. SH-86d — the ▲ sits
  // in today's week column at today's day, with a stem up to the bars; its label goes to the side
  // that has room (the right in the road's first half, the left after)
  const todayPct=todayIdx<0?0:(dayIn(allWeeks[todayIdx].w.start,today)+0.5)/7*100;
  const todaySide=todayIdx*2>=allWeeks.length?"end":"start";
  const todayMarker=todayIdx<0?"":`<span class="roadtodaycell" style="grid-column:${todayIdx+1}"><span class="roadtoday ${todaySide}" style="left:${todayPct.toFixed(2)}%"><i class="roadstem"></i><b>▲</b><em>today</em></span></span>`;
  const road=allWeeks.length?`<section class="road" style="--n:${allWeeks.length}">
    <div class="roadlbl">The road · ${allWeeks.length} weeks · ${U.unit} per week</div>
    <div class="roadlegend"><span>colour = phase (band below)</span><span><i class="rl-run"></i>run</span><span><i class="rl-plan"></i>planned</span><span><i class="rl-on"></i>shown below</span></div>
    <div class="roadflags">${roadFlags}</div>
    <div class="roadbars">${roadBars}</div>
    <div class="roadband">${roadBand}</div>
    <div class="roadaxis"><div class="roadmarks">${todayMarker}</div><div class="roaddates"><span>${sessDate(allWeeks[0].w.start)}</span><span>${sessDate(weekEndIso(allWeeks[allWeeks.length-1].w.start))}</span></div></div>
  </section>`:"";

  // — the week pane: the selected week's own card, plus its phase's line —
  const weekPane=selIt?(()=>{
    const w=selIt.w, pk=selIt.pk, idx=allWeeks.indexOf(selIt);
    const status=weekHoldsToday(w,today)?"This week":(w.frozen||(w.start&&weekEndIso(w.start)<today))?"Lived week":"Planned week";
    const ctlHint=qhint("CTL = chronic load (your fitness), a slow ~42-day average of training; ATL = acute load (recent fatigue), a fast ~7-day average. Shown here is each value projected to this phase's end.");
    let phaseLine;
    if(pk==="rebase"){
      phaseLine=`Re-base · ${planWeeks.length} weeks from ${rb.start} · ends CTL ${rb.end_ctl}/ATL ${rb.end_atl}${ctlHint} · Σ ${phaseTot}${ranTot?` · ${ranTot}`:""}${adh}`;
    }else{
      const blk=p[pk];
      const km=((blk&&blk.weeks)||[]).reduce((s,x)=>s+(x.km||0),0);
      const builds=(blk&&blk.builds&&blk.builds.length)
        ?` · builds ${blk.builds.map(c=>COMPL[c]||c).join(" + ")}${qhint("Marathon fitness decomposes into four components: VO₂max × running economy × SSmax/LT2 (the classic three-factor model) plus physiological resilience — how little the first three decay over "+U.d(42.2,U.mi?1:0)+" "+U.unit+". Each quality session is tagged with the component it chiefly builds (hover a session's chip); this line sums what the phase is FOR. The assertive plan periodizes them: VO₂max developed early then merely maintained (red-cell persistence makes it cheap to hold), marathon-pace work growing through the build, and the peak pivoting to resilience — the long run's MP segment extends at constant speed. Framing follows John Davis; the fractions are our operationalization.")}`
        :"";
      phaseLine=`${esc(phaseNameOf(pk))} · ${((blk&&blk.weeks)||[]).length} weeks from ${blk?blk.start:""} · ends CTL ${blk?blk.end_ctl:""}/ATL ${blk?blk.end_atl:""}${ctlHint} · Σ ${U.d(km,0)} ${U.unit}${builds}`;
    }
    const compOpen=PLAN_OPEN==="components";
    return `<section class="weekpane" id="weekpane">
      <div class="wplbl">${status}</div>
      <div class="wphd"><h3>${esc(selIt.phase.split(" ")[0])} · week ${w.wk}</h3>
        <span class="wpnav"><button type="button" id="wkPrev" aria-label="Previous week"${idx<=0?" disabled":""}>‹</button><button type="button" id="wkNext" aria-label="Next week"${idx>=allWeeks.length-1?" disabled":""}>›</button></span></div>
      <div class="muted mono" style="font-size:12px">${sessDate(w.start)} – ${sessDate(weekEndIso(w.start))} · ${U.d(w.km)} ${U.unit} · ${w.runs} runs${w.proj_ctl!=null?` · fitness at week end CTL ${Math.round(w.proj_ctl)}`:""}</div>
      <div class="wphase">${phaseLine}
        <button type="button" class="linkbtn plansrc" data-open="components" aria-expanded="${compOpen}">${compOpen?"Hide the source ↑":"What these mean, and the source →"}</button></div>
      ${compOpen?`<div class="limsrc">${sourceHtml("components","")}</div>`:""}
      ${pk==="rebase"?rebaseCard(w):weekHtml(w,p,today)}
    </section>`;})():"";

  // — the strip: posture, paces and the rebuild stamp, each with its source one click away —
  const postureOpen=PLAN_OPEN==="posture", pacesOpen=PLAN_OPEN==="paces";
  const planStrip=`<section class="planstrip">
    ${RG.mode?`<button type="button" class="stripbtn plansrc" data-open="posture" aria-expanded="${postureOpen}"><span class="rgbadge">${RG.mode==='assertive'?'⟢ Assertive build':'◇ Conservative'}</span>${rideTxt?` <span class="rgride">${rideTxt}</span>`:""}</button>`:""}
    <span class="zones" style="margin:0">${zoneChips}</span>
    <button type="button" class="linkbtn plansrc" data-open="paces" aria-expanded="${pacesOpen}">${pacesOpen?"Hide the source ↑":"Where these come from →"}</button>
    ${refreshed}
    ${postureOpen?`<div class="limsrc stripanel">${sourceHtml("posture", RG.reason||"", "Now")}</div>`:""}
    ${pacesOpen?`<div class="limsrc stripanel">${sourceHtml("paces", U.t(p.note), "Now")}</div>`:""}
  </section>`;

  host.innerHTML=`
    ${planStale?`<div class="stalebanner">⟳ <b>This plan was built by an earlier version of the engine.</b>
      Updating the app does not rebuild saved plans — the weeks below, and every number derived from
      them, are exactly as they were generated${p.engine_version?` by v${esc(p.engine_version)}`:''}.
      ${SH_READONLY?"":`Hit <b>Generate plan</b> to re-read them on v${esc(p.engine_running)}.`}</div>`:''}
    ${seedStale?`<div class="stalebanner">⟳ <b>This plan was built from an older reading of your shape.</b>
      Generated ${p.generated_at?esc(new Date(p.generated_at).toLocaleString()):'earlier'}, seeded from
      your ${esc(SN.was_from||'earlier')} state (CTL ${esc(SN.was.ctl)} · ATL ${esc(SN.was.atl)}). Your
      ${SN.source==='reconstruction'?`end-of-yesterday state, summed from your own runs (it agrees with Runalyze's ${esc(SN.from)} reading to the rounding),`:`settled ${esc(SN.from)} state${SN.bridged_days?`, rolled forward ${SN.bridged_days}
      day${SN.bridged_days>1?'s':''} by measurement,`:''}${SN.stale_unresolved?` (every snapshot in the window was taken before a later upload, so each was checked against your own runs instead),`:SN.stale_skipped?` (${SN.stale_skipped} snapshot${SN.stale_skipped>1?'s':''} taken before a later upload skipped),`:''}${SN.tainted_skipped?` (${SN.tainted_skipped} snapshot${SN.tainted_skipped>1?'s':''} that did not match your own runs skipped),`:''}`} now reads CTL ${esc(SN.ctl)} · ATL
      ${esc(SN.atl)}. ${SH_READONLY?"":`Hit <b>Generate plan</b> to re-read today off it.`}</div>`:''}
    ${diffBanner(LASTDIFF)}
    ${adjBanner(p)}
    <div class="racesdrawer" id="racesDrawer"${RACES_OPEN?"":" hidden"}>${objManager(p)}</div>
    <div class="plantiles">${weekTile}${limitsTile}${racesTile}${tilePanel}</div>
    ${road}
    ${weekPane}
    ${planStrip}`;
  // SH-86 — a tile's "why" click lands here: render that limit's row open and bring it into view.
  // The accordion rule (SH-86d) holds by construction: the rows just rendered all closed, so
  // exactly the named card ends up open.
  if(LIM_OPEN){
    const el=host.querySelector(`#weekpane .limhd[data-lim="${LIM_OPEN}"]`);
    if(el){
      el.setAttribute("aria-expanded","true");
      const src=el.nextElementSibling; if(src&&src.classList.contains("limsrc")) src.hidden=false;
      const cta=el.querySelector(".limcta"); if(cta) cta.textContent="Hide the source ↑";
      scrollToEl(el);
    }
    LIM_OPEN=null;
  }
  // the road's bars pick the week the pane shows — marked in place first (the re-render below
  // rebuilds the same state), so neither the paint nor the ARIA ever reads stale
  host.querySelectorAll(".road .weekseg").forEach(seg=>seg.addEventListener("click",()=>{
    host.querySelectorAll(".road .weekseg").forEach(s=>{ s.classList.toggle("on",s===seg); s.setAttribute("aria-pressed", s===seg); });
    WEEK_SEL=seg.dataset.pk+":"+seg.dataset.wk; LIM_OPEN=null; renderPlan(LAST_PLAN);
  }));
  // ‹ › step the shown week
  const stepWeek=d=>{ const i=allWeeks.indexOf(selIt), j=i+d;
    if(!selIt||j<0||j>=allWeeks.length) return;
    WEEK_SEL=allWeeks[j].pk+":"+allWeeks[j].w.wk; LIM_OPEN=null; renderPlan(LAST_PLAN); };
  const wprev=host.querySelector("#wkPrev"); if(wprev) wprev.addEventListener("click",()=>stepWeek(-1));
  const wnext=host.querySelector("#wkNext"); if(wnext) wnext.addEventListener("click",()=>stepWeek(1));
  // the tile's "open the week" selects its week and brings the pane into view
  const ow=host.querySelector("#openWeek");
  if(ow&&curIt) ow.addEventListener("click",()=>{
    WEEK_SEL=curIt.pk+":"+curIt.w.wk; LIM_OPEN=null; renderPlan(LAST_PLAN);
    const pane=$("#weekpane");
    scrollToEl(pane);
  });
  // a tile's limit button opens that row in the week pane
  host.querySelectorAll(".limgo[data-lim]").forEach(el=>el.addEventListener("click",()=>{
    LIM_OPEN=el.dataset.lim; if(curIt) WEEK_SEL=curIt.pk+":"+curIt.w.wk; renderPlan(LAST_PLAN);
  }));
  // source panels (a race's range, the posture, the paces, the phase components)
  host.querySelectorAll(".plansrc[data-open]").forEach(el=>el.addEventListener("click",()=>{
    PLAN_OPEN=PLAN_OPEN===el.dataset.open?null:el.dataset.open; renderPlan(LAST_PLAN);
  }));
  // the empty races tile's own way into the drawer
  const ropen=host.querySelector("#racesOpen");
  if(ropen) ropen.addEventListener("click",()=>{ RACES_OPEN=true;
    const rbtn=$("#racesBtn"); if(rbtn) rbtn.setAttribute("aria-expanded","true");
    renderPlan(LAST_PLAN); });
  // SH-86 — a limit row opens its source panel: the sentence's why, the rule, the named source
  // SH-86d — an accordion: one card open at a time within its .limits grid; opening one closes
  // the others, clicking the open card closes it
  const limSet=(el,open)=>{
    el.setAttribute("aria-expanded", String(open));
    const src=el.nextElementSibling; if(src&&src.classList.contains("limsrc")) src.hidden=!open;
    const cta=el.querySelector(".limcta"); if(cta) cta.textContent=open?"Hide the source ↑":"Why, and the source →";
  };
  host.querySelectorAll("#plan .limhd").forEach(el=>el.addEventListener("click",()=>{
    const open=el.getAttribute("aria-expanded")==="true";
    const grid=el.closest(".limits");
    if(!open&&grid) grid.querySelectorAll(".limhd").forEach(o=>{ if(o!==el) limSet(o,false); });
    limSet(el,!open);
  }));
  // a completed session → load that day's run into the activity tile + its route map
  host.querySelectorAll(".sline.sclick").forEach(el=>el.addEventListener("click",()=>{
    loadActivity(+el.dataset.actId);
    scrollToEl(document.getElementById("recent"));
  }));
  // a double's per-run links → that specific run's map (stop bubbling so the line's own click doesn't fire)
  host.querySelectorAll("button[data-deload]").forEach(el=>el.addEventListener("click", async ev=>{
    ev.preventDefault(); ev.stopPropagation();
    const week=el.dataset.deload, retire=el.dataset.retire==="1";
    el.disabled=true;
    const r=await fetch("/api/plan/deload",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({week, retire})});
    const p=await r.json();
    if(!r.ok||!p.ok){ el.disabled=false; const sp=el.closest("span"); if(sp) sp.textContent=p.error||"Could not record the answer."; return; }
    LASTDIFF=p.diff; await refreshPlan(p);
  }));
  host.querySelectorAll(".brkrun[data-act-id]").forEach(el=>el.addEventListener("click",ev=>{
    ev.stopPropagation();
    loadActivity(+el.dataset.actId);
    scrollToEl(document.getElementById("recent"));
  }));
  // §CHAIN3 — the races tile doubles as the race selector: tap a leg to view ITS OWN headline
  // and its own drift below, persisted across visits. Enter/Space picks it from the keyboard.
  host.querySelectorAll(".chainrace[data-race]").forEach(el=>{
    const pick=()=>{
      RACE_SEL = el.dataset.race;
      try{ localStorage.setItem("sh.raceSel", RACE_SEL); }catch(e){}
      renderPlan(LAST_PLAN);
      loadDrift();
    };
    el.addEventListener("click",pick);
    el.addEventListener("keydown",ev=>{ if(ev.key==="Enter"||ev.key===" "){ ev.preventDefault(); pick(); } });
  });
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
// SH-86 — the races drawer: the button lives in the section heading (outside #plan's re-renders),
// so its wiring lands once here; renderPlan re-hides or re-shows the drawer from RACES_OPEN.
{ const rbtn=$("#racesBtn");
  if(rbtn){
    if(SH_READONLY) rbtn.textContent="Races";   // the public box edits nothing; the drawer is a list there
    rbtn.addEventListener("click",()=>{
      RACES_OPEN=!RACES_OPEN;
      rbtn.setAttribute("aria-expanded", String(RACES_OPEN));
      const d=$("#racesDrawer"); if(d) d.hidden=!RACES_OPEN;
    });
  } }
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
  if(all.length<2){ host.innerHTML=`<div class="empty">Not enough history yet — the road will move as results come in.</div>`; return; }
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
    if(rr.status==="dnf"){ resMain=`${U.d(rr.dnf_km)} ${U.unit}`; resCls="behind"; resSub="DNF"; }
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
  const volMain = v.state==="unknown"?"—" : v.state==="level"?"on the road" : `${sign(U.d(v.gap))} ${U.unit} ${v.state}`;
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
// §SYM-A — the CTL projection's own track record, read back onto the chart that draws it. The
// engine has never before shown a number about ITSELF here; this one says how far the projection
// has actually run from what happened. The `n` is printed inside the sentence rather than under it
// on purpose — a median over three weeks is a hint, not a finding, and the count is half the claim.
function ctlBiasHTML(b){
  if(!b) return "";
  if(!b.n) return `<p class="note">No weekly fitness forecast has settled yet — once one does, this line says how far it ran from what happened.</p>`;
  const at = (b.lead_span && b.lead_span[0]!==b.lead_span[1])
    ? `${b.lead_span[0]}–${b.lead_span[1]} days out` : `${b.lead_days} days out`;
  const hint = qhint(
    `Every week the engine projects your CTL forward, that projection is scored against the fitness you actually reached, and the score is written down and never rewritten. `+
    `This is the median of those scores — median rather than average because at this many weeks a single odd one moves an average more than the signal does. `+
    `Positive means you came out FITTER than projected: the model under-predicted you. It is a reading, not a lever — nothing in the plan acts on it.`);
  if(b.direction==="level")
    return `<p class="note">At ${at}, the fitness projection has run <b style="color:var(--ok)">level</b> with what happened — median miss inside ±${b.close_within} CTL, over <b>${b.n}</b> scored week${b.n>1?"s":""}. ${hint}</p>`;
  const dir = b.direction==="under" ? "under-predicted" : "over-predicted";
  return `<p class="note">At ${at}, this model has <b style="color:var(--warn)">${dir}</b> your CTL by a median `+
    `<b>${Math.abs(b.median_err_pct)}%</b> (${Math.abs(b.median_err)} CTL), over <b>${b.n}</b> scored week${b.n>1?"s":""}. ${hint}</p>`;
}
// §6m — effort discipline: HR-led "are the athlete's easy days actually easy?" Judged by heart rate (terrain
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
        `<td class="mono">${U.d(r.km)}${U.k}</td><td class="mono">${EFFP(r.gap_pace)}</td>`+
        `<td style="color:${col};font-weight:600">${lbl}</td></tr>`;
    }).join("");
    headCols=`<th>date</th><th>session</th><th>dist</th>`+
      `<th>GAP ${qhint("Grade Adjusted Pace — pace corrected for hills so efforts compare fairly across terrain (from Runalyze).")}</th>`+
      `<th>vs easy ${qhint("Whether the grade-adjusted pace stayed at or below the easy-pace ceiling. The public read judges on pace; heart rate stays private.")}</th>`;
    capLine=`Last ${d.window_days} days · <b>${c.on}/${c.judged}</b> easy runs stayed at easy pace (≤ <b>${esc(U.p(d.easy_pace_ceiling||"—"))}</b>/${U.unit}, grade-adjusted).${c.too_hard?` <b style="color:var(--danger)">${c.too_hard}</b> ran quicker than easy.`:""}`;
    note=`Judged on grade-adjusted pace, not heart rate (HR stays private). Pace is a rough proxy — a run that looks easy on pace can still have been hard on heart rate (heat, hills, fatigue it can't see), so a verdict here can differ from the private HR-led console on the same run.`;
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
        `<td class="mono">${U.d(r.km)}${U.k}</td><td class="mono col-sec">${EFFP(r.gap_pace)}</td>`+
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
      ? `easy-pace ceiling ≈ <b>${esc(U.p(d.easy_pace_ceiling||"—"))}</b>/${U.unit} ${basis}`
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
      `LT1 ${qhint("Your aerobic-threshold pace, which we set at ≈80% of your 5k pace (a pace-first anchor informed by John Davis) — the ceiling your easy days should stay under. It's derived from your CURRENT fitness, so it moves as you get fitter or detrain, and heart rate is the cross-check.")}: easy ceiling ≈ <b>${esc(U.p(L.lt1_pace_fmt))}</b>/${U.unit}`+
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
  // 0.58.0 — the wider ledger
  const L=d.leads||{}, leadLine = Object.keys(L).some(h=>L[h]&&L[h].n) ? `<div class="trrow">Forecast bias by horizon: ${["7","14","28"].map(h=>{
      const b=L[h]||{}; return b.n ? `<b>${h} d</b> ${b.median_err_pct>0?"+":""}${b.median_err_pct}% <span class="muted">(n=${b.n})</span>` : `<b>${h} d</b> <span class="muted">—</span>`; }).join(" · ")}</div>` : "";
  const pr=d.prescription_rows||{}, prLine = pr.n ? `<div class="trrow"><b>${pr.n}</b> week${pr.n===1?"":"s"} of prescription scored · median ran ${Math.round((pr.median_ratio||0)*100)}% of the sheet · <b>${pr.over_run}</b> over-run, <b>${pr.under_run}</b> under-run</div>` : "";
  const rc=d.readiness||{}, rcLine = rc.n ? `<div class="trrow"><b>${rc.n}</b> readiness call${rc.n===1?"":"s"} scored · green days completed ${rc.green&&rc.green.completed_rate!=null?Math.round(rc.green.completed_rate*100)+"%":"—"} <span class="muted">(n=${rc.green?rc.green.n:0})</span> · amber/red days completed ${rc.amber_or_red&&rc.amber_or_red.completed_rate!=null?Math.round(rc.amber_or_red.completed_rate*100)+"%":"—"} <span class="muted">(n=${rc.amber_or_red?rc.amber_or_red.n:0})</span></div>` : "";
  const ov=d.overrides||{}, ovLine = ov.n ? `<div class="trrow"><b>${ov.n}</b> override week${ov.n===1?"":"s"} banked (run at ≥1.5× the intent, no race)${ov.rows?": "+ov.rows.map(r=>`${esc(r.week)} ×${r.ratio}${r.any_heavy_or_poor?" ⚠":""}`).join(", "):""}</div>` : "";
  const rows = races.map(r=>`<tr><td>${esc(r.label||r.type||"race")}</td><td class="mono">${esc(r.date||"")}</td>
      <td>${r.kind==="race_t8"?`T−${Math.round((r.horizon_days||0)/7)} weeks`:"final call"}</td>
      <td class="${r.in_band?"ok":"bad"}">${r.in_band==null?"—":r.in_band?"inside the band":"outside"}</td>
      ${r.err_pct!=null?`<td class="mono">${sign(r.err_pct)}%</td>`:"<td></td>"}</tr>`).join("");
  host.innerHTML = ctlLine + leadLine + prLine + rcLine + ovLine + bandLine + (rows?`<table class="trtbl"><thead><tr><th>Race</th><th>Date</th>
      <th>Scored at</th><th>Band</th><th>Error</th></tr></thead><tbody>${rows}</tbody></table>`:"")
    + `<div class="help">Every row was written when the outcome settled and is never rewritten — a
       forecast that can be re-scored after the fact is not a forecast.</div>`;
}

async function loadDrift(){
  const host=$("#drift"); if(!host) return;
  // §CHAIN4 — the viewed race (RACE_SEL when it names a leg of THIS chain, else the next race ahead,
  // else null for the headline) rides along; the server ignores a date that isn't in the CURRENT
  // plan's own chain and falls back to the headline itself.
  const vr = viewRaceOf(LAST_PLAN, (LOG&&LOG.today) || new Date().toISOString().slice(0,10));   // §CHAIN4
  const driftUrl = "/api/plandrift" + (vr ? `?race=${encodeURIComponent(vr.date)}` : "");
  let d; try{ d=await getJSON(driftUrl); }catch(e){ tileFail(host, "Plan drift", loadDrift, e); return; }
  if(!d || !d.ok){ host.innerHTML=`<div class="empty">${esc((d&&d.error)||"No plan history yet.")}</div>`; return; }
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
  // §CHAIN3 — a multi-A build can read any of its own legs; point at the selector when there's a
  // choice to make (single-A never shows this — `races` comes back empty).
  if((d.races||[]).length>1) cap+=`<div class="note" style="margin-top:2px">Showing ${esc(r.label||'the headline race')} — pick a race in the chain strip above to switch.</div>`;
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
      ${ctlBiasHTML(d.ctl_forecast_bias)}
      <span id="lg-ctl">${driftLegend(ctlLines)}</span><div id="drift-ctl"></div></div>
    <div class="driftblock"><h3 id="h-out">Projected race-day fitness</h3>
      <p class="note" id="note-out">The race-day CTL the engine projected at each re-plan. Is your goal race getting more or less reachable as your results come in?</p>
      <span id="lg-out">${driftLegend(outLines)}</span><div id="drift-out"></div></div>
    ${fd.length?`<div class="driftblock"><h3>Predicted finish · the ledger</h3>
      <p class="note">The finish the engine predicted at each re-plan — the product watching itself. Lower is faster; the dashed envelope is the 80% band (it starts where plans began carrying one) and should narrow as the race nears. Steps in the line are model upgrades or your shape moving, both on the record.</p>
      <span id="lg-fin">${driftLegend(finLegend)}</span><div id="drift-fin"></div></div>`:""}`;
  const setLg=(id,lines)=>{const e=$(id); if(e) e.innerHTML=driftLegend(lines);};
  const FOUND_OUT_NOTE="The race-day CTL the engine projected at each re-plan. Is your goal race getting more or less reachable as your results come in?";
  const drawFounding=()=>{
    setLg("#lg-dist",distLines); setLg("#lg-eff",effLines); setLg("#lg-ctl",ctlLines); setLg("#lg-out",outLines);
    if(fd.length) setLg("#lg-fin",finLegend);
    const nout=$("#note-out"); if(nout) nout.textContent=FOUND_OUT_NOTE;
    const hout=$("#h-out"); if(hout) hout.textContent="Projected race-day fitness";
    const ob=$("#drift-out"); if(ob) ob.innerHTML="";
    mkChart($("#drift-dist"), distLines, {zeroBase:true, fmt:v=>U.d(v,0)+" "+U.unit, nowT:ISO2T(d.today)});
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
    if(!cfTried){ cfTried=true; try{ cfData=(await getJSON(driftUrl+(RACE_SEL?"&":"?")+"compare=1")).counterfactual; }catch(e){} }
    const cf=cfData, cav=$("#drift-caveat");
    if(!cf){ if(cav){ cav.textContent="Comparison needs an objective set."; cav.style.display=""; } return; }
    const ACC2="var(--accent2, var(--accent))", ACC="var(--accent)";
    const lbl=rn(cf.regime), nlbl=rn(cf.vs);
    // direction matters (Duarte's 2026-07-04 catch): from a CONSERVATIVE plan the assertive road is
    // the upper envelope the athlete can EARN; from an ASSERTIVE plan the conservative road is the FLOOR the
    // engine would hold the athlete to if the gate re-closed — "what earning it unlocks" reads as nonsense there.
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
    mkChart($("#drift-dist"), dl, {zeroBase:true, fmt:v=>U.d(v,0)+" "+U.unit, nowT:ISO2T(d.today)});
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
// 0.68.0 — the Settings window is tabbed. Fields are dealt to tabs by key; each tab has ONE Save that
// posts only the changed fields inside it and any key pasted into it, so a Save on Athlete never
// touches Connections. The AI switches sit next to the Claude key they gate; a key the app adds later
// lands on Athlete until this table names its tab.
const SETTINGS_TABS = {
  athlete: ["athlete_context","athlete_age","manual_lthr","units","long_run_day","rest_day_rank"],
  connections: ["ai_narration","ai_parsing","ai_judgment"],
  console: ["house_url","house_name","private_url"]
};
function settingField(s){
  const id="set_"+s.key;
  if(s.kind==="flag"){   // 0.56.0 §S5 — an on/off switch; the value is "1"/"0"
    const on = String(s.value||"").trim()==="1";
    return `<div class="setrow flagrow">
      <label for="${id}" class="flaglbl"><input id="${id}" type="checkbox" data-key="${s.key}" data-flag="1" data-orig="${on?"1":"0"}" ${on?"checked":""}> ${esc(s.label)}<span class="src">${esc(s.source)}</span></label>
      <div class="help">${esc(s.help)}</div>
      <div class="err" id="err_${s.key}"></div>
    </div>`;
  }
  // data-orig = the value as loaded, so a Save posts ONLY the fields the user changed — posting an
  // untouched env/default-sourced value would persist it to meta and shadow the env.
  const ctl = s.kind==="text"
    ? `<textarea id="${id}" data-key="${s.key}" data-orig="${esc(s.value||"")}">${esc(s.value||"")}</textarea>`
    : `<input id="${id}" data-key="${s.key}" data-orig="${esc(s.value||"")}" type="text" value="${esc(s.value||"")}">`;
  return `<div class="setrow">
    <label for="${id}">${esc(s.label)}<span class="src">${esc(s.source)}</span></label>
    ${ctl}
    <div class="help">${esc(s.help)}</div>
    <div class="err" id="err_${s.key}"></div>
  </div>`;
}
const SAVEBAR = (scope)=>`<div class="setbar"><button class="primary" type="submit">Save</button><span class="ok" id="setok-${scope}"></span></div>`;
async function loadSettings(){
  const host=$("#settings"); if(!host) return;
  let d; try{ d=await getJSON("/api/settings"); }catch(e){ host.innerHTML=`<div class="empty">Could not load settings.</div>`; return; }
  if(!d.ok){ host.innerHTML=`<div class="empty">${esc(d.error||"unavailable")}</div>`; return; }
  const byKey={}; d.settings.forEach(s=>{ byKey[s.key]=s; });
  const named=new Set(Object.values(SETTINGS_TABS).flat());
  const rows = keys => keys.map(k=>byKey[k]).filter(Boolean).map(settingField).join("");
  const stray = d.settings.filter(s=>!named.has(s.key)).map(settingField).join("");
  host.innerHTML=`<form class="setform" id="setform" data-scope="athlete">
    ${rows(SETTINGS_TABS.athlete)}${stray}
    ${SAVEBAR("athlete")}
  </form>
  <div class="setrow" style="margin-top:14px">
    <label>Away / can't run</label>
    <ul class="avlist" id="avlist" style="list-style:none;margin:0 0 8px;padding:0"></ul>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <input id="avfrom" type="date" style="max-width:150px" aria-label="Away from">
      <span class="muted">→</span>
      <input id="avto" type="date" style="max-width:150px" aria-label="Away until">
      <input id="avnote" type="text" placeholder="note (optional) — e.g. flight" style="flex:1;min-width:120px">
      <button type="button" class="ghost" id="avadd">Add</button>
    </div>
    <div class="help">Days you can't run (travel, life). The plan lays around them: runs slide to the
      nearest sensible day, spacing rules hold, and a heavily blocked week gets lighter, never
      crammed. Single day: leave the second date empty. Private — away days never appear on
      the public page. Saved as you add them.</div>
    <div class="err" id="averr"></div>
  </div>`;
  $("#setform").addEventListener("submit", saveSettings);
  const ai=$("#aiBox");
  if(ai){
    ai.innerHTML=`<form class="setform" id="setform-connections" data-scope="connections">
      <div class="sectitle" style="margin-top:4px">AI features</div>
      ${rows(SETTINGS_TABS.connections)}
      ${SAVEBAR("connections")}
    </form>`;
    $("#setform-connections").addEventListener("submit", saveSettings);
  }
  const con=$("#consoleBox");
  if(con){
    con.innerHTML=`<form class="setform" id="setform-console" data-scope="console">
      <div class="sectitle" style="margin-top:4px">Links</div>
      ${rows(SETTINGS_TABS.console)}
      ${SAVEBAR("console")}
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
    </div>`;
    $("#setform-console").addEventListener("submit", saveSettings);
  }
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
    // SH-49 — a window cleared in the last 24 h stays listed, muted, with put back (§OBJ3's twin)
    const rowHtml=a=> a.active===0
      ? `<li class="muted" style="display:flex;gap:8px;align-items:center;padding:2px 0">
        <span class="mono">${fmt(a.date_from)}${a.date_to!==a.date_from?" → "+fmt(a.date_to):""}</span>
        ${a.note?`<span>${esc(a.note)}</span>`:""}<span>· cleared</span>
        <button type="button" class="ghost avputback" data-avput="${a.id}" title="Put these away days back — the plan re-lays around them" style="margin-left:auto;padding:1px 8px">put back</button>
      </li>`
      : `<li style="display:flex;gap:8px;align-items:center;padding:2px 0">
        <span class="mono">${fmt(a.date_from)}${a.date_to!==a.date_from?" → "+fmt(a.date_to):""}</span>
        ${a.note?`<span class="muted">${esc(a.note)}</span>`:""}
        <button type="button" class="ghost" data-av="${a.id}" title="Clear — the plan re-lays" style="margin-left:auto;padding:1px 8px">✕</button>
      </li>`;
    list.innerHTML = rows.length ? rows.map(rowHtml).join("")
      : `<li class="muted" style="padding:2px 0">none — you're fully available</li>`;
    list.querySelectorAll("[data-av]").forEach(b=>b.addEventListener("click",async()=>{
      if(b.disabled) return;
      const a=rows.find(x=>String(x.id)===b.dataset.av);
      if(!await confirmDanger({title:"Clear these away days?",
          intro:"The plan re-lays these days as days you can run.",
          lines: a?[fmt(a.date_from)+(a.date_to!==a.date_from?" → "+fmt(a.date_to):"")+(a.note?" · "+a.note:"")]:[],   // confirmDanger escapes each line itself
          alt:"They stay listed here for 24 hours with a put-back control, in case this was a slip.",
          confirmLabel:"Clear"})) return;
      b.disabled=true;
      try{
        const r=await fetch(`/api/availability/${b.dataset.av}/remove`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
        const p=await r.json();
        if(!p.ok){ await notice({title:"Could not clear the away days", intro:p.error||"unknown", tone:"danger"}); }
        else { LASTDIFF=p.diff; await refreshPlan(p); }
      } finally{ b.disabled=false; }
      await render();
    }));
    // SH-49 — put a cleared window back: the undo of ✕, no confirm.
    list.querySelectorAll("[data-avput]").forEach(b=>b.addEventListener("click",async()=>{
      if(b.disabled) return;
      b.disabled=true;
      try{
        const r=await fetch(`/api/availability/${b.dataset.avput}/restore`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
        const p=await r.json();
        if(!p.ok){ await notice({title:"Could not put the away days back", intro:p.error||"unknown", tone:"danger"}); }
        else { LASTDIFF=p.diff; await refreshPlan(p); }
      } finally{ b.disabled=false; }
      await render();
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
async function saveSettings(e){
  e.preventDefault();
  const form=e.target, section=form.closest(".settab")||form, scope=form.dataset.scope||"athlete";
  section.querySelectorAll(".err").forEach(n=>n.textContent="");
  const ok=$("#setok-"+scope); if(ok) ok.textContent="";
  const payload={};
  form.querySelectorAll("[data-key]").forEach(n=>{
    const val = n.dataset.flag ? (n.checked ? "1" : "0") : n.value;   // a switch saves "1"/"0"
    if(val !== n.dataset.orig) payload[n.dataset.key]=val;             // changed fields only
  });
  // 0.68.0 — the tab's one Save also lands every key pasted into it (write-only rows, no Save of their own)
  const pasted=[...section.querySelectorAll("input[id^='sec_']")].filter(i=>i.value);
  let keysSaved=0;
  for(const i of pasted){ if(await saveSecret(i.id.slice(4), false, true)) keysSaved++; }
  if(keysSaved) loadSecrets(true);
  const keyNote = keysSaved ? ` · ${keysSaved} key${keysSaved>1?"s":""} saved` : "";
  if(Object.keys(payload).length===0){ if(ok) ok.textContent = keysSaved ? `${keysSaved} key${keysSaved>1?"s":""} saved ✓` : (pasted.length ? "" : "No changes"); return; }
  const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
  const d=await r.json();
  if(!d.ok){
    const errs=d.errors||{};
    Object.keys(errs).forEach(k=>{ const n=$("#err_"+k); if(n) n.textContent="⚠ "+errs[k]; });
    return;
  }
  if(ok) ok.textContent="Saved ✓"+keyNote;
  const keep=ok?ok.textContent:"";
  await loadSettings();          // refresh provenance badges (env → saved)
  const ok2=$("#setok-"+scope); if(ok2) ok2.textContent=keep;
  fetch("/healthz").then(r=>r.json()).then(h=>{ if(h.ai) AI=h.ai; }).catch(()=>{});   // the AI switches gate the buttons on the next render
}
// Keys block — the two secrets (Runalyze token / Claude key). WRITE-ONLY: the value is never sent back,
// so the field is always empty and shows status only. Private console only (#secretsBox is removed on
// the public view). Saving applies live — no .env edit, no restart.
// §SG — `justSaved` is the key whose field should come back EMPTY; every other field keeps whatever
// the user had typed into it. Re-rendering the whole block used to wipe unsaved siblings, which with
// five keys means pasting three Suunto credentials and losing two of them to the first Save.
// 0.56.0 §AUTH — the Console access block in Settings: how this session is signed in, change the
// passphrase (signs every other device out), log out. Absent on the public and demo boxes.
async function loadAuth(){
  const host=$("#authBox"); if(!host) return;
  let d; try{ d=await getJSON("/api/auth/status"); }catch(e){ host.innerHTML=""; return; }
  if(!d.ok || d.mode==="none"){ host.innerHTML=""; return; }
  const via = d.mode==="proxy"
    ? `Signed in through the proxy in front of this console${d.user?` as <b>${esc(d.user)}</b>`:""}; the passphrase is the fallback when that proxy is not there.`
    : `Signed in with the passphrase${d.passphrase_set_on?` (set ${esc(String(d.passphrase_set_on).slice(0,10))})`:""}. Sessions last ${d.session_days||30} days per device.`;
  host.innerHTML=`<div class="secblock"><div class="sectitle">Console access</div>
    <div class="help">${via} Changing the passphrase signs every other device out.${d.cookie_secure?"":" ⚠ SH_COOKIE_SECURE=0 — the session cookie travels over plain http."}</div>
    <form class="setform" id="ppform" autocomplete="off" style="margin-top:8px">
      <div class="setrow"><label for="pp_cur">Current passphrase</label><input id="pp_cur" type="password" autocomplete="current-password"></div>
      <div class="setrow"><label for="pp_new">New passphrase (12+ characters)</label><input id="pp_new" type="password" autocomplete="new-password" minlength="12"></div>
      <div class="setrow"><label for="pp_new2">Once more</label><input id="pp_new2" type="password" autocomplete="new-password"></div>
      <div class="setbar"><button class="primary" type="submit">Change passphrase</button>
        <button class="ghost" type="button" id="logoutBtn">Log out</button><span class="ok" id="ppok"></span></div>
    </form></div>`;
  $("#ppform").addEventListener("submit", async e=>{
    e.preventDefault();
    if($("#pp_new").value!==$("#pp_new2").value){ $("#ppok").textContent="⚠ the two new entries differ"; return; }
    const r=await fetch("/api/auth/passphrase",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({current:$("#pp_cur").value, new:$("#pp_new").value})});
    const j=await r.json();
    $("#ppok").textContent = j.ok ? "Changed ✓ — other devices are signed out" : "⚠ "+(j.error||"could not change");
    if(j.ok){ $("#pp_cur").value=""; $("#pp_new").value=""; $("#pp_new2").value=""; }
  });
  $("#logoutBtn").addEventListener("click", async ()=>{ try{ await fetch("/logout",{method:"POST"}); }catch(e){} location.href="/login"; });
}
// 0.56.1 (review O1) — the System block in Settings: the operational telemetry that already lived in
// `meta` (last sync, the nightly's outcome, failures in a row, the watch push, the newest backup),
// on the page instead of in `docker logs`.
async function loadSystem(){
  const host=$("#systemBox"); if(!host) return;
  let d; try{ d=await getJSON("/api/system"); }catch(e){ host.innerHTML=""; return; }
  if(!d.ok){ host.innerHTML=""; return; }
  const when = iso => iso ? new Date(iso).toLocaleString() : "never";
  const age = h => h==null ? "—" : h<1 ? "under an hour ago" : h<48 ? `${Math.round(h)} h ago` : `${Math.round(h/24)} d ago`;
  const row = (k,v,warn) => `<div class="sysrow${warn?' warn':''}"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;
  host.innerHTML=`<div class="secblock"><div class="sectitle">System</div>
    ${row("Engine", esc(d.engine_version||"")+(d.tz?` · clock ${esc(d.tz)}`:""))}
    ${row("Database", d.db_writable===false ? `<b>not writable</b> (${esc((d.db_unwritable||[]).join(", "))}) — restart the container; DEPLOY.md §9` : "writable", d.db_writable===false)}
    ${row("Last sync", esc(when(d.last_sync)), d.sync_stale)}
    ${row("Nightly", d.sched.last_ok ? `ok ${esc(when(d.sched.last_ok))}` : "no successful run yet", !d.sched.last_ok)}
    ${row("Failures in a row", String(d.sched.fail_count||0), (d.sched.fail_count||0)>0)}
    ${row("Watch push", d.suunto.connected ? (d.suunto.last_push ? `pushed ${esc(when(d.suunto.last_push))}` : "connected") : "not connected")}
    ${row("COROS push", d.coros&&d.coros.connected ? (d.coros.last_push ? `pushed ${esc(when(d.coros.last_push))}` : "connected") : "not connected")}
    ${row("Newest backup", d.backup.latest ? `${esc(d.backup.latest)} · ${age(d.backup.age_h)}` : "none yet", !d.backup.latest || (d.backup.age_h||0)>48)}
    ${row("Backups kept in", esc(d.backup.dir||""))}
    ${row("Cloudflare Access", d.access&&d.access.bypass
        ? `login bypass on · team ${esc(d.access.team||"")}${d.access.seen&&!d.access.matches?" · <b>the tokens seen carry a different team or tag</b>":""}`
        : d.access&&d.access.seen
          ? `seen: team <b>${esc(d.access.seen.team)}</b> · tag <span class="mono">${esc(d.access.seen.aud)}</span> — to skip the login, on the host: <span class="mono">sh prepare_env.sh --from-container</span> then <span class="mono">docker compose up -d</span> (DEPLOY.md §2a)`
          : "no Access token seen on requests yet (direct, or another proxy)", d.access&&d.access.bypass&&d.access.seen&&!d.access.matches)}
    <div class="help">Backups are written after each successful nightly; ${d.backup.push?"a push hook is configured":"no push hook (SH_BACKUP_PUSH)"} — see DEPLOY.md §7.</div></div>`;
}
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
        ${s.source==="saved"?`<button type="button" class="ghost" data-clr="${s.key}">Clear</button>`:""}
      </div>
      <div class="help">${esc(s.help)}</div>
      <div class="err" id="secerr_${s.key}"></div>
    </div>`;
  // 0.68.0 — the music module's keys render on the Music tab (its own host) when the module is there
  const musicHost=$("#musicKeys");
  const isMusic=s=>/^(spotify_|lastfm_|listenbrainz_)/.test(s.key);
  const main=d.secrets.filter(s=>!(musicHost && isMusic(s)));
  if(musicHost) musicHost.innerHTML=d.secrets.filter(isMusic).map(row).join("");
  host.innerHTML=`<div class="secblock"><div class="sectitle">Connections &amp; keys</div>
    ${main.map(row).join("")}<div id="suuntoBox"></div><div id="corosBox"></div>
    <div class="help aidisc" style="margin-top:6px"><b>What a Claude key sends to Anthropic</b> — plan narration: the computed plan summary and your athlete context; goal parsing: the goal text you type, and your objectives list for race advice; check-in judgment: your check-in note, energy and sleep answers, HRV state, today's session, and anything typed into “Tell the horse”. Each is a switch under <i>AI features</i> below; judgment is off until you turn it on.</div>
    <div class="help" style="margin-top:2px">Keys are write-only — the value never comes back to this
      page; paste a value and press the tab's Save. <code>fp</code> is the first 8 hex of its sha256, so you can confirm which value is stored:
      <code>printf %s "$KEY" | sha256sum | cut -c1-8</code>.</div></div>`;
  // put back what was being typed (minus the field just saved) BEFORE anything can steal focus
  Object.entries(drafts).forEach(([id,v])=>{ if(id==="sec_"+justSaved) return;
    const el=document.getElementById(id); if(el) el.value=v; });
  document.querySelectorAll("#settingsDialog button[data-clr]").forEach(b=>b.addEventListener("click",()=>saveSecret(b.dataset.clr,true)));
  if(musicHost) Object.entries(drafts).forEach(([id,v])=>{ if(id==="sec_"+justSaved) return; const el=document.getElementById(id); if(el) el.value=v; });
  loadSuunto();
  loadCoros();
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
// §CG — the COROS link: a device-style login (Connect returns a link; the athlete signs in on
// COROS's page in a new tab, and each status read polls the pending session once until the tokens
// land), then the nightly re-plan mirrors the road onto the COROS calendar as one training plan
// the watch syncs. Tokens never reach the browser (status only).
async function loadCoros(){
  const box=$("#corosBox"); if(!box) return;
  let d; try{ d=await getJSON("/api/coros/status"); }catch(e){ box.innerHTML=""; return; }
  if(!d.ok){ box.innerHTML=""; return; }
  const badge = d.connected ? `<span class="src ok">✓ connected</span>`
    : d.pending ? `<span class="src warn">waiting for sign-in</span>`
                : `<span class="src warn">not connected</span>`;
  box.innerHTML=`<div class="setrow">
      <label>COROS watch (training-plan push) ${badge}</label>
      <div class="secinput">
        ${d.connected
          ? `<button type="button" class="primary" id="corosPush">Push to COROS</button>
             <button type="button" class="ghost" id="corosDisc">Disconnect</button>`
          : d.pending
            ? `<a class="primary" id="corosLink" href="${esc(d.login_url||"#")}" target="_blank" rel="noopener">Sign in to COROS</a>
               <button type="button" class="ghost" id="corosDone">I've signed in</button>`
            : `<button type="button" class="primary" id="corosConn">Connect COROS</button>`}
      </div>
      <div class="help">Mirrors the plan onto your COROS calendar as one structured training plan
        (COROS syncs it to the watch; days already completed are left alone). Auto-pushes nightly
        after the re-plan once connected.${d.plan_end?` The COROS plan currently runs to ${esc(d.plan_end)}.`:""}
        ${d.last_push?` Last push ${esc(new Date(d.last_push).toLocaleString())}.`:""}</div>
      <div class="err" id="corosErr"></div>
    </div>`;
  const err=$("#corosErr");
  const conn=$("#corosConn"); if(conn) conn.addEventListener("click",async()=>{
    err.textContent=""; conn.disabled=true; conn.textContent="Starting…";
    let r; try{ r=await (await fetch("/api/coros/connect",{method:"POST"})).json(); }
    catch(e){ r={ok:false,error:"network error"}; }
    conn.disabled=false; conn.textContent="Connect COROS";
    if(!r.ok){ err.textContent="⚠ "+(r.error||"could not start the COROS sign-in"); return; }
    loadCoros();   // the status read now carries the pending login_url
  });
  const done=$("#corosDone"); if(done) done.addEventListener("click",()=>loadCoros());
  const disc=$("#corosDisc"); if(disc) disc.addEventListener("click",async()=>{
    try{ await fetch("/api/coros/disconnect",{method:"POST"}); }catch(e){}
    loadCoros();
  });
  const push=$("#corosPush"); if(push) push.addEventListener("click",async()=>{
    err.textContent=""; push.disabled=true; push.textContent="Pushing…";
    let r, resp; try{ resp=await fetch("/api/coros/push",{method:"POST",headers:{"Content-Type":"application/json"},
                                                      body:JSON.stringify({})}); r=await resp.json(); }
    catch(e){ r={ok:false,error:resp?`the server answered HTTP ${resp.status}`:"network error"}; }
    push.disabled=false; push.textContent="Push to COROS";
    const skipped=(r.skipped_days||[]).length;
    if(r.ok) err.innerHTML=`<span class="src ok">✓ ${r.pushed||0} day${(r.pushed===1)?"":"s"} pushed`
      +`${skipped?` — ${skipped} completed day${skipped===1?"":"s"} left alone`:""}${r.note?" — "+esc(r.note):""}</span>`;
    else err.textContent="⚠ "+(r.error||"push failed");
  });
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
async function saveSecret(key, clear, quiet){
  const inp=$("#sec_"+key), errEl=$("#secerr_"+key); if(errEl) errEl.textContent="";
  const value = clear ? "" : (inp ? inp.value : "");
  if(!clear && !value){ if(errEl) errEl.textContent="⚠ paste a value first"; return false; }
  let d; try{
    const r=await fetch("/api/secrets",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,value})});
    d=await r.json();
  }catch(e){ if(errEl) errEl.textContent="⚠ could not save"; return false; }
  if(!d.ok){ if(errEl) errEl.textContent="⚠ "+(d.error||"could not save"); return false; }
  if(inp) inp.value="";
  if(!quiet) loadSecrets(true, key);   // refresh badges + re-validate; siblings keep their unsaved drafts
  // a freshly-set token/key changes what the app can do — refresh the affected surfaces live
  fetch("/healthz").then(r=>r.json()).then(h=>{ LLM_OK=!!h.llm; if(h.ai) AI=h.ai; TOKEN_OK=!!h.token_configured; SYNC_LAST=h.last_sync||null; paintFreshness(); refreshFirstRun(); }).catch(()=>{});
  return true;
}

// learn whether the LLM layer is configured (§6c) before the plan/objectives render.
// §RB — the /runs explorer boots only what it shows: the calendar + the activity tile; the
// dashboard's status loaders (plan, drift, shape, health…) stay dashboard-only.
if(SH_PAGE==="runs"||SH_PAGE==="music"){   // §BEAT — the /music page boots its own loaders (music.js)
  if(SH_PAGE==="runs"){ loadRunsCal(); loadRecent(); }
  // the explorer shows the same footer as the dashboard, so it needs the same sync time; /healthz
  // is the one read it already has a reason to make (private-only page, so `last_sync` is served).
  fetch("/healthz").then(r=>r.json()).then(d=>{ SYNC_LAST=d.last_sync||null; noteSync(d.last_sync);
    footSync(d.last_sync); }).catch(()=>{});
}else{
fetch("/healthz").then(r=>r.json()).then(d=>{ LLM_OK=!!d.llm; if(d.ai) AI=d.ai; TOKEN_OK=!!d.token_configured; SYNC_LAST=d.last_sync||null; noteSync(d.last_sync); paintFreshness(); _frSeen.tok=true; refreshFirstRun(); loadPlan(); }).catch(()=>{ _frSeen.tok=true; refreshFirstRun(); loadPlan(); });
if(SH_PAGE!=="today"){ loadShape(); loadRecent(); loadProjector(); loadWeekly(); loadEffort(); loadZones(); loadTrack(); }
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
  const noteHint=qhint("Pace and HR are two INDEPENDENT estimators of the same fitness (VDOT from VO₂max vs the LTHR grid), not averaged into one number. On a rebuild they can disagree — cardiac decoupling: a given easy pace costs more HR than VDOT predicts. The Pace↔HR line under Effort discipline tracks that divergence; when they disagree on an easy day, heart rate is the direct read of how easy it was. On short intervals the opposite caveat applies: HR lags the effort, so pace leads there.");
  const paceCell=r=>{
    const st=r.pace_slower_than, tg=r.pace_target;
    if(st && st.fmt) return `slower than <b>${esc(U.p(st.fmt))}</b>/${U.unit}`;
    if(tg && tg.fmt) return `≈ <b>${esc(U.p(tg.fmt))}</b>/${U.unit}`;
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
    ? `VO₂max <b>${pa.vo2max}</b>${pa.p5k_fmt?` (5k-equiv ${esc(U.p(pa.p5k_fmt))}/${U.unit})`:""}` : "no VO₂max snapshot";
  host.innerHTML=`
    <div class="efftbl-wrap"><table class="efftbl">
      <thead><tr><th>Intent</th><th>Pace ${paceHint}</th><th>HR ${hrHint}</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="muted" style="font-size:11px;margin-top:6px">Pace from ${paceAnchorTxt} · HR from ${hrAnchorTxt}.
      Two independent reads of the same fitness ${noteHint} — both move as you train.</div>`;
}

// Settings modal open/close (private only — the button is removed on the public view below).
const _setBtn=$("#settingsBtn"), _setDlg=$("#settingsDialog");
// 0.68.0 — the tab bar is built from the <section class="settab"> panels the shell carries (a module
// adds a tab by adding a panel); the last tab used is remembered per device.
function settingsTabs(){
  const bar=$("#settingsTabs"); if(!bar||!_setDlg) return;
  const secs=[..._setDlg.querySelectorAll("section.settab")];
  bar.setAttribute("role","tablist");   // the role arrives WITH the tabs — the static shell carries no tablist over nothing
  bar.innerHTML=secs.map(s=>`<button type="button" role="tab" class="settab-btn" data-for="${esc(s.dataset.tab)}" aria-selected="false" aria-controls="${esc(s.id)}">${esc(s.dataset.label||s.dataset.tab)}</button>`).join("");
  bar.querySelectorAll("[data-for]").forEach(b=>b.addEventListener("click",()=>showSettingsTab(b.dataset.for)));
  bar.addEventListener("keydown",e=>{ if(e.key!=="ArrowRight"&&e.key!=="ArrowLeft") return;
    const btns=[...bar.querySelectorAll("[data-for]")], i=btns.indexOf(document.activeElement); if(i<0) return;
    const n=btns[(i+(e.key==="ArrowRight"?1:btns.length-1))%btns.length]; n.focus(); showSettingsTab(n.dataset.for); e.preventDefault(); });
}
function showSettingsTab(name){
  if(!_setDlg) return;
  const secs=[..._setDlg.querySelectorAll("section.settab")];
  if(!secs.some(s=>s.dataset.tab===name)) name=(secs[0]||{dataset:{}}).dataset.tab;
  secs.forEach(s=>{ s.hidden = s.dataset.tab!==name; });
  _setDlg.querySelectorAll("#settingsTabs [data-for]").forEach(b=>b.setAttribute("aria-selected", b.dataset.for===name?"true":"false"));
  try{ localStorage.setItem("sh.settings.tab", name); }catch(e){}
  document.dispatchEvent(new CustomEvent("sh:settings-tab",{detail:{tab:name}}));
}
function openSettings(tab){
  if(!_setDlg) return;
  if(!$("#setform")) loadSettings();   // (re)load settings if the initial fetch failed
  loadSecrets(true);                    // refresh + live-validate keys on each open
  let last=null; try{ last=localStorage.getItem("sh.settings.tab"); }catch(e){}
  showSettingsTab(tab||last||"athlete");
  if(!_setDlg.open) _setDlg.showModal();
}
window.SHSettings={open:openSettings, show:showSettingsTab, saveSecret, reloadSecrets:()=>loadSecrets(true)};
if(_setDlg){
  settingsTabs();
  if(_setBtn) _setBtn.addEventListener("click", ()=>openSettings());
  const _x=$("#settingsClose"); if(_x) _x.addEventListener("click", ()=>_setDlg.close());
  _setDlg.addEventListener("click", e=>{ if(e.target===_setDlg) _setDlg.close(); });  // backdrop click
  // §SG — landing back from the Suunto OAuth bounce (/?suunto=connected|denied|…): reopen Settings
  // on Connections so the fresh status (or the failure) is right there; scrub the flag from the URL.
  const _su=new URLSearchParams(location.search).get("suunto");
  if(_su){ history.replaceState(null,"",location.pathname); openSettings("connections"); }
}
// 0.57.0 (§5.2) — the analytics sections are <details>, closed on a fresh device and remembered per
// device; the plan, the readiness card and the latest run stay open. The showcase (public and demo)
// opens on the track record: the engine's own scoring is its argument, so it goes first.
(function(){
  document.querySelectorAll("details.section").forEach(d=>{
    const k="sh-open-"+d.id;
    try{ const v=localStorage.getItem(k); if(v==="1") d.open=true; else if(v==="0") d.open=false; }catch(e){}
    d.addEventListener("toggle", ()=>{ try{ localStorage.setItem(k, d.open?"1":"0"); }catch(e){} });
  });
  if(SH_PAGE==="dash"){
    const tab=new URLSearchParams(location.search).get("tab");
    if(tab && ["today","plan","fitness","body"].includes(tab)){
      document.body.dataset.mtab=tab; history.replaceState(null,"",location.pathname);
      document.querySelectorAll(".mnav-btn[data-goto]").forEach(b=>b.setAttribute("aria-current", b.dataset.goto===tab?"page":"false"));
    }
    buildRail();
  }
})();
// 0.57.0 (§5.2, review U6) — a section rail on wide screens: the same list the phone's tab bar
// implies, one link per section, opening a closed section on the way. Desktop only (CSS ≥1100px).
function buildRail(){
  const secs=[...document.querySelectorAll("main.wrap > .section, main.wrap > details.section")].filter(s=>s.id!=="sec-runs" && s.offsetParent!==null || s.tagName==="DETAILS");
  const items=secs.map(s=>{ const h=s.querySelector("h2"); if(!h) return null;
    const label=(h.childNodes[0]&&h.childNodes[0].textContent||h.textContent).trim().replace(/\s+/g," ");
    return {id:s.id, label}; }).filter(Boolean);
  if(items.length<3) return;
  const nav=document.createElement("nav"); nav.className="rail"; nav.setAttribute("aria-label","Sections");
  nav.innerHTML=items.map(it=>`<a href="#${it.id}" data-sec="${it.id}">${esc(it.label)}</a>`).join("");
  nav.addEventListener("click", e=>{ const l=e.target.closest("a[data-sec]"); if(!l) return; e.preventDefault();
    const s=$("#"+l.dataset.sec); if(!s) return; if(s.tagName==="DETAILS") s.open=true;
    const reduce=window.matchMedia&&matchMedia("(prefers-reduced-motion: reduce)").matches;
    s.scrollIntoView({behavior:reduce?"auto":"smooth", block:"start"});
    nav.querySelectorAll("a").forEach(x=>x.classList.toggle("on", x===l)); });
  const main=document.querySelector("main.wrap"); if(main) main.prepend(nav);
}
if(SH_READONLY){
  // public view: health markers stay private; the readiness VERDICT tile stays (the server
  // redacts its inputs/HRV/note). Drop the write controls; surface read-only + the Log-in link.
  // §RB — the run browser (route geo + HR calendar) is private-only: drop its section + links too.
  ["sec-health","sec-zones","settingsDialog","firstrun","sec-runs","runsLink","durcard","effcard"].forEach(id=>{const e=$("#"+id); if(e) e.remove();});
  ["syncBtn","backfillBtn","planBtn","settingsBtn"].forEach(id=>{const e=$("#"+id); if(e) e.remove();});
  const cluster=document.querySelector(".topctl");
  if(cluster){
    let extra='<span class="ro-badge" title="Read-only public view">read-only</span>';
    if(SH_PRIVATE_URL) extra+=`<a class="adminlink" href="${esc(SH_PRIVATE_URL)}" title="Private console">🔒 Log in</a>`;
    cluster.insertAdjacentHTML("afterbegin", extra);
  }
}
// 0.60.2 — the page chrome is decided by the PAGE, on every box. The public Today page used to fall
// through the read-only branch above and keep "☀ Today" as its only link — no way back to the dashboard.
if(SH_PAGE==="today"){
  // 0.57.0 — the daily surface: the readiness card, the session and the check-in, and nothing else
  document.title="Sparing Horse — today";
  const l=$("#todayLink"); if(l){ l.textContent="← Dashboard"; l.href="/"; l.title="Back to the dashboard"; }
  const mt=document.querySelector('.mnav-btn[data-goto="today"]'); if(mt) mt.setAttribute("aria-current","page");
  loadReadiness();
  // §LIMITS (0.58.0) — this week's binding limit, from the plan's own limits block
  getJSON("/api/plan").then(p=>{
    const today=new Date().toISOString().slice(0,10);
    let cur=null;
    for(const k of Object.keys(p||{})){ const v=p[k]; if(v&&typeof v==="object"&&Array.isArray(v.weeks)) for(const w of v.weeks){ if(w.start&&w.start<=today&&today<=weekEndIso(w.start)) cur=w; } }
    if(!cur||!cur.limits) return;
    const L=cur.limits, b=L.binding, ax=b?L[b]:null;
    const line = b ? `This week is held by <b>${esc(LIMIT_NAMES[b]||b)}</b>${ax&&ax.ceiling!=null?` — ${limV(ax.laid,ax.unit)} of ${limV(ax.ceiling,ax.unit)} ${esc(limU(ax.unit||""))}`:""}${ax&&ax.basis?` <span class="muted">(${esc((BASIS[ax.basis]||{}).label||ax.basis)})</span>`:""}.` : "No limit binds this week.";
    let lines = `<div class="whyline"><b>Binding limit:</b> ${line}</div>`;
    // §C (0.59.0) — the judged down week's read, when this is one
    const dl=cur.deload;
    if(dl&&dl.judged) lines += `<div class="whyline"><b>Deload:</b> ${dl.retired?"retired — ":dl.offer?"not owed by the numbers — answer on the week card. ":""}${esc(dl.why||"")}</div>`;
    TODAY_LINES = lines;   // §TODAY-LINES — this fetch races renderReadiness's own rewrites; the string outlives either order
    if($("#readiness .statuscard") && !$("[data-today-lines]")) $("#readiness").insertAdjacentHTML("beforeend", `<div data-today-lines="1">${TODAY_LINES}</div>`);
  }).catch(()=>{});
}else if(SH_PAGE==="runs"){
  // §RB — explorer chrome: the header link points back home; the mobile Runs tab reads active.
  document.title="Sparing Horse — run browser";
  const l=$("#runsLink"); if(l){ l.textContent="← Dashboard"; l.href="/"; l.title="Back to the status dashboard"; }
  const mr=$("#mnavruns"); if(mr) mr.setAttribute("aria-current","page");
}else if(SH_PAGE==="music"){
  // §BEAT — the music page's chrome and loaders live in music.js; nothing of the dashboard boots here
}else if(SH_READONLY){
  loadReadiness();   // the public dashboard: the verdict card only (the server redacts its inputs)
}else{
  loadReadiness(); loadHealth(); loadSettings(); loadSecrets(); loadAuth(); loadSystem(); loadDurability(); loadEfficiency();
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
    if(b && b.dataset.goto && SH_PAGE!=="dash"){ location.href = "/?tab="+b.dataset.goto; return; }   // 0.57.0: /today and /runs hand the tab to the dashboard
    if(!b || !b.dataset.goto) return;                   // the Runs tab is a real <a> — let it navigate
    go(b.dataset.goto, true);
  });
  if(SH_PAGE!=="runs") go((location.hash || "").replace("#",""), false);   // restore tab from the URL (defaults to today)
})();


// §DEMO — reveal the banner and wire its reset. Everything else on the page is the private console
// exactly as the user sees it; the banner is the only difference, because a visitor who does not
// know the athlete is invented will read the numbers as a claim about a real person.
if (SH_DEMO) {
  const bar = $("#demobar"), btn = $("#demoReset");
  if (bar) bar.hidden = false;
  // 0.60.2 — the reset control rides in the top control cluster beside the theme button. It used to
  // sit at the banner's right end, where the fixed cluster covered it on a desktop and stacked over
  // it on a phone. The fixed cluster and the hub link then clear the banner's MEASURED height (it
  // wraps to two lines on a narrow window), so nothing floats over the notice.
  const cluster = document.querySelector(".topctl");
  if (btn && cluster) cluster.prepend(btn);
  const place = () => { if (bar && !bar.hidden) document.documentElement.style.setProperty("--demobar-h", bar.offsetHeight + "px"); };
  place(); window.addEventListener("resize", place);
  if (btn) btn.addEventListener("click", () => {
    btn.disabled = true;
    const was = btn.textContent;
    btn.textContent = "Resetting…";
    // A reset re-seeds and re-plans server-side, so the honest way to show the result is to reload
    // rather than to patch a dozen cards from here and hope they agree.
    fetch("/api/demo/reset", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})
      .then(r => r.json())
      .then(() => location.reload())
      .catch(() => { btn.disabled = false; btn.textContent = was; });
  });
}
