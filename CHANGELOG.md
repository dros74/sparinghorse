# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: the minor version moves for features, the patch version for fixes).

> **Status — the training-plan engine is under heavy, active development.** Its behaviour, levers and
> outputs may change between releases as the model matures. Versions are checkpoints on a moving
> target, not a stable API.

## [0.44.6] - 2026-08-26

### Fixed

- **The cold-start intake seeded its load state from a day that had not finished.** `_ft_cold_start`
  and `plan_seed` fill the **same** `ctl0`/`atl0` argument — `generate_plan` takes one or the other —
  so they owe the same contract, and §PRO20 defines that slot as the state at the **end of
  yesterday**, because `generate_block` rolls the projection from today *inclusive*. A seed that has
  already run through today gets today applied twice. The snapshot path was fixed for that in
  §PRO20; the no-snapshot path was not, leaving two seeds filling one parameter on two different day
  boundaries.

  It carried §PRO5b's phantom rest day with it. `reconstruct_history` pads to its `end`, so a
  cold-start plan generated in the *morning* charged today a full day of decay before it had
  happened. Measured on a constructed month of steady running: **ATL₀ 52.5 before the day's run was
  logged against 70.0 after** — the same athlete, the same day, a 33% swing in the acute-load seed
  decided by what time the plan was generated.

  Nothing is lost by stopping at yesterday: `today_trimp` is computed for **both** seed paths and
  floors today's projection under §PRO20b, so today's own run is applied exactly once — by the roll —
  instead of once by the seed and again by the roll.

  Tooth (g) on `det/plan-seed`, beside the §PRO20 teeth it completes: two databases identical but
  for a run logged today must seed identically, and the seed must equal the settled end-of-yesterday
  reconstruction. Reverted and seen to fail on both.

### Changed

- `test/golden/cold-start.json` is rewritten; the other nine are byte-identical, since it is the only
  scenario that takes the no-snapshot path. **Measured before accepting it**, because the headline
  number moved the alarming way: week 1's reported `peak_acwr` goes 2.635 → 3.334 while its
  prescribed distance does not move at all (10.1 km either way), and the whole 21-week road changes
  by **+1.2 km — 0.15%, rounding**. The ratio rose because the seed now states the acute load
  honestly against a near-zero cold-start chronic base, not because the governor loosened; on a cold
  start the ACWR ceiling cannot bind at CTL ≈ 4 and the §PER1 floors own the verdict, which is why
  the *distance* is unchanged. Every later week reads a marginally **lower** ACWR than before.

## [0.44.5] - 2026-08-26

### Fixed

- **A read the decoder had superseded was still being used to grade a session.** After 0.44.3 an
  interval session showed the two halves of one page disagreeing: the read-back was correct —
  4× 2min — while the Effort Discipline panel a few centimetres away still called it *too hard*.
  Which raises the right question: is that classification a stored verdict from the previous
  read-back, or is it recomputed?

  It is always recomputed — `/api/effort-discipline` has no response cache and recomputes every grade
  on every load. What was stale was its **input**. The monitor reads structures cached-only
  (`fetch=False`) by design: it grades a dozen runs at once, and letting a panel load fan out into a
  dozen stream fetches is exactly the cost that rule exists to prevent. But on a `STRUCT_VERSION`
  mismatch the cached-only path handed the old row back **verbatim** — there is no fetch to heal it —
  so the panel went on grading the session off the three reps v8 had found, at a work pace biased
  fast by the rep v8 had lost. Opening the run is what re-classified and stored v9; the panel had
  been rendered before that write.

  A version stamp is the decoder saying *its semantics moved*. So a caller that turns a structure
  into a **judgement** may no longer read one written under the old ones: the two cached-only reads
  in the effort monitor now pass `stale_ok=False` and fall back to the whole-run read — a verdict it
  can justify, at **low** confidence, with no per-rep claim attached. It stops asserting a reading it
  no longer has.

  **Display deliberately keeps the old behaviour.** A slightly-old decode is descriptive rather than
  a judgement; it self-heals the moment the private box views or re-syncs the run; and the public
  container shares this same database while never being able to fetch, so blanking the read-back
  there would strand it with nothing that could ever restore it. `stale_ok` therefore defaults to
  true, and only the grading path opts out.

  Four teeth added to `det/effort-discipline`, on the same fixture as the per-rep read they qualify:
  the identical cached row one version stamp apart must grade sharply at the current version and not
  at all at the superseded one, the display path must still receive it, and a current-version row
  must survive the same flag — otherwise a fix that simply stopped trusting the cache would pass
  while silently killing the per-rep read for every run. Both halves of the wiring reverted and seen
  to fail.

## [0.44.4] - 2026-08-26

### Fixed

- **The assertive ceiling followed the time of day the plan was regenerated.** `shape_response` — the
  §PRO5 read that compares measured fitness to the plan's own projection and eases the ride when he
  is behind — ended the CTL curve on **today**. `reconstruct_history` pads to its `end`, so a day
  whose training had not been logged yet arrived as a day with *no* training, and the EWMA charged it
  a full day of decay before it had happened.

  On a live database the same curve read **66.6** at the end of the 25th and **63.5** on the
  morning of the 26th — a drop of 3.1, exactly `66.6 × (1 − 2/43)`, pure phantom rest. With a
  2% on-track dead band, that put every *morning* regeneration ~4.5% below projection (ease the ride)
  and every *evening* one on track (full ceiling). Same athlete, same day, same data.

  Realised fitness is now measured at the settled **end of yesterday**. Yesterday is a complete day;
  today never is. This is §PRO20's boundary — the plan's seed was moved there for the same reason,
  and `_ft_state_at` already reads a race day at `day_before` — with the measurement side the one
  place still reading a partial day. `projected` is a week-END value, so both sides now sit on day
  boundaries, and the existing "Sunday strictly before today" filter already places that Sunday on or
  before the day being measured. Today's own load is not lost: §PRO20b still floors the projection
  with it.

  `det/shape-response` gains two teeth for the property that actually matters — the same athlete on
  the same day, seen before and after the day's run is logged, must produce the same `realized` and
  the same ride factor. They are built so the pre-fix code straddles the on-track boundary, and on
  revert they report the defect in its own terms: **0.954 in the morning against 1.0 in the
  evening.** ⚠ The det's existing assertions were all *relative* to whatever `realized` came back, so
  it passed identically before and after; it could never have caught this.

### Changed

- The ten golden plans are rewritten, and the diff is the point: **10 lines, one `realized` field per
  scenario, nothing else.** Every golden scenario carries `projected: null`, so its ride factor was
  1.0 either way and no prescribed number moved. Which also means the goldens never exercised the
  ratio path at all.

## [0.44.3] - 2026-08-26

### Fixed

- **A 4×2min VO₂ session read back as "3× reps + 4× strides", and the effort monitor called it too
  hard.** Two independent defects in the §RD decoder produced one symptom, and the second one turned
  a correctly-executed session into a red mark on the card.

  **A rep is a contiguous stretch of work running, not a single block.** Segmentation cuts on pace
  CONTRAST, so a rep he did not hold perfectly flat splits in two — and each fragment was then
  measured against the ~2min rep floor *alone*. In the reported session rep 1 went out hard and
  settled (45s @4:51 +
  90s @5:21, a 10% shift, one frame over the sustain bar); both halves fell under the floor, both
  were re-labelled *warm-up*, and a rep that had actually been run vanished from the read. Adjacent fast
  blocks carry no recovery between them, so they are one effort by construction: they are now fused
  first and measured after, at the honest time-over-distance pace.

  **The peak inside a rep is not a stride.** The stride pass ran *before* the block grammar and
  counted prominent short peaks anywhere in the run. A rep's own summit is short, rides far over the
  local floor and lifts cadence — every stride gate passes — so the same two minutes were counted on
  both axes at once. It now runs after work detection and skips the frames the reps already own.
  A genuine strides session has no work reps, so nothing is excluded and its count is untouched.

  **Why it mattered beyond the read-back.** The lost rep was the *slowest* — the first one, off the
  warm-up — so dropping it biased the effort monitor's work pace fast: **4:53/km instead of 4:57**,
  against a 5:05 interval target with a ±4% tolerance. The session was graded `too_hard` by
  **0.0001 in log space, about 0.04 s/km**. With all four reps counted it reads `on` — which is what
  the session actually was.

  `STRUCT_VERSION` 8 → 9, so cached reads re-classify lazily on first view.

  **Impact measured, not assumed.** Real streams were re-fetched and v8 A/B'd against v9 on
  identical inputs across 14 activities — every quality session, every strides session and the
  easy-runs-with-strides in the recent corpus. **Exactly one read changes: the broken one.** ⚠ A
  first pass at that sweep reported six changes and every one was the harness — today's pace zones
  applied to June runs, and the wrong floor for §SJ parts. A comparison of this kind needs
  `_zones_asof` and the part-relative `min_s`, or it invents regressions.

  `det/rd-double-count` builds 1Hz streams from a plan of legs with deterministic jitter (a flat
  synthetic pace chart has no peaks at all, so the stride tooth could never have bitten without
  texture) and reproduces that session verbatim: pre-fix it returns 3 reps / 4 strides. Six teeth,
  each half of the fix reverted and seen to fail independently — including the work pace coming back
  as 293 against the session's real 297. Three of the six exist to stop the fix over-reaching: a
  genuine strides session still counts its strides, fusion never crosses a recovery (2×5min with a
  float between stays two reps), and a lone surge still isn't a workout.

  ⚠ The §RD decoder had **no det at all** before this — which is why a change of this size passed
  the suite silently on the first run.

## [0.44.2] - 2026-08-26

### Fixed

- **A day already run kept changing what it had been asked to do.** Reported from a live plan card:
  a Monday prescribed **8.2 km** on Monday read **11.1 km** by Tuesday and **11.5 km** by Wednesday.
  Plan history holds the whole trail — 7.4 → 8.1 → 8.2 → 11.1 → 11.5 — and none of it was a
  re-anchor or a regime change: the week's intent is legitimately recomputed every day against fresh
  CTL/ATL, and the days *already run* were being recomputed along with it.

  The straddling week has to be re-laid on every regeneration — the remainder must be re-governed —
  and the elapsed slice of that re-lay was published as the day's prescription. `generate_block`'s
  own docstring had promised the opposite since §6o ("the elapsed days are kept verbatim"), but the
  slice was cut from a fresh lay of the whole week, so it tracked today's arithmetic.

  Lived days are now read back from **plan history**: for each one, the newest saved plan generated
  *on or before* that date whose road covers it. Such a plan necessarily laid the day as a
  today-or-future session — the elapsed slice is strictly `< today`, so even a plan generated on the
  day itself carries it in the governed remainder — which means a re-laid value can never become the
  source. And once the day passes, no new plan can qualify: the answer is frozen by construction
  rather than by a flag. A day the road covered but prescribed **nothing** for stays empty, so a
  prescribed rest day cannot gain a session after the fact. A day no saved plan reaches
  (fresh or rebuilt DB) falls back to the re-lay, so this degrades to the old behaviour rather than
  to a hole.

  This is the §PRO12 posture applied to the current week. `_laid_sessions` deliberately lets the
  *newest* carrier win, which is correct for fully-elapsed weeks (frozen verbatim by §6f Step E) and
  exactly wrong here — the newest carrier **is** the re-lay.

  Two knock-on corrections came with it. The week-level §PRO9 cap note is now read only off sessions
  *this* generation laid: a pinned day carries the flag from the cap in force when it was written,
  and the card names today's trailing-4wk number — pairing an old decision with today's figure is
  the §6e2 defect. And a pinned session that predates the `km` field is refused rather than
  published, because `km` is summed unguarded into the week header and a partial pin would have
  crashed plan generation outright.

  `det/lived-days-pinned` pins seven teeth: the pin itself, stability across two regenerations on
  different seeds, rest days staying empty, the newest-on-or-before ordering, the graceful fallback,
  and the governed remainder plus ACWR projection being byte-identical with and without pinning.
  ⚠ The seventh exists because the first six were **measured** to be vacuous at the seam — with the
  `pinned_past=` argument deleted from its one call site they all still passed. It plants a
  distinctive distance in a saved plan and drives `generate_plan` → `_split_freeze` →
  `generate_block` end to end, with a control run on wiped history proving the value could only have
  come from history. Both reverts now fail: deleting the wiring, and neutering the pin.

## [0.44.1] - 2026-08-25

### Fixed

- **The map and the read-back went blank, because Runalyze stopped sending streams unasked.**
  `get_activity_details` grew an `include_streams` flag that defaults to **false** — raw per-sample
  series are too large to return by default — and we were still calling it with an activity id
  alone. The reply came back perfectly valid and perfectly empty: `streams_included: false`,
  `returned_points: 0`, every array `[]`, while `streams_meta.available` listed ten recorded
  channels over 2090 samples. Nothing raised, nothing logged. The route map, the pace/HR profile and
  the §RD read-back all quietly stopped existing for any run synced after the change. The read now
  asks for the streams it needs by name (distance, time, HR, cadence, elevation, lat/long); the one
  caller that only wants the summary — the HR-zone distribution — still doesn't pay for them.

- **And the emptiness was being written down as an answer.** Both caches key on a version: a profile
  stamped with the current version is a hit, and a stored structure that already refused is a hit.
  So the blank profile and the "no usable pace/distance streams" verdict would have been served
  *forever*, on rows nothing ever revisits — the run would have stayed mapless and unread long after
  the request was fixed, and the same would happen to any run whose upload is still landing when the
  nightly sync reaches it. An empty read is now only an answer when the source itself says there is
  nothing to read (`streams_meta.available` empty); otherwise the next look tries again. A run that
  genuinely records no streams still settles on the first read and is never re-fetched.

  `det/stream-optin` pins the request, both retry rules and both settle rules. Six mutations, six
  reds — including the two that restore the old cache behaviour, which is the half that would have
  survived the outage.

## [0.44.0] - 2026-08-25

### Fixed

- **The guides were not guiding.** First live guided interval session: the watch offered the guide,
  the summary was right, the transitions fired on time — a beep at ten minutes, a beep at every two
  after — and it never once said what to do. Everything a step carries (the countdown, the pace and
  heart-rate targets, the detail line) lives on the SuuntoPlus display, which is a *screen of its
  own*; the watch does not switch to it for you. So the whole prescription sat on a page nobody was
  looking at, and each step change arrived as an unexplained noise. The Guide format has the answer
  and we simply never emitted it: **`notification`**, a popup shown for about twenty seconds when a
  step starts, on whatever screen is actually in front of you. Every step now fires one — the step's
  name, what the block is, and its targets: `Work 1/4` · `2min at interval - 5:00/km - HR 169-184`.
  It is capped at 13 and 54 characters, so segments are added whole and dropped whole: a target left
  out beats `HR 169-1`. A rep whose detail already opens with its duration does not announce itself
  twice.

- **`@` was being deleted from every instruction sent to the watch.** The watch charset genuinely
  has no `@`, and the sanitiser dropped unsupported characters rather than translating them — so
  `2min @ interval` arrived as `2min  interval`, an instruction with its preposition removed, and
  `short VO₂ touch` as `short VO touch`. Characters that carry meaning are now transliterated:
  `@`→`at`, `₂`→`2`, `≥`→`>=`, `×`→`x`. The map is the complete out-of-charset inventory of the last
  sixty saved plans rather than a guess; `✓`, a decoration whose sentence reads the same without it,
  still goes.

  `det/guide-notify` locks both, over three fixtures — the third exists only to overflow the
  54-character budget, because nothing else in the battery reached it and a drop-whole rule asserted
  by cases that always fit is not asserted at all. Six mutations, six reds.

## [0.43.0] - 2026-08-25

### Added

