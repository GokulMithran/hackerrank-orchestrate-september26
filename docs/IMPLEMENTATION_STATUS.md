# Implementation status

Implementer: Claude Code. Controlling plan: `docs/IMPLEMENTATION_PLAN.md`.
Review standard: `docs/REVIEW_CHECKLIST.md`.

---

## M0 — contract and measurement (complete; hardened after code review)

**Milestone and status:** M0 complete, including the input-boundary cleanup
required by `docs/reviews/M0_CODE_REVIEW.md`. M1 not started, by instruction.
Current suite: **106 tests, passing on Python 3.10.11 and 3.12.5.**
The M0 section below describes the original pass; the cleanup is recorded after it.

**Problem solved and observable behavior:**
The repository had three zero-byte Python files and no contract, loaders, tests,
or split. It now loads and validates the whole dataset strictly, audits it across
rows, freezes the development/reporting split, and exposes two CLIs. Both
prediction modes are wired and deliberately refuse to run until M1, so no
placeholder row can be mistaken for a baseline.

```
python code/main.py                           -> dataset audit, 0 findings, exit 0
python code/main.py --mode deterministic      -> refuses, writes nothing, exit 2
python code/evaluation/main.py --show-split   -> audit header + frozen split, exit 0
python -m unittest discover -s code/tests -t code -p "test_*.py"  -> 77 tests, OK
```

**Files changed and why:**

| File | Responsibility |
|---|---|
| `code/buy_or_wait/money.py` | `Decimal` parsing, rounding policy, dataset rendering convention. Rejects NaN/inf/negative; blank stays `None` |
| `code/buy_or_wait/schema.py` | Output contract (8 columns), closed input vocabularies, typed records |
| `code/buy_or_wait/data.py` | Strict loaders, exact-header checks, indexes, request scoping, sha256 input manifest |
| `code/buy_or_wait/audit.py` | Cross-row structural/referential audit, findings A01–A12 |
| `code/buy_or_wait/__init__.py` | Package doc and module map |
| `code/main.py` | CLI: audit / deterministic / assisted, exit codes 0/1/2 |
| `code/evaluation/splits.py` | Frozen split manifest: compute, write once, thereafter verify only |
| `code/evaluation/labels.py` | The only module permitted to read expected outputs |
| `code/evaluation/main.py` | Evaluation CLI and the pre-metric audit header |
| `code/evaluation/split_manifest.json` | The frozen split (generated once) |
| `code/tests/fixtures.py` | Synthetic contract-valid dataset builder + single-cell mutators |
| `code/tests/test_{money,data,audit,splits}.py` | 77 offline tests |

Also: `docs/PLAN.md` reduced to a superseded stub, `docs/FINDING_TO_CHANGE.md`
added, all five `.claude/agents/*.md` rewritten (see the review response below).

**Contract/architecture decisions, including alternatives rejected:**

1. **`money.py` added** beyond the plan's suggested decomposition. The plan's §3
   mandates `Decimal` with an explicit rounding policy; concentrating it in one
   module keeps the convention testable and stops it being re-derived per caller.
2. **Rendering convention is measured, not tabulated per currency.** Rejected a
   per-currency precision table: across all 25 gold rows no amount exceeds two
   decimals and trailing zeros are stripped (EUR `603.3`, ZAR `25256`, IDR
   `17229139.2`). `Decimal.normalize()` is avoided because it renders IDR as
   `1.5656E+7`.
3. **Two separate category vocabularies, only one of them gating.**
   `expense_categories_to_protect` / `_reduce` / `_stop` are validated strictly
   against the 22 event categories, because they authorize interventions.
   `financial_priorities` is accepted as-is and only reported by the audit,
   because a priority authorizes nothing. Rejected the alternative of validating
   both against one set — it would reject 278 legitimate tokens
   (`emergency_savings`, `retirement_investment`, `travel` are goals, not
   categories). This surfaced as a real failure: my first constant listed seven
   priority tokens and the data has eight (`healthcare` was missing).
4. **No category alias layer.** Measured: every protect/reduce/stop token is
   already an exact event category, and every user's willing-to-change tokens
   match at least one of their own events. An alias map would be speculative
   complexity with nothing behind it.
5. **Booleans accept case variants, reject everything else.** `TRUE`/`True` are
   formatting; `1`, `yes`, blank are semantic guesses. This column decides
   `allows_partial_payment` on 80 requests.
