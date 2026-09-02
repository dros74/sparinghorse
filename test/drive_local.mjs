// Playwright driver for the local PRIVATE test instance (seeded synthetic DB, no token).
// Exercises REAL flows against a running server — the gap an isolated CSS harness can't cover.
// Two modes (MODE env):
//   full  (default) — a fully-seeded instance: dashboard populates, the Settings dialog
//                     closed→open→close→reopen cycle (the #67 regression), and the first-run
//                     card is hidden (configured) + surfaces "Add a race" once objectives are gone.
//   empty           — a fresh tokenless/dataless instance: the first-run card shows step ①.
//
// Run via test/run_local_test.sh, or standalone against a server:
//   NODE_PATH=/usr/lib/node_modules BASE_URL=http://127.0.0.1:8770 MODE=full node test/drive_local.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const GLOBAL_MODULES = process.env.NODE_PATH || '/usr/lib/node_modules';
const { chromium } = require(`${GLOBAL_MODULES}/playwright`);

const BASE  = process.env.BASE_URL || 'http://127.0.0.1:8770';
const SHOTS = process.env.SHOT_DIR || '.';
const MODE  = process.env.MODE || 'full';
let pass = 0, fail = 0;
const ok = (name, cond) => { cond ? (pass++, console.log('  ✓ ' + name))
                                  : (fail++, console.log('  ✗ ' + name)); };

const browser = await chromium.launch();
// bypassCSP: axe-core is injected as an inline script (0.56.1); the CSP itself is pinned by det/csp-worker
const page = await browser.newPage({ viewport: { width: 1280, height: 1700 }, bypassCSP: true });
// 0.57.0 — the analytics sections are <details>, closed on a fresh device. The flows below read and
// hit-test what is inside them, so every page load starts with them remembered as open.
await page.addInitScript(() => { try { for (const id of ['sec-shape', 'sec-effort', 'sec-zones', 'sec-ff', 'sec-vol', 'sec-drift', 'sec-track', 'sec-health']) localStorage.setItem('sh-open-' + id, '1'); } catch (e) {} });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));

// 0.56.1 (review U2/U3) — axe-core on every surface the flows already reach. Two hard rules: no
// CRITICAL violation (unlabelled controls, missing names) and no colour-contrast violation, in every
// theme the page offers. Everything else is printed, not enforced.
const AXE_SRC = require('fs').readFileSync(new URL('./vendor/axe.min.js', import.meta.url), 'utf8');
async function axeCheck(label, pg = page) {
  await pg.addScriptTag({ content: AXE_SRC });
  const r = await pg.evaluate(async () => {
    const res = await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'best-practice'] } });
    return res.violations.map(v => ({ id: v.id, impact: v.impact, n: v.nodes.length,
      sample: v.nodes.slice(0, 2).map(x => x.target.join(' ') + ' — ' + (x.any[0] ? x.any[0].message : x.failureSummary || '').slice(0, 120)) }));
  });
  const critical = r.filter(v => v.impact === 'critical');
  const contrast = r.filter(v => v.id === 'color-contrast');
  const other = r.filter(v => v.impact !== 'critical' && v.id !== 'color-contrast');
  ok(`axe ${label}: no critical violation (${critical.length})`, critical.length === 0);
  ok(`axe ${label}: no colour-contrast violation (${contrast.length})`, contrast.length === 0);
  for (const v of [...critical, ...contrast]) console.log(`      ${v.id} ×${v.n}: ${v.sample.join(' | ')}`);
  if (other.length) console.log(`      (axe ${label}: ${other.map(v => v.id + '×' + v.n).join(', ')} — informational)`);
}
async function axeThemes(label) {
  // 0.57.0: one button cycles auto → dark → light → auto; the run starts on auto (light in headless)
  for (const t of ['dark', 'light', 'auto']) {
    await page.locator('#themeBtn').click(); await page.waitForTimeout(300);
    await axeCheck(`${label} · ${t}`);
  }
}