- **"Rebuild on watch" — for guides you deleted from the wrist.** The nightly push is idempotent: a
  session already on your Suunto account is *updated in place*, keeping its guide id, so a re-plan
  quietly keeps the watch current without ever stacking duplicates. That is the right behaviour and
  it has one blind spot, which only shows up when you clear the guides off the watch yourself.
  Suunto hands the watch the guides it does not already hold, keyed on that same id — and an update
  does not change it. So the push honestly reports every session sent, and exactly one of them
  appears on the wrist: the day that has just entered the seven-day horizon, whose id is new to the
  account and therefore created rather than updated. Six sent, one arrives, and nothing anywhere is
  in error. The Settings window now carries a second button beside the push. **Rebuild** deletes
  each guide in the window and sends it again under a fresh id, so the whole week syncs as new. It
  is deliberately not the default — churning every id nightly would be a delete-and-recreate cycle
  on a watch that is already correct — and a delete that fails falls back to the ordinary update,
  because a stale guide beats no guide. `det/guide-recreate` runs both modes over one fixture and
  pins the difference in both directions, including that neither mode ever touches a guide that
  isn't ours.

## [0.42.0] - 2026-08-25

### Fixed

- **The readiness card stopped reporting things you never told it.** Legs and sleep defaulted to
  "ok" whenever they were absent, on both sides of the app. So a day with no check-in was scored as
  though you had declared fresh legs and decent sleep, and the card printed **"All signals normal"**
  over it. With the HRV feed also dark since mid-August, that was the live state: a green light and
  a confident sentence resting on nothing at all — no check-in, no heart-rate variability, no
  evidence of any kind. Saving a check-in that was only a note wrote the same two invented
  declarations into the database, where they read back as reported signals for the rest of the day.
  Absence is now absence. The card names which signals actually informed it, says plainly when none
  did, and the check-in dropdowns start on "—" instead of pre-selecting an answer you did not give.
  **The verdict itself is unchanged, deliberately.** Green here means "run the plan", and easing
  training because nobody filled in a form is the same mistake pointing the other way — it would
  have fired every day for seven weeks. What changed is the claim, not the call: the light can still
  be green, but it no longer pretends to know why.

### Changed

- **A check-in can now say less than everything.** Leaving legs or sleep unset records nothing for
  them rather than storing "ok", so a note-only check-in is exactly that. Any value you do pick is
  still validated against the same vocabulary.

## [0.41.0] - 2026-08-25

### Added

- **A chart for the thing that actually moves first: speed per heartbeat.** Running the same pace at
  a lower heart rate *is* aerobic fitness arriving, and unlike a training-load model it needs nothing
  but the watch. The readiness section now carries a full-width chart of it — one point per aerobic
  run, with the fitted trend and a hover carrying that run's pace, distance and heart rate. It sits
  above the durability tile and is deliberately more prominent: economy decay over a long run only
  says something once the long runs are long, while efficiency reads on every easy run there is.
  **Temperature is drawn underneath, in its own panel, on its own scale.** Not overlaid on a second
  axis — that is the standard way to make two series look like they explain each other, and this is
  exactly the case where you must not: heat depresses efficiency, and the cool runs are also the
  recent, fitter ones. The two panels share only their time axis, and **nothing subtracts a
  temperature correction**, because how large that correction should be is not settled on one
  runner's data. The card says so in as many words, and carries the correlation as a number.
  Aerobic runs only — average heart rate at or below the top of Z2, using the app's own
  LTHR-anchored zones. Mixing an all-out 5k with an easy hour makes the trend read the training mix
  rather than the athlete: on the calibration corpus that alone moved the fit from r 0.40 to r 0.78.
  Private-only, like durability — it is heart-rate data. Measure-first: shown and trended, governing
  nothing.

### Fixed

- **Chart marks stopped turning into ellipses on a phone.** The trend charts here are drawn stretched
  to whatever width they are given, which is fine for lines and bars and quietly wrong for a circle:
  the same markup that drew round points on a laptop drew tall ovals at 390px. The new chart's points
  are drawn so their size is fixed in real pixels and the stretch cannot deform them. This is the
  same defect as the squeezed axis labels fixed in 0.29.0, one layer down — the stretch scales marks
  as well as lettering.

## [0.40.1] - 2026-08-25

### Fixed

- **The readiness card stopped telling you to take a hard session easy.** The daily readiness
  narrator was given your HRV, your legs, your sleep and your note — and nothing at all about the
  session it was writing above. So on an interval day it wrote what it assumed: *"run as planned at
  an easy, conversational effort"*, printed directly over a card reading INTERVAL SESSION. The
  wording blended two different verdicts, because the instructions it works from used "easy" as the
  word for *hold back* while telling it green means *run as planned*. It now knows what today's
  session is and what pace it was prescribed at, and its instructions say plainly that softening a
  hard session is a change to the plan it is not allowed to make.
  This mattered more than a clumsy sentence: the rule for the whole AI layer is that it may narrate
  and may raise caution, never prescribe — and telling a runner to take a prescribed interval
  session conversationally is prescribing, in prose, past a green light that was itself perfectly
  correct. The check that guards the verdict never read the sentence. So the fix is not only better
  instructions: on a green light with a hard session prescribed, a "take it easy" narration is now
  dropped for the engine's own wording, deterministically. It stays out of the way on easy days,
  where the word is right, and on amber and red days, where the narrator being cautious is the
  entire point.

## [0.40.0] - 2026-08-25

### Added

- **The long run can finally become a marathon long run.** The plan's ceiling on how much of a week
  may sit in one run is no longer a single number for the whole road. Base keeps the published
  doctrine (a long run is a quarter to a third of the week) — that is the right ceiling while
  weekly volume is still being built. The marathon-specific block is allowed more, so the road now
  arrives at the start line by way of a 30 km and a 32 km run instead of topping out at 24 km. The
  block's total distance does not change; the same training is redistributed. **The two big runs are
  delivered by the existing +10 %-per-week ladder, not by the new ceiling** — a share ceiling only
  ever permits, and the check that holds this says so: with the ladder in force, the lifted ceiling
  may not add a single metre. Every other brake is untouched.
  ⚠ The cost is time on feet, and it is deliberate: 32 km at an easy pace is about 3h36, past the
  2:30–3:00 duration cross-check the doctrine also carries, and the injury evidence behind the
  ladder is specifically about the longest run. Recorded as a deliberate operator decision, with
  the simulated ladder alongside it, in the calibration inventory.
- **The scorecard says which half was wrong.** The engine has been grading its own four-week fitness
  forecasts since 0.37.0, and every settled forecast so far came in low. That single number fused
  two very different failures: a model whose physics is wrong, and a plan the runner did not run.
  Each scored week now also recovers what its plan asked for that week and what was actually run, so
  the two can be told apart — and on the first four weeks they separate cleanly, which is a finding
  about the prescription, not about the projector. Derived when the scorecard is read, so the ledger
  of scored forecasts is still written once and never revised. The public showcase gets the ratio;
  the raw weekly distances stay on the private box.

### Changed

- **A week's role is a field, not a sentence.** Whether a week is a down week, a taper week or a
  race week used to be decided by reading the first word of the sentence written for the athlete —
  seven separate governors re-parsed that copy, so rewording a caption could silently move the
  recovery anchor, the taper's starting point and the progression gates all at once. Each week now
  carries its role and its phase as real fields, published in the plan, with the sentence rendered
  for humans alongside. Nothing about any plan changes; a check now fails if a pure copy edit ever
  moves a training decision again.
- **A week's stated target is the target it was actually set.** The number the plan published as
  "what this week asked for" was the fixed template the block started from, not the volume the week
  was really governed to. While the engine is riding its ceiling — which it does whenever the
  evidence allows — those two diverged badly: through the base phase the stated bar read between a
  half and a third of the sheet the athlete was actually handed, and then snapped back into
  agreement at the next phase boundary. It now publishes the bar the week was governed to, so
  adherence is measured against what was really asked. The cautious path is unchanged.

## [0.39.0] - 2026-08-23

### Changed

- **The plan engine is its own file.** Everything a training plan is computed from — the engine, the
  feasibility model, the fitness/fatigue projector, the pace maths — has left the application file
  and lives in `sh_engine.py`. It imports nothing from the app: the arrow points one way, so the
  engine can be read and tested on its own, and a change to the web layer cannot alter a plan. The
  application file drops from 12,815 lines to 8,244 and is what its name says — routes, syncing,
  storage, the scheduler. Nothing about the app behaves differently, and the proof that it doesn't
  was part of the move: the ten golden plans regenerate byte-identical.
- **The self-test knows which file it is looking at.** The battery's clock pin, its constant
  inventory and its source scans all walk a list of the app's modules rather than assuming one file,
  so the next time something moves out, the checks move with it instead of quietly covering less.
  Two new checks hold the split itself: one fails if the engine ever imports the app back, if a
  re-exported name drifts, or if a module is missing from those lists; the other fails if a module
  the app imports is missing from the container image — a mistake that shows up nowhere except in
  the container.

## [0.38.0] - 2026-08-23

### Added

- **The footer says which version you are looking at.** Between the sync line and the Runalyze
  attribution, the page now names the release that served it — the same string the stylesheet and
  script are cache-busted with, so a screenshot can never claim a version the browser did not
  actually load. It is served by the page, not typed by hand: it cannot be forgotten at release time.

### Fixed

- **The stop-the-run control no longer out-sizes the row it sits in.** "I had to stop / chest
  symptom" was set two-and-a-bit points larger than the ENERGY and SLEEP labels beside it, in the
  same uppercase mono — big enough to read as a different typeface rather than as emphasis. It takes
  the sibling labels' type now. The emphasis that control needs is the one it already had: it turns
  red when it is ticked.
- **The run browser's footer had never learned the sync time.** The explorer and the dashboard are
  one document and have always shared a footer, but only the dashboard's loaders ever painted it, so
  `/runs` sat on the shell's placeholder — "not synced yet" — however recently you had synced. Both
  pages now paint that line through one writer, and the two footers are compared, word for word, by
  the browser suite.

## [0.37.1] - 2026-08-23

### Fixed

- **A week stopped being "this week" a day early — in the southern hemisphere, once a year.** The
  code that works out which Sunday a training week ends on mixed a UTC timestamp with local-time
  date arithmetic. In New Zealand and eastern Australia, where daylight saving begins on a Sunday
  at 2 a.m., that lands the end one day early for the week containing the switch — so on that
  week's final Sunday the plan no longer recognised it as the current week. It has never affected
  anyone in Europe or the Americas, which is exactly why it survived: the arithmetic is only wrong
  in timezones nobody has run this app from. Now computed entirely in UTC, and checked by a browser
  test that loads the page in Auckland.

## [0.37.0] - 2026-08-23

### Added

- **A track record: the engine now keeps score on itself.** Every night the app forecasts a weekly
  fitness trajectory and a finish time with a band — and every night the new plan replaced the old
  one, so those forecasts were overwritten before anyone could check them. A model that
  continuously re-forecasts and never scores itself cannot be wrong, which is another way of saying
  it cannot be trusted. Each prediction is now scored the first time its outcome is knowable,
  written down, and **never rewritten**: weekly fitness checkpoints (with the *bias* reported apart
  from the scatter, because a persistent sign is a model error while noise is just weather) and
  race bands, scored both at the final call and at eight weeks out. Only forecasts made at least
  four weeks before the outcome are scored at all — grading the plan regenerated the night before
  would be grading hindsight. A new *Track record* panel shows it, and the public box carries it
  too, with one deliberate difference: it publishes whether a band contained the race, never the
  finish time. Publishing a prediction beside its error would hand the result back, and results
  stay private.

## [0.36.1] - 2026-08-23

### Documentation

- **Every number in the engine now says where it came from.** The manual gains a short note on what
  a self-hoster who is not the author inherits: most of the engine's numbers are the sport's
  published consensus and travel to any runner, but about twenty were fitted to one athlete's own
  history. The governors among those fail safe — they are ceilings that only ever reduce load, so
  being wrong for you makes a plan too cautious rather than too aggressive — while the finish-time
  projection does not, which is why it always ships with an uncertainty band. Treat the band, not
  the time, as the output. The full constant-by-constant inventory lives in the engine-science notes
  and is now checked by the test suite: a new constant that nobody has classified fails the build.

## [0.36.0] - 2026-08-23

### Changed

- **The plan explanation is computed once per plan, not once per click.** Asking "why this plan?"
  twice about the same plan used to mean two AI calls, two waits and two bills for an answer that
  could not have changed. It is now remembered against the plan it describes, so the second look is
  instant, and it is forgotten the moment the plan is regenerated, the change being explained is
  different, or the athlete context that shapes the wording is edited. A failed call is never
  remembered — one provider hiccup must not become the plan's permanent explanation.

### Added

- **The AI boundary is now written down.** ENGINE_SCIENCE gains an architecture decision record
  stating, and then evidencing, that the language model parses, narrates and listens but never
  prescribes and never clamps: a table of all five places it is used and what each is allowed to
  return, the deterministic tests that hold each safety limit, and the plainest evidence of all —
  the engine plans, governs and projects identically with no API key at all. It also records what
  would have to be true before a model-generated number could ever become part of a plan.

## [0.35.0] - 2026-08-23

### Added

- **Keys now show a fingerprint.** A key box that never shows you the key cannot tell you *which*
  key it is holding — "configured" reads exactly the same before and after you rotate a token, so a
  save that silently failed and a save that worked look identical. Each stored credential now
  displays eight hex characters of its SHA-256 beside the status badge. It is one-way, so nothing
  about the key is revealed, and you can compute the same digest yourself (the MANUAL gives the
  one-liner) to confirm the box holds what you think it does.

### Fixed

- **Saving one key no longer wipes the ones you were typing.** The keys block re-renders itself
  after a save, and the re-render used to blank every field — so pasting the three Suunto
  credentials and pressing the first Save lost the other two, with nothing to say why. Unsaved text
  in the other fields is now preserved; only the field you just saved is cleared.

## [0.34.0] - 2026-08-23

### Added

- **The health page now tells you when a feed has died.** Sleep, HRV and overnight heart rate arrive
  from the watch every night — and when that stopped on 15 August, nothing on the page said so. The
  charts did not look broken; they simply ended, with the last point sitting there reading like
  today's, and the gap was found by eye more than a week later. A banner above the markers now names
  the feeds that have gone quiet and the date they stopped, and the card that owns a dead feed
  carries its age. It stays silent for anything that is not actually a broken pipe: a blood marker
  from last winter is not stale, and a weight you typed in yourself last month is you telling us
  something, not a sync to worry about. A cue that fires on ordinary data is a cue you learn to
  ignore, which is how the real outage stayed invisible.

## [0.33.0] - 2026-08-23

### Changed

- **Two numbers the app collected but never used are no longer collected.** Every sync wrote
  Runalyze's *monotony* and *training strain* into the daily shape snapshot, and nothing in the
  engine — no governor, no view, no page — had ever read either one. Both are computed from TRIMP,
  the training-load axis this project's own science notes identify as the wrong one for predicting
  injury (running injuries are biomechanical; the evidence for the acute:chronic family is thin),
  so the honest answer was to stop carrying them rather than to find them a job: a number already
  sitting in the database is the one a future change reaches for precisely because it is there.
  The columns stay in the database so the readings already banked are not deleted, and the verbatim
  upstream response is still kept, as it always was, privately. A new check fails the build if any
  code writes, reads or names either field again — the point is not the removal, it is that the
  re-introduction now has to be a decision.
- **The signals that were built but not wired now have written answers.** ENGINE_SCIENCE gains a
  reckoning section covering all three: long-run decoupling stays *display-only* (the durability
  tracker measures and shows, and the bar it would have to clear before it governs anything is
  written down in advance), monotony/strain are dropped as above, and the psychological axis —
  feel, readiness notes, free-text status — is confirmed as a one-way gate. It can ease a week or
  halt it; it can never add load. That asymmetry is deliberate, and the reasons are on the record.

## [0.32.1] - 2026-08-23

### Fixed

- **The readiness row breathes again.** Three cards abreast made the durability tracker's debut row
  cramped. The verdict card now takes the full width of the section — it is the one thing that
  matters today — and the acute:chronic and durability trackers share the row below, half each.
  On the public box, where durability stays private, the acute:chronic card takes the whole second
  row instead of sitting beside an absence.