6. **Prediction modes exit 2 rather than emitting rows.** Per
   `docs/CLAUDE_HANDOFF.md`: an `output.csv` of zeros is not a baseline.
7. **Split is hash-ordered, not seeded-random**, so it reproduces on any machine
   and Python version without depending on RNG stability.

**Tests and exact commands, with results and failures:**

```
$ python -m unittest discover -s code/tests -t code -p "test_*.py"
Ran 77 tests in 4.293s
OK
```

Four failures were hit and fixed during the pass, not papered over:
- 3 × `DataError: 'healthcare' is not a known priority` — root cause was my
  incomplete constant; fixed by the design change in decision 3 above.
- 1 × boolean test asserted rejection of `TRUE` while the code accepted it —
  docstring and code disagreed; resolved in favour of decision 5 and the test
  split into two (rejects semantic guesses / normalizes case).

Coverage highlights: strict parsing and every rejection path; blank amount is
`None` and never `0`; row-order independence; request scoping does not leak
another request's message; label isolation (structural + behavioural + a grep
asserting `buy_or_wait/` never imports `evaluation`); audit defects A04 cross-user
link / dangling / self-link / cycle, A05, A06, A07, A08, A09, A10, A11; split
determinism, order-independence, disjointness, and refusal to regenerate after a
hand edit.

**Evaluation run IDs, split and metrics:** No metrics — scoring is M1. The split
is frozen at fingerprint `ecafb49177d9`, 10 development / 15 reporting,
disjoint by request **and** by user, containing no evaluation request id.

**Evidence/financial safety checks completed:**
- Real dataset audit: **0 findings, 0 errors**, 250 requests / 275 profiles /
  25,342 events / 790 options / 215 messages / 16 images.
- 16 unknown amounts confirmed as **15 debits + 1 credit**; every one has a
  linked image; an unknown amount with no image is a hard A07 error.
- **140/140** foreign-currency events resolve on an exact directed
  settlement-date rate — verified in a test, which is what makes "exact lookup,
  no fallback" safe rather than aspirational. A test proves an earlier-dated
  rate does not satisfy a later settlement date.
- All 790 payment offers satisfy `payment_amount × n == total_payable_amount`
  under `Decimal`; a mismatch is an A06 error, never repaired.
- Input file hashes recorded; `git status dataset/` clean.
- Secret scan of `code/` returns nothing.

**Known limitations and degraded cases:**
1. **Sample exposure is real and disclosed.** All 25 public examples were read
   during planning, before the split existed — including rows that landed in the
   reporting subset (`request_06`, `request_11`, `request_19`, `request_21` were
   quoted with their gold values in planning documents). Reported numbers must be
   labelled *fixed public-sample reporting subset, prior exposure disclosed*,
   never "held-out". Re-drawing the split to move quoted rows into development
   was rejected as exactly the manipulation the manifest exists to prevent.
2. `code/evaluation/usage_report.md` is still empty. It is an M4 deliverable and
   must be generated from the actual final run.
3. The audit is structural. It makes no financial judgement and does not
   semantically verify image contents.
4. `messages.sent_at` is validated as ISO-8601 and kept verbatim; no user
   timezone is assumed. The request-date availability cutoff is M2's decision.
5. Environment: `python` is 3.10.11 with `pydantic`/`anthropic`/`pandas`;
   `py -3.12` is 3.12.5 with **none** of them. The plan's `py -3.12` commands
   would fail from M2. M0 is standard-library-only and runs under both.

**Next milestone:** M1 — cash-state reconstruction, recurrence, FX, forecast,
full/wait/fallback, atomic eight-column output.

**Review requested from Codex:** Yes. Please review this diff against
`docs/IMPLEMENTATION_PLAN.md` §3–§4 and `docs/REVIEW_CHECKLIST.md` "Contract and
data". Specific things worth attacking: the gating/non-gating vocabulary split
(decision 3), the rendering convention (decision 2), the exit-2 behaviour
(decision 6), and whether the request-scoping rule in
`data.DataSet.request_context` is the correct availability boundary before M2
adds a time cutoff.

---

## M1 — deterministic financial core (complete, ready for Codex review)

**Milestone and status:** M1 complete and model-free. No provider imports, no
network, no API key. Runs on Python 3.10.11 and 3.12.5.