async function runFull() {
  await page.waitForSelector('#tiles .tile', { timeout: 15000 });
  ok(`shape tiles render (${await page.locator('#tiles .tile').count()} ≥ 4)`,
     await page.locator('#tiles .tile').count() >= 4);

  const bodyText = await page.locator('body').innerText();
  ok('VO₂max tile present', /VO/.test(bodyText));
  ok('CTL / Fitness tile present', /Fitness|CTL/.test(bodyText));

  await page.waitForFunction(
    () => { const r = document.querySelector('#recent'); return r && !/Loading/.test(r.innerText); },
    { timeout: 15000 });
  ok('latest-activity tile populated', !/Loading latest activity/.test(bodyText));

  await page.waitForFunction(
    () => { const o = document.querySelector('#objbar'); return o && o.innerText.trim().length > 0; },
    { timeout: 15000 });
  ok('objective pill rendered (Demo Marathon)', /Demo Marathon/.test(await page.locator('#objbar').innerText()));
  ok('plan phase bar rendered', await page.locator('#plan .phaseseg').count() >= 1);

  // first-run card must be HIDDEN on a configured instance (token-or-data + objective)
  ok('first-run card hidden when configured', await page.locator('#firstrun .firstrun').count() === 0);

  await page.screenshot({ path: `${SHOTS}/01-dashboard.png`, fullPage: true });

  // ── Footer chrome + the check-in row's type scale ───────────────────────────
  // The footer names the release that actually served the page: the same string the asset
  // cache-buster rides on, so a screenshot can never claim a version the browser didn't load.
  const foot = await page.evaluate(() => {
    const f = document.querySelector('#footver'), sync = document.querySelector('#foot'),
          att = document.querySelector('footer .ralink');
    const js = [...document.querySelectorAll('script[src]')].map(e => e.src).find(u => /app\.js/.test(u)) || '';
    return { txt: f && f.textContent.trim(), bust: (js.match(/[?&]v=([^&]+)/) || [])[1],
             ordered: !!(f && sync && att)
               && !!(sync.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING)
               && !!(f.compareDocumentPosition(att) & Node.DOCUMENT_POSITION_FOLLOWING) };
  });
  ok(`footer names the running version (${foot.txt})`,
     /^v\d+\.\d+\.\d+$/.test(foot.txt || '') && foot.txt === 'v' + foot.bust);
  ok('the version tag sits between the sync line and the Runalyze attribution', foot.ordered);

  // The stop-the-run control is a label among labels. It used to be set 13px against its 10px
  // ENERGY/SLEEP siblings in the same uppercase mono — bigger, so it read as a different font
  // instead of an emphasis. Computed style, not the stylesheet: this is what the cascade did.
  await page.waitForSelector('.checkin .stop', { timeout: 15000 });
  const type = await page.evaluate(() => {
    const g = e => { const c = getComputedStyle(e);
      return [c.fontSize, c.fontFamily, c.textTransform, c.letterSpacing].join(' | '); };
    const stop = document.querySelector('.checkin .stop');
    const sib = [...document.querySelectorAll('.checkin label')].find(e => !e.classList.contains('stop'));
    return { stop: stop && g(stop), sib: sib && g(sib) };
  });
  ok(`the stop-symptom label is typeset like its siblings (${(type.stop || '').split(' | ')[0]})`,
     !!type.stop && type.stop === type.sib);

  // ── Settings dialog: the #67 closed→open→close→reopen cycle ───────────────
  const dlg = page.locator('#settingsDialog');
  const btn = page.locator('#settingsBtn');
  ok('Settings button present (private view)', await btn.count() === 1);
  ok('dialog hidden on load', !(await dlg.isVisible()));
  ok('closed dialog takes no layout box', (await dlg.boundingBox()) === null);

  await btn.click(); await page.waitForTimeout(200);
  ok('dialog visible after open', await dlg.isVisible());
  ok('settings form loaded', await page.locator('#setform').count() === 1);
  await page.screenshot({ path: `${SHOTS}/02-settings-open.png` });

  await page.locator('#settingsClose').click(); await page.waitForTimeout(200);
  ok('dialog hidden after close', !(await dlg.isVisible()));
  ok('closed-again dialog takes no layout box (#67 guard)', (await dlg.boundingBox()) === null);

  await btn.click(); await page.waitForTimeout(200);
  ok('dialog visible after REOPEN', await dlg.isVisible());
  // §SG — saving ONE key must not wipe the keys being typed beside it. Five credentials live in this
  // block; before this, pasting three Suunto values and hitting the first Save lost the other two.
  // The POST is intercepted so the throwaway instance never actually stores a credential.
  await page.route('**/api/secrets', r => r.request().method() === 'POST'
    ? r.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, secrets: [] }) })
    : r.continue());
  await page.evaluate(() => {
    document.getElementById('sec_suunto_client_id').value = 'CLIENT-ID-DRAFT';
    document.getElementById('sec_suunto_client_secret').value = 'CLIENT-SECRET-DRAFT';
    document.getElementById('sec_suunto_subscription_key').value = 'SUBKEY-DRAFT';
  });
  await page.evaluate(() => saveSecret('suunto_client_id', false));
  await page.waitForTimeout(400);
  const kept = await page.evaluate(() => ({
    saved: document.getElementById('sec_suunto_client_id').value,
    sib1: document.getElementById('sec_suunto_client_secret').value,
    sib2: document.getElementById('sec_suunto_subscription_key').value }));
  await page.unroute('**/api/secrets');
  ok('saving one key clears ONLY that field', kept.saved === '');
  // §SG — the fingerprint: WHICH value is stored, shown without ever showing the value.
  await page.route('**/api/secrets', r => r.request().method() === 'GET'
    ? r.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, secrets: [
        { key: 'runalyze_token', label: 'Runalyze API token', help: 'h', configured: true,
          source: 'saved', fingerprint: 'a1b2c3d4' }] }) })
    : r.continue());
  const fpTxt = await page.evaluate(async () => { await loadSecrets(false);
    const el = document.querySelector('#secretsBox .fp');
    return { fp: el ? el.textContent.trim() : '', box: document.getElementById('secretsBox').innerText }; });
  await page.unroute('**/api/secrets');
  ok(`a configured key shows its fingerprint (${fpTxt.fp})`, fpTxt.fp === 'fp a1b2c3d4');
  ok('the fingerprint block explains how to check it yourself', /sha256sum/.test(fpTxt.box));
  await page.evaluate(() => loadSecrets(false));   // back to the real (tokenless) state
  ok(`the sibling keys keep their unsaved drafts (${kept.sib1}, ${kept.sib2})`,
     kept.sib1 === 'CLIENT-SECRET-DRAFT' && kept.sib2 === 'SUBKEY-DRAFT');

  await page.locator('#settingsClose').click(); await page.waitForTimeout(150);
  ok('tiles intact after dialog cycle', await page.locator('#tiles .tile').count() >= 4);

  // ── Effort-discipline table (§6m, private surface) ────────────────────────
  await page.waitForFunction(
    () => { const e = document.querySelector('#effort'); return e && !/Loading/.test(e.innerText); },
    { timeout: 15000 });
  ok('effort-discipline table rendered', await page.locator('#effort table.efftbl tbody tr').count() >= 1);

  // ── A|B|C priority selector (#62): promote the B objective → re-periodize ──
  const objNow = await page.evaluate(() => fetch('/api/objectives').then(r => r.json()));
  const bObj = objNow.find(o => o.priority === 'B');
  ok('seeded B objective present', !!bObj);
  await page.locator(`.prseg[data-oid="${bObj.id}"][data-pri="A"]`).click();
  await page.waitForSelector(`.prseg[data-oid="${bObj.id}"][data-pri="A"].on`, { timeout: 15000 });
  ok('priority selector promotes B→A and re-renders', true);
  ok('re-plan diff banner shown after re-periodize', await page.locator('#plan .diff').count() >= 1);

  // ── Explicit re-plan via Generate plan → diff banner ──────────────────────
  await page.locator('#planBtn').click();
  await page.waitForSelector('#plan .diff', { timeout: 15000 });
  // innerText reflects CSS text-transform (the .dh banner is uppercased) → match case-insensitively
  ok('Generate plan re-plans (diff banner)', /re-planned/i.test(await page.locator('#plan .diff').innerText()));

  // ── UX-2 — the stop-symptom control halts the plan from the check-in row ──
  await page.waitForSelector('#ci_stop', { timeout: 15000 });   // readiness check-in row rendered
  const stop = page.locator('#ci_stop');
  ok('stop-symptom control rendered in the check-in row', await stop.count() === 1);
  ok('stop control speaks the halt voice',
     /I had to stop/i.test(await page.locator('.checkin .stop').innerText()));   // innerText is CSS-uppercased
  // Posture (validation-review F3): an inactive medical control must not shout danger every day nothing
  // is wrong — muted at rest, danger only once it is checked. Computed colours, not the stylesheet text,
  // so this also proves the :has(:checked) selector actually resolves in a real browser.
  const stopColours = await page.evaluate(() => {
    const el = document.querySelector('.checkin .stop'), box = document.querySelector('#ci_stop');
    const cs = document.documentElement;
    const tok = n => getComputedStyle(cs).getPropertyValue(n).trim();
    const read = () => getComputedStyle(el).color;
    const norm = hex => { const d = document.createElement('span'); d.style.color = hex;
      document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; };
    const rest = read();
    box.checked = true;
    const checked = read();
    box.checked = false;
    return { rest, checked, muted: norm(tok('--muted')), danger: norm(tok('--danger')) };
  });
  ok(`stop control is muted at rest, not an alarm (got ${stopColours.rest}, muted ${stopColours.muted})`,
     stopColours.rest === stopColours.muted);
  ok(`stop control turns danger once checked (got ${stopColours.checked}, danger ${stopColours.danger})`,
     stopColours.checked === stopColours.danger);
  await stop.check();
  await page.locator('#ciBtn').click();
  await page.waitForSelector('#readiness .statuscard.red', { timeout: 15000 });
  const haltCard = await page.locator('#readiness .statuscard').innerText();
  ok('card flips red + halted on a stop-symptom check-in',
     /contact your doctor/i.test(haltCard) && /halted/i.test(haltCard));
  ok('stop control reads back checked (state truth)', await page.locator('#ci_stop').isChecked());

  // ── §PRO14 — BOTH plan endpoints must stamp the running engine ────────────
  // The UI renders the generate RESPONSE directly (refreshPlan(p) skips the GET), so a stamp
  // present only on /api/plan leaves the staleness banner unable to evaluate for the entire
  // post-regeneration render — it reads "current" no matter what. That shipped once; pin it.
  const stamps = await page.evaluate(async () => {
    const g = await fetch('/api/plan').then(r => r.json());
    const p = await fetch('/api/plan/generate', { method: 'POST' }).then(r => r.json());
    return { get: g.engine_running, post: p.engine_running, ver: p.engine_version };
  });
  ok('GET /api/plan stamps the running engine', !!stamps.get);
  ok('POST /api/plan/generate stamps it too (same value)',
     !!stamps.post && stamps.post === stamps.get);
  ok('a plan just generated reads as current, not stale', stamps.ver === stamps.post);

  // ── Plan-drift view renders its five charts (now ≥1 plan version) ─────────
  // FIVE, not four: §FT4 added the prediction ledger (#drift-fin). The old count of four passed
  // vacuously once the ledger shipped — it never named the chart it was missing.
  await page.waitForFunction(
    () => { const d = document.querySelector('#drift'); return d && !/Loading/.test(d.innerText); },
    { timeout: 15000 });
  ok('plan-drift view renders 5 charts (incl. the §FT4 prediction ledger)',
     await page.locator('#drift-dist, #drift-eff, #drift-ctl, #drift-out, #drift-fin').count() === 5);

  await page.screenshot({ path: `${SHOTS}/06-flows.png`, fullPage: true });

  // ── UX-8: the axis survives a phone ──────────────────────────────────────
  // Both trend charts stretch (preserveAspectRatio="none"), so anything lettered INSIDE the SVG is
  // squeezed horizontally by the same factor as the trace: at 390px the old 9px <text> measured
  // about 3px wide — drawn, in the DOM, unreadable. This run's 1280px window is exactly where that
  // lie is invisible (the stretch is ~1:1 there), so drive a real phone width and MEASURE the glyph
  // boxes. A source check can see the markup change; only this can see the type.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => document.querySelector('.mnav-btn[data-goto="fitness"]')?.click());
  await page.waitForTimeout(500);
  const ax = await page.evaluate(() => {
    const vis = [...document.querySelectorAll('#ffchart .axlbl .ax')].filter(e => e.offsetParent);
    const w = vis.map(e => e.getBoundingClientRect().width);
    return { n: vis.length, min: w.length ? Math.min(...w) : 0,
             // BOTH lettered charts, not just this one: #drift holds five more, and a check
             // scoped to #ffchart would have watched the defect survive in all of them
             svgText: document.querySelectorAll('#ffchart svg text, #drift svg text').length,
             host: (document.querySelector('#ffchart') || {}).clientWidth || 0 };
  });
  ok(`fitness chart is on screen at 390px (host ${ax.host}px)`, ax.host > 200 && ax.host < 390);
  ok('both lettered charts keep their axis in HTML, not inside the stretched SVG', ax.svgText === 0);
  ok(`axis labels keep real width on a phone (${ax.n} shown, narrowest ${Math.round(ax.min)}px \u2265 8)`,
     ax.n > 0 && ax.min >= 8);
  // Nothing clips an axis label. This is a NET, not a proof, and worth saying so: measured, it stays
  // green with the thinner removed entirely, because the cards sit inside the wrap's 15px padding and
  // a label can hang off a chart without ever widening the document. It is here to catch a future
  // regression at the page boundary, not to demonstrate anything about the thinner.
  const oflow = await page.evaluate(() =>
    ({ doc: document.documentElement.scrollWidth, win: window.innerWidth }));
  ok(`no horizontal page overflow at 390px (${oflow.doc} ≤ ${oflow.win})`, oflow.doc <= oflow.win);
  await page.screenshot({ path: `${SHOTS}/09-axis-phone.png`, fullPage: true });
  // Real type collides where 3px-wide type merely looked like dust, so the labels thin themselves —
  // but the thinner only has a job on a span long enough to crowd, and the seeded plan is four
  // months. Drive the REAL renderer with a synthetic 30-month series: the only way to watch the one
  // piece of logic this change added actually do its work.
  await page.evaluate(() => {
    document.querySelector('.mnav-btn[data-goto="plan"]')?.click();
    const d = document.querySelector('#sec-drift'); if (d) d.open = true;
  });
  await page.waitForTimeout(500);
  const thin = await page.evaluate(() => {
    const pts = [], d0 = new Date(2024, 0, 1);
    for (let i = 0; i < 900; i++)
      pts.push({ date: new Date(d0.getTime() + i * 86400000).toISOString().slice(0, 10),
                 val: 40 + 20 * Math.sin(i / 40) });
    const host = document.querySelector('#drift-ctl');
    mkChart(host, [{ pts, cls: 'actual', color: '#c60', label: 'ctl' }], { fmt: v => v.toFixed(0) });
    const all = [...host.querySelectorAll('.axlbl .ax.x')];
    const r = all.filter(e => e.offsetParent).map(e => e.getBoundingClientRect())
                 .sort((a, b) => a.left - b.left);
    let overlaps = 0;
    for (let i = 1; i < r.length; i++) if (r[i].left < r[i - 1].right) overlaps++;
    return { built: all.length, shown: r.length, overlaps };
  });
  // `shown > 3` is not decoration: a hidden tab measures zero and every label reports no layout box,
  // which would satisfy "fewer shown than built, none overlapping" with the axis entirely blank.
  ok(`a 30-month axis thins to fit a phone (${thin.shown} of ${thin.built} shown, ${thin.overlaps} overlapping)`,
     thin.built >= 25 && thin.shown > 3 && thin.shown < thin.built && thin.overlaps === 0);
  // …and widening gives them back with NO re-render: the ResizeObserver is what makes a rotation,
  // a tab switch and a <details> toggle all the same event as far as the labels are concerned.
  await page.setViewportSize({ width: 1280, height: 1700 });
  await page.waitForTimeout(400);
  const back = await page.evaluate(() =>
    [...document.querySelectorAll('#drift-ctl .axlbl .ax.x')].filter(e => e.offsetParent).length);
  ok(`widening the window gives every thinned label back, no re-render (${back} of ${thin.built})`,
     back === thin.built);

  // ── UX-9: the keyboard reaches it, and pressing it does something ─────────
  // Six of the plan's controls were divs and spans with click handlers: reachable by mouse, invisible
  // to Tab. The rule the app now holds is a pair — role="button" and tabindex="0" travel together —
  // because either one alone is its own bug. A role with no tab stop announces a button a keyboard
  // user cannot reach; a tab stop with no role is a focus stop that announces nothing. Scan the LIVE
  // DOM rather than the source: three of these sites build their attributes at render time.
  const aria = await page.evaluate(() => {
    const roleNoTab = [...document.querySelectorAll('[role="button"]')]
      .filter(e => e.getAttribute('tabindex') !== '0' && e.tagName !== 'BUTTON')
      .map(e => e.className || e.tagName);
    const tabNoRole = [...document.querySelectorAll('[tabindex="0"]')]
      .filter(e => e.getAttribute('role') !== 'button')
      .map(e => e.className || e.tagName);
    // a tablist whose children are not tabs tells a screen reader about a structure that isn't there
    const orphanTablists = [...document.querySelectorAll('[role="tablist"]')]
      .filter(t => !t.querySelector('[role="tab"]')).length;
    return { roleNoTab, tabNoRole, orphanTablists,
             custom: document.querySelectorAll('[role="button"]:not(button)').length };
  });
  ok(`custom buttons are keyboard-reachable (${aria.custom} found, 0 missing a tab stop)`,
     aria.custom >= 4 && aria.roleNoTab.length === 0);
  ok(`no focus stop without a role${aria.tabNoRole.length ? ' — ' + aria.tabNoRole.join(', ') : ''}`,
     aria.tabNoRole.length === 0);
  ok('no tablist without tabs (the drift control is a group of pressed buttons)',
     aria.orphanTablists === 0);

  // Pressing Enter on a focused week does what clicking it does. This is the whole point: the
  // delegated handler keys off the ARIA, so every element that takes the role takes the keys.
  const kb = await page.evaluate(async () => {
    const segs = [...document.querySelectorAll('.weekseg')];
    const off = segs.find(s => s.getAttribute('aria-pressed') === 'false');
    if (!off) return { ran: false };
    const pk = off.dataset.pk, wk = off.dataset.wk;
    off.focus();
    return { ran: true, focused: document.activeElement === off, pk, wk,
             before: off.getAttribute('aria-pressed') };
  });
  ok('a week segment takes keyboard focus', kb.ran && kb.focused);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(200);
  const after = await page.evaluate(({ pk, wk }) => {
    const seg = document.querySelector(`.weekseg[data-pk="${pk}"][data-wk="${wk}"]`);
    const detail = document.querySelector(`.weekdetail[data-pk="${pk}"][data-wk="${wk}"]`);
    return { pressed: seg && seg.getAttribute('aria-pressed'),
             shown: !!(detail && detail.classList.contains('active')),
             onlyOne: document.querySelectorAll(`.weekstrip[data-pk="${pk}"] .weekseg[aria-pressed="true"]`).length };
  }, { pk: kb.pk, wk: kb.wk });
  ok(`Enter on a focused week opens it (pressed ${kb.before} → ${after.pressed}, detail shown ${after.shown})`,
     after.pressed === 'true' && after.shown && after.onlyOne === 1);

  // The focus ring itself. Two rules used to blank the outline on the only elements the app had made
  // focusable, so a keyboard user could operate them without ever seeing where they were. Tab for
  // real — :focus-visible is about the input modality, and a programmatic .focus() does not prove it.
  await page.keyboard.press('Tab');
  const ring = await page.evaluate(() => {
    const a = document.activeElement;
    const s = getComputedStyle(a);
    return { el: a.className || a.tagName, width: s.outlineWidth, style: s.outlineStyle,
             color: s.outlineColor };
  });
  ok(`keyboard focus draws a visible ring (${ring.el}: ${ring.width} ${ring.style})`,
     parseFloat(ring.width) >= 1 && ring.style !== 'none' && !/transparent|rgba\(0, 0, 0, 0\)/.test(ring.color));

  // "Reduce motion" is an OS setting, and the app has to honour it in style AND in script: a
  // transition is CSS, but scrollIntoView's smoothness is a JS argument no media query can reach.
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const rm = await page.evaluate(() => {
    const probe = document.querySelector('.tile') || document.body;
    return { js: typeof REDUCE === 'function' ? REDUCE() : null,
             css: getComputedStyle(probe).transitionDuration };
  });
  ok('reduce-motion is honoured in script (the smooth scrolls read the media query)', rm.js === true);
  ok(`reduce-motion is honoured in style (transition-duration ${rm.css})`,
     /^0s|0\.00001s|0ms/.test(rm.css) || parseFloat(rm.css) < 0.01);
  await page.emulateMedia({ reducedMotion: 'no-preference' });

  // ── UX-10: the sub-floor controls project a ≥24px transparent hit area ────
  // The visuals did not change; what grew is a ::before the eye can't see. elementFromPoint sees
  // through it to the origin element, so sampling the extended coordinates measures the REAL
  // hittable box — a source grep can only claim one. The range control needs a real series first:
  // 65 readings across 192 days clears its own eligibility rule (>50 points spanning >183 days),
  // driven through the real API and the real renderer.
  await page.evaluate(async () => {
    const key = document.querySelector('#hmarker').options[0].value;
    for (let i = 0; i < 65; i++) {
      const d = new Date(Date.now() - (210 - i * 3) * 86400000);
      const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      await fetch('/api/health', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ marker: key, date: iso, value: 70 + (i % 7) * 0.3, source: 'manual' }) });
    }
    await loadHealth();
  });
  const hits = await page.evaluate(() => {
    // elementFromPoint only sees what's on screen — bring each control into view before sampling
    const probe = (el, pts) => {
      el.scrollIntoView({ behavior: 'auto', block: 'center' });
      const r = el.getBoundingClientRect();
      return pts.map(([dx, dy]) => {
        const t = document.elementFromPoint(r.left + r.width / 2 + dx, r.top + r.height / 2 + dy);
        return t ? (t === el || el.contains(t) ? 'self' : (t.className || t.tagName)) : 'void';
      });
    };
    const out = {};
    const sw = document.querySelector('#themeBtn');                                // 0.57.0: a ≥28px button
    out.swatch = probe(sw, [[0, 0], [0, -11], [0, 11]]);
    const q = [...document.querySelectorAll('#zones .qhint')].find(e => e.offsetParent);   // 15×15
    out.qhint = q ? probe(q, [[0, 0], [0, -11], [0, 11], [-11, 0], [11, 0]]) : null;
    const segs = [...document.querySelectorAll('#plan .prseg')];                   // 18×18
    out.prseg = segs.map(s => probe(s, [[0, 0], [0, -11], [0, 11], [-6, 0], [6, 0]]));
    // every point of each segment's nominal 24px box resolves to A segment of the control: the
    // 3px boundary strips belong to a sibling by paint order (native segmented controls resolve
    // a boundary tap the same way) — what must never happen is a dead spot
    out.prsegVoid = 0;
    segs.forEach(s => {
      s.scrollIntoView({ behavior: 'auto', block: 'center' });
      const r = s.getBoundingClientRect();
      for (const dx of [-11, 0, 11]) for (const dy of [-11, 0, 11]) {
        const t = document.elementFromPoint(r.left + r.width / 2 + dx, r.top + r.height / 2 + dy);
        if (!(t && t.classList && t.classList.contains('prseg'))) out.prsegVoid++;
      }
    });
    const hb = [...document.querySelectorAll('.hrange button')];                   // ~17px tall
    out.hrange = hb.map(b => probe(b, [[0, 0], [0, -11], [0, 11]]));
    return out;
  });
  ok(`theme button hittable across its box (${hits.swatch})`,
     hits.swatch.every(v => v === 'self'));
  ok(`? hint hittable 11px out in all four directions (${hits.qhint})`,
     !!hits.qhint && hits.qhint.every(v => v === 'self'));
  ok(`priority segments hittable 11px above/below + across their heart (${hits.prseg.length} segments)`,
     hits.prseg.length >= 3 && hits.prseg.every(a => a.every(v => v === 'self')));
  ok(`no dead spots anywhere in the priority selector (${hits.prsegVoid} alien hits)`, hits.prsegVoid === 0);
  ok(`health range buttons hittable 11px above/below (${hits.hrange.length} buttons)`,
     hits.hrange.length >= 3 && hits.hrange.every(a => a.every(v => v === 'self')));

  // ── UX-11: the browser chrome follows the theme, and the small repairs ────
  const chrome0 = await page.evaluate(() => document.querySelector('meta[name="theme-color"]').content);
  await page.locator('#themeBtn').click();   // auto → dark (0.57.0: one button cycles the themes)
  const chrome1 = await page.evaluate(() => ({
    meta: document.querySelector('meta[name="theme-color"]').content,
    man: document.querySelector('link[rel="manifest"]').getAttribute('href') }));
  await page.locator('#themeBtn').click(); await page.locator('#themeBtn').click();   // → light → auto: leave the run in Daylight
  ok(`theme-color starts on Daylight (${chrome0})`, chrome0 === '#f4f1ea');
  ok(`theme chrome follows the switch (${chrome1.meta} · ${chrome1.man})`,
     chrome1.meta === '#191a1d' && (chrome1.man || '').includes('theme=dark'));

  // the ratified category palette (0.30.0): each shape tile takes its hue from its data-m metric
  // identity — measured in the live cascade, since a source check can't see a rule that matches nothing
  const hues = await page.evaluate(() =>
    [...document.querySelectorAll('#tiles .tile')].map(t =>
      [t.dataset.m || 'none', getComputedStyle(t).getPropertyValue('--accent').trim()]));
  const distinct = new Set(hues.map(h => h[1]));
  ok(`shape tiles carry metric-identity hues (${hues.map(h => h.join(':')).join(' · ')})`,
     hues.length >= 4 && hues.every(h => h[0] !== 'none') && distinct.size >= 4);

  // the gauge "you:" pill is clamped inside the gauge even at the scale's very ends
  const pill = await page.evaluate(() => {
    const g = document.getElementById('gauge'), you = document.getElementById('gyou');
    if (!g || !you || !you.textContent.trim()) return null;
    const gr = g.getBoundingClientRect();
    const margin = v => { you.style.setProperty('--gx', v); const r = you.getBoundingClientRect();
      return Math.min(r.left - gr.left, gr.right - r.right); };
    const hi = margin('100%'), lo = margin('0%');
    you.style.removeProperty('--gx');
    return { hi, lo };
  });
  ok(`gauge pill never overhangs the gauge (end margins ${pill && Math.round(pill.hi)}/${pill && Math.round(pill.lo)}px)`,
     !!pill && pill.hi >= -1 && pill.lo >= -1);

  // the gauge and the readiness card read ONE ratio: ATL ÷ CTL of the same snapshot row. Runalyze's
  // own ACWR field is computed on another basis and can disagree with the ratio on the same row —
  // so drive the REAL loadShape with a doctored snapshot where they differ (field 1.09, ratio 0.87)
  // and watch which one gets painted. (The readiness side's half of the contract is det/acwr-agreement.)
  await page.route('**/api/shape', r => r.fulfill({ contentType: 'application/json', body: JSON.stringify({
    ok: true, last_sync: null, duplicate_count: 0, duplicates: [], ignored: [], history: [],
    latest: { fitness: 50, fatigue: 43.5, acwr: 1.09 } }) }));
  const pillTxt = await page.evaluate(async () => { await loadShape();
    return document.getElementById('gyou').textContent; });
  await page.unroute('**/api/shape');
  ok(`a snapshot whose field ≠ its ratio paints the RATIO (${pillTxt.trim()})`, /you: 0\.87/.test(pillTxt));
  await page.evaluate(() => loadShape());   // put the real snapshot back for the rest of the run

  // ── 0.32.0 — the durability tracker (§3.3, private-only) ──────────────────
  // On the seed no long run carries decoupling, so the card must reach its HONEST empty state —
  // then drive the REAL renderer with a synthetic populated payload (the gauge-ratio test's
  // pattern) to watch the gauge, the verdict and the per-run bars actually render.
  await page.waitForFunction(
    () => { const b = document.querySelector('#durbody'); return b && !/Loading/.test(b.innerText); },
    { timeout: 15000 });
  ok('durability card renders its honest empty state on the seed',
     /No long runs with decoupling yet/.test(await page.locator('#durbody').innerText()));
  const durPayload = {
    ok: true, min_km: 16, n_long: 12,
    recent_median_raw: 300, recent_median_pct: 3.0, recent_median_km: 20,
    prior_median_raw: 600, trend: 'improving', verdict: 'durable',
    good_below_raw: 500, high_above_raw: 1000,
    series: Array.from({ length: 12 }, (_, i) => ({
      date: new Date(Date.now() - (i * 7 + 1) * 86400000).toISOString().slice(0, 10),
      km: 20 + (i % 3), decoupling_raw: 300 + i * 25, decoupling_pct: 3 + i * 0.25, hr: 146, feel: null })),
  };
  await page.route('**/api/durability', r => r.fulfill({ contentType: 'application/json', body: JSON.stringify(durPayload) }));
  const dur = await page.evaluate(async () => { await loadDurability();
    const pillEl = document.querySelector('#durbody .gyou span');
    return { txt: document.querySelector('#durbody').innerText,
             pill: pillEl ? pillEl.textContent : '',
             pillCls: pillEl ? pillEl.className : '',
             bars: document.querySelectorAll('#durbody .durchart rect').length,
             dashes: document.querySelectorAll('#durbody .durchart .thresh').length,
             title: (document.querySelector('#durbody .durchart rect title') || {}).textContent || '' }; });
  await page.unroute('**/api/durability');
  ok(`durability gauge paints the 6-run median (${dur.pill.trim()} · ${dur.pillCls})`,
     /you: 3\.0%/.test(dur.pill) && dur.pillCls === 'inb');
  ok('durability verdict + trend render', /durable/.test(dur.txt) && /improving/.test(dur.txt));
  ok(`durability tracker draws one bar per long run (${dur.bars}) + both threshold dashes (${dur.dashes})`,
     dur.bars === 12 && dur.dashes === 2);
  ok(`a bar's tooltip carries its distance (${dur.title})`, /km/.test(dur.title));
  await page.evaluate(() => loadDurability());   // put the real (empty) state back

  // the health form's date is prefilled to today (local, not UTC)
  const hd = await page.evaluate(() => {
    const n = new Date();
    return { val: document.getElementById('hdate').value,
             iso: `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}-${String(n.getDate()).padStart(2, '0')}` };
  });
  ok(`health-form date prefilled to today (${hd.val})`, hd.val === hd.iso);

  // A week's Sunday, computed in a zone whose DST starts on a Sunday at 02:00. The old arithmetic
  // (UTC instant + LOCAL setDate + toISOString) lands one day early there once a year, so the plan
  // stops treating the current week as current on its own final Sunday. Northern zones never see it,
  // which is exactly why it survived: nobody who runs this app was ever in one of those zones.
  const nzCtx = await browser.newContext({ timezoneId: 'Pacific/Auckland' });
  await nzCtx.addCookies(await page.context().cookies());   // 0.56.0 — a fresh context has no session; carry it over or the page is /login
  const nzPage = await nzCtx.newPage();
  await nzPage.goto(page.url(), { waitUntil: 'domcontentloaded' });
  const nz = await nzPage.evaluate(() => ({
    end: weekEndIso('2026-09-21'),
    holds: weekHoldsToday({ start: '2026-09-21' }, '2026-09-27'),
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone }));
  await nzCtx.close();
  // 0.57.0 — the desktop section rail and the Today page
  ok('the desktop section rail lists the sections (≥8 links)', await page.locator('nav.rail a').count() >= 8);
  await page.locator('nav.rail a[data-sec="sec-track"]').click(); await page.waitForTimeout(400);
  ok('a rail link opens its (closed) section', await page.$eval('#sec-track', d => d.open));
  ok('a fresh device sees the analytics closed (details, no open by default)',
     await page.evaluate(() => { localStorage.clear(); return [...document.querySelectorAll('details.section')].length; }) >= 8);
  await page.goto(BASE + '/today', { waitUntil: 'networkidle' }); await page.waitForTimeout(800);
  ok('/today shows the readiness card and the check-in', await page.locator('#readiness .statuscard').count() === 1 && await page.locator('#ciBtn').count() === 1);
  ok('/today carries the why-this-session line', await page.locator('.whyline').count() === 1);
  ok('/today shows no plan, no analytics, no latest-run tile', await page.evaluate(() =>
     ['#sec-plan', '#sec-recent', '#sec-shape', '#sec-track'].every(sel => { const e = document.querySelector(sel); return !e || e.offsetParent === null; })));
  ok('/today links back to the dashboard', (await page.locator('#todayLink').getAttribute('href')) === '/');
  await axeCheck('today');
  await page.goto(BASE + '/', { waitUntil: 'networkidle' }); await page.waitForTimeout(500);
  await axeCheck('dashboard'); await axeThemes('dashboard');
  await page.click('#settingsBtn'); await page.waitForTimeout(800); await axeCheck('settings modal');
  await page.keyboard.press('Escape'); await page.waitForTimeout(200);
  await page.goto(BASE + '/runs', { waitUntil: 'networkidle' }); await page.waitForTimeout(800); await axeCheck('run browser');
  await page.goto(BASE + '/', { waitUntil: 'networkidle' }); await page.waitForTimeout(500);
  ok(`a week's end survives a southern DST switch (${nz.tz}: 2026-09-21 → ${nz.end})`,
     nz.end === '2026-09-27' && nz.holds === true);

  // §TR — the track record. The seed has one plan generated today, so NOTHING is scorable: the
  // honest empty state is the check, and it must explain the lead rule rather than just say "none".
  await page.waitForFunction(() => { const e = document.querySelector('#track'); return e && !/Loading/.test(e.innerText); }, { timeout: 15000 });
  const trEmpty = await page.locator('#track').innerText();
  ok(`track record's empty state explains the lead rule (${trEmpty.slice(0, 40).replace(/\s+/g, ' ')}…)`,
     /Nothing has settled yet/.test(trEmpty) && /28 days old/.test(trEmpty));
  await page.route('**/api/track-record', r => r.fulfill({ contentType: 'application/json', body: JSON.stringify({
    ok: true,
    ctl: { n: 9, mae: 2.4, bias: 1.8, close_rate: 0.556, close_within: 2,
           points: [{ week: '2026-06-01', predicted: 40, actual: 43, err: 3, lead_days: 35 }] },
    races: [{ key: 'r1', kind: 'race_t8', label: 'Test Marathon', date: '2026-05-01',
              type: 'marathon', horizon_days: 56, in_band: false, err_pct: 4.2 },
            { key: 'r2', kind: 'race_final', label: 'Test Marathon', date: '2026-05-01',
              type: 'marathon', horizon_days: 3, in_band: true, err_pct: 2.0 }],
    summary: { races_scored: 2, banded: 2, in_band: 1, in_band_rate: 0.5, lead_days: 28, t8_days: 56 } }) }));
  const tr = await page.evaluate(async () => { await loadTrack();
    return { txt: document.querySelector('#track').innerText,
             rows: document.querySelectorAll('#track .trtbl tbody tr').length,
             miss: document.querySelectorAll('#track .trtbl td.bad').length }; });
  await page.unroute('**/api/track-record');
  ok(`track record states the weekly forecast's miss and bias (${tr.txt.split('\n')[0].slice(0, 52)}…)`,
     /9 weekly fitness checkpoints/.test(tr.txt) && /2\.4/.test(tr.txt) && /came out fitter/.test(tr.txt));
  ok('track record states how many bands contained the race', /1 of 2 finish bands contained/.test(tr.txt));
  ok(`track record draws a row per scored prediction and marks the miss (${tr.rows} rows, ${tr.miss} missed)`,
     tr.rows === 2 && tr.miss === 1);
  await page.evaluate(() => loadTrack());   // back to the real (empty) state

  // §HS — the staleness cue. First the quiet state: the seed's markers are all hand-entered, so a
  // banner here would be the cry-wolf failure. Then a routed payload with a DEAD nightly feed.
  ok('no staleness banner when nothing has died (the seed is all manual readings)',
     await page.locator('#health .dqwarn').count() === 0);
  const hPayload = await page.evaluate(async () => {
    const live = await (await fetch('/api/health')).json();
    const day = n => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
    live.series.hrv = [{ date: day(20), value: 44, source: 'runalyze', note: '' },
                       { date: day(8), value: 41, source: 'runalyze', note: '' }];
    live.staleness = { markers: { hrv: { last: day(8), days: 8, watched: true, stale: true } },
                       summary: { markers: ['hrv'], last: day(8), days: 8 } };
    return live;
  });
  await page.route('**/api/health', r => r.fulfill({ contentType: 'application/json', body: JSON.stringify(hPayload) }));
  const hs = await page.evaluate(async () => { await loadHealth();
    const card = [...document.querySelectorAll('#health .hcard')].find(c => c.dataset.k === 'hrv');
    return { banner: (document.querySelector('#health .dqwarn') || {}).innerText || '',
             cards: document.querySelectorAll('#health .hcard').length,
             age: (card && card.querySelector('.stalen')) ? card.querySelector('.stalen').textContent : '' }; });
  await page.unroute('**/api/health');
  ok(`a dead nightly feed raises a banner that NAMES it (${hs.banner.slice(0, 48).replace(/\s+/g, ' ')}…)`,
     /Nightly data has stopped/.test(hs.banner) && /HRV/.test(hs.banner) && /8 days ago/.test(hs.banner));
  ok(`the card that owns the dead feed carries its age (${hs.age})`, /last reading 8 days ago/.test(hs.age));
  ok(`the banner does not replace the cards (${hs.cards} still drawn)`, hs.cards > 1);
  await page.evaluate(() => loadHealth());   // put the real (fresh) state back

  // the OSM tiles take a theme filter. The seed records no GPS, so no real map renders — this
  // measures the cascade on a real element carrying the real class inside the real map host.
  const tileCss = await page.evaluate(() => {
    const host = document.getElementById('actmap');
    if (!host) return null;
    const t = document.createElement('img'); t.className = 'leaflet-tile'; host.appendChild(t);
    const read = () => getComputedStyle(t).filter;
    const light = read();
    document.documentElement.dataset.theme = 'dark'; const dark = read();
    const aurora = dark;   // 0.57.0: two themes
    document.documentElement.dataset.theme = 'light'; t.remove();
    return { light, dark, aurora };
  });
  ok(`map tiles unfiltered on Daylight, filtered on Charcoal/Aurora (${tileCss && tileCss.dark})`,
     !!tileCss && tileCss.light === 'none' && tileCss.dark !== 'none' && tileCss.aurora !== 'none');

  // .profhint's 320px reserve is a desktop arrangement — on a phone the legend wraps below instead
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => document.querySelector('.mnav-btn[data-goto="today"]')?.click());
  await page.waitForTimeout(300);
  const ph = await page.evaluate(() => {
    const h = document.querySelector('#recent .profhint'), pm = document.querySelector('#recent .profmeta');
    if (!h || !pm) return null;
    return { pad: parseFloat(getComputedStyle(h).paddingRight), meta: getComputedStyle(pm).position };
  });
  ok(`.profhint reserve capped on a phone (${ph && ph.pad}px, legend ${ph && ph.meta})`,
     !!ph && ph.pad <= 20 && ph.meta === 'static');
  await page.setViewportSize({ width: 1280, height: 1700 });
  await page.waitForTimeout(300);

  // the offline shell dates its data: the stamp persisted while online is what the failure
  // terminus reads when the service worker serves the shell with the API unreachable
  const stamp = await page.evaluate(() => localStorage.getItem('sh-last-sync'));
  ok(`last-sync stamp persisted for the offline shell (${stamp})`, !!stamp);
  await page.route('**/api/**', r => r.abort());
  await page.route('**/healthz', r => r.abort());
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);   // let every loader settle into its terminus
  const offline = await page.evaluate(() => {
    const els = [...document.querySelectorAll('.tilefail')].map(e => e.innerText);
    return { n: els.length, dated: els.filter(t => /data as of your last sync/.test(t)).length,
             sample: (els[0] || '').slice(0, 90) };
  });
  ok(`offline termini date the data from the stored stamp (${offline.dated}/${offline.n} — "${offline.sample}…")`,
     offline.n >= 5 && offline.dated === offline.n);
  await page.unroute('**/api/**'); await page.unroute('**/healthz');

  // ── First-run step ③: with data but no objective, the card surfaces "Add a race" ──
  const objs = await page.evaluate(() => fetch('/api/objectives').then(r => r.json()));
  for (const o of objs)
    await page.evaluate(id => fetch('/api/objectives/' + id + '/remove', { method: 'POST' }), o.id);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#tiles .tile', { timeout: 15000 });
  await page.waitForFunction(
    () => { const f = document.querySelector('#firstrun .firstrun'); return f && /Add your first race/.test(f.innerText); },
    { timeout: 15000 });
  ok('first-run surfaces step ③ when data present but no objective', true);
  // CTA focuses the objective-name input
  await page.locator('#fr_race').click();
  await page.waitForTimeout(600);
  ok('"Add a race" CTA focuses the objective form',
     await page.evaluate(() => document.activeElement && document.activeElement.id === 'ao_label'));
  await page.screenshot({ path: `${SHOTS}/03-firstrun-addrace.png`, fullPage: true });

  // ── §RB — the explorer wears the dashboard's footer, painted ────────────────
  // One document serves both pages, so the markup was never in doubt (det/footer-chrome pins that).
  // What was: /runs boots only its own loaders, so its footer sat on the shell's "not synced yet"
  // placeholder for as long as the page has existed. Compare the rendered text, both pages.
  // innerText reflects the footer's uppercase text-transform → match case-insensitively
  await page.waitForFunction(() => /synced/i.test(document.querySelector('#foot').innerText),
                             { timeout: 15000 });
  const dashFoot = await page.locator('footer').innerText();
  await page.goto(`${BASE}/runs`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#runscal', { timeout: 15000 });
  await page.waitForFunction(() => /synced/i.test(document.querySelector('#foot').innerText),
                             { timeout: 15000 }).catch(() => {});
  const runsFoot = await page.locator('footer').innerText();
  ok(`the explorer states the sync time too, not the placeholder (${runsFoot.split('\n')[0].slice(0, 46)}…)`,
     /synced \d/i.test(runsFoot));
  ok("the explorer's footer is the dashboard's footer, word for word", runsFoot === dashFoot);
  await page.screenshot({ path: `${SHOTS}/11-runs.png`, fullPage: true });
}