## [0.32.0] - 2026-08-23

### Added

- **The durability tracker.** Long-run aerobic decoupling — how much your pace:HR drifts from the
  first half of a long run to the second, the proxy for how well your running economy holds up over
  distance — has been measured and banked since July, but only reachable as an API payload. It now
  has a home: a third card in *Today's readiness*, beside the verdict card and the acute:chronic
  tracker. A gauge shows your median decoupling over the last six long runs (durable under 5%, high
  fade past 10%), a verdict line names the state and the prior median it moved from, and a tracker
  chart draws one bar per long run, coloured by its fade band, with each bar's distance in its
  tooltip — because decoupling drifts with distance, a chart that hid duration would lie. The trend
  word (improving / steady / declining) voids itself when your recent long-run distances change mix,
  exactly as the engine has always read it. This is **measure-first**: the card tracks and trends
  the signal and governs nothing — it earns a governing role only if the corpus shows it predicts
  race fade. It is also **private-only** (decoupling is HR-adjacent): the card and its new
  `GET /api/durability` endpoint are absent from the public box.

## [0.31.2] - 2026-08-22

### Fixed

- **The public plan gets its detail back.** Tightening the public view in 0.31.0 also removed seven
  small fields it should have kept — the "long-run held" chip, the marker that turns a completed
  week's rest day into "optional", the flags explaining why a week has no distinct long run, and
  the number behind the "measured fitness is X% of projection" line, which without it read 0%.
  None of them say anything about the athlete; they describe the plan. They are back, and there is
  now a check that every field the plan engine produces has been deliberately classified as either
  published or withheld — the list built from test data alone had missed the ones that only appear
  when the training-load governors act.

## [0.31.1] - 2026-08-22

### Fixed

- **The watch stops collecting guides it should have thrown away.** A planned session's guide is
  identified by its date and its kind, but a re-plan can change the kind on a day already sent — an
  easy-only check-in turns a tempo into an easy run, and the load ceiling turns a clipped long run
  into a shakeout. The new guide was uploaded and the superseded one simply stayed on the watch,
  because nothing in the app could delete a guide at all. The nightly push now removes its own
  guides once they are superseded, once their day has passed, or once the day no longer carries a
  session, so the watch shows one guide per planned day and nothing behind today. It never touches
  a guide it didn't write, and a day whose upload failed keeps the guide it already had.
- **A duplicated internal helper.** A time-formatting function was defined twice; the first copy had
  been unreachable since it was written. Removed, with a check added so a shadowed definition can't
  slip in again unnoticed — in a single-file application it produces no error of any kind.

## [0.31.0] - 2026-08-22

### Changed

- **The public site now names what it publishes.** Every public endpoint used to serve the private
  payload and then remove the fields someone remembered were personal. That is only ever as good as
  the last person to think about it, and two things had slipped through: the shape endpoint was
  serving the entire upstream snapshot record — including the HRV baseline and normal range — and
  the same endpoint published the time of the household's nightly sync, which the health endpoint
  deliberately withholds and which the page footer was printing. Both are gone. The rule is
  inverted: a field reaches the public view because it is listed, not because nobody removed it, so
  anything added in future is private until someone publishes it deliberately.
- **Settings and keys are applied as one unit.** The values a self-hoster can set in the Settings
  window were applied one at a time, which left a brief window where a page could render with half
  the old configuration and half the new. They are now resolved first and published together.

### Fixed

- **Changing the Runalyze token in the Settings window now takes effect immediately.** The HTTP
  session kept the key it was built with, so data pulls carried on authenticating with the replaced
  token until the app was restarted — while the other half of the client picked the new one up at
  once. Both now follow the change.
- **Two simultaneous reconnections to Runalyze no longer trip over each other.** A page load and
  the nightly sync could each start a new session at the same moment and the wrong one could win,
  leaving the app holding a connection the server had already dropped.

## [0.30.1] - 2026-08-22

### Fixed

- **The readiness card no longer depends on a payload it doesn't need.** The HRV signal read the
  latest shape snapshot's raw payload without checking there was one. Every row the sync writes
  carries it, so the hole was latent — but any other writer (a restored backup, an interrupted
  write, a test fixture) leaves it empty, and the whole readiness card would then fail with it,
  over a nice-to-have. A missing or unreadable payload now reads as "no HRV signal", the card
  still renders, and the stop-symptom safety floor still halts.
- **Two self-test checks that could only pass on one kind of database.** The taper's race-pace
  check and the week-card truth check both read whatever plan the running instance happened to
  hold. On an instance with no upcoming race — a demo instance after its race day, or any runner
  between goals — there is no taper and no race week to inspect, so both checks reported a failure
  that was about the data, not the code. They now build the road they need (a marathon anchor and
  a non-marathon one) and assert on that, keeping the live instance as an extra check when it has
  one to give. Net effect: the suite is green on every seeded variant, and both checks now cover
  ground they never reached before — the non-marathon taper pace had no live road at all, and the
  race-week card rule is now checked on every instance rather than only on one mid-block.

## [0.30.0] - 2026-08-22

### Changed

- **The category colours are back.** The square-polychrome palette — the shape tiles tinted by
  metric, the plan phases in their colour families, the green volume bars, the blue fatigue trace —
  came out in 0.29.0 as an undocumented trial, and one day of the flat single-hue dashboard settled
  the question: too dull. It is back, and this time it is written into the design document instead
  of diverging from it. One improvement over the trial: the shape tiles take their hue from what
  they measure (a metric identity) rather than where they sit, so adding or reordering a tile can
  no longer repaint the row.

### Fixed

- **The two acute:chronic numbers on the dashboard agree.** The readiness card divided the synced
  snapshot's fatigue by its fitness, while the Acute:chronic gauge painted the ratio field the sync
  provides — which is computed on a different basis and can sit visibly apart on the same row
  (0.87 against 1.09). The gauge now divides the same row the same way: one number, as the gauge's
  own caption always claimed.

## [0.29.0] - 2026-08-22

### Changed

- **The front end is code now.** The dashboard's markup, style and script — some 270 KB — used to
  live inside the Python file as one giant string, where a syntax error in 2,600 lines of
  JavaScript could only be found by opening the page and noticing nothing worked. It is three real
  files (`static/index.html`, `app.css`, `app.js`), served versioned and checked in CI like
  everything else. Nothing about the page should look different — and the proof that it doesn't
  (byte-identical screenshots in all three themes) was part of the move.
- **The app speaks in its own voice.** Every remaining browser `alert()`/`confirm()` is gone;
  destructive questions and errors arrive in the app's own dialog, which explains the consequences
  before you confirm.
- **Colours belong to the theme.** The heart-rate zones were the last hard-coded hex values and are
  per-theme tokens now. An activity's pace trace no longer ramps red→green — the one pair a
  colourblind reader can't separate — but deepens a single hue, fastest of the run strongest, as
  before. The square-polychrome tile overlay (a trial that never made it into the design document)
  is removed; the code agrees with the document again.
- **The keyboard reaches everything the mouse can.** The phase bar, week strip, completed-session
  lines, per-run links, calendar days and metric chips take focus and press with Enter/Space, with
  a visible focus ring, and "reduce motion" is honoured by the animations and the scrolling alike.
  Segmented controls that announced themselves to screen readers as tab lists with no tabs now
  describe themselves truthfully.
- **Chart axes survive a phone.** The trend charts stretch to fill their width, and the axis labels
  used to stretch with them — about 3px wide on a 390px screen: drawn, present, unreadable. The
  labels are HTML now, they thin themselves rather than collide, and a resize, rotation or tab
  switch re-fits them without a re-render.
- **Small controls grew invisible padding.** The theme swatches, the "?" help bubbles, the A|B|C
  priority segments and the health-range buttons all sat under the 24px touch floor; their hit
  areas clear it now, with no change to how anything looks.
- **The browser chrome follows the theme.** The tab and PWA title-bar colour, and the installed
  window's splash, match Daylight, Charcoal or Aurora — whichever is active.

### Added

- **A freshness chip on the readiness card** (private view): "synced 3 h ago", amber past 26 h —
  the same staleness threshold the nightly sync's own catch-up uses, so the screen and the
  scheduler can't disagree. The public card stays silent on timing: a visitor has no business
  learning the household's sync routine.
- **An honest offline shell.** Open the installed app with no connection and each tile says it is
  offline and dates its data from the last sync the app remembers — never a parked "Loading…".

### Fixed

- **The public box no longer points visitors at buttons it doesn't have.** The empty plan tile, the
  "this plan is stale" banners, the readiness card's no-plan line and the drift view's empty state
  all used to suggest hitting Generate plan — a control that exists only on the private console.
- **Small repairs.** The gauge "you:" pill is clamped inside the gauge at the scale's ends; the
  health form's date is prefilled to today; the activity profile hint keeps its text on a phone
  rather than reserving legend space it no longer needs; the route map's tiles dim to match the
  dark themes; and the stretched charts draw their strokes at constant width, as the design
  document specifies.

## [0.28.1] - 2026-08-22

### Fixed

- **The browser self-check's readiness probe was looking in the wrong place.** It asked the server for
  today's readiness and then read the verdict from a field the answer has never contained, so it
  reported a failure with an empty result — and had done since the day it was written in June. Nothing
  was ever wrong with the readiness verdict itself; only the check was broken. It reads the right
  field now, and a new test pins both halves — that the server puts the verdict where it says it does
  (on the private view and the public one), and that the page's probe reads it from there. A check
  that only runs when someone opens a page can rot for months without a sound.

## [0.28.0] - 2026-08-21

### Added

- **The engine's answers are frozen as golden snapshots.** Nine synthetic training scenarios — a cold
  start, a mid-block, a maintenance stretch, the week after a race, both regime postures, a short
  rebuild, a taper, a week away — are rebuilt at a fixed clock and compared byte-for-byte against
  files committed to the repository. A refactor now has to prove it changed nothing; a deliberate
  change to what the engine prescribes has to update those files in the same commit, where the diff
  is readable line by line. The fixtures are hermetic: they were checked under wall clocks years apart
  and produce the same plan, so they cannot quietly rot as time passes.
- **Continuous integration.** Every push runs the full battery on a freshly seeded synthetic database,
  checks the goldens still match, and scans for personal data — plus the browser flows in six modes.
  The gate used to exist only on one laptop.

### Fixed

- **A plan is computed for the day it is for, not the day the server happens to be running.** The
  engine takes the date it should plan for, and three readings ignored it and asked the clock
  instead: how fit you have actually become, and — on a first-ever plan — which past race still
  describes you and what starting fitness to assume from it. In everyday use the two dates are the
  same, so nothing about your plan changes here. It mattered for testing: a plan built for a fixed
  date quietly answered differently tomorrow, which is exactly the kind of drift the golden snapshots
  exist to catch, and it was hiding one layer below them. A new check rebuilds every scenario under
  clocks eight months apart and requires the same plan.

### Changed

- **The self-test battery no longer runs inside the app.** It lives in its own file and runs as its
  own process against a snapshot of the database. Two things follow. A mistake in a test can no longer
  take the web app and the nightly sync down with it — it breaks the self-test and nothing else. And
  the app no longer goes offline while testing itself: it used to answer "a self-test is running,
  retry in a minute" to every request for about forty seconds, which was the only downtime it ever
  had, and it inflicted it on itself. Starting a battery now answers immediately and the page follows
  its progress; the site stays fully usable throughout.
- **The scenarios that call the language model are opt-in.** The default battery is free and
  deterministic — no key needed, no dependence on a model's mood. Set `SH_SELFTEST_LLM=1` on the
  machine where the key lives to exercise those paths for real.

## [0.27.2] - 2026-08-21

### Fixed

- **The readiness card is readable in every theme and state.** White text on the amber and red
  gradients measured as low as 1.8:1 at the big verdict — washed out somewhere in all three themes.
  Daylight is back on the design spec's deeper greens and maps amber/red to the theme's own
  warn/danger colours; Charcoal darkens its neon stops; Aurora keeps the neon and switches to dark
  ink. A new self-test computes the WCAG contrast ratios straight from the stylesheet, so a future
  recolour can't silently regress (verdict ≥3:1, card footer ≥4.5:1, every theme × every state).
- **The stop-symptom halt has an explicit control.** The check-in accepted a stop-the-run symptom
  flag all along, but the only way to raise it was a matching phrase in the free-text note. A quiet
  "I had to stop / chest symptom" checkbox now posts the flag directly — the card flips to red +
  halted and the plan rests — while the note's phrase catch stays on as a backstop. It is quiet on
  purpose: it sits in the same grey as the energy and sleep labels beside it, and only turns the
  warning colour once it is actually ticked, so it isn't raising an alarm on every ordinary day.
- **Tiles fail honestly instead of spinning forever.** A dashboard tile whose fetch failed used to
  sit on "Loading…" for the life of the page. Every loader now ends somewhere: data, an honest empty
  state, or a failure notice that names a dropped connection ("offline — data as of your last sync")
  versus a server error ("unavailable") and offers a working retry. That now includes the zones and
  effort-discipline panels: the effort section used to hide itself outright when its read failed, so a
  server problem looked exactly like a feature you didn't have. The check-in button shows busy and
  couldn't-save states instead of silently doing nothing. The plan-drift view is also no longer loaded
  twice on every page open — the heaviest read on the page was being fetched two times over.
- **A missed nightly sync can't hide.** A container restart across the nightly minute used to skip
  the night without a trace. The scheduler now records each run's outcome, runs a catch-up pass at
  boot when the last success is more than 26 hours old, and reports through `/healthz` — timestamps
  on the private console, booleans only on the public box so a probe can't learn the household's
  routine. Each successful nightly also leaves a dated database snapshot beside the data volume,
  keeping a week.
- **API errors answer as JSON, without internals.** An unhandled route error now returns
  `{ok:false,error}` with status 500 for API paths (and a quiet HTML page for the app itself) —
  never a traceback; the details go to the server log. The run-metrics endpoint's numeric filters
  are bounded and validated like every other route (junk values used to be silently ignored).
- **The drift view's accent no longer depends on the trial palette.** Several drift styles used the
  second accent colour bare, which only the removable polychrome trial block defines in two of the
  three themes — removing that block would have stripped those tints and strokes. They now fall
  back to the main accent, as the design doc promises.

## [0.27.1] - 2026-08-21

### Fixed

- **Away days no longer reach the public view — on any phase, through any payload.** The public plan
  strip covered the four classic phases only, so a re-base week or a multi-race chain segment kept its
  away dates; and the public training log (`/api/log`) spread the same week records whole and was never
  stripped at all — a real away date was served there for the week it mattered. The
  strip now walks every record at any depth and runs on the plan *and* the log; the self-test drives
  both endpoints for real instead of checking a fixture. Found by an external review (Gemini 3.1 Pro),
  widened in verification.
- **"Explain this plan" works again.** Since 2026-07-04 the explainer's grounding summary referenced a
  variable that was no longer bound, so every explanation answered a 502 — invisible to the key-gated
  check on a keyless box. A new key-free self-test builds that summary on chain and caution plans. The
  per-phase summary handed to the explainer now covers multi-race chain segments, not just
  Base/Build/Peak/Taper.
- **A dead Runalyze MCP session is no longer sticky.** The client re-initialised with the stale session
  id still attached, and a non-JSON "not found" answer crashed before the re-initialise could run — so
  one expired session broke every MCP read (hover profiles, LTHR derivation, run read-back, the
  health/sleep sync) until a restart. It now re-initialises cleanly, once, and retries.
- **Several tabs opening at once run one sync, not several.** The page-load sync throttle was
  check-then-act; simultaneous tabs each pulled from Runalyze (harmless to the data, wasteful to the
  API). A lock lets the first one run and tells the others it is in flight; "Sync now" and the nightly
  job queue behind it rather than overlap.