**Problem solved and observable behavior:** the engine now reconstructs cash
state, converts currency, detects recurrence, forecasts 90 days, decides
`full_payment` / `wait` / `not_recommended`, gates every row, and publishes the
eight-column `output.csv` atomically.

```
$ python -m unittest discover -s code/tests -t code -p "test_*.py"
Ran 150 tests in 6.979s          OK      (3.10.11)
Ran 150 tests in 6.571s          OK      (3.12.5, py -3.12 -B)

$ python code/main.py --mode deterministic
audit: 0 finding(s), 0 error(s)
wrote 250 rows to <repo>/output.csv
method distribution: full_payment=53, not_recommended=186, wait=11
degraded rows: 2 | gate failures: 0 | unhandled errors: 0

$ python code/evaluation/main.py --split dev
affordability_status      : 5/10
recommended_payment_method: 5/10
```

**Files added:** `code/buy_or_wait/{config,fx,state,recurrence,forecast,planner,validation,output}.py`,
`code/evaluation/metrics.py`, `code/tests/{oracle,test_engine}.py`.
**Files changed:** `code/main.py` (prediction path + `decide_one`),
`code/evaluation/main.py` (scoring).

**Core routing function:** `main.decide_one(context, rates)` —
`state.reconstruct` → `recurrence.detect` → `forecast.build` →
`planner.choose` → `validation.check`, with a conservative fallback on any gate
failure. Every request takes exactly this path.

### Contract decisions, including alternatives rejected

1. **Recurrence is modelled in two parts, not one.** Fixed commitments (constant
   amount, regular cadence) project as discrete dated movements, because *when*
   they land decides whether the balance dips. Everything else is reserved as a
   per-category daily rate built from what the user actually spent.
   An earlier single-model version was wrong in both directions at once:
   requiring three occurrences dropped 12 of 25 real series for `user_13`
   (under-reserving), while taking the maximum observed amount over-reserved the
   ones it kept. Using observed totals over the observed window is
   self-calibrating. DEV method accuracy 3/10 → 5/10 on this change alone.
2. **Income needs two occurrences, spending needs three**, and income groups by
   *category* rather than description. `user_01`'s salary appears as "Prorated
   first salary" then "Next confirmed salary" — one cadence under two names.
   Requiring three description-matched occurrences forecast no salary at all and
   made an `affordable_now` row look unaffordable.
3. **Lapsed income is not carried forward.** A series silent for more than one
   full period has stopped. This is the deterministic half of the ended-seasonal-
   contract case (`message_09`); M2 adds the message-driven half.
4. **Intra-day order is existing debits → credits → proposed payment.**
   Obligations first is conservative (the user does not control when a direct
   debit clears). A *proposed* payment goes last because the user chooses when to
   pay. This is evidence-driven, not aesthetic: gold
   `earliest_date_for_full_payment` values are repeatedly the salary date itself,
   which is unreachable if a voluntary payment must precede that day's credit.
   Ranking the payment first shifted every such answer one day late.
5. **Recurring `investment` contributions are not reserved as essential.**
   `problem_statement.md` defines safety as covering *essential* expenses; a
   recurring contribution is the user moving money into savings. Confirmed future
   investment debits are still reserved — this affects projection only.
   CALIBRATED on DEV: ZAR normalized MAE 0.123 → 0.061, mean date error 17.5 →
   14.6 days.
6. **An unquantified debit blocks every recommendation.** `Forecast.certifiable`
   is False while any obligation cannot be quantified, which forces
   `amount_safe_to_pay` to 0 and suppresses all candidates. Implements review
   finding R01. A bug found by the gate during the first run: the planner was
   generating `full_payment` from `is_safe()` without consulting `certifiable`.
7. **Safety is proved by replay, never by shape.** `validation.check` re-walks
   the forecast with the chosen schedule injected (rule P1). Tests compare
   against `code/tests/oracle.py`, an independent replay written from the
   contract, plus hand-computed fixtures.

### Verification

150 tests, all offline. Notable: forecast agrees with the independent oracle
across three payment scenarios; hand-computed balance; landing exactly on the
minimum is safe and one cent below is not; same-day debit/credit ordering;
a later bill blocks a payment that looks affordable today; window boundaries at
day 0 and day 90; the metamorphic check that an extra debit can never increase
capacity; unquantified debit forces zero; every settled debit is counted exactly
once (no event feeds both a series and a rate).