async function runSettled() {
  // §6s — a race ran 5 days ago: the drift scorecard should RECKON it (not project). The drift section
  // is a collapsed <details>, so open it first (its content is hidden from innerText while collapsed).
  await page.evaluate(() => { const d = document.querySelector('#sec-drift'); if (d) d.open = true; });
  // innerText reflects CSS text-transform (.sc-head is uppercased) → match case-insensitively
  await page.waitForFunction(
    () => { const d = document.querySelector('#drift'); return d && /how the race went/i.test(d.innerText); },
    { timeout: 15000 });
  const txt = (await page.locator('#drift').innerText()).replace(/\s+/g, ' ');
  ok('scorecard reckons the race ("How the race went")', /how the race went/i.test(txt));
  ok('result verdict shows goal vs actual', /goal 3:45.*ran 3:52:30/i.test(txt));
  // The GUN clock, not moving time (§33f `_race_seconds`). The fixture encodes the difference on
  // purpose: duration 13920 s = 3:52:00, elapsed_time 13950 s = 3:52:30. A race is timed by the
  // clock on the gantry, so the read must be 3:52:30 / missed by 7:30 — reading 7:00 here would
  // mean the reckoning had quietly gone back to moving time.
  ok('result missed-the-goal read is on the gun clock', /missed by 7:30/i.test(txt));
  ok('fitness reckoning present', /arrived at CTL/i.test(txt));
  await page.screenshot({ path: `${SHOTS}/07-reckoning.png`, fullPage: true });
}