- **The public view's hover profile never fetches or writes.** On a cache miss the tokenless public box
  made a doomed call and — had a token been present — tried to write into its query-only database (a
  server error instead of the intended "not cached" answer). It now serves what is cached or says so.
- **The self-test battery holds other requests while it runs.** The battery swaps module globals for
  its ~40 s (read-only mode, the planner, the key store); any other request served meanwhile could read
  those values. During a battery the private server now answers other requests 503 + `Retry-After`
  (health, `/selftest` and its API stay reachable), refuses a second battery (409), and the nightly
  job waits for it to finish.

## [0.27.0] - 2026-08-21

### Changed

- **The dashboard says what the load ratio is, not what it predicts.** The acute:chronic tile used to
  call its band a "sweet spot" with "injury risk" above it and "detraining" below it. It now reads
  descriptively: ATL ÷ CTL, the band where a steady training week usually sits, taper and down weeks
  below it *by design*, the 1.30 ceiling and 1.25 planning target the engine actually holds — and
  the plain statement that a load ratio is not an injury predictor (the long-run-jump and fast-load
  brakes are the governors aimed at that). The "you:" marker no longer turns red below the band,
  only above it. The week badges' hint says the same thing.
- **The regime badge names the ceiling it quotes.** "riding the full safe ceiling (ACWR ≤ 1.25)"
  is now "riding the full load ceiling (ACWR planning cap 1.25, hard cap 1.30)": 1.25 is the
  settled-week planning target, 1.30 the in-week hard cap; neither is called "safe". On a phone the
  badge wraps instead of overflowing. The manual's regime paragraph matches.
- **The product narrates nobody's history.** The three halt messages ("the exertional symptom that
  preceded 2025…") now describe the symptom, not a person's year; the AI reply is addressed to "the
  runner". Same red light, same halt, same doctor referral.
- **README caught up with 0.26.0.** The front page still described the removed gate — "earned
  progression", "2 well-absorbed weeks banked", the opt-in earned levers, the CTL floor. It now
  describes the regime rule the engine actually runs: body evidence only.
- **Hygiene** (from an external review; none reachable as an attack, all belt-and-braces): the
  private self-test page escapes every scenario field before rendering; the adjustment dialog
  escapes its error text; the secrets store is created owner-only (`0600`) on disk.

## [0.26.3] - 2026-08-20

### Fixed

- **A bad objective can no longer jam the planner.** Adding a race with a malformed date used to be
  saved first and re-planned second — the save stuck, the re-plan crashed, and from then on *every*
  regeneration (the nightly one included) failed silently behind the last good plan until the row
  was removed by hand. Dates and priorities are now checked at the door (a clear error, nothing
  saved), and every change that triggers a re-plan — objectives, availability, adjustments — is
  applied and re-planned as one unit: if the re-plan fails, the change is rolled back and you get
  a readable error instead of a half-applied state.
- **The daily check-in only accepts its own vocabulary.** An unexpected energy or sleep value used
  to be stored as-is and then read as "all signals normal"; it is now rejected.
- **Numeric query parameters answer junk with a clear error** (`days`, `weeks`, `months` on the
  effort, projector, weekly-volume and VO₂max endpoints) instead of a server error page.
- **Self-test honesty.** The card-truth check's frozen-week coverage no longer depends on what
  happens to be in *your* database (it built its fixture on top of the live road anchor, so a fresh
  or synthetic install reported a spurious failure); the browser test-drive's cold-start check was
  updated to the 0.26.0 regime rule; and the release script now refuses to publish unless the whole
  suite is green on the synthetic demo database — the one a new install actually runs.

## [0.26.2] - 2026-08-19

### Fixed