Independent check of the published `output.csv`: exact header, 250 rows in
requests.csv order, unique ids, and **0 invariant violations** across bounds,
enum membership, plan chronology, plan totals, deadline compliance,
`affordable_now` date rule, status/method agreement, and non-empty explanations.

### Known limitations and degraded cases

1. **74% of rows are `not_recommended`, and most of that is missing capability,
   not genuine refusal.** `installments`, `partial_payment` and spending changes
   are M3. On DEV, 2 of the 5 remaining failures are exactly this — and the
   forecast behind them is accurate (`request_17`: our 243798.78 vs gold
   243849.58, a 0.02% difference, refused only because the user does not accept
   `full_payment`). **This output.csv is a partial-capability baseline and must
   not be reported as a finished submission.**
2. **2 rows are degraded** (unquantified obligation → zero safe amount). Both
   need M2's image extraction. `request_16` is the DEV example.
3. **`request_03` is silently under-forecast**, and this is the most interesting
   M2 dependency: its salary is one of the 16 blank amounts, so recurrence builds
   a 1,964,250/month series when the linked payslip says 4,365,000. The row is
   *not* marked degraded — correctly, because the unknown event is settled and
   historical, so it is already inside `current_available_balance`. The balance
   is right; the projection is not. M2 must feed resolved image amounts into
   recurrence, not only into cash state.
4. **Two spec ambiguities the 25 samples cannot settle**, flagged rather than
   guessed:
   - We report `earliest_date_for_full_payment` even on `not_recommended` rows
     when capacity exists within the window (`problem_statement.md:163`;
     corrected C13). All 7 gold `not_recommended` rows have an empty date, but in
     all 7 capacity never arrives, so gold is consistent with either reading.
     `request_32` in the current output is an example of the divergence.
   - `wait` is rejected when the capacity date falls after
     `desired_completion_date`, per `IMPLEMENTATION_PLAN.md` §5. All 6 gold
     `wait` rows meet their deadline, so this too is untested by the samples.
5. **DEV exposure**: constants in decisions 5 and 2 were chosen against the
   development subset only. The reporting subset has not been scored.
6. `evaluation/usage_report.md` remains empty — M4, and correct for a run with
   zero model calls until that run is the final one.

**Next milestone:** recommend **M3 before M2**. M3 covers ~24% of gold rows
(installments 20%, partial 4%); M2's images cover 16 of 275 requests (~6%). The
forecast is already accurate enough that M3 is mostly candidate generation
against `request_payment_options.csv`, which is fully supplied data.

**Review requested from Codex:** yes. Worth attacking specifically: the intra-day
ordering choice (decision 4), the `investment` exclusion (decision 5), the
two-part recurrence model's claim that every settled debit is counted exactly
once, and whether the two ambiguities in limitation 4 are resolved the way you
read the contract.

---

## M0 cleanup — response to `docs/reviews/M0_CODE_REVIEW.md` (fix-first, 8 findings)

**Status:** R-M0-01 … R-M0-08 all fixed, all reproduced first, all covered by
negative regression tests. Verdict accepted in full; no finding disputed.

**Verification:**

```
$ python -m unittest discover -s code/tests -t code -p "test_*.py"     # 3.10.11
Ran 106 tests in 6.854s
OK
$ py -3.12 -B -m unittest discover -s code/tests -t code -p "test_*.py" # 3.12.5
Ran 106 tests in 7.352s
OK
$ python code/main.py
audit: 0 finding(s), 0 error(s)          (exit 0, 16 media hashes now listed)
```

29 tests added (77 → 106), of which 26 are the new negative fixtures in
`code/tests/test_input_boundary.py`.