async function runNoplan() {
  // history pulled, never planned, no objective — the genuine first-run step-③ state. The race
  // form is NOT mounted (no plan), so the CTA must generate a plan before it can focus the input.
  await page.waitForSelector('#firstrun .firstrun', { timeout: 15000 });
  ok('first-run card shown (data, no plan, no objective)', await page.locator('#firstrun .firstrun').count() === 1);
  ok('step ③ "Add your first race" is active',
     await page.locator('#firstrun .fr-step.active .fr-label').innerText().then(t => /Add your first race/.test(t)));
  ok('objective form absent before CTA (no plan yet)', await page.locator('#ao_label').count() === 0);
  await page.locator('#fr_race').click();
  await page.waitForSelector('#ao_label', { timeout: 15000 });   // CTA generated the plan → form mounts
  await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'ao_label', { timeout: 5000 });
  ok('CTA generated the plan and focused the race input', true);
  await page.screenshot({ path: `${SHOTS}/05-firstrun-noplan.png`, fullPage: true });
}

async function runEmpty() {
  await page.waitForTimeout(800); await axeCheck('empty first run');
  // fresh tokenless + dataless instance: tiles stay empty, the first-run card drives setup
  await page.waitForSelector('#firstrun .firstrun', { timeout: 15000 });
  ok('first-run card shown on an unconfigured instance', await page.locator('#firstrun .firstrun').count() === 1);
  const frText = await page.locator('#firstrun').innerText();
  ok('step ① "Connect Runalyze" is the active step',
     await page.locator('#firstrun .fr-step.active .fr-label').innerText().then(t => /Connect Runalyze/.test(t)));
  // The token lives in the private SH_SECRETS_DB store and is applied live — so the card must send
  // the runner to Settings, NOT tell them to export an env var and restart. Naming RUNALYZE_TOKEN
  // here would be actively wrong advice now, which is why the absence is asserted too.
  ok('token step sends the runner to Settings', /token in Settings/i.test(frText));
  ok('token step does not tell the runner to set an env var', !/RUNALYZE_TOKEN/.test(frText));
  ok('later steps present but pending', await page.locator('#firstrun .fr-step').count() === 3);
  await page.screenshot({ path: `${SHOTS}/04-firstrun-empty.png`, fullPage: true });
}