- **Strides no longer stack onto the heavy end of the week.** They used to ride the first easy run
  — Monday, right between Sunday's long run and Tuesday's intervals, three harder days in a row.
  Strides now land on the easy day furthest from every hard session (this week's long, any quality
  day, and last Sunday's long), which on a typical week means Thursday. Same sessions, same weekly
  volume — just spread so your legs get to be fresh for the part that needs them.

## [0.26.1] - 2026-08-19

### Fixed

- **No more 0.0 km runs.** When you front-load a week hard enough that the safety brake rests the
  remaining days entirely, the plan used to keep an empty placeholder — a "0.0 km · long easy run"
  that even counted as a run in the week's header. A rested remainder now simply lays nothing: the
  card shows what you ran and nothing fake ahead. If the brake ever rests a whole *future* week, the
  card now says the build was capped by recent fatigue instead of showing an unexplained blank.
- **The absorption week no longer doubles up.** If the plan was regenerated *during* a down week,
  the engine forgot that the recovery it was asking for was already happening — so it pulled the
  next scheduled down week forward and you got two absorption weeks back to back, while the
  recovery that belonged at the end of the block vanished (and the miscount rippled extra troughs
  into the build). The week you are living now counts exactly as it will once it's over: one
  recovery per cycle, and the road ahead no longer depends on which day of the week you hit
  regenerate.

## [0.26.0] - 2026-08-18

### Changed

- **The plan follows your measured form — the obedience gate is gone.** Until now the engine
  inferred your health from how closely each week matched its prescription: a "banked streak" of
  plan-adherent weeks earned the normal (assertive) build, and any single miss — even a travel week
  where you ran *more* kilometres than planned, in fewer runs, and absorbed them cleanly — zeroed
  that streak and collapsed the whole road into the post-illness rebuild. That machinery is
  removed: the streak, the regime's "well-absorbed weeks banked" requirement, the re-base's earned
  early exit, the opt-in earned faster build, and the opt-in earned 6th run. The conservative
  posture now engages on **body evidence only** — a medical hold, or a stop-symptom check-in within
  the last 56 days — and lifts by itself once that window is clean. How you lived last week is
  never a reason to shrink the next one; your measured fitness, and the safety ceilings, already
  carry that. (The two opt-in toggles disappear from Settings; nothing you need to do.)
- **Coming back after a long healthy break starts from a small dose, not from zero.** With the
  gate gone, a runner whose recent weeks are empty (travel, a lapsed season) is planned from a
  conservative restart dose — the same first week the post-illness block prescribes — and the plan
  ramps from there by measurement. Previously this case could only produce the gated rebuild;
  internally the ungated path would have degenerated to near-zero weeks, which is also fixed.

## [0.25.3] - 2026-08-15

### Fixed

- **Completed weeks now say what you actually ran.** A week already lived kept showing the total
  that had been *prescribed* for it, carried forward unchanged from the plan it was first laid in —
  so a card could keep claiming "48.6 km · 5 runs" over a week you really ran as 42.4 km, for the
  life of the block. Every completed week's header is now recomputed from your training log on each
  replan; the sessions listed under it remain what the plan asked of you that week, so the per-day
  plan-vs-reality lines are unchanged. Weeks completed before this release correct themselves on
  the first replan after deploying.
- **Adherence is judged against the plan again, not against yourself.** The header fix in 0.25.2
  had a quiet side effect: the run count the engine checks your completed weeks against — the
  evidence behind the assertive posture, the earned volume lift and the sixth-run advance — was
  read from the same field the header rewrite had just set to your *actual* count, so the "did you
  run within one of the planned runs" test always passed. The prescribed count is now stored
  separately, survives every header rewrite, and the earn-back evidence reads that bar. Your
  current posture is unchanged by this — it only bites, honestly, on weeks lived from now on.

## [0.25.2] - 2026-08-07

### Fixed

- **Out-running the plan no longer shrinks the current week's headline.** The week in progress
  summed its *prescribed* elapsed days plus the remaining prescription — so every kilometre you ran
  beyond a day's prescription made the week's total *smaller*, and a well-run week could read below
  a recovery week (the reported case: the card said 35.8 km while the week was really 29.1 already
  run + 11.3 still ahead = 40.4, above the 38.3 km down week it appeared to sit under). The current
  week's header now counts days you have already run at their real logged distance and days ahead at
  their prescription, shows the split ("29.1 run + 11.3 ahead"), and a same-evening replan never
  counts today twice — the day's prescription is superseded by what you actually ran. Future weeks,
  down weeks and completed weeks are untouched, and the reduced *remaining* prescription after an
  over-run is deliberate and unchanged: volume already run is never re-prescribed.

## [0.25.1] - 2026-08-07

### Fixed

- **The taper's "race-pace touch" now actually runs at race pace.** Every taper carried a short
  sharpening session labelled a race-pace touch — hardcoded to the threshold zone. For a 10k that's
  about right; for a marathon it prescribed the plan's *only* threshold-zone repetitions, at a pace
  no session in the previous seventeen weeks had visited, two weeks before the race — at nearly
  twice the per-kilometre tissue load of the pace the label promised. A marathon taper now sharpens
  at marathon pace, the pace every marathon-pace long run has rehearsed since the build began; other
  race distances keep the threshold touch they always had, and in a multi-race plan each taper
  sharpens at its own race's pace. New `det/taper-touch` checks the published plan, not just the
  template, and fails if a taper ever debuts a pace the build never rehearsed.

- **A week's card now states what its own listing shows.** The header above each week — "35.8 km ·
  5 runs" — printed the *template's* run count, while the sessions listed under it came from the
  governed week, which can shed a day (a crushed budget), add one (spreading a capped long run), or
  drop the dead days after a race. Three weeks of the current plan were wrong, in both directions:
  a four-run week titled "5 runs", a six-run week titled "5 runs", and the race week claiming five
  over the single remaining shakeout. The count, the distance and the load in every week's header
  are now recomputed from exactly the sessions displayed beneath them, on every path that publishes
  a week — and a rest note (an eased-away day, an optional-rest card) is never counted as a run.
  Verified on the rendered page, not just the data. New `det/card-truth` sweeps every published
  week in both regimes and fails if any header disagrees with its own listing.

- **The plan now runs on your clock, not the server's.** Only one part of the app knew what time zone
  you live in: the nightly sync, which fires at your wall-clock hour. Everything else simply asked the
  machine what day it was — and the containers run on UTC. So from midnight until the server's
  midnight (two hours in summer, one in winter), the whole engine believed it was still yesterday,
  while your runs arrived carrying their real local date. Two clocks in one database. In practice it
  meant a plan generated late at night was built for the wrong day, and the daily fitness snapshot was
  filed under the previous one — 22 of 53 snapshots and 3 of 79 plans in this database were stamped
  that way. The timezone setting now governs the whole engine's idea of "today", takes effect the
  moment you save it, and — because leaving it unset means "use whatever clock this machine is on" —
  changes nothing for anyone who never set it. Its label and help text were misleading about all of
  this and have been rewritten.
- **A timezone that cannot be loaded is now refused out loud instead of quietly becoming UTC.** The
  obvious version of the fix above — setting the container's timezone in the compose file — would have
  done *nothing at all* on this deployment, and would have done nothing silently: the slim Python image
  ships without the system timezone database, and when the C library is given a zone name it cannot
  load it does not fail, it reads the name as an abbreviation with a zero offset and carries on in UTC.
  The app now supplies the database itself and then checks that the clock actually moved, saying so in
  the log if it didn't. New `det/one-clock`.

## [0.25.0] - 2026-08-07

### Changed

- **Your easy days are no longer all the same distance.** Every easy run in every planned week came
  out at exactly the same number — four 7.6 km days in a row, then the long run. No training plan in
  any book looks like that, and it isn't what you have ever actually run. Your own weeks were the
  evidence: across 161 of them, easy days grade steeply from longest to shortest (the longest is
  about 1.4× the week's average easy day, the shortest about 0.7×), and that grading explains more
  than half of all the variation between your easy runs. What it does *not* depend on is the day of
  the week — there is no Monday effect, no Friday effect, nothing. So the sizes come from your
  history, and where each one lands comes from how recovery works: the longest easy run goes on the
  day furthest from any long run, and the days on either side of a long run — the recovery day after
  the last one, the freshness day before the next — get the short ones. A week that used to read
  7.6 / quality / 7.6 / 7.6 / 7.6 / 12.0 long now reads 7.5 / quality / 9.6 / 8.5 / 6.4 / 12.3 long.
  Nothing about the safety limits was loosened to do this; in fact the highest fatigue ratio anywhere
  in the plan comes down slightly, because putting the bigger easy day further from your heaviest day
  costs less under the same brake.

### Fixed

- **Your week can no longer outgrow your longest run.** The plan was tracking two things that should
  move together and didn't: how much you run in a week, which grew whenever recent fatigue allowed
  it, and how far your longest run may go, which grows by at most 10% over the previous few weeks.
  Nothing connected them. So the week rode its fatigue ceiling and the long run, held to its own
  slower ladder, shrank to whatever share was left — 21–24% of the week, under the 25–30% that
  distance plans actually prescribe and that this plan's own skeleton was already asking for. That
  isn't a plan with a long run in it, it's a pile of distance with a longest day. Worse, because the
  ceiling is a ratio against your *recent* load, the week became a readout of the last fortnight
  rather than a prescription — two rest days could triple it. The week is now capped at whatever
  distance the long run can properly anchor, and the long run is raised to its own ceiling instead of
  being left below it, so the two advance together. Your long run now holds 25–30% of the week all
  the way through the block.
- **A weekly safety check was being verified against a slightly wrong number.** The value the volume
  governor decides on was calculated privately, so everything that reported on it afterwards had to
  reconstruct it — and the reconstruction used your fitness at the *end* of a week where the engine
  uses the week's *average*. On a steeply-rising week those differ by about 5%, enough to report a
  ceiling breach that had not happened. The governor now publishes the number it actually used.

## [0.24.1] - 2026-08-04

### Fixed

- **A good run no longer shrinks the week that follows it.** The plan tracks a second kind of load
  besides effort — the mechanical pounding a run puts through your legs, which depends on how fast you
  were going. Two things about how that was measured could make it move on its own. First, pace was
  sorted into bands with hard edges, so two runs a single second per kilometre apart could be scored
  40% apart. Second, past runs were re-scored against your *current* fitness estimate, and those band
  edges shift as that estimate changes — so a run from a fortnight ago could quietly change what it
  had cost you. Together they bit: a good evening run nudged the fitness reading up, an old run
  slipped across a band edge, and the following week was cut by 7 km. Pace is now weighted on a smooth
  curve with no edges to fall off, and every past run is scored against the fitness you had on the day
  you ran it, so finished training keeps the value it earned. The scale itself is unchanged — the same
  paces mean the same thing they always did. One side effect worth knowing: a briskly-run easy day
  used to be recorded as costing no more than a gentle jog, and now it counts for what it was.

## [0.24.0] - 2026-08-03

### Fixed

- **A long run is now actually longer than your easy days.** Your week is built from a total distance
  and a ceiling on how far any single run may jump from recent weeks. When those two meet — the week
  is big enough that dividing it across your running days reaches the ceiling — every day came out the
  same length, and the plan called one of them a long run anyway. Measured on a real week: four easy
  days of 11.3 km and a "long run" of 11.4 km. The long run's benefit comes from its duration, from
  the things that only happen once a run goes on long enough, and five identical runs cannot supply
  it. The plan now makes the long run bigger where it is free to, holds the easy days below it where
  the jump ceiling has the long run pinned (spreading the freed distance over an extra easy day, so
  the weekly total is unchanged), and where the week is too small for either — a three-run week, for
  instance — it stops calling any of them a long run and says so. Weekly distance is unchanged and
  the hardest week of the block now peaks lower than before, because the same distance spread over
  more days is a smaller single-day spike.

### Added

- **Your plan now tells you when it was built from an older reading of your shape.** A plan is seeded
  from where your fitness and fatigue stood at the end of the previous day, so the plan waiting for
  you in the morning was built for yesterday. When that starting point has since moved, the plan says
  so and shows both readings, so you can decide whether to regenerate. It reports only that the
  starting point changed — never that your sessions would change, which is a different and much more
  expensive question. If the new reading is the same as the old one, nothing is shown.

## [0.23.3] - 2026-07-31

### Fixed

- **The app's log messages now actually appear in `docker logs`.** Container output is a pipe rather
  than a terminal, so Python held everything the app printed in an 8 KB buffer that a long-running
  server never fills — while the web server's own logging, which goes to a different stream, kept
  scrolling past. The result was a log that looked healthy with the application's voice missing from
  it entirely, including the warning that explains why an unreadable nightly-sync time fell back to
  the default. The container now runs unbuffered. No training or plan behaviour changes.

## [0.23.2] - 2026-07-31

### Fixed

- **A plan generated on a day you had already trained no longer believes you were rested.** The
  projection that decides how much a week may carry rolls forward from today, so the state it starts
  from has to be the state at the end of *yesterday*. It was instead taking the fitness/fatigue
  reading dated today — and that reading has already moved through today with whatever training had
  been recorded when it was taken. Taken before an evening run it reads the day as a rest day, so the
  day was counted twice: once as rest, once as the session. Fatigue decays about a quarter in a day,
  so the plan was consistently handed a figure well below the truth, saw headroom that was not there,
  and offered a week roughly twice the size it should have. Taken after the run it failed the other
  way round, the day's load being counted once by the reading and once again by the projection.
  Thirteen of the forty-one plans generated in July were built this way, every one of them in the
  direction that inflates the week — and because there is one reading per day, a later sync
  overwrites the row, so nothing afterwards showed what a plan had actually been built from. The
  state is now taken from the last settled day and rolled forward over the training actually
  recorded, which makes it independent of when any reading happened to be taken. A day whose reading
  never arrived is bridged by the same arithmetic rather than by treating an older figure as current,
  and a saved plan now records which day it was seeded from and how many days it had to bridge.
- **A day you have already trained now reaches the projection as what you ran, not as what was
  prescribed.** With the seed correctly stopping at the end of yesterday, today's load arrived only
  through today's prescription — and once you have run, that prescription is beside the point. On the
  evening this was found the plan had prescribed rest and the run had been an hour of work, so the
  projected in-week peak read 1.13 against a measured 1.47, and the rest of the week was bounded
  against load already spent. Today's recorded load is now applied as a floor: it can only ever raise
  the projected figure, so it can only ever make the plan more cautious, and it goes into the
  projection alone — what the plan asks of you is unchanged. Across the four days checked, the
  projected peak now lands within 0.03 of the independently measured value, where before it sat up to
  0.37 below it.
- **The note that says a week's volume is already run now quotes the figure the decision was actually
  made on.** It was printing the skeleton weekly target while the engine had decided against a larger
  one — "32km of 22km planned", where the number that closed the week was 25.6. Both readings were
  honest arithmetic; only one of them was the one being used.
- The week currently underway is the part that changes: on the day this was found, that week's
  allowance went from 51 km to 25 km, closer to the 32 km already run. Season totals barely move
  (1138 → 1070 km, projected finish 4:17:06 → 4:18:09), which is the expected shape — the error was
  concentrated in the week being laid, and later blocks re-derive from their own carried state. Plans
  built from a reading that was already settled are unaffected.

## [0.23.1] - 2026-07-29

### Fixed

- **The note offering the earned sixth weekly run no longer describes itself as dormant when it
  isn't.** It ended in a fixed sentence promising the plan would stay at five runs until weekly
  volume grew enough for a sixth run to be real training rather than a token one. That was true when
  the sentence was written, at low volume — and it was never revisited, so as volume grew the offer
  went on calling itself quiet while accepting it would in fact have added a day to most building
  weeks. It could also sit directly above a week already showing six runs, because when the long-run
  ceiling holds a long run back the surplus distance is spread onto an extra easy day — a different
  mechanism entirely, with nothing to do with the opt-in. The offer is now measured rather than
  asserted: the engine counts the weeks whose run count would genuinely change and says how many,
  and it excludes weeks that already carry a sixth day for that other reason, since accepting would
  not move those. A plan saved before this release carries no such count, so the sentence is left out
  rather than guessed. No plan numbers change.

## [0.23.0] - 2026-07-29

The limit on how much a plan may build moves off the heart-rate axis and onto the biomechanical one.
Plans now grow to a genuine marathon volume instead of sitting flat for months.

### Changed

- **The ceiling that decides how much a week may carry is no longer an acute-to-chronic heart-rate
  ratio.** That ratio was read on the last day of the week — which is the long-run day, the biggest
  session there is. At an identical, perfectly flat weekly load the reading moves from 0.97 to 1.51
  depending only on *where in the week the long run sits*. Judged against a fixed ceiling, most of the
  room for progression was being spent on the calendar rather than on training, and the long run —
  being the spike that inflated the reading — was effectively taxing itself. Two limits replace it,
  both measuring tissue load rather than cardiac load: no single session may jump beyond a set step
  over the largest session of the previous four weeks, and the week's total tissue load may not jump
  beyond its own ceiling. This follows the evidence rather than convention: in a study of over five
  thousand runners, sharp increases in the **longest single run** predicted injury, while increases in
  weekly mileage did not. The heart-rate reading is still computed, still shown, and is still one of
  the limits — it is simply no longer the one deciding the volume, and it is now read in a way that
  does not depend on which weekday you look at.
- **The long run is now capped at 30% of weekly volume, in line with mainstream marathon coaching.**
  It was previously allowed up to half the week. Counter-intuitively this makes plans *larger*, not
  smaller: the long run is the single biggest session, so capping its share frees tissue-load headroom
  that flows into more easy running. It also brings the long run inside the usual duration guidance of
  two-and-a-half to three hours for most runners. It still grows every week, on a ladder that never
  adds more than ten percent over the longest run of the past four weeks.
- **The mid-week safety check that could strip a week of its quality session now triggers only at the
  problem it was written for.** It was set at a threshold ordinary training weeks routinely reach, so
  it had quietly become a volume limiter rather than the rare rescue it was designed to be.

### Fixed

- **A plan regenerated mid-week no longer shrinks the long run along with everything else.** The
  remaining days of a partly-run week are governed as a share of what is left, and the long run was
  taking that reduction like any other session. Because the long-run ladder measures itself against
  recent long runs, one shortened long run then held the ceiling down for weeks afterwards. The long
  run is now sized against the week as a whole; when the remainder cannot carry everything, an easy
  day is dropped instead.
- **The long-run progression ceiling now covers every run in the week, not only the one labelled
  "long".** With the long run taking a smaller share of a larger week, an ordinary easy day could be
  laid longer than the long run itself — and therefore above the ceiling — while the session named
  "long" sat safely under it. The ceiling applies to the longest run of the week whatever it is
  called, which is what it always meant.

## [0.22.1] - 2026-07-28

The notice added in 0.22.0 could not appear immediately after regenerating — the one moment it most
needed to be trustworthy.

### Fixed

- **The "built by an earlier version" notice was suppressed for the whole render that follows a
  regeneration.** The check compares the version stamped into the plan against the version of the
  engine serving it. That second value was attached only to the request the page makes when it
  loads — but after you regenerate, the page renders the response of the regeneration itself and
  never makes that request. With nothing to compare against, the comparison quietly resolved to
  "current", so the notice could not be shown no matter what was true. It looked correct, because
  regenerating a plan does normally make it current; it would have stayed just as silent on a plan
  that had failed to save. A warning that cannot be wrong cannot be relied on to be right. Both
  responses now carry the serving version, from a single shared place rather than one path each, so
  the notice is driven by real data whichever way the plan reached the screen. As before, the
  serving version is attached only on the way out and is never written into the saved plan.

## [0.22.0] - 2026-07-28

A saved plan now tells you when it was built by an older version of the app.

### Added

- **Plans carry the version of the engine that generated them, and say so when that is no longer
  the version running.** A plan is a versioned artifact: updating the application deliberately does
  not rebuild the plans already saved, because the weeks you have lived are frozen on purpose. The
  consequence is that immediately after an upgrade the app serves a plan produced by code that is no
  longer installed — same numbers as yesterday, computed by an engine that has since been corrected.
  There was a warning for this, but it worked by recognising the *shape* of one specific older
  payload, so it could only ever catch the breakage it had been written for; a plan that was merely
  one release behind slipped past it silently, showing stale figures with nothing to indicate it.
  Every generated plan is now stamped with its engine's version, the served plan is compared against
  the version actually running, and any difference raises a banner above the plan explaining that
  the weeks below — and everything derived from them — are exactly as they were generated, with a
  prompt to regenerate. The comparison is a plain match rather than an ordering, so a rollback is
  reported just as clearly as an upgrade, and a plan predating the stamp entirely is correctly
  treated as out of date. An internal check ties the stamp to this changelog, so a release cannot be
  published with the version marker left behind.

## [0.21.1] - 2026-07-28

Regenerating a plan part-way through the week no longer quietly shrinks the rest of the season.

### Fixed

- **A plan regenerated mid-week no longer collapses the week in progress — or every week after it.**
  The engine runs one of two regimes: a cautious one, which treats the planned week's volume as
  written, and an assertive one — unlocked by consistently well-absorbed training — which
  deliberately rides the safe ceiling above that figure. The logic handling the week you are
  currently in predates the assertive regime and only ever knew the cautious rule, so it re-laid the
  remainder of that week at the cautious volume no matter which regime the plan was actually
  running. On an assertive plan that is roughly a third less training than intended. The damage did
  not stop at the one week: the plan projects fitness forward from wherever it currently stands, and
  later volume tracks that projection, so the shortfall propagated down the entire road to race day,
  fading only slowly and never fully recovering. Because a plan regenerates daily, most
  regenerations land mid-week — this was the ordinary case, not an edge case. The week in progress
  now follows the intent its own regime holds. **Safety is unchanged:** the acute-load ceiling still
  bounds the remaining days exactly as it did before, and on a constrained week it still binds; only
  the target being bounded was wrong, and it is the target that changed.
- **The days already run this week are shown at the volume that was actually prescribed for them.**
  The same mistaken figure drove the display of the elapsed part of the week, so a day you had
  already completed could be listed at a shorter distance than the plan had asked for. The two
  related checks that decide whether a week's training is "already covered" — and therefore whether
  the remaining days become optional — read the corrected intent too, so an assertive week is no
  longer declared complete a third of the way short.

## [0.21.0] - 2026-07-28

The prediction gets stricter about what counts as evidence — and the plan stops forgetting what you
have already banked.

### Changed

- **A short recording can no longer move your speed estimate.** The model's speed axis is built from
  per-run aerobic estimates, which are inferred from pace against heart rate and therefore need a
  steady effort to mean anything. A stride set recorded separately from the run it belongs to has no
  steady effort in it at all — bursts threaded through jogged floats, with heart rate lagging the
  bursts it is being divided by — and such fragments were scattering estimates in both directions.
  Two changes: the unit is now the **session**, not the recording, so a deliberately split training
  day counts once instead of twice and cannot leave your current speed sitting on whichever piece
  you saved last; and a recording below a minimum distance cannot inform the axis at all. That floor
  is calibrated, not chosen — measured against the smoothed history, short recordings scatter three
  to four times as widely as full ones — and it is locked below the shortest race distance the model
  predicts, so a 5 km race always still counts as evidence for the axis it is evidence for.
- **The prediction band's width is measured rather than assumed.** The band widened by a fixed
  amount per remaining week, which is not how forecast error actually behaves: fitness is
  mean-reverting, so uncertainty rises quickly at first and then slowly. Measured against your own
  history — how far the projection has really strayed over two, four, eight, nineteen weeks — the
  old rule was more than twice too narrow for a race a month out and half again too wide for one far
  off. It is now one measured term in place of the two estimated ones it replaces (which were also
  double-counting each other), with the shape shared across runners and the size learned from each
  runner's own record, falling back to the population value until there is enough history to earn
  its own.
- **A speed estimate from before a layoff no longer presents itself as today's shape.** The
  projection compares where you are now against where the plan takes you, but "now" was simply the
  most recent estimate on file, however old. If your most recent measured running is older than
  about eight weeks — the same window past which the long-run readiness measure already goes neutral
  — the comparison is withheld and labelled, rather than quoting a pre-layoff number as current. It
  is not decayed by some assumed rate of detraining: there is no honest way to calibrate that yet,
  and silence beats a guess. Running re-anchors it automatically. The race-day projection itself is
  unaffected.

### Fixed

- **Evidence of your completed weeks survives the plan moving on.** A plan describes the road ahead,
  so when a training block ends the road re-anchors to the new block and weeks already lived are no
  longer part of it. That is deliberate — but the gates that reward consistent training were reading
  their evidence from that document, so on the day a block ended they found no completed weeks at
  all. A build that had earned its assertive posture dropped back to caution, projected race-day
  fitness fell sharply with it, and because the next plan saved was equally short, the reset
  sustained itself. Those gates now read the whole plan history instead. Where the plan still covers
  its own past — the ordinary case — the result is unchanged.
- **The effort monitor no longer loses prescriptions when the plan re-anchors.** The same fault, in
  the place a runner would actually notice it: prescribed quality sessions are matched to your runs
  and excluded from the easy-day score, and with those prescriptions invisible your hardest sessions
  were re-graded against the easy bar and marked too hard. It also reads the full history now.
- **A run on a rest day is graded like a run, not like a rest day.** Taking a run on a day the plan
  left free had it matched to the rest day itself and marked "too easy" — a verdict that cannot be
  true of a run nobody asked for — and it consumed the match, distorting the nearby
  moved-session matching. A rest day is no longer treated as a session to execute; such a run is
  simply held to the easy bar, the standard it would have been given had it been prescribed.

### Documentation

- The manual and README described the finish projection as it looked before it became a range, and
  never mentioned the prediction band at all. Both are rewritten for what the engine actually does:
  the range as the headline, what the block buys you, how the band's width is composed, when the
  engine declines to answer and why, and the ledger that scores every prediction against the result.
  The manual also states what a plan document is — the road ahead, not a record of the past — and
  where the past is kept instead.

## [0.20.1] - 2026-07-27

A prediction saved by an older engine now says so, instead of quietly presenting itself as current.

### Fixed

- **A plan saved by an earlier version of the engine is labelled as such.** Plans are versioned
  artifacts: the app serves the most recently *generated* plan, and updating the application does
  not regenerate it (past weeks are frozen on purpose). That meant a plan generated before an engine
  upgrade rendered through the new interface with its old numbers and no indication that a newer
  model was available — the finish projection in particular could show a pre-upgrade estimate as
  though it were current. Such a plan now carries an explicit *"saved by an earlier engine —
  regenerate to re-read"* marker, and the same note in the projection's hover. Regenerating the plan
  re-reads it on the current model.
- **The projection never claims a trend its own numbers do not show.** The finish strip's hover
  offered a "with more runway" comparison unconditionally. On a plan whose projection was flat, that
  printed a sequence of identical times directly after stating that more runway means a faster race —
  asserting a trend and then contradicting it in the same tooltip. The comparison is now suppressed
  whenever the projected times do not actually differ, whatever the reason.

## [0.20.0] - 2026-07-27

The finish projection stops answering a question nobody asked, and starts answering the one every
runner actually has: **what does this block buy me?**

### Changed

- **The finish strip now shows what the training buys.** It used to read `now → +4w → +8w`, which
  looks like a timeline but is not one: those points price a **later race date** at the same
  training, a hypothetical nobody asked about. Worse, before the projection was un-frozen in 0.19.0
  the three values were often identical, so the strip read as *"eight more weeks of training changes
  nothing."* It now runs the same model at **today's measured state** — current speed axis, current
  fitness, and the long-run ladder actually behind you — against the race-day projection, and names
  the difference: *off today's shape 5:32 → by race day 4:45, the build buys 47 min.* The runway
  points move into the hover, labelled as the what-if they always were.

  The framing stays honest when the answer is not flattering: a runway that is mostly taper reads
  *"this runway costs 6 min"*, and one that projects nowhere reads *"holds today's shape"* rather
  than implying movement. The 80% range remains the headline; this pair is the trend detail.

### Fixed

- **A correction learned at one race distance no longer leaks into another.** The engine learns a
  personal correction from your own race results. Because its speed axis and the classic reference
  construction diverge by an amount that **grows with race duration** — roughly 1% at 5k rising to
  4.4% at the marathon — a correction learned on marathons carried marathon-specific scale error
  into a 10k prediction, and vice versa. That deviation was previously assumed to be a constant,
  which would have cancelled harmlessly; it is not. The correction is now carried across distances
  explicitly, stripping the tilt of the distances it was learned on and applying the target
  distance's own. This is **exactly neutral** when your races and your objective are the same
  distance, and when you have no raced history at all, so the established path and the cold-start
  prior are both unchanged — only genuine cross-distance transfer moves.
- **Race-day noise is no longer overstated for a mixed-distance history.** The band's race-noise
  term pooled results across distances without accounting for the same tilt, booking the gap between
  distances as day-to-day variability the runner never produced. It is now measured on a common
  scale. A single-distance history is unaffected.

## [0.19.0] - 2026-07-27

The plan and the prediction become one object. The engine now says what it thinks you will run —
as a **range**, projected along the build it is actually laying for you — and then keeps score of
its own bets when the race settles.

### Added

- **The predicted finish is a model, not a frozen number (§FT1).** The old estimate stopped
  responding to fitness above a certain threshold: three different projected fitness levels could
  return the same finish time to the second, which made the whole "more training → faster race"
  story unfalsifiable. The prediction now reads three state axes — projected race-day fitness, the
  projected long-run ladder, and the speed axis — and is **strictly monotone in every one of them**,
  which is asserted by a permanent invariant test. A more demanding (but still safely governed)
  plan can therefore never predict a slower race, by construction. Below the healthy-finish floor
  the old conservative fade is preserved exactly, so the "too soon" / "earn it" verdicts are
  unchanged.
- **A per-runner correction, learned from your own races (§FT1).** Every race in your history is
  re-predicted from the state you actually carried into it, and the ratio between prediction and
  clock becomes a shrunk personal correction — one noisy race nudges it, a consistent history moves
  it. With no raced history the population model stands unchanged.
- **The speed axis projects along the build (§FT2).** Your aerobic capacity is no longer frozen at
  today's value for a race months away. It is projected week by week through the load the plan
  actually prescribes, saturating toward your own demonstrated ceiling, with the response rate
  shrunk from the population prior toward your measured one. Every regeneration re-bases the
  projection on the **measured** value, so a fast or slow responder can never drift away from
  reality.
- **The band IS the prediction (§FT3).** The headline is now an 80% range; the median is reported
  as the trend detail. The width is composed from real sources — race-day noise, how well your
  corpus has calibrated the model, how much of the projected gain is still unrealized, and how much
  runway remains — so it narrows honestly as runs land, races bank, and the race approaches. A cold
  start gets a wide band by design.
- **The prediction ledger — the product watches itself (§FT4).** Every saved plan already carried a
  prediction, so the plan table was a retroactive ledger of every bet the engine has made. A new
  chart draws it: predicted finish per regeneration, with the band envelope. When a race resolves,
  the last pre-race prediction is scored against the clock — inside the band or not, plus a **proper**
  log score, so an over-tight band is punished exactly as hard as an over-wide one and the band
  cannot cheat its way to looking calibrated. Races that settled before scoring existed are
  backfilled.
- **Cold start: an age, one race effort, and an objective (§FT5).** A brand-new database with no
  fitness snapshot can now generate a real plan. A recent race-distance effort seeds the speed axis
  by inversion, whatever training history exists reconstructs the fitness seeds (truth beats a magic
  constant), and an optional age in Settings anchors a heart-rate prior until real data lands. The
  plan starts in its conservative mode by construction and every seed is replaced by measurement as
  runs arrive.

### Fixed

- **Today's high can no longer freeze tomorrow's projection.** A runner sitting at their own
  all-time best had their ceiling pinned to that value, which made the projected gain exactly zero —
  the same frozen-curve failure 0.17.0 fixed on the fitness axis, re-grown on the speed axis. The
  ceiling now always keeps headroom above the current value, at every history size.
- **The predicted finish moved in 42-second steps.** The model read race pace off the *displayed*
  pace grid, which is rounded to a whole second per kilometre — over a marathon that quantized every
  prediction into 42-second treads. Fitness could improve measurably with the predicted time frozen,
  and then jump a whole tread at once, so the ledger chart drew a staircase and the band's
  state-uncertainty term wobbled by ±15% depending on where the rounding fell. The model now
  evaluates the same pace fraction continuously. Displayed training zones are untouched.
- **Duplicate and manually-ignored activities reached the speed model.** Every other consumer of
  your history drops them; the new speed series did not, so a duplicated row could drag the current
  value, the ceiling and the response fit.
- **The cold-start seed could come from a personal best years ago.** It now requires a race effort
  from the last twelve months, so the seeded pace and the reconstructed fitness describe the same
  runner rather than contradicting each other.
- **Race scoring used moving time while calibration used elapsed time.** A race result is
  gun-to-mat. The two had drifted apart, so every stopped second read back as prediction error.
  There is now one definition, shared by the corpus that calibrates the model and the ledger that
  scores it — including races recorded in several chunks.
- **Projected load double-counted the current week.** The remaining weekly load is now summed from
  the sessions still ahead, so days already run this week are not counted twice on top of the
  measured value they already produced, and race day itself is no longer counted as training for
  the race. The response calibration is also restricted to runs, matching the run-only load the
  plan prescribes.
- **The public read-only view could serve cold-start seeds** (an age and a heart-rate prior). It
  no longer does, matching the existing privacy posture for personal signals.
- **The prediction ledger's band tooltip** showed a dangling value with no label on hover.

## [0.18.0] - 2026-07-24

The recovery cadence learns coordination — the tissue limiter re-phases the plan's own down weeks
instead of stacking extra ones, and the projected build finally climbs to the race.

### Fixed
- **Re-phase, don't stack (§PRO11).** The progression floor (0.17.0) made every building week ride
  near the ceiling *by design* — which meant the tissue limiter's consecutive-week counter now
  tripped on schedule, forcing a deload every fourth week forever and stripping that week's quality.
  Those forced troughs landed one or two weeks away from the plan shape's own 3:1 down weeks, so a
  meso spent two recoveries where it designed one, and projected fitness plateaued at roughly the
  runner's current shape months before the race. Now, when the counter trips and the shape already
  schedules a down week later in the block, that down week is **pulled forward** (the two weeks
  swap): one trough per cycle at the tissue-safe cadence, and the displaced building week keeps its
  quality sessions. The limiter's guarantee is unchanged — never more than three consecutive
  near-ceiling weeks — and a shape with no down week ahead still gets the original forced deload.
  The early-arriving down week is labelled in the plan (`deload_pulled`). On the reference data this
  moves projected race-day fitness from a flat line at current shape to a monotone build peaking
  just before the taper.

## [0.17.0] - 2026-07-23

The projection learns progressive overload — a plan that used to draw a flat fitness line to race
day now compounds, honestly labelled. And the run read-back learns that workouts end.

### Added
- **Progressive-overload floor on the assertive ceiling (§PRO10).** The weekly-load ceiling is a
  ratio of chronic load, so riding it has a fixed point: the projection allowed ~maintenance, the
  down weeks handed the small surplus back, and a months-long build could project **zero fitness
  gain by race day**. Now a building week's allowance can't be soft-clipped below a modest
  progression (+6%/wk) over the last realised non-down week. The acute guardrails are untouched
  and always win: the in-week peak ACWR hard cap, the chronic-ramp cap, forced deloads, the
  long-run step cap and the biomechanical ceiling all still bound every week — and the floor
  suspends itself entirely whenever the shape-response brake measures that absorption is lagging.
  Weeks the floor lifted are labelled in the plan (`prog_ridden`): the drawn trajectory assumes
  continued clean absorption and re-anchors on your actuals at every regeneration.
- **Build phase intents grow again.** The Build weekly intent ramp rises 2%→4.5% (matching Base):
  "lightly growing" plus 3:1 down weeks nearly cancelled, so even the conservative regime asked
  for a flat Build.

### Fixed
- **Read-back (§RD v8): the workout ends.** A trailing work-zone block whose rest gap dwarfs the
  session's own observed rest scale (≥8 min AND ≥3× the longest real inter-rep rest) is cooldown
  drift — terrain letting go on the way home — not another rep. A 2-rep VO₂ session no longer
  gains a phantom third rep from a cool-down that quickens when the uphill flattens; everything
  after the last real rep reads as cooldown, so per-rep effort grading stays honest. Long-recovery
  formats (uniform 5-min jogs) keep their genuine final reps; a plain 2-rep session can never
  lose its second rep. Cached reads re-heal lazily on first view.

## [0.16.0] - 2026-07-20

Record a session in deliberate parts — easy body saved, fresh recording for the strides — and the
engine reads it back as ONE session. And it now counts your strides even when you barely rest
between them.

### Added
- **Split sessions — "1+1" (§SJ).** Some runners deliberately record a mixed session as separate
  parts so no platform averages the strides into the easy run's numbers. The engine now agrees
  with them: same-day recordings a save-and-restart apart (≤30 min, never overlapping) are read as
  **one logical session**, derived at view time — activity rows are never merged or edited.
  Everything downstream understands the group: the **effort monitor** judges your easy discipline
  on the session's aerobic body only (the strides part's HR stays out of your easy score — the
  whole point of splitting) and matches ONE prescription per session, so a second same-day
  recording can no longer "reschedule" itself onto a neighbouring day's quality session; the
  **read-back line** joins the parts ("47min @6:42/km · then 6min · 10× strides @4:24/km") with a
  `1+1` chip on the tile and a merged day entry in `/runs`; the **long-run progression cap**
  anchors on the whole outing's distance (a 3-minute save-and-restart doesn't reset a continuous
  run); a race recorded in chunks (watch save mid-race) can still resolve; and a short part that
  was unreadable alone (under 10 min / 2 km) is read on the strength of the session it belongs
  to. Public mirror posture unchanged: pace-only, every HR field withheld server-side.
