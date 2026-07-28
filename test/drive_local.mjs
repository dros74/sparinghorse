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
const page = await browser.newPage({ viewport: { width: 1280, height: 1700 } });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));

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

async function runCold() {
  // §FT5 — cold start: one 10k + an objective, NO snapshot. API-level (DOM-free) assertions:
  // the seeded plan exists, carries the cold_start seeds, runs in caution, and the §FT3 band
  // is present + wide by design. Page itself must load without errors (checked globally).
  const plan = await page.evaluate(() => fetch('/api/plan').then(r => r.json()));
  ok('cold-start plan generated + persisted', !!(plan && plan.ok));
  const cs = plan && plan.cold_start;
  ok('cold_start seeds surfaced (vo2 from the 10k)', !!(cs && cs.vo2_seed > 25 && cs.race_type === '10k'));
  ok('regime starts caution (the safe-learning path)',
     ((plan && plan.regime) || {}).mode === 'caution');
  const band = (((plan || {}).feasibility || {}).finish_time || {}).band;
  ok('prediction is a band, wide by design (cold σ)', !!(band && band.sigma_log >= 0.08));
  await page.screenshot({ path: `${SHOTS}/05-coldstart.png`, fullPage: true });
}

try {
  console.log(`\n→ driving ${BASE}  (mode=${MODE})\n`);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  if (MODE === 'empty') await runEmpty();
  else if (MODE === 'noplan') await runNoplan();
  else if (MODE === 'settled') await runSettled();
  else if (MODE === 'cold') await runCold();
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