async function runBlocked() {
  // UX-3 — with /api/* + /healthz unreachable, every dashboard tile must reach a FAILURE TERMINUS
  // (no parked "Loading…", no uncaught page error), and a retry hook must reload the tile once the
  // API answers again. The page first loads normally (prologue), then the routes are cut and it reloads.
  await page.route('**/api/**', r => r.abort());
  await page.route('**/healthz', r => r.abort());
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);   // let every loader settle into its terminus
  ok('no "Loading…" stranded with the API down', !/Loading…/.test(await page.evaluate(() => document.body.innerText)));
  const termini = await page.locator('.tilefail').count();
  ok('failure termini shown on the data tiles (≥5)', termini >= 5);
  // The dashboard hosts that start life on "Loading…" or render a data tile. Naming them is the point:
  // counting .tf-retry against .tilefail can never fail (tileFail writes the pair as one unit), and an
  // innerText sweep can't see a tile whose section was hidden on the way down — both read green while a
  // tile quietly offers the runner no way back. These two assertions are what actually hold UX-3 up.
  const HOSTS = ['#tiles', '#recent', '#chart', '#ffchart', '#readiness', '#drift', '#effort', '#zones', '#health', '#durbody', '#track'];
  const stranded = await page.evaluate(hs => hs.filter(h => {
    const el = document.querySelector(h); return el && /Loading…/.test(el.innerHTML);
  }), HOSTS);
  ok(`no dashboard tile is left on "Loading…" in the DOM${stranded.length ? ' — stranded: ' + stranded.join(' ') : ''}`,
     stranded.length === 0);
  const noRetry = await page.evaluate(hs => hs.filter(h => {
    const el = document.querySelector(h); return !el || !el.querySelector('.tf-retry');
  }), HOSTS);
  ok(`every dashboard tile offers a retry with the API down${noRetry.length ? ' — no retry in: ' + noRetry.join(' ') : ''}`,
     noRetry.length === 0);
  await page.screenshot({ path: `${SHOTS}/08-blocked.png`, fullPage: true });
  // back online — retry reloads the tile
  await page.unroute('**/api/**'); await page.unroute('**/healthz');
  await page.locator('#tiles .tf-retry').click();
  await page.waitForSelector('#tiles .tile', { timeout: 15000 });
  ok('retry reloads the shape tiles once the API is back', true);
  await page.locator('#readiness .tf-retry').click();
  await page.waitForSelector('#readiness .statuscard', { timeout: 15000 });
  ok('retry reloads the readiness card once the API is back', true);
}