- **Strides execution read (§SQ).** When a session carries strides, the read-back gains an
  execution line: **count vs what was prescribed** ("10× strides · count on target (8–12)"),
  strides-only pace, and — private box only — the HR *response*: per-rep peaks and whether your
  HR floor recovers between reps (a creeping floor reads as "rest ran short"). HR is reported as
  response, never as an effort verdict — a 15–20 s stride's cardiac peak lands in the recovery,
  so pace remains the honest effort signal.

### Fixed
- **Short stride-dense recordings read as Strides, not "tempo" (§RD).** A 6-minute strides
  recording with jog recovery used to blend into a "sustained effort, no easy bracket" verdict:
  the wall-to-wall-hard branch answered before the stride count was consulted, and the old rule
  required a walking-pace base calibrated on full-length sessions. Now ≥4 counted strides inside
  ~12 minutes is a strides session regardless of recovery pace, and the strides check runs first.
- **Strides counted through frame aliasing (cadence-burst pass, §RD).** With rests shorter than
  the 15 s analysis grid, every other stride can vanish into a pace blend (a real 10-stride set
  counted 6). The raw ~1 Hz **cadence** stream keeps one distinct high-cadence run per stride —
  legs stop turning over instantly even when frame-blended pace doesn't — so cadence graduates
  from countersign to counting channel: bursts ≥10% over a recovery-quartile cadence floor,
  5–60 s wide, corroborated against pace (half the stride bar, so a flat-pace cadence flutter
  never counts), deduped against pace-counted strides. Pace stays primary. Each stride rep's own
  pace now also quotes the fastest frame it touches, never a rest-blend frame.

## [0.15.0] - 2026-07-19

The plan learns to lay itself around your life: declare the days you can't run and the week
re-shapes — honestly.

### Added
- **Away days (§AV).** Settings gains an **"Away / can't run"** row: declare a date or range
  (travel, life — no explanation needed, a note is optional) and the plan **re-lays the week
  around it** immediately. The displaced run slides to the nearest sensible day (a blocked
  Tuesday moves its session to Wednesday, not to a far corner of the week), hard sessions keep
  their spacing, and the long run takes the last available day — a blocked weekend moves it to
  Friday. A heavily blocked week gets honestly **lighter**: runs with no legal day left are shed
  *with their load*, never crammed into the surviving days, and a relocation is refused rather
  than allowed to create an unbroken multi-day run streak. Affected weeks carry an `✈ away` tag.
  Away days are **structural, not easing** — different from a check-in ("legs flat"), which
  reduces load; this moves it. Deterministic input, no LLM in the path, works on a keyless
  install. **Private by design**: away dates never appear on the public read-only mirror in any
  form — not as rows, tags, or markers (an away date on a public page is an empty-house
  broadcast; redacted at the data layer, locked by `det/av-public-strip`). MANUAL §9 documents
  it. New `det/availability`.

### Fixed
- **Progression caps anchor on what you actually ran, not on old prescriptions (§PRO9/§3.1).**
  The +10% long-run cap and the biomechanical (eq-km) ceiling window over *elapsed* weeks used
  to read the plan's own frozen prescriptions — so an athlete who consistently out-ran a
  conservative stretch could see the cap collapse below their real trailing long run and the
  honest day-padding degenerate into many tiny no-rest days. Elapsed and straddling weeks now
  seed those windows from **logged actuals** (weeks with no runs contribute nothing, so the cap
  follows reality down too). New `det/cap-truth-anchor`.
- **A still-ahead quality session survives the mid-week re-plan (§6o-QF).** The week that
  straddles today regenerates its remaining days as easy — correct for a quality session whose
  day already passed (a missed session is never crammed into the back of the week), but wrong
  for one whose laid day is still ahead (now normal, since §AV can relocate a session later
  into the week). A future quality day now keeps its session, pinned in place, with an
  easy-only fallback when the week's remaining budget can't honestly carry it. New
  `det/quality-forward`.
- **Interval read-back no longer counts a hot run-home as an extra rep (§RD v5).** The
  classifier's work/easy contrast baseline could anchor on a recovery *float's own pace* level,
  letting a faster-than-prescribed cooldown clear the work-contrast bar by a hair and read as a
  work rep. The baseline is now the slowest qualifying level's time/distance pace. Cached
  read-backs reclassify lazily on next view. New `det/float-baseline` fixture.
- **Readiness tile refreshes on a morning sync even when no new run arrived.** The page-load
  auto-sync only re-rendered tiles when activities were added, so an HRV-only overnight pull
  could leave yesterday's amber readiness on screen until a manual reload. Any real sync now
  re-reads readiness.
- **Settings window scrolls as one surface.** With the Suunto key rows present, the keys section
  alone could fill the dialog's height — collapsing the settings area below it to zero and
  making everything under the keys unreachable on shorter windows. The dialog body is now a
  single scroll region.

## [0.14.0] - 2026-07-12

The watch learns the plan, a finished race settles its own score, and your data finally has a
backup story.

### Fixed
- **Health-marker range toggle windows from the last reading, not today.** A series that stopped
  (e.g. resting HR after a watch change) always produced an empty 6m/1y window and silently fell
  back to the full span with the button still lit; the window is now the last 6m/1y *of data*.
  The toggle also renders as a proper segmented control — one continuous outline instead of
  per-button borders that broke around the selected segment.

### Added
- **Suunto watch push (§SG).** Planned sessions can now land on a Suunto watch as **SuuntoPlus
  Guides**: connect once via OAuth in Settings (bring your own Suunto partner-app keys — client
  id/secret + subscription key from `apizone.suunto.com`; there is no central relay), and the
  nightly re-plan pushes the upcoming days' structured sessions — steps with duration/distance,
  pace windows and HR bands — replacing yesterday's push idempotently. Without keys or a
  connection the push is simply off; nothing else changes. *(First on-hardware verification is
  still pending — a Suunto-side login issue is blocking the initial OAuth connect; details that
  their docs leave unspecified may need small follow-up fixes.)*