| Finding | Fix | File | Regression test |
|---|---|---|---|
| R-M0-01 surplus/truncated CSV rows | `read_rows` sets `restkey`/`restval` sentinels and rejects any row whose width differs, naming file, row and surplus cells | `data.py` `read_rows` | `RowWidthTests` ×4 |
| R-M0-02 image path traversal | `parse_safe_id` rejects non-component ids at load; `safe_media_path` re-validates the id *and* proves `resolved.parent == media_dir`; `DataSet.media_path()` is the single resolver M2 must use | `data.py` | `MediaPathTests` ×3 (incl. `..`, `a/b`, `/etc/passwd`, `C:file`) |
| R-M0-03 cross-user request refs | new audit code **A13**: a message/image whose `request_id` is owned by another user is an error. `request_context` re-checks ownership when admitting request-linked evidence | `audit.py`, `data.py` | `CrossUserReferenceTests` ×4 |
| R-M0-04 load-time rounding | `parse_amount` replaced by `parse_decimal`, which never rounds. `quantize` is now explicitly the *output* policy. New audit **A14** warns if supplied precision exceeds the 2-dp rendering convention | `money.py`, `data.py`, `audit.py` | `RatePrecisionTests` ×5 |
| R-M0-05 media not in manifest | `DataSet.media_manifest` hashes all 16 PNGs, kept distinct from the CSV manifest; printed by the audit CLI | `data.py`, `main.py` | `MediaManifestTests` ×3 |
| R-M0-06 lenient dates | explicit `^\d{4}-\d{2}-\d{2}$` check before `date.fromisoformat` | `data.py` | `StrictDateTests` ×3 |
| R-M0-07 forged request object | `requests_by_id` index; `request_context` requires an exact match with the loaded record; `context_for(request_id)` added as the safe entry point | `data.py` | `ForgedRequestTests` ×4 |
| R-M0-08 suite did not test the boundary | `code/tests/test_input_boundary.py` added | — | the 26 above |

**One correction to the finding as filed (R-M0-06).** It is worse than
"`fromisoformat` is lenient". That function is **strict on Python 3.10 and
lenient on 3.11+**:

```
python 3.10.11 : 20240303 -> rejected      2024-W09-7 -> rejected
py -3.12 3.12.5: 20240303 -> 2024-03-03    2024-W09-7 -> 2024-03-03
```

So the loader's date strictness silently depended on the interpreter, and the
reviewer (on 3.12) and I (on 3.10) were running materially different validation
of the same code. That is a portability defect, not only a strictness one, which
is why the whole suite is now run on both interpreters and that is recorded as
the standing verification command.

**A defect in my own test fixture, surfaced by the new A13 rule.** The "clean"
synthetic dataset had `message_02` (user_01) referencing `sample_01`, which
belongs to user_02 — a cross-user reference sitting inside the fixture that was
supposed to represent a valid dataset. The scoping test it supported was
therefore passing for the wrong reason. Fixed by adding `request_02`, a second
request genuinely owned by user_01, so same-user/different-request scoping is
tested without crossing users. Worth noting because it is exactly the class of
error the review exists to catch: the fixture encoded my assumption rather than
the contract.

**Residual risk:** `A14` is a warning, not an error. If the shipped dataset ever
carries more than two decimals, values are preserved exactly but the output
rendering convention measured from the gold samples would need revisiting before
publication. It does not fire on the current data.

---

## Review response — `docs/AGENT_CONFIG_REVIEW.md` (fix-first, 10 findings)

All ten accepted. Full disposition with evidence:
**`docs/FINDING_TO_CHANGE.md`**.

Summary of what changed:
- One controlling plan (R08). `docs/IMPLEMENTATION_PLAN.md` is authoritative;
  `docs/PLAN.md` is a superseded stub holding only measurements and a
  rejected-rule register. All five agent prompts name the same plan; orchestration
  and shared files are owned by the main session; every agent may write `log.txt`.
- Unresolved **debits** stay in state and block unsupported approval (R01).
- Exact settlement-date FX only, no fallback (R02) — also corrected my own claim
  that rate dates are only the 15th; they are `{1, 15}`.
- Spending actions and claim provenance separated into two axes (R03).
- Effect vocabulary must express cessation and bounded intervals (R04), verified
  against `message_09`.
- C13/R1/R2 withdrawn; replaced by independent plan replay and
  render-from-facts (R05).
- Split moved to M0 and implemented here (R06).
- Content-hash cache keying required (R07).
- `Decimal` throughout (R09) — implemented in `money.py`.
- Baseline and submission-ready separated; `usage_report.md` required (R10).

One qualification returned in the other direction: the August evidence F1 of
0.480 was itself computed on matched held-out pairs, so the low score is a real
measurement of that run even though my proposed *cause* for it was not
established. Both statements travel together; neither is a September result.