async function runCold() {
  // §FT5 — cold start: one 10k + an objective, NO snapshot. API-level (DOM-free) assertions:
  // the seeded plan exists, carries the cold_start seeds, runs ASSERTIVE (§FORM1, 2026-08-18: the
  // regime is body evidence only — a blank history is not illness; the safe-learning path is
  // MEASUREMENT, the governors ramp from near-zero trailing load), and the §FT3 band is present +
  // wide by design. Page itself must load without errors (checked globally).
  const plan = await page.evaluate(() => fetch('/api/plan').then(r => r.json()));
  ok('cold-start plan generated + persisted', !!(plan && plan.ok));
  const cs = plan && plan.cold_start;
  ok('cold_start seeds surfaced (vo2 from the 10k)', !!(cs && cs.vo2_seed > 25 && cs.race_type === '10k'));
  ok('regime starts assertive (§FORM1: no body evidence ⇒ no gate; measurement ramps the load)',
     ((plan && plan.regime) || {}).mode === 'assertive');
  const band = (((plan || {}).feasibility || {}).finish_time || {}).band;
  ok('prediction is a band, wide by design (cold σ)', !!(band && band.sigma_log >= 0.08));
  await page.screenshot({ path: `${SHOTS}/05-coldstart.png`, fullPage: true });
}

