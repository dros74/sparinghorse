// §BEAT (0.61.0) — the music module's script. 0.68.0: two jobs in one file. On EVERY private page it
// renders the Settings → Music tab (connections, matching, library) when that tab is shown; on /music
// it renders the page, whose job is the next run: the hero (session, target, Build), the glances
// (coming up, the ramp) and the evidence (the last run read back). Self-contained on purpose (own
// helpers, own loaders): app.js provides the shell, the footer and the Settings dialog's tab bar, and
// nothing here depends on its internals beyond window.SHSettings, so the module can be stripped from a
// build without a trace.
(function(){
  "use strict";
  const q = s => document.querySelector(s);
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  // §MUSIC-XPORT — a lapsed session sends the page to login instead of parking it on "unavailable";
  // a transport failure (offline, server down) is a d.ok:false the caller already knows how to show.
  async function getJ(url){ const r = await fetch(url, {credentials:"same-origin"}); if(r.status===401){ location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); return new Promise(()=>{}); } const d = await r.json().catch(()=>null); if(!r.ok && d && d.error) throw new Error(d.error); return d; }
  async function postJ(url, body){ let r; try{ r = await fetch(url, {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body||{})}); }catch(e){ return {ok:false, error: (typeof navigator !== "undefined" && navigator.onLine === false) ? "offline — the request did not leave the browser" : "no answer from the server — try again"}; } if(r.status===401){ location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); return new Promise(()=>{}); } let d = await r.json().catch(()=>({})); if(!r.ok && !d.ok) d.ok = false; return d; }
  const ZONE = {easy:"Easy", lt1:"Aerobic threshold", marathon:"Marathon", threshold:"Threshold", interval:"Interval"};
  const KIND = {easy:"Easy", long:"Long run", long_mp:"Long run + MP", tempo:"Tempo", interval:"Intervals",
                progression:"Progression", race_pace:"Race pace", race:"Race", strides:"Strides"};
  const SRC = {neighbours:"similar listeners", cf:"collaborative picks", weekly:"weekly exploration", radio:"LB radio"};
  const fmtDate = iso => { const d = new Date(iso + "T12:00:00"); return d.toLocaleDateString(undefined, {weekday:"short", day:"numeric", month:"short"}); };
  const fmtPace = sec => Math.floor(sec/60) + ":" + String(Math.round(sec%60)).padStart(2,"0");
  const paceOf = s => (s.minutes && s.km) ? fmtPace(s.minutes*60/s.km) : "—";
  const clock = iso => iso ? esc(String(iso).slice(11, 16)) : "";
  const PAGE = document.body.dataset.page;
  const openSettings = tab => { if(window.SHSettings) window.SHSettings.open(tab); };

  // ══ Settings → Music: the tab every private page can open ══════════════════════════════════════
  let STATUS = null, pollT = null;

  async function renderMusicSettings(){
    const host = q("#settingsMusic"); if(!host) return;
    try{ STATUS = await getJ("/api/music/status"); }catch(e){ q("#mSpotify").innerHTML = `<div class="empty">Could not load the music status.</div>`; return; }
    const st = STATUS, sp = st.spotify, lf = st.lastfm, lb = st.listenbrainz || {}, lib = st.library, s = st.settings, lbl = lib.listenbrainz || {};
    const pill = (ok, yes, no) => `<span class="mpill ${ok?"ok":"warn"}">${ok?yes:no}</span>`;
    q("#mSpotify").innerHTML = `<div class="mhead"><span class="dot ${sp.connected && !sp.needs_reconnect ? "ok" : ""}"></span>Spotify
        <span class="mprog">${sp.connected ? (sp.needs_reconnect ? "connected · play history not granted" : "connected" + (sp.user ? " · " + esc(sp.user) : "")) : (sp.configured ? "not connected" : "needs the app keys below")}</span></div>
      <div class="mhint" style="margin:0">${st.played.count} plays stored${st.played.last ? " · pulled " + clock(st.played.last) : ""} · Spotify keeps only the last 50, so the history is pulled when the page opens and every night</div>
      <div class="mrow">
        ${sp.connected && !sp.needs_reconnect ? `<button type="button" class="ghost" id="mDisc">Disconnect</button>` : `<button type="button" class="primary" id="mConn" ${sp.configured?"":"disabled"}>${sp.connected ? "Reconnect Spotify" : "Connect Spotify"}</button>`}
        <button type="button" class="ghost" id="mPull" ${sp.connected && !sp.needs_reconnect ? "" : "disabled"}>Pull plays now</button>
        <span class="mprog" id="mSpotMsg"></span></div>
      ${sp.needs_reconnect ? `<div class="mnote">This version also reads the play history, so each run can be read back song by song. Reconnect once to grant it.</div>` : ""}`;
    q("#mServices").innerHTML = `<div class="mhead"><span class="dot ${(s.lastfm_user && lf.key) || (s.listenbrainz_user) ? "ok" : ""}"></span>Listening history
        <span class="mprog">${[s.lastfm_user ? "last.fm " + esc(s.lastfm_user) : "", s.listenbrainz_user ? "ListenBrainz " + esc(s.listenbrainz_user) : ""].filter(Boolean).join(" · ") || "no service set"}</span></div>
      <div class="mrow"><label for="mLfm">last.fm user</label><input type="text" id="mLfm" value="${esc(s.lastfm_user)}" placeholder="username">${pill(!!lf.key, "key set", "key missing")}</div>
      <div class="mrow"><label for="mLb">ListenBrainz user</label><input type="text" id="mLb" value="${esc(s.listenbrainz_user||"")}" placeholder="username (empty = off)">${pill(!!lb.token, "token set", "no token — radio off, the public sources still read")}</div>
      <div class="mhint" style="margin:2px 0 0">last.fm weighs the songs you actually play; ListenBrainz brings in what similar listeners run to.</div>`;
    const red = q("#mRedirect"); if(red) red.textContent = st.redirect_uri;
    q("#mMatching").innerHTML = `
      <div class="mrow"><label for="mMax">Comfort ceiling</label><input type="number" id="mMax" min="120" max="220" value="${esc(s.max_spm)}" placeholder="spm (empty = observed p90)"><span class="mprog">the cadence no target passes</span></div>
      <div class="mrow"><label for="mStep">Step</label><input type="number" id="mStep" min="0" max="${st.constants.max_step}" step="0.5" value="${esc(s.step_pct)}"><span class="mprog">% over the recent curve on easy segments · work segments take ${st.constants.work_step} % · at most ${st.constants.max_step}</span></div>
      <div class="mrow"><label>Matching</label>
        <span class="chk"><input type="checkbox" id="mHalf" ${s.half_time==="1"?"checked":""}> allow half-time (one beat per stride)</span>
        <span class="chk"><input type="checkbox" id="mDisco" ${s.discovery==="1"?"checked":""}> ReccoBeats discovery when a window runs dry (measured yield 1 in 264 near 170 bpm)</span></div>`;
    const bands = Object.entries(lib.tempo_bands||{}), mx = Math.max(1, ...bands.map(b => b[1]));
    const srcRows = Object.entries(lb.sources || {}).filter(([k, v]) => v.tracks).map(([k, v]) => `<tr><td>${esc(SRC[k]||k)}</td><td class="n">${v.with_tempo}/${v.tracks}</td><td class="n">${v.served}</td><td class="n">${v.read}</td><td class="n">${v.followed_share == null ? "—" : Math.round(100*v.followed_share) + " %"}</td><td class="n">${v.score == null ? "—" : (v.score > 0 ? "+" : "") + v.score.toFixed(2)}</td></tr>`).join("");
    q("#mLibrary").innerHTML = `
      <div class="mlibnums">
        <div><div class="v">${lib.tracks}</div><div class="l">tracks</div></div>
        <div><div class="v">${lib.with_features}</div><div class="l">with tempo</div></div>
        <div><div class="v">${lib.with_taste}</div><div class="l">weighted by plays</div></div>
        ${s.listenbrainz_user ? `<div><div class="v">${lbl.resolved||0}</div><div class="l">from ListenBrainz</div></div><div><div class="v">${lbl.never_run||0}</div><div class="l">never run to</div></div>` : ""}
        <div style="margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;gap:4px"><button type="button" class="ghost" id="mRefresh" ${lib.job.running?"disabled":""}>Refresh library</button><span class="mprog" id="mJob">${jobLine(lib.job)}</span></div>
      </div>
      <div class="mbands">${bands.map(([k,v]) => `<div style="height:${Math.max(2, Math.round(40*v/mx))}px" title="${k} bpm: ${v} tracks"><span>${k.split("-")[0]}</span></div>`).join("")}</div>
      <div class="mbandcap">tracks per 5-bpm band, 150–189${lib.unresolved_scrobbles ? ` · ${lib.unresolved_scrobbles} scrobbles unmatched` : ""}${lib.feature_misses ? ` · ${lib.feature_misses} without a tempo` : ""}</div>
      ${srcRows ? `<table class="mtbl" style="margin-top:10px"><thead><tr><th>Source</th><th>Tempo/tracks</th><th>Listed</th><th>Read</th><th>Followed</th><th>Legs</th></tr></thead><tbody>${srcRows}</tbody></table>` : ""}
      <div class="mhint">Sources: Spotify saved, top and own playlists (the hand-built bpm playlists count), last.fm top tracks and loved; with a ListenBrainz user, the top recordings of the most similar listeners there, its collaborative picks, its weekly exploration lists and (with the token) LB radio around the top artists${s.listenbrainz_user ? ` — ${lbl.candidates||0} candidates read, ${lbl.with_tempo||0} with a tempo, ${lbl.in_band||0} in 160–185, each list carries up to ${lb.fresh_quota} never run to` : ""}. Tempo from ReccoBeats. The refresh runs on a click and every Monday night.</div>`;
    const conn = q("#mConn"); if(conn) conn.addEventListener("click", () => { location.href = "/api/music/spotify/connect"; });
    const disc = q("#mDisc"); if(disc) disc.addEventListener("click", async () => { await postJ("/api/music/spotify/disconnect"); renderMusicSettings(); });
    const pull = q("#mPull"); if(pull) pull.addEventListener("click", async () => { const d = await postJ("/api/music/played/pull"); q("#mSpotMsg").textContent = d.ok ? `${d.new} new plays stored` : (d.error || "could not pull"); });
    q("#mRefresh").addEventListener("click", async () => {
      const d = await postJ("/api/music/library/refresh");
      if(!d.ok){ q("#mJob").textContent = d.error || "could not start"; return; }
      q("#mRefresh").disabled = true; pollJob();
    });
    const save = q("#mSave");
    if(save && !save.dataset.wired){
      save.dataset.wired = "1";
      save.addEventListener("click", async () => {
        const msg = q("#mSaveMsg"); msg.textContent = "";
        const body = {lastfm_user: q("#mLfm").value, listenbrainz_user: q("#mLb").value, max_spm: q("#mMax").value, step_pct: q("#mStep").value,
                      half_time: q("#mHalf").checked ? "1" : "0", discovery: q("#mDisco").checked ? "1" : "0"};
        const d = await postJ("/api/music/settings", body);
        // the tab's one Save also lands any key pasted into it (the rows are write-only, no Save of their own)
        let keys = 0;
        if(window.SHSettings){
          for(const i of document.querySelectorAll("#settab-music input[id^='sec_']")){ if(i.value && await window.SHSettings.saveSecret(i.id.slice(4), false, true)) keys++; }
          if(keys) window.SHSettings.reloadSecrets();
        }
        msg.textContent = d.ok ? "Saved ✓" + (keys ? ` · ${keys} key${keys>1?"s":""} saved` : "") : Object.values(d.errors||{}).join("; ") || "could not save";
        if(d.ok){ renderMusicSettings(); if(PAGE === "music"){ loadStatus(); loadSessions(); } }
      });
    }
    if(lib.job.running) pollJob();
  }
  function jobLine(j){
    if(!j) return "";
    if(j.running) return `refreshing — ${esc(j.step)}${j.total ? ` ${j.done}/${j.total}` : ""}`;
    if(j.error) return `last refresh failed: ${esc(j.error)}`;
    if(j.summary){ const s = j.summary; return `last refresh: ${s.spotify_saved} saved · ${s.spotify_top} top · ${s.spotify_playlisted} in ${s.playlists} playlists · ${s.lfm_tracks} scrobbled · ${s.resolved} matched · ${s.unresolved} unmatched${s.lb_candidates ? ` · ListenBrainz ${s.lb_candidates} read, ${s.lb_new} new, ${s.lb_resolved} matched now, ${s.lb_unresolved} waiting` : ""}${(s.lb_errors||[]).length ? ` · ListenBrainz: ${esc(s.lb_errors.join("; "))}` : ""} · ${s.features} features (${s.feature_misses} without)`; }
    return "";
  }
  function pollJob(){
    clearTimeout(pollT);
    pollT = setTimeout(async () => {
      let d; try{ d = await getJ("/api/music/library/status"); }catch(e){ return; }
      const el = q("#mJob"); if(el) el.innerHTML = jobLine(d.library.job);
      if(d.library.job.running) pollJob(); else { renderMusicSettings(); if(PAGE === "music"){ loadStatus(); loadSessions(); } }
    }, 2000);
  }
  document.addEventListener("sh:settings-tab", e => { if(e.detail && e.detail.tab === "music") renderMusicSettings(); });

  if(PAGE !== "music") return;

  // ══ The /music page ═══════════════════════════════════════════════════════════════════════════
  document.title = "Sparing Horse — music";
  const home = q("#musicLink"); if(home){ home.textContent = "← Dashboard"; home.href = "/"; home.title = "Back to the status dashboard"; }
  const mm = q("#mnavmusic"); if(mm) mm.setAttribute("aria-current", "page");
  const flag = new URLSearchParams(location.search).get("spotify");
  const FLAG = {connected:"Spotify connected.", denied:"Spotify authorisation was declined.",
                state_error:"The Spotify return did not match the request that started it — try again.",
                exchange_error:"Spotify refused the code exchange — check the client ID and secret in Settings."};
  if(flag) history.replaceState(null, "", "/music");
  const sbtn = q("#musicSettingsBtn"); if(sbtn) sbtn.addEventListener("click", () => openSettings("music"));

  async function loadStatus(){
    const line = q("#musicStatus");
    try{ STATUS = await getJ("/api/music/status"); }catch(e){ line.textContent = "Could not load the music status."; return; }
    const st = STATUS, sp = st.spotify, lib = st.library, lbl = lib.listenbrainz || {};
    const bits = [];
    bits.push(sp.connected ? (sp.needs_reconnect ? "Spotify needs a reconnect" : "Spotify connected" + (sp.user ? " · " + esc(sp.user) : "")) : "Spotify not connected");
    bits.push(`${lib.with_features} tracks with tempo`);
    if(st.settings.listenbrainz_user) bits.push(`${lbl.never_run||0} never run to`);
    if(st.played.last) bits.push(`plays pulled ${clock(st.played.last)}`);
    line.innerHTML = bits.join(" · ") + (flag && FLAG[flag] ? ` · <span class="merr" style="display:inline">${esc(FLAG[flag])}</span>` : "");
    try{ const c = await getJ("/api/music/cadence"); renderCadence(c, STATUS); }catch(e){ q("#musicCad").innerHTML = `<div class="empty">Could not load the cadence curve.</div>`; }
  }

  function renderCadence(c, st){
    const host = q("#musicCad"), cv = c.curve;
    if(!cv){ host.innerHTML = `<h3>Cadence</h3><div class="empty">No runs with cadence yet — sync a few runs first. The targets come from this runner's own speed–cadence curve, nothing else.</div>`; return; }
    const rp = c.ramp, rg = rp && rp.rung;
    let head = `<h3>The ladder <span class="mpill">no race on the road</span></h3>
      <p class="sub">The recent curve plus the step, per build — a race on the plan would give the lift a calendar.</p>`;
    let stats = "";
    if(rp){
      const when = rp.phase === "before" ? `starts ${esc(fmtDate(rp.start))}` : rp.phase === "landed" ? `landed ${esc(fmtDate(rp.end))}` : `lands ${esc(fmtDate(rp.end))}`;
      head = `<h3>The ramp <span class="mpill">to ${esc(rp.label)} · ${rp.days_to_race} days out</span></h3>`;
      stats = `<div class="mstats">
          <div class="mstat"><div class="mk">Week</div><div class="v">${rp.phase === "ramp" ? rp.week : (rp.phase === "landed" ? rp.weeks : 0)} <small>of ${rp.weeks}</small></div></div>
          <div class="mstat"><div class="mk">${rp.phase === "before" ? "Starts" : "Lands"}</div><div class="v">${esc(fmtDate(rp.phase === "before" ? rp.start : rp.end).replace(/^\w+,?\s*/, ""))}</div></div>
          <div class="mstat"><div class="mk">Last rung</div><div class="v">${!rg ? "—" : rg.held ? "held" : "missed"}</div></div>
        </div>
        <div class="mbar"><i style="width:${Math.round(100 * (rp.fraction || 0))}%"></i></div>
        <p class="mhint" style="margin:0 0 8px">${when} (${rp.lead_days} days before the race). ${!rg ? "No read-back with a playlist yet — the rung is one step over the recent curve."
          : rg.held ? `${esc(fmtDate(rg.date))} ran <b>${Math.round(rg.ran)}</b> against ${Math.round(rg.target)} over ${rg.songs} playlist song${rg.songs === 1 ? "" : "s"} — the next target can be one step higher.`
          : `${esc(fmtDate(rg.date))} ran <b>${Math.round(rg.ran)}</b> against ${Math.round(rg.target)} over ${rg.songs} playlist song${rg.songs === 1 ? "" : "s"} — the ramp waits there.`}</p>`;
    }
    const rows = (c.table||[]).map(r => `<tr><td>${esc(ZONE[r.zone]||r.zone)}</td><td class="n">${esc(r.pace)}</td><td class="n"><b>${Math.round(r.spm)}</b></td><td class="why">${esc(r.why)}</td></tr>`).join("");
    host.innerHTML = `${head}${stats}
      <table class="mtbl"><thead><tr><th>Zone</th><th>Pace</th><th>Target</th><th>Set by</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="mhint">Comfort ceiling <b>${cv.comfort_max} spm</b> (${esc(cv.comfort_source)}) · step ${esc(st.settings.step_pct)} % on easy segments, ${st.constants.work_step} % on work · window ±${Math.round(st.constants.window*100)} % · <a class="mlink" href="#musicHow" id="mHowLink">how the targets are set</a></p>`;
    // the full curve — trained and recent per zone — lives behind the explainer
    const fit = ln => ln ? (ln.fitted ? `fitted on ${ln.n} runs` : `${ln.n} runs — median on the pooled slope`) : "—";
    const full = (c.table||[]).map(r => `<tr><td>${esc(ZONE[r.zone]||r.zone)}</td><td class="n">${esc(r.pace)}</td><td class="n">${r.trained}</td><td class="n">${r.recent}</td><td class="n"><b>${Math.round(r.spm)}</b></td><td class="why">${esc(r.why)}</td></tr>`).join("");
    const recent = (c.recent||[]).slice(-5).map(r => `${esc(r.date.slice(5))} ${esc(r.pace)}/km · <b>${r.spm}</b>`).join(" &nbsp;·&nbsp; ");
    const ct = q("#musicCurveTable");
    if(ct) ct.innerHTML = `<p class="sub" style="font-size:12px;color:var(--muted);margin:0 0 8px">Steps per minute at each of the plan's paces, from ${cv.n_rows} runs: the trained self (${fit(cv.trained)}), the last ${Math.round((Date.now()-new Date(cv.recent_from))/864e5)} days (${fit(cv.recent)}), and the target the playlists are built to.</p>
      <table class="mtbl"><thead><tr><th>Zone</th><th>Pace</th><th>Trained</th><th>Recent</th><th>Target</th><th>Set by</th></tr></thead><tbody>${full}</tbody></table>
      ${recent ? `<p class="mhint">Last runs: ${recent}</p>` : ""}`;
    const hl = q("#mHowLink"); if(hl) hl.addEventListener("click", e => { e.preventDefault(); const d = q("#musicHow"); d.open = true; d.scrollIntoView({behavior:"smooth", block:"start"}); });
  }

  let SESSIONS = null;
  async function loadSessions(){
    const hero = q("#musicNext"), host = q("#musicSessions");
    let d; try{ d = await getJ("/api/music/sessions"); }catch(e){ hero.innerHTML = `<div class="empty">Could not load the sessions.</div>`; host.innerHTML = ""; return; }
    SESSIONS = d;
    if(!d.has_plan){ hero.innerHTML = `<div class="empty">No plan yet — generate one on the dashboard first.</div>`; host.innerHTML = `<h3>Coming up</h3><div class="empty">Nothing to play.</div>`; return; }
    if(!d.sessions.length){ hero.innerHTML = `<div class="empty">Nothing to play in the next ten days.</div>`; host.innerHTML = `<h3>Coming up</h3><div class="empty">Nothing in the next ten days.</div>`; return; }
    const next = d.sessions[0], rest = d.sessions.slice(1);
    const chips = (next.segs||[]).filter(sg => sg.spm).map(sg => `<span class="mchip" title="${esc(sg.why||"")}">${esc(String(sg.label).split(" — ")[0].toLowerCase())} ${Math.round(sg.spm)}</span>`).join("");
    hero.innerHTML = `
      <div class="mcol"><div class="mk">Next run</div>
        <div class="mbig">${esc(fmtDate(next.date))} · ${esc(KIND[next.kind]||next.kind)}${next.race ? ` — ${esc(next.note||"")}` : ""}</div>
        <div class="mprog" style="font-size:13px">${next.km} km · ${next.minutes} min · ${esc(paceOf(next))} /km</div>
        <div class="mhint" style="margin:0">${next.built ? `Playlist built ${esc(String(next.built.built_at||"").slice(0,10))} — <a class="mlink" href="${esc(next.built.url)}" target="_blank" rel="noopener">open in Spotify ↗</a>` : "No playlist yet for this run."}</div></div>
      <div class="mcol"><div class="mk">Target cadence</div>
        ${next.target_spm ? `<div class="mnum">${Math.round(next.target_spm)} <small>spm</small></div><div class="mhint" style="margin:0">${esc(next.why||"")}</div><div class="mchips">${chips}</div>` : `<div class="mhint" style="margin:0">no cadence curve yet — sync a few runs with cadence first</div>`}</div>
      <div class="mcol mact">
        <button type="button" class="primary" data-build="${esc(next.key)}" ${d.has_curve?"":"disabled"}>${next.built ? "Rebuild the playlist" : "Build the playlist"}</button>
        <button type="button" class="ghost" data-prev="${esc(next.key)}">Preview the songs first</button>
        <span class="mhint" style="margin:0;text-align:center" id="mHeroNote"></span></div>`;
    const rows = rest.map(s => `<tr class="${s.race?"race":""}">
        <td>${esc(fmtDate(s.date))}</td><td>${esc(KIND[s.kind]||s.kind)}${s.race ? ` — ${esc(s.note||"")}` : ""} · ${s.km} km</td>
        <td class="n">${s.target_spm ? `<b>${Math.round(s.target_spm)}</b>` : "—"}</td>
        <td class="act">${s.built ? `<a class="mlink" href="${esc(s.built.url)}" target="_blank" rel="noopener">open ↗</a> ` : ""}<button type="button" class="ghost" data-prev="${esc(s.key)}">Preview</button> <button type="button" class="ghost" data-build="${esc(s.key)}" ${d.has_curve?"":"disabled"}>${s.built ? "Rebuild" : "Build"}</button></td>
      </tr>`).join("");
    host.innerHTML = `<h3>Coming up <span class="mpill">next 10 days + the race</span></h3>
      ${rest.length ? `<div style="overflow-x:auto"><table class="mtbl"><thead><tr><th>Day</th><th>Session</th><th>Target</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="empty">Only the next run is on the ten-day road.</div>`}
      <p class="mhint">Target is the minutes-weighted cadence across the session's segments. Build writes a private Spotify playlist named for the session; Rebuild replaces its tracks. A race list takes the ramp of the day it is built — rebuild it in race week.</p>`;
    document.querySelectorAll("#sec-music [data-prev]").forEach(b => b.addEventListener("click", () => run(b.dataset.prev, false, b)));
    document.querySelectorAll("#sec-music [data-build]").forEach(b => b.addEventListener("click", () => run(b.dataset.build, true, b)));
  }

  async function run(key, write, btn){
    const host = q("#musicResult"); host.hidden = false;
    host.innerHTML = `<div class="empty">${write ? "Building the playlist…" : "Previewing…"}</div>`;
    if(btn) btn.disabled = true;
    const d = await postJ(write ? "/api/music/build" : "/api/music/preview", {key});
    if(btn) btn.disabled = false;
    renderResult(d, write);
    if(write && d.written) loadSessions();
    host.scrollIntoView({behavior:"smooth", block:"start"});
  }

  function renderResult(d, write){
    const host = q("#musicResult");
    if(!d.ok){ host.innerHTML = `<h3>Playlist</h3><div class="merr">${esc(d.error||"could not build")}</div>`; return; }
    const s = d.session || {};
    const segs = (d.segments||[]).map(sg => `<div class="mseg">
        <div class="mhead"><b>${esc(sg.label)}</b><span class="mprog">${sg.minutes} min · target <b>${Math.round(sg.target_spm)}</b> spm (${esc(sg.target.why)}) · ${sg.filled_min} min filled${sg.fresh != null && sg.tracks.length ? ` · ${sg.fresh} of ${sg.tracks.length} new to the last ${d.rotation_days} days` : ""}</span></div>
        ${sg.tracks.length ? `<ol>${sg.tracks.map(t => `<li>${esc(t.title)} — ${esc(t.artist)}<span class="meta">${Math.round(t.tempo)} bpm${t.hit==="half"?" ×2":""} · ${Math.round(t.duration_ms/60000)}′${t.weight ? ` · taste ${t.weight.toFixed(2)}` : ""}${t.follow != null ? ` · legs ${t.follow > 0 ? "+" : ""}${t.follow.toFixed(1)}` : ""}${t.served ? ` · on ${t.served} recent list${t.served > 1 ? "s" : ""}` : ""}</span>${t.never_run ? `<span class="new">never run to${t.source ? " · " + esc(String(t.source).split(",").map(k => SRC[k]||k).join(", ")) : ""}</span>` : t.discovered ? `<span class="new">new</span>` : ""}</li>`).join("")}</ol>` : `<div class="mnote">no track in the window</div>`}
      </div>`).join("");
    host.innerHTML = `<h3>${esc(d.name)}</h3>
      <p class="sub">${esc(fmtDate(s.date))} · ${esc(KIND[s.kind]||s.kind)} ${s.km} km · ${d.minutes} min · ${d.tracks} tracks, ${d.filled_min} min · overall target ${Math.round(d.target_spm)} spm${d.never_run ? ` · ${d.never_run} never run to` : ""}${d.sensor_bias_spm ? ` · songs ${d.sensor_bias_spm.toFixed(1)} bpm above it (the watch counts low)` : ""}</p>
      ${write ? (d.written ? `<div class="mok">Written to Spotify — <a class="mlink" href="${esc(d.url)}" target="_blank" rel="noopener">open the playlist ↗</a></div>` : `<div class="merr">${esc(d.error||"not written")}</div>`) : `<div class="mprog">Preview only — nothing was written.</div>`}
      ${(d.notes||[]).map(n => `<div class="mnote">${esc(n)}</div>`).join("")}
      ${segs}`;
    const note = q("#mHeroNote"); if(note && SESSIONS && d.key === SESSIONS.sessions[0].key) note.textContent = `${d.tracks} songs${d.never_run ? ` · ${d.never_run} never run to` : ""}${d.fresh != null ? ` · ${d.fresh} new to the last ${d.rotation_days} days` : ""}`;
  }

  // ── the evidence: the last run read back (the nightly's, or on a click) ───────────────────────
  let RUNS = [];
  async function loadRuns(){
    const host = q("#musicReadback");
    let d; try{ d = await getJ("/api/music/runs"); }catch(e){ host.innerHTML = `<div class="empty">Could not load the runs.</div>`; return; }
    RUNS = d.runs || [];
    if(!RUNS.length){ host.innerHTML = `<h3>Last run</h3><div class="empty">No runs yet.</div>`; return; }
    const current = RUNS.find(r => r.readback_at) || RUNS.find(r => r.playlist) || RUNS[0];
    showRun(current.id);
  }
  function showRun(id){
    const host = q("#musicReadback"), r = RUNS.find(x => x.id === id); if(!r) return;
    const opts = RUNS.map(x => `<option value="${x.id}" ${x.id === id ? "selected" : ""}>${esc(fmtDate(x.date))} · ${x.km} km · ${x.playlist ? esc(x.playlist) : "no playlist"}${x.readback_at ? " · read" : ""}</option>`).join("");
    host.innerHTML = `<div class="mtitle" style="margin:0 0 8px"><h3 style="margin:0">Last run, read back</h3>
        <span class="mprog">${esc(fmtDate(r.date))} · ${r.km} km · ${r.minutes} min${r.playlist ? ` · ${esc(r.playlist)}` : " · no playlist built"}</span>
        <span class="mrow" style="margin:0 0 0 auto"><label for="mRunSel" style="min-width:0">Run</label><select id="mRunSel">${opts}</select>
        <button type="button" class="ghost" id="mReadBtn">${r.readback_at ? "Read again" : "Read back"}</button></span></div>
      <div id="musicRb">${r.readback_at ? `<div class="empty">Loading the read-back…</div>` : `<div class="empty">Not read yet — the nightly reads the day's run once its plays are in, or press Read back.</div>`}</div>
      <p class="mhint">Which song was playing when, and the cadence the legs held through it, seams excluded. From the watch: <b>one lap press</b> during a song = the beat lost me here · <b>two presses within six seconds</b> = never again · a headset <b>skip</b> sets the song aside for that kind of segment. The stream adds <b>followed</b> (cadence within 1 % of the song's tempo) and <b>dips</b> (4 spm under the song's median for 4 s or more). Together they score each track for the next lists. A song the play history dropped is read from the list order when the gap between its neighbours fits it, and is marked <b>from the list</b>. On a reps day a song is read over the reps only; one that fell on the recoveries is left ungraded.</p>`;
    q("#mRunSel").addEventListener("change", e => showRun(Number(e.target.value)));
    q("#mReadBtn").addEventListener("click", async () => {
      const b = q("#mReadBtn"); b.disabled = true; q("#musicRb").innerHTML = `<div class="empty">Reading the run…</div>`;
      const res = await postJ("/api/music/readback", {run_id: id}); b.disabled = false;
      if(res.ok){ r.readback_at = res.computed_at || "now"; b.textContent = "Read again"; }
      renderReadback(res);
    });
    if(r.readback_at) getJ(`/api/music/readback/${id}`).then(renderReadback).catch(e => { q("#musicRb").innerHTML = `<div class="merr">${esc(e.message || "could not load the read-back")}</div>`; });
  }
  let CURRENT_RB = null;
  function verdictCell(s){
    const badge = s.manual ? `<b>never again</b> <span class="mprog">(yours)</span> <button type="button" class="linkbtn" data-keep="${esc(s.spotify_id)}">keep</button>`
      : s.run_rating === "never" ? `<b>never again</b>`
      : s.run_rating === "break" ? `<b>break${s.breaks > 1 ? ` ×${s.breaks}` : ""}</b> <span class="mprog">at ${(s.break_at || []).map(t => fmtPace(t)).join(", ")}</span>`
      : s.run_rating ? `<b>${esc(s.run_rating)}</b> <span class="mprog">(older protocol)</span>` : "";
    const leave = s.run_rating !== "never" ? ` <button type="button" class="linkbtn" data-never="${esc(s.spotify_id)}">leave it out</button>` : "";
    return badge + leave;
  }
  function renderReadback(r){
    CURRENT_RB = r;
    const host = q("#musicRb");
    if(!r.ok){ host.innerHTML = `<div class="merr">${esc(r.error || "could not read the run")}</div>`; return; }
    const f = r.followed || {}, ran = r.songs.filter(s => s.spm).map(s => s.spm);
    const mean = ran.length ? Math.round(ran.reduce((a, b) => a + b, 0) / ran.length) : null;
    const stats = `<div class="mstats">
        <div class="mstat"><div class="mk">Legs followed</div><div class="v">${f.songs != null ? `${f.entrained} <small>of ${f.songs} songs</small>` : "—"}${r.sensor_bias_spm ? ` <small>legs +${r.sensor_bias_spm.toFixed(1)} spm</small>` : ""}</div></div>
        <div class="mstat"><div class="mk">Breaks</div><div class="v">${f.breaks || 0}</div></div>
        <div class="mstat"><div class="mk">Set aside</div><div class="v">${r.set_aside || 0}</div></div>
        <div class="mstat"><div class="mk">Cadence</div><div class="v">${mean != null ? mean : "—"}${r.target_spm ? ` <small>vs ${Math.round(r.target_spm)} asked</small>` : ""}</div></div>
      </div>`;
    const rows = r.songs.map(s => `<tr><td class="n">${Math.round(s.start_s/60)}′</td><td>${esc(s.title)} <span class="mprog">— ${esc(s.artist)}</span>${s.skipped ? ` <span class="mpill warn">skipped</span>` : ""}${s.inferred ? ` <span class="mpill">from the list</span>` : ""}${s.in_playlist === false ? ` <span class="mpill warn">off the playlist</span>` : ""}${s.segment ? ` <span class="mprog">· ${esc(String(s.segment).split(" — ")[0])}</span>` : ""}</td>
        <td class="n">${Math.round(s.read_s/60)}′</td><td class="n"><b>${Math.round(s.spm)}</b>${s.work_s != null && !s.jogs_only ? ` <span class="mprog">reps ${Math.round(s.work_s/60)}′</span>` : ""}${s.vs_target != null ? ` <span class="mprog">${s.vs_target > 0 ? "+" : ""}${s.vs_target.toFixed(1)}</span>` : ""}</td>
        <td class="n">${s.pace_sec ? fmtPace(s.pace_sec) : "—"}</td>
        <td>${s.jogs_only ? `<span class="mprog">on the jogs</span>` : s.entrained == null ? `<span class="mprog">no tempo</span>` : `${s.entrained ? `<span class="mpill ok">followed</span>` : `<span class="mprog">${s.delta_pct > 0 ? "+" : ""}${(100*s.delta_pct).toFixed(1)} %</span>`}${s.dips ? ` <span class="mpill warn">${s.dips} dip${s.dips > 1 ? "s" : ""}</span>` : ""}${s.follow != null ? ` <span class="mprog">legs ${s.follow > 0 ? "+" : ""}${s.follow.toFixed(1)}</span>` : ""}`}</td>
        <td>${verdictCell(s)}</td></tr>`).join("");
    const unmatched = (r.presses || []).filter(p => !p.spotify_id).length;
    host.innerHTML = `${stats}
      <p class="mhint" style="margin:0 0 8px">Read from the ${r.stream_source === "fit" ? "FIT file (1 Hz, lap presses)" : "summary stream"}${r.computed_at ? ` on ${esc(String(r.computed_at).slice(0, 16).replace("T", " "))}` : ""}${r.skipped ? ` · ${r.skipped} skipped` : ""}${r.off_playlist ? ` · ${r.off_playlist} off the playlist (not read for the rung)` : ""}${r.inferred ? ` · ${r.inferred} read from the list` : ""}${r.clock_shift_s ? ` · the file's clock ran ${Math.round(Math.abs(r.clock_shift_s)/60)} min ${r.clock_shift_s < 0 ? "ahead of" : "behind"} the run and was re-anchored` : ""}${unmatched ? ` · ${unmatched} press${unmatched > 1 ? "es" : ""} outside any song` : ""}</p>
      <div style="overflow-x:auto"><table class="mtbl"><thead><tr><th>At</th><th>Song</th><th>Read</th><th>Cadence</th><th>Pace</th><th>Legs</th><th>Verdict</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    if(!host.dataset.wired){
      host.dataset.wired = "1";
      host.addEventListener("click", async e => {
        const btn = e.target.closest("[data-never],[data-keep]");
        if(!btn || !CURRENT_RB) return;
        const old = host.querySelector(".merr"); if(old) old.remove();
        const spotify_id = btn.dataset.never || btn.dataset.keep;
        const verdict = btn.dataset.never ? "never" : "keep";
        btn.disabled = true;
        const d = await postJ("/api/music/verdict", {run_id: CURRENT_RB.run_id, spotify_id, verdict});
        if(!d.ok){
          btn.disabled = false;
          host.insertAdjacentHTML("beforeend", `<p class="merr">${esc(d.error || "could not save the verdict")}</p>`);
          return;
        }
        let res; try{ res = await getJ(`/api/music/readback/${CURRENT_RB.run_id}`); }catch(err){ res = {ok:false, error: err.message}; }
        renderReadback(res);
      });
    }
  }

  loadStatus(); loadSessions(); loadRuns();
  if(flag) openSettings("music");   // back from the Spotify OAuth bounce: the fresh connection state is on the tab
})();