- **Backup & export (§BX).** The app finally has a data-safety story. Settings gains a
  **Backup & export** section with two private-only downloads: a **database snapshot** (a
  consistent, compacted copy of the whole DB, safe to take while the app runs — restore by
  dropping it into `./data`) and a portable **JSON export** of everything that cannot be
  rebuilt from Runalyze (objectives + race outcomes, readiness check-ins, reflections,
  adjustments, lab markers, ignore-list, plan history, settings). A new
  `python SparingHorse.py import <file>` restores the JSON into a fresh instance — and
  refuses to import over existing data. API keys live in the separate secrets store and are
  never in either artifact (locked by the new `det/backup-export`). MANUAL §12 documents the
  whole story.
- **Race lifecycle (§RL).** A race that has passed now resolves instead of staying "upcoming"
  forever: the nightly re-plan (and the objectives list itself) matches the race-day run and
  settles the objective — **done** with a recorded result (finish time, DNF distance, and
  whether the goal time was beaten) or **lapsed** when no race-day run appears after a short
  sync-grace window (a `custom` race with no standard distance settles done/unverified rather
  than being accused of a DNS). Resolved races show in a private-only **Past races** strip on
  the objectives card, and the drift scorecard's post-race reckoning now survives resolution.
  Plans are untouched — periodization already ignored passed races. Race results are redacted
  at the data layer on the public read-only mirror (`outcome`/`resolved_at` never leave the
  private console). New `det/race-lifecycle`.

## [0.13.0] - 2026-07-05

Sleep lands on the health view, the run browser learns your whole history, and an over-run week
finally stops prescribing.

### Fixed
- **An over-run week no longer keeps prescribing.** The in-flight week's remaining days are now
  charged for the km you have already run (§6o-B): once the week's planned volume is done — even
  when the run *count* is short, as with doubles — the remaining days become optional rest
  (marked *"✓ volume run — today optional"*) instead of laying more sessions just to hit a count.
  A partial over-run shrinks the remaining prescription to exactly the km still owed. The charge
  only ever reduces: an under-run week still gets its day-prorated share, so a missed day is never
  crammed into the back of the week, and a week tracking its plan is byte-identical.

### Added
- **The run browser's stats rail now spans three windows.** Next to the browsed month's totals sit
  two more columns: the trailing 12 calendar months ending with that month (so browsing back to a
  peak year compares its rolling volume, not today's), and all time — dated from your first
  recorded run. Same rows throughout: runs, distance, time on feet, average pace, duration-weighted
  average HR, training load, longest run.
- **Sleep, on the health view.** The nightly sleep summary now syncs from Runalyze alongside HRV,
  weight and resting HR — total duration, deep and REM minutes, and the 0–10 quality score, one
  honest point per night (a night is attributed to the morning you woke, and the main sleep wins
  over any nap). It also brings **Overnight low HR**, a de-facto resting-heart-rate trend that —
  unlike the existing Resting HR series, which dead-ends where the device changed — carries
  through to today. These are **displayed signals only**: a study of four years of nights against
  next-day run quality found no predictive link, so sleep never steers the plan or a readiness
  gate. The honest training brakes stay the mechanism; sleep is there to look at.

## [0.12.0] - 2026-07-05

The app now reads your runs back to you — and a safety-brake week can no longer decay into junk
prescriptions.

### Added
- **"Read back" — automatic workout-structure detection.** When a run lands, the app decodes its
  recorded pace profile into the plan's own vocabulary and narrates it on the activity tile:
  `Intervals — 12min wu @5:50 · 3× 3–4min @5:05–5:15 w/ 60–90s floats · 15min cd @6:30`, or simply
  `Easy run — 45min @6:25/km · 2× strides`. Structure is found by *contrast* (sustained pace
  shifts on grade-adjusted streams at any sample rate — hills can't fake intervals), then each
  block is named against your zones **as of that day**, so the read tracks your fitness, not a
  constant. Honest by design: a noisy signal yields no label rather than a guess, and the
  structure line describes what you did — whether it was too hot stays the effort monitor's
  verdict. New runs classify at sync; older ones the first time you open them (the run browser
  included). The label is pace-based and public-safe; per-segment heart rate stays private.
- **Strides, counted the way a human reads the chart.** Short, prominent speed peaks over the
  run's local valley floor — each countersigned by a cadence rise, because a GPS speed spike
  leaves cadence flat and pace alone cannot tell them apart — with width judged on the time axis
  (seconds is a stride, minutes is a rep). A dedicated strides workout gets its own read:
  `Strides — 18min @7:53/km · 11× strides (5+6) @4:40/km` — count, set grouping detected from the
  gap pattern, and the strides-only pace alongside the whole-session pace.
- **Per-rep effort verdicts.** A quality session with a detected structure is now graded on its
  work reps only — warm-up, floats and cool-down excluded — against the prescribed zone's HR
  band, tagged *·reps read* (the whole-run *·rough read* remains only where no structure exists).
  Reps shorter than ~3 minutes are judged on pace instead: a short rep starts rested and the HR
  peak lands in the recovery, so a within-rep average would under-read every short rep.
- **Junk-run floor.** When the safety governor crushes a week's budget (say, after an acute-load
  spike), the week now sheds run-days instead of prescribing 300-metre stubs: no planned run
  under 2.5 km, a gutted week collapses to fewer real runs (a single one at the extreme), taper
  race-week leg-looseners stay exempt, and normal weeks are untouched.

### Fixed
- The earned-volume and 6th-run gate self-tests run their end-to-end checks on a constructed
  fixture instead of the ambient database — their verdicts no longer flip with whatever data is
  loaded (both failed on a live copy after the training-regime flip).
- The quality-drop reshape's documentation now states its honest guarantee: weekly load is
  *bounded* at the pre-drop level, not "preserved exactly" — the pure-easy layout can carry
  slightly less.
- The last plan readers that only understood single-race plans now read the whole multi-race
  road (chain segments count toward banked evidence and effort matching), with a legacy fallback
  so an old saved plan can't silently zero the banked streak right after an upgrade.

## [0.11.0] - 2026-07-04

Quality sessions now hand you their execution plan, and a new run-browser page turns your whole
training history into a clickable, zone-coloured calendar.

### Added
- **Run browser — a new `/runs` page.** The first "explorer" page beside the status dashboard: a
  month calendar of your whole training history where every day with runs carries dots coloured by
  the run's average-HR zone — the same unified zone grid the activity chart band uses — so a month
  reads as an intensity map at a glance (a healthy polarized block is mostly Z1/Z2 dots). Click a
  day and the run opens below in the full activity tile you already know: metrics, hoverable
  pace/HR/cadence/climb traces, route map. Days with several runs open a picker; non-run activity
  shows as a faint tick (its load still counts — the viewer is run-centric). Beside the calendar,
  a month-totals rail sums the month at a glance: runs, distance, time on feet, average pace,
  duration-weighted average heart rate, training load, longest run. Navigation is bounded
  to where your data actually exists. Desktop gets a header link, the phone's bottom bar gains a
  Runs tab. Private-only: the calendar grades intensity by heart rate and the tile carries route
  maps, so the public container serves neither the page nor its API.
- **Workout instruction cards.** A structured quality session (tempo, VO₂ intervals, the long run's
  marathon-pace finish) now presents a clear execution plan instead of a cramped note string: a
  one-line "how to run it" cue plus a per-segment table — duration, approximate distance, the pace
  window and the heart-rate band for every segment, with totals. Pace and HR are cut from the
  *current* unified training zones (the same grid the effort monitor, the activity-chart band and
  the zones card share), so the card always prescribes today's fitness, never the plan's birthday.
  The card sits open on the Today readiness tile when the day's session is structured, and expands
  from any quality-session row in the plan's week panels. The public read-only view shows paces
  only — heart-rate data stays private.
- **Phase-aware Today tile.** The readiness tile's session kicker now names the week's actual phase
  ("Base week 3", "Build week 5") instead of the hardcoded "re-base week" caption left over from
  the caution-era plan shape, and structured sessions get honest titles ("Interval session",
  "Long run · MP finish").

### Fixed
- **Strides pinned to the run that carries them.** The plan's weekly "strides×2" note floated
  under the week's session list, disconnected from any run and silent about when to do them. The
  marker now sits on the session line of the run that actually carries the strides (the week's
  first short easy run — where the engine has always scheduled them), with a hover explaining
  what a stride is and that it adds no training load.
- **Effort-table "low conf" tag was ambiguous.** It read like a compliance judgment; it actually
  flags the verdict's own confidence (a structured session's whole-run average HR blends work
  reps with recovery, so its "did you hit it" read is approximate). Now rendered as "·rough read"
  with a hover spelling that out, and the verdict column's help covers it too.
- **Regime-comparison overlay copy was written one-way.** With the plan now assertive, the
  "Conservative vs Assertive" drift view still framed the other road as "what earning conservative
  unlocks — upper envelope", which is nonsense in that direction. The copy is now direction-aware:
  from a conservative plan the assertive line remains "what earning it unlocks"; from an assertive
  plan the conservative line is presented as what it is — the floor the engine would hold you to
  if the readiness gate re-closed.

## [0.10.0] - 2026-07-04

Every session now says what it's *for* — and the assertive plan sequences fitness by component,
with the quality mix calibrated against real training history.

### Added
- **Component-aware training plan (the four-component model).** Marathon fitness decomposes into
  VO₂max × running economy × SSmax/LT2 plus physiological resilience — how little the first three decay
  over the race distance. Every quality session now carries a chip naming the component it chiefly
  builds (hover for the science), and each phase header sums what the phase is *for*, derived from the
  sessions themselves so the label can never drift from the prescription. An eased or fatigue-capped
  session loses its chip — a session that won't happen builds nothing.
- **Component periodization on the assertive regime ("VO₂max early, maintain late").** The earned
  assertive build now sequences quality by component: a short VO₂ touch appears already in the Base
  phase (developed early, it's cheap to hold — and deliberately small, sized to fit the biomechanical
  damage budget of a steep volume rebuild), the mid-week interval session then shifts to a
  *maintenance role* through Build and Peak while the marathon-pace long-run segment grows week over
  week **at constant speed** (longer, not faster), and the Peak pivots to resilience — the long-fast
  run becomes the workout. Calibration note (verified by replay on real data): the maintenance
  session keeps its full *size* — a smaller mid-week session concentrates the week's load into fewer
  hard days, and under the per-day safety cap that lowered the whole plan's safe volume with no
  offsetting benefit. All existing safety governors (ACWR ceiling, polarized easy floor, phase
  hard-caps, long-run jump cap, the biomechanical brake) bound the new mix unchanged; the
  conservative regime's plan is untouched. Locked by a new `det/components` self-test.

### Fixed
- **An assertive plan looked half-missing: no training-log overlay, and a "No active plan"
  readiness tile.** Three readers of the plan looked only at the re-base phase's weeks — and the
  assertive regime *skips* the re-base, so once a plan graduated: elapsed weeks lost their
  done/missed marks, actual km + pace, unplanned-run entries and journal reflections; the Today's
  Readiness tile claimed there was no active plan at all; and the AI plan explainer was handed an
  empty week list. All three now read the whole road through one shared reader (every phase's
  weeks, each tagged to its phase — multi-race chain segments included), so past/current weeks in
  any phase carry the full overlay and today's session always resolves. Locked by a new
  `det/log-phases` self-test.

## [0.9.0] - 2026-07-03

Zones you can see, a threshold you can set, damage weights tuned to real history — and a fix that
stops a cheerful check-in from slowing your graduation.

### Added
- **A "Current zones" table on the Fitness tab (private).** Your training-intent zones — easy, marathon,
  threshold, interval — each with a pace window and an HR band, both tracking *current* fitness: paces are
  fractions of vVO₂max off your live VO₂max (easy bar = LT1 ≈ 80% of 5k pace), and HR bands are cut from
  the same unified zone grid the effort monitor and chart band read, so the table can't disagree with the
  verdicts (the easy row's top *is* the monitor's easy ceiling). The two columns are deliberately
  independent estimators (VDOT vs LTHR) and their divergence is the decoupling diagnostic, not an error.
  Every cell carries a "?" explaining exactly how it was computed. Locked by `det/zones`.