async function runPublic() {
  // UX-11 — the public read-only box never points a visitor at a control it doesn't have, and the
  // theme chrome works there too. Driven against the no-plan fixture, so the plan tile shows its
  // empty state: the copy must not send a stranger hunting for the (removed) Generate button.
  await page.waitForSelector('#plan .empty', { timeout: 15000 });
  const bodyText = await page.locator('body').innerText();
  ok('the public box never points a visitor at generating (no such control exists there)',
     !/generate/i.test(bodyText));
  ok('public plan empty state renders honest copy',
     /No plan published yet/.test(await page.locator('#plan .empty').innerText()));
  ok('no health form on the public box', await page.locator('#hform').count() === 0);
  ok('no durability card on the public box (decoupling is HR-adjacent)',
     await page.locator('#durcard').count() === 0);
  ok('the public box carries the track record (the engine publishes its own scoring)',
     await page.locator('#sec-track').count() === 1
     && await page.evaluate(() => fetch('/api/track-record').then(r => r.status)) === 200);
  ok('the durability endpoint itself is closed to the public box', await page.evaluate(
    () => fetch('/api/durability').then(r => r.status)) === 403);
  const chip = await page.evaluate(() => {
    const e = document.querySelector('#scFresh'); return e ? e.textContent.trim() : ''; });
  ok('public readiness card carries no sync age (the household routine stays private)', chip === '');
  await page.locator('#themeBtn').click();   // auto → dark (0.57.0: one button cycles the themes)
  const tc = await page.evaluate(() => document.querySelector('meta[name="theme-color"]').content);
  ok('theme-color follows the switch on the public box too', tc === '#191a1d');
  ok('0.57.0 — the public box opens on the track record (ledger-first, open)', await page.evaluate(() => {
    const t = document.getElementById('sec-track'), o = document.getElementById('objbar');
    return !!t && !!o && t.open === true && o.nextElementSibling === t; }));
  await page.screenshot({ path: `${SHOTS}/10-public.png`, fullPage: true });
  await axeCheck('public'); await axeThemes('public');
}

