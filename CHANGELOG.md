# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: the minor version moves for features, the patch version for fixes).

> **Status — the training-plan engine is under heavy, active development.** Its behaviour, levers and
> outputs may change between releases as the model matures. Versions are checkpoints on a moving
> target, not a stable API.

## [Unreleased]

## [0.25.2] - 2026-08-07

### Fixed

- **Out-running the plan no longer shrinks the current week's headline.** The week in progress
  summed its *prescribed* elapsed days plus the remaining prescription — so every kilometre you ran
  beyond a day's prescription made the week's total *smaller*, and a well-run week could read below
  a recovery week (the owner's case: the card said 35.8 km while the week was really 29.1 already
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
- **Biomechanical damage weights calibrated to the owner's own history.** The eq_km damage-per-km
  multipliers shipped in 0.8.0 as a literature starting point, with the promise they'd be tuned to real
  data before hardening. A full-history replay (4.7 years, ~1,100 runs, zones tracking fitness at the
  time) kept its promise pointing the other way: the one week in the whole record where a fast-load spike
  coincided with an escalating overuse symptom is caught by this axis (and only this axis — volume
  brakes pass it), but the literature weights would also have falsely eased seven quality weeks the
  owner demonstrably absorbed at his fittest. The weights are now softened (marathon ×1.4, threshold
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