- **Manual LTHR override + a readiness-gated field-test suggestion.** You can now enter a field-tested
  lactate-threshold HR (Settings → Manual LTHR); a fresh entry outranks the data-derived estimate
  everywhere the app anchors on LTHR (effort ceilings, the HR-zone band, the LT1 cross-check), then
  **ages out over weeks** — LTHR moves with fitness, so the derived estimate takes back over rather than
  trusting a stale number, and re-saving the same value deliberately doesn't re-freshen it. The manual
  documents the 30-minute test protocol, and the app only *suggests* the test when every clearance
  holds — assertive regime, green readiness, no medical hold, and an anchor that's actually improvable —
  never during a conservative rebuild (it's a near-maximal effort; the safety gate is the point). Locked
  by a new `det/lthr-manual` self-test.

### Fixed
- **A positive check-in could silently delay your graduation to the assertive build.** Telling the app
  a run *felt good* records a "no change" note — but the week-banking rule counted *any* recorded
  note-with-dates as "the engine eased this week" and threw the week's evidence out. So a week you
  fully absorbed (plan met, recovery clean) could fail to bank purely because you engaged positively —
  the opposite of the design's intent, and it kept a real training block on the conservative regime
  longer than earned. Banking now distinguishes a genuine ease (reduced volume, easy-only, a clamp, a
  medical hold — any of these still voids the week, even if later superseded) from a no-op check-in,
  which no longer costs anything. Locked into the regime-gate self-test with both cases.

### Changed
- **Biomechanical damage weights calibrated against a real training history.** The eq_km damage-per-km
  multipliers shipped in 0.8.0 as a literature starting point, with the promise they'd be tuned to real
  data before hardening. A full-history replay (4.7 years, ~1,100 runs, zones tracking fitness at the
  time) kept its promise pointing the other way: the one week in the whole record where a fast-load spike
  coincided with an escalating overuse symptom is caught by this axis (and only this axis — volume
  brakes pass it), but the literature weights would also have falsely eased seven quality weeks the
  corpus demonstrably absorbed at peak fitness. The weights are now softened (marathon ×1.4, threshold
  ×2.5, interval ×3.5) — the true catch is kept, false brakes on proven-absorbable training drop by
  more than half, and the jump threshold is unchanged. Harsher weights were strictly worse on the same
  data. With a single true positive on record this remains a false-positive-rate calibration, not a
  validated injury model; it will be re-calibrated as the corpus grows.

## [0.8.0] - 2026-07-02

New injury brakes on load axes the acute:chronic ratio structurally can't see — a pace-weighted
biomechanical lens and a long-run progression cap — plus a fitness-tracking easy-pace ceiling (LT1),
a read-only durability signal, and a side-by-side view of the assertive build your data could unlock.

### Added
- **Conservative-vs-assertive overlay on the plan-drift charts.** A toggle turns the drift view into a
  side-by-side of the road you're on versus the assertive build your data could unlock — distance, load,
  fitness and projected finish, on the same axes. It's a lazy, read-only projection (never persisted, so it
  can't pollute your plan history) and honestly labelled an upper envelope. Locked by `det/regime-compare`.
- **Biomechanical load axis (eq_km).** A second load lens — the tissue-damage axis John Davis's
  biomechanical-load writing highlights — one that heart-rate load (TRIMP/ACWR) structurally can't see. `eq_km` is a *damage-equivalent* distance
  (km weighted by pace, since fast running does several times more tissue damage per km than easy). On the
  assertive build it adds a **soft biomechanical brake**: if a week's pace-weighted load would jump too far
  above your recent weeks *because of fast running* (not just more volume), the engine eases that week's fast
  work back to easy — keeping the aerobic volume, only ever reducing load, and never hard-refusing (injury is
  probabilistic, so it shapes the odds). The conservative rebuild is byte-for-byte identical, and the damage
  weighting is deliberately uncalibrated-to-you for now, so it's contained and will be tuned to your own fast
  sessions and injury history before it tightens. Each plan week now shows its biomechanical `eq-km` (on
  quality weeks, where it exceeds raw km), and a week the brake eased is flagged *"fast load eased"*; locked
  by `det/eq-km`.
- **LT1 — your fitness-tracking easy-pace ceiling.** Making pace the intensity anchor (informed by John
  Davis's pace-first intensity model), the app now derives **LT1 (your aerobic threshold)** from your current
  fitness — operationalized as ≈80% of 5k pace — so the easy-day ceiling *moves as you get fitter or detrain*
  instead of sitting at a fixed heart-rate percentage. The effort monitor now reads against this moving LT1 (with a pace cross-check on
  every run) and no longer falls back to a fixed heart-rate percentage when it lacks a threshold estimate —
  though where a trustworthy, moving heart-rate threshold exists it stays the primary easiness read, because
  your own data shows heart rate is the honest signal of how easy a run really was (and pace alone would
  over-flag a rebuild). When you're rebuilding and your easy pace runs a little ahead of the heart rate it
  costs (normal cardiac decoupling), it says so and explicitly *doesn't* police your easy pace. Surfaced on
  the private effort panel and `/api/lt1`. Locked by `det/lt1`.
- **Durability signal (read-only).** A resilience read from your long-run *aerobic decoupling* — how
  much your pace-to-heart-rate efficiency drifts over the back half of a long run. Low = your economy
  holds over the distance (durable); high or rising = it's decaying — the durability/resilience limit
  John Davis's marathon framework emphasizes. It's computed with a legible verdict and a distance-aware trend
  and accumulating cases on your private run-metrics data, but deliberately *governs nothing yet* — it
  earns its way into race projection only once your corpus shows it predicts your actual race fade
  (the same evidence discipline that walked back the heat coefficient). Locked by `det/durability`.
- **Long-run progression cap (the strongest single injury lever).** Informed by John Davis's writing on
  the Aarhus/Nielsen injury cohort (n≈5000), the plan now never *prescribes* a long run that jumps more
  than +10% beyond your longest run of the previous four weeks — in that cohort a sharp long-run jump
  predicted injury where weekly-mileage jumps did not, and it lives on a biomechanical axis that
  load/ACWR can't see (the +10% figure is our own conservative operationalization). The freed volume is redistributed to your easy runs, so
  the week's total load and its ACWR are unchanged — only the single long-run spike is held back. It
  applies in the assertive build (where the plan grows fast enough to risk such a jump); the
  conservative rebuild is byte-for-byte identical. A week the cap held is flagged *"long-run held (+10%)"*.
  Locked by a new `det/long-run-step` self-test.

### Fixed (pre-release, from an adversarial multi-angle review of the above)
- **Long-run cap could be defeated by its own redistribution.** When the cap fell below a week's natural
  even-run length (a small recent-longest run + a growing week — the returner profile), the freed long-run
  volume made the *easy* runs longer than the capped long, so the true longest run quietly exceeded +10%.
  Now no single run may exceed the cap: the volume spreads over more (shorter) easy days instead, holding
  the promise the cap exists to make.
- **Effort monitor could call a redlined run "easy" when it lacked a reliable heart-rate threshold.** After
  the LT1 change, a run at easy *pace* but with the heart rate sitting at hard-effort level could read "on
  target". It now still won't over-police a merely-elevated heart rate on an easy run (that's normal when
  you're rebuilding), but a genuine redline can no longer read as easy.

### Safety
- Every brake is preserved and was adversarially reviewed: the ACWR ceiling is never breached, a
  recovery trough and periodic deloads bound the connective-tissue load the ratio can't see, readiness
  and symptom halts still dominate, and a recent medical hold keeps the conservative regime in force.

## [0.7.0] - 2026-06-30

The plan now *uses the safe headroom your own metrics reveal* instead of always playing it timid —
an accelerator to match the brakes — while keeping every safety guardrail intact.

### Added
- **Adaptive, goal-driven progressive overload (the "assertive" build).** Once your data shows the
  restart caution isn't needed — a clean symptom/medical window, settled readiness, and a couple of
  well-absorbed weeks banked — the engine graduates from the conservative re-base into an assertive
  build that *rides the safe ACWR ceiling* rather than a timid fixed ramp, so volume and load grow as
  fast as the science safely allows and your fitness is retained through the build instead of bleeding
  away. It's earned automatically from your training data (never from raw fitness alone), and the
  conservative regime stays byte-for-byte identical for anyone still rebuilding.
- **A self-calibrating sense of your response.** The build compares your measured fitness to what it
  projected and eases off when you're falling behind, rides the full ceiling when you're on track —
  your own rate of adaptation tunes how hard it pushes.
- **Finish-time honesty valve.** A short runway isn't a refusal — it's a slower projected finish, and
  now the plan says so: a projected marathon time plus how it improves with more weeks of runway.
- **Two new at-a-glance surfaces on the plan** — a regime badge (with the reason it chose conservative
  vs assertive) and the finish-time curve.

### Safety
- Every brake is preserved and was adversarially reviewed: the ACWR ceiling is never breached, a
  recovery trough and periodic deloads bound the connective-tissue load the ratio can't see, readiness
  and symptom halts still dominate, and a recent medical hold keeps the conservative regime in force.

## [0.6.0] - 2026-06-29

A more accurate read on your fitness, and a safety fix that keeps easy days easy when you're
rebuilding from a low base.

### Fixed
- **More precise fitness & fatigue reconstruction** — the reconstructed fitness/fatigue curve was
  systematically undershooting the values your training service reports, and the gap grew as
  training load rose. The smoothing was corrected so the reconstruction now matches the source
  closely across both rest days and hard days. This sharpens every place the plan reasons about
  fatigue.

### Changed
- **Easy days stay easy at low fitness** — when fitness is low and the safety governor trims a
  week's volume hard, the small fixed amount of quality work could become an outsized share of the
  shrunken week, leaving you doing intensity exactly when you're most fragile. The plan now drops
  that week's quality to easy — never adding volume — so the week stays genuinely easy-dominant,
  and restores the quality automatically as fitness returns. Your marathon-pace long-run finish is
  preserved; only true high-intensity work is held back.

## [0.5.0] - 2026-06-28

The plan starts learning from how a run actually went — not just that it happened. Every run
becomes a controlled data point, compared against your own past runs on the same route, so
patterns in heat, terrain, fatigue and how you felt can surface over time.

### Added
- **Per-run metrics table** — one queryable row per run with every signal we capture (weather,
  terrain, heart rate, an efficiency measure, and your fitness/fatigue state on the day),
  plus an automatic same-route analysis that only compares like-for-like (terrain and fitness
  held) so a finding has to be real, not a season artifact. Private; not exposed on the public box.
- **Worked examples** — each run is auto-compared to your recent runs on the same route, with
  the directional changes laid out and a flag when how you *felt* pointed the opposite way to the
  objective markers (fatigue, HRV). It records the case; it doesn't pass a verdict from a single
  run. The casebook grows as you log more same-route runs. Private.
- **Full-history fitness & fatigue on every run** — the reconstructed fitness/fatigue curve now
  backfills every past run, so the analysis spans your whole history instead of the few days the
  fitness service reports directly.

### Changed
- **Frequency-met days** — once you've already run the week's prescribed number of runs *and*
  its distance, today's remaining run becomes optional rest rather than a forced extra. A short
  junk run on an already-met week does nothing for aerobic shape; the plan stops asking for it.
- **Honest building weeks** — when recent fatigue forces the safety governor to cut a long run
  below a real long-run distance, the plan now relabels it a shakeout and flags the week, instead
  of quietly calling a fitness-trivial run a "long run".

### Fixed
- **Plan ACWR-ceiling self-test** scoped to the weeks the governor actually controls, clearing a
  spurious failure caused by a settled past week's load.

## [0.4.0] - 2026-06-28

Heart-rate zones get a real physiological anchor, and the health view starts tracking the
metrics behind the engine — HRV, weight and resting heart rate over the long horizon.

### Added
- **LTHR-anchored HR zones** — heart-rate zones now anchor on a data-derived lactate-threshold
  HR (Friel's %LTHR grid) when there's enough data, falling back to a %HRmax grid otherwise.
  One definition drives the chart, the new zone band, and the effort monitor, so they can't
  disagree.
- **HR-zone band on the activity chart** — a thin strip along the top colours each section of
  a run by the zone you were in, with an always-on legend showing the basis (LTHR vs %HRmax).
- **Pace ↔ HR coherence check** — surfaces when your prescribed easy *pace* and your easy *HR*
  ceiling disagree (classic when detrained), shown in the effort card. Diagnostic only — it
  never changes the plan.
- **Watch metrics in the health charts** — each sync pulls daily HRV (sleeping RMSSD), body
  weight and resting heart rate from Runalyze, charted against your own long-horizon baseline
  (the view a watch's short rolling baseline can't give you). Private; stripped on the public box.

### Changed
- **Effort monitor reads heart rate against LTHR** when it's confidently known — sharper at the
  easy↔threshold turnpoint than %HRmax — and labels the basis it used. The easy ceiling never
  loosens relative to the previous %HRmax read.

### Fixed
- **Sync/backfill errors are legible** — a backfill that exceeds the gateway timeout now shows a
  clear message instead of a cryptic JSON-parse error, and the sync endpoint always returns JSON.

## [0.3.0] - 2026-06-27

A mobile app experience. On a phone the app now behaves like an installed app rather than
a long web page, and it opens on what matters today.

### Added
- **Mobile app shell** — on phones a fixed bottom tab bar (Today / Plan / Fitness / Body)
  replaces the endless scroll; each area is its own tab with deep-linkable sections.
- **Home-screen icons** — crisp app icons for installing Sparing Horse to your home screen.

### Changed
- **Readiness-first home** — the app opens on Today's Readiness, with your current main
  objective pinned at the top and the latest activity right beneath it.
- **Current-shape numbers** moved into their own section, grouped with the rest of your
  fitness readouts under the Fitness tab.

### Fixed
- **Effort table on small screens** — it now fits a phone in portrait (the key columns,
  with the verdict always visible) and shows every column again in landscape.

## [0.2.0] - 2026-06-26

Multi-race periodization, a more honest feasibility verdict, in-app key setup, and
the app becomes installable. Each engine change ships with a regression lock test.

### Added
- **Installable app (PWA)** — install Sparing Horse to your home screen or desktop for a
  standalone window with an offline app shell. The service worker caches only the UI shell,
  never your data or the API.
- **Set your API keys in the app** — the Settings window now configures your Runalyze token
  and (optional) Claude key directly, with a live "valid / rejected" check, so a fresh
  self-host needs no `.env` editing. Keys live in a private-only store, never the shared DB.
- **Per-race fitness on combined A-race builds** — each race in a chained build shows its own
  projected race-day fitness and feasibility verdict, not just the final peak.
- **Multi-peak plan-drift** — the drift scorecard breaks out each A-race's projected-fitness
  drift against the founding plan and names the next peak still ahead.
- **Public effort-discipline** — the read-only showcase now shows a sanitized, pace-based
  easy-discipline score (no heart-rate data or personal critique).
- **Self-hoster manual** — a full how-to (`MANUAL.md`): setup, the first-run checklist,
  daily/weekly workflow, and a panel-by-panel reading guide.

### Changed
- **The taper now lands on race week** — periodization is anchored so the final taper week
  spans race day, instead of ending up to ~2 weeks short for a race that isn't a whole number
  of weeks out.
- **Honest "earn it" feasibility verdict** — a third reading between "too soon" and "finish":
  when the plan's own projection is below the fitness a healthy finish needs but the runway is
  long, the verdict says the race is reachable *only if you build into it*, rather than
  promising a flat "finish".
- **Anticipated / postponed sessions read correctly** — a run is matched to its nearest
  prescribed session within ±2 days, so doing tomorrow's quality session today (or shifting an
  easy day) is no longer misread as a missed session plus a stray extra.
- **Trail and treadmill runs count** — the running family now reaches the plan-side views
  (effort, mileage, HR), not just activities typed exactly "Running".
- **Stable re-base anchor** — the re-base start is derived from your run history, so it's
  consistent across machines and database rebuilds.
- **Readiness card colours** — the light theme's readiness status card adopts the richer
  green/amber/red signal colours from the dark theme.
- **Deletes explain themselves** — removing an activity now spells out the consequence before
  you confirm.

### Fixed
- **Phase-bar week count** — the "periodization" label now distinguishes the full re-base→race
  span from the weeks still ahead, instead of overstating time-to-race.
- **Medical-hold residuals** — closed remaining gaps so a logged stop symptom holds the plan at
  rest until explicitly cleared; the dominant medical track is locked by test.

## [0.1.1] - 2026-06-26

A full-engine safety and correctness review. Seven load/safety defects and a batch
of correctness fixes — each paired with a regression lock test that reproduces the
bug before the fix.

### Fixed
- **Peak-load ceiling enforcement** — a quality session's minimum impulse could push a
  week's mid-week peak acute:chronic load ratio past the hard ceiling at low fitness,
  even though the week-level governor stayed in band. The full-week builder now re-checks
  the peak and drops to an all-easy week when a quality day would breach the ceiling.
- **Readiness medical-stop gate** — a free-text stop symptom (e.g. "chest pain") no longer
  depends on an optional language model to be caught; a deterministic phrase net halts the
  day on its own, and the halt now persists and reduces the prescription instead of
  reverting the next day.
- **Persistent medical hold** — a logged stop symptom now writes a forced-rest directive,
  regenerates the plan, and keeps the day red until cleared, rather than clearing on the
  next calendar day.
- **Post-race recovery block over-prescription** — the fitness-tracking volume floor no
  longer lifts the post-race bridge, so a recovery re-build keeps its conservative shape
  instead of being inflated by a fresh taper's slack governor headroom.
- **Re-plan transparency** — the plan diff now reports per-phase volume and run-frequency
  changes (and any adjustment directive), so a regeneration that raises load can no longer
  be summarised as "no change".
- **Cross-phase week continuity** — an already-lived week that drifts across a phase
  boundary as the race nears is now frozen verbatim from history instead of being
  redrawn under a different phase's shaper.
- **Feasibility honesty bound** — the feasibility check warns "too soon" only when a short
  runway *and* a low projected race-day fitness coincide; a short runway off high fitness,
  or a long runway off a low fitness base, still reads "finish".
- **Re-base runway clamp** — a near-term race can no longer push the taper past race day;
  the introductory re-base block is clamped to the available runway.
- **Output escaping** — remaining user- and engine-supplied strings in the single-page app
  are HTML-escaped (outcomes, diff banner and change list, adjudication and adjustment
  summaries, plan explanation headline).

### Changed
- **Activity sync** — sync now refreshes an activity whose source data was edited after
  import (reported separately from new activities) and no longer double-counts training
  load when a near-duplicate is re-imported after a local delete.

## [0.1.0]

First documented baseline — the deployed state at the start of the changelog.

### Added
- Deterministic running-coach engine: chronic/acute training-load model (fitness, fatigue,
  acute:chronic ratio) with a safety governor that keeps prescribed load in band.
- Dynamic, objective-driven training plans across re-base → base → build → peak → taper
  phases, regenerated and version-diffable on every change.
- Multi-race build chaining: sequential priority-A races planned as one build with
  bridge / peak / taper segments and a priority selector.
- Daily readiness assessment and an effort-discipline monitor that grades each run against
  its prescription.
- Plan-drift view: original plan versus the plan as it stands, with a settle-the-score
  verdict.
- Post-race reckoning: an honest end-of-race endgame.
- Two-instance deployment off one codebase: a public read-only instance (no private data,
  no mutations) and a private full-console instance behind an access gate.
- Security hardening: content-security-policy, security headers, CSRF tokens, a health
  endpoint, and a public/private data-leak and mutation gate.
- In-app self-test harness guarding the engine invariants.

[Unreleased]: https://github.com/dros74/sparinghorse
[0.1.1]: https://github.com/dros74/sparinghorse
[0.1.0]: https://github.com/dros74/sparinghorse