async function runPublicFull() {
  // 0.55.1 — the READONLY shell over the FULL fixture (objectives + a plan). The no-plan public flow
  // above cannot see an objectives strip, and two review findings live on it: U1 (the public strip
  // used to render live date pickers and remove buttons that answered 403) and S3 (every run was
  // served by id with its time of day; now only the runs the page points at, date only).
  await page.waitForSelector('#plan .objs .obj', { timeout: 15000 });
  ok('U1 — the public objectives strip carries no date pickers',
     await page.locator('input.odate').count() === 0);
  ok('U1 — the public objectives strip carries no remove buttons',
     await page.locator('#plan button.x').count() === 0);
  ok('U1 — the race dates still show, as text', await page.locator('.odate-ro').count() >= 1);
  const oldest = await page.evaluate(() => fetch('/api/activity/1').then(r => r.status));
  ok('S3 — an old, unreferenced run is not served by id on the public box', oldest === 404);
  const latest = await page.evaluate(() => fetch('/api/activity/latest').then(r => r.json()));
  ok('S3 — the latest run is served without its time of day or title',
     !!(latest && latest.id) && !('date_time' in latest) && !('title' in latest));
  const byId = await page.evaluate(id => fetch('/api/activity/' + id).then(r => r.status), latest.id);
  ok('S3 — the latest run IS served by id (the page points at it)', byId === 200);
  const recentText = await page.locator('#recent').innerText();
  ok('the latest-activity card still renders its date (any locale order)',   // CI's Chromium says "Tue, Sep 1"; a UK locale "Tue 1 Sept"
     /\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b/.test(recentText) && !/Invalid Date/.test(recentText));
  await page.screenshot({ path: `${SHOTS}/11-public-full.png`, fullPage: true });
  await axeCheck('public-full');
}

async function runMap() {
  // 0.55.2 — Leaflet is served from /static/vendor and the CSP names no third-party script host.
  // Driven over the `--demo` fixture (the only seed whose runs carry a route): the map must still
  // render, from this origin, with a clean console — the tooth that catches a CSP that broke the
  // map, a vendor path that 404s, or an SRI pin that no longer matches the bytes on disk.
  const cspErrors = [];
  page.on('console', m => { if (/Content.Security.Policy|Refused to|integrity|leaflet/i.test(m.text())) cspErrors.push(m.text()); });
  await page.waitForSelector('#actmap.leaflet-container', { timeout: 20000 });   // Leaflet classes the container itself
  const src = await page.evaluate(() => [...document.querySelectorAll('script[src]')].map(e => e.getAttribute('src')));
  ok('Leaflet loads from this origin, not a CDN',
     src.some(u => u.startsWith('/static/vendor/leaflet-1.9.4/leaflet.js')) && !src.some(u => /unpkg|cdn/.test(u)));
  ok('the map rendered tiles or a route', await page.locator('#actmap .leaflet-tile, #actmap .leaflet-overlay-pane path').count() > 0);
  ok('window.L is Leaflet 1.9.4', await page.evaluate(() => (window.L && window.L.version) || '') === '1.9.4');
  await page.waitForTimeout(800);
  ok(`no CSP / integrity / Leaflet console errors (${cspErrors.length})`, cspErrors.length === 0);
  await page.screenshot({ path: `${SHOTS}/12-map.png`, fullPage: false });
  await axeCheck('demo');
}

// 0.56.0 §AUTH — the private console locks itself. A fresh instance lands on /setup; a later visit
// on /login. The driver sets and uses one passphrase, so every private flow below runs signed in —
// which is also the assertion that the login pages work in a real browser.
const PASSPHRASE = 'driver passphrase for the flows';
async function ensureLoggedIn() {
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  if (page.url().includes('/setup')) {
    await page.fill('#passphrase', PASSPHRASE); await page.fill('#confirm', PASSPHRASE);
    await page.click('button[type=submit]');
    await page.waitForURL(u => !u.toString().includes('/setup'), { timeout: 15000 });
    ok('§AUTH — a fresh private box asked for a passphrase and opened after setting one', !page.url().includes('/login'));
  } else if (page.url().includes('/login')) {
    await page.fill('#passphrase', PASSPHRASE); await page.click('button[type=submit]');
    await page.waitForURL(u => !u.toString().includes('/login'), { timeout: 15000 });
    ok('§AUTH — the login page signed the driver in', true);
  }
  ok('§AUTH — the console is open after signing in', await page.evaluate(() => fetch('/api/objectives').then(r => r.status)) === 200);
}

try {
  console.log(`\n→ driving ${BASE}  (mode=${MODE})\n`);
  if (!['public', 'publicfull', 'map'].includes(MODE)) await ensureLoggedIn();   // public boxes and the demo have no login
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  if (MODE === 'empty') await runEmpty();
  else if (MODE === 'noplan') await runNoplan();
  else if (MODE === 'settled') await runSettled();
  else if (MODE === 'cold') await runCold();
  else if (MODE === 'blocked') await runBlocked();
  else if (MODE === 'public') await runPublic();
  else if (MODE === 'publicfull') await runPublicFull();
  else if (MODE === 'map') await runMap();
  else await runFull();
  ok('no uncaught page errors', errors.length === 0);
  if (errors.length) errors.forEach(e => console.log('    pageerror: ' + e));
} catch (e) {
  fail++;
  console.log('  ✗ EXCEPTION: ' + e.message);
  await page.screenshot({ path: `${SHOTS}/99-failure.png`, fullPage: true }).catch(() => {});
} finally {
  await browser.close();
}

console.log(`\n${fail ? '✗' : '✓'} ${pass} passed, ${fail} failed  (mode=${MODE}, shots in ${SHOTS})\n`);
process.exit(fail ? 1 : 0);
