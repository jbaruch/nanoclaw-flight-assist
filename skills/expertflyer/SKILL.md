---
name: expertflyer
description: Check seat availability or fare-class (upgrade) inventory on a flight, judge whether the seat already assigned is beaten by anything open, review seats across every upcoming flight at once, or create an ExpertFlyer alert when the wanted thing is not already available. Actions - check fare-class/upgrade inventory (Z class, upgrade certificate, SkyTeam partner); check seats on one flight; judge one held seat against everything open in its cabin and the cabins above it; review upcoming flights for better seats; create a seat or fare-class alert; diagnose access. Use when the operator asks whether a seat is open, asks about Comfort+ / premium economy / business availability, asks whether their seat is the best available or worth changing, says make sure I have the best seats or check my upcoming flights for better seats, asks to be alerted when a seat or fare class opens up, or a new booking has just appeared.
---

# ExpertFlyer

This skill is an action router — pick the step that matches the operator's intent and execute only that step. Do not run other steps; do not parallelize.

Every alert request is **check first, alert only if absent**. An alert for something already bookable is worse than useless: it delays the booking while the operator waits for an email describing space they could have taken on the spot. Report the check result either way, so it is visible why no alert was set. Only skip the check when the operator explicitly says to set the alert regardless.

The browser automation, the ExpertFlyer credential and the session live in the `jbaruch/expertflyer-api` service. This container holds none of them — every step below is one HTTP call through `skills/expertflyer/scripts/expertflyer.py`, which reads `EXPERTFLYER_API_URL` and `EXPERTFLYER_API_TOKEN`.

## Step 1 — Check fare-class (upgrade) inventory

For "is there Z on KL642", "can I use an upgrade certificate", "check business availability".

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py fare-class \
    --origin JFK --destination AMS --date 2026-08-31 \
    --airline KL --flight 642 --class Z
```

Outputs `flight`, `seats`, `available`, `display_capped`, `alternatives` (other flights that day with space), `recommend_alert`.

`seats: 0` means the bucket exists and is empty — an answer, not a missing value. `display_capped: true` means *at least* that many. Codeshares are excluded by default because inventory lives on the operating carrier; pass `--include-codeshares` to see them.

Report the count plainly. When `available` is true, say so and **do not** offer an alert. When false, name any `alternatives` and offer the alert (Step 5).

Finish here unless the operator accepts the alert.

## Step 2 — Check seat availability

For "is there a non-middle seat in Comfort+ on DL2957", "any window left in premium economy".

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py seats \
    --airline DL --flight 2957 --date 2026-08-11 \
    --cabin "comfort+" --want non-middle
```

`--cabin` takes the cabin the operator named — `premium economy`, `comfort+`, `business`, `first`, `economy` — or a bare code. Premium economy is Delta's **Premium Select** (`A`) and is a different cabin from Comfort+ (`W`); the service rejects an unrecognised cabin rather than falling back to economy. `--want` accepts `non-middle` (aisle and window), `aisle,window`, `middle`, or `any`. `--origin`/`--destination` are optional — omit them and the route is resolved from the flight number.

Outputs `cabin_present`, `seats_in_cabin`, `available_total`, `recommend_alert`, the service's own criteria filter `matching`, and three fields the client adds by ranking the response:

- `ranked` — bookable seats worth taking, best first, each with a `why` such as `12A (window)`
- `best` — the top seat's description, or `null` when nothing is worth taking
- `acceptable_total` — how many seats are worth taking

Decide on `best` and `acceptable_total`, never on `matching`. Ranking drops seats the operator will not take, so `matching` can list a seat that `ranked` excludes — a middle is reported by the service and refused by the ranking. Treat `matching` as informational only.

A response carrying `error` has no `best`, `ranked` or `acceptable_total`. Absent is not `null`. Go to Step 6.

On every other response:

1. `cabin_present` is false — the aircraft has no such cabin. Say so. Offer no alert.
2. `best` is set — name it and say it is open. Offer no alert.
3. `best` is `null` — nothing in the cabin is worth taking, whatever `available_total` says. Offer the alert (Step 5).

Ranking rules live in `skills/expertflyer/scripts/seat_quality.py`.

Finish here unless the operator accepts the alert.

## Step 3 — Judge one held seat

For "is 21F the best I can do on DL2957".

Step 2 answers what is open. It does not answer whether any of it beats the seat already assigned, and those are different questions. This one runs the comparison in the script, so no seat is judged by eye.

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py assess \
    --airline DL --flight 2957 --date 2026-08-11 \
    --held 21F --held-cabin "comfort+" --held-position window
```

`--held` is the seat currently assigned. `--held-cabin` is the cabin it is in. `--held-position` is `window`, `aisle` or `middle`; omit it and the column is read off the open seats in the same cabin. `--scan-up` sets how many cabins above the held one to include — the default and its cost are in `skills/expertflyer/scripts/expertflyer.py`.

Get the held seat from byAir before calling this. It lives on the flight's booking info as `seat_number` and `seat_type`, written by `byair_update_booking_info`. When byAir has no seat for the flight, ask the operator for it, write it back to byAir, then call this. Never infer the seat from a previous conversation.

Outputs `verdict`, `held` (with `why` and `position_source`), `upgrades`, `best_upgrade`, `alert_recommended`, `cabins_scanned`, `cabins_absent`, `cabins_unscanned` and `seats_compared`.

`cabins_scanned` is the whole evidence base and `seats_compared` is its size. `cabins_unscanned` lists the cabins above the sweep that were never read; widen it with `--scan-up`.

Report `verdict` as it comes. Do not re-derive it from `upgrades`:

**`optimal`** — nothing open in `cabins_scanned` beats the held seat.

- Say so, naming the cabins in `cabins_scanned`.
- Name `held.why`.
- Name `cabins_unscanned` as not checked, when it is non-empty.
- Offer the alert (Step 5) on the cabins in `cabins_scanned`.

Never report `optimal` as "nothing better exists". The sweep reads `cabins_scanned` and stops. A cabin in `cabins_unscanned` may hold a better seat and was never looked at.

**`upgrade`** — something open beats it.

- Name `best_upgrade`.
- Offer no alert.

**`no_held_seat`** — no seat was passed.

- Get it from byAir, or from the operator.
- Report nothing about seat quality.

**`held_position_unknown`** — the seat map does not say what the column is.

- Ask the operator whether the seat is a window, an aisle or a middle.
- Pass the answer as `--held-position`.

**`nothing_open`** — no open seat was found in any scanned cabin, so nothing was compared.

- Say no seat is open to move to, better or worse.
- Never report this as the held seat being best.
- Offer to widen the sweep with `--scan-up`.
- Check the cabin: a sold-out cabin and a seat that is not in that cabin look identical here.

**`held_cabin_mismatch`** — the held seat's row appears in another scanned cabin and nowhere in the one it was assessed as.

- Relay `detail`. It names the cabin the row was actually seen in.
- Confirm the cabin with the operator.
- Re-run with the corrected `--held-cabin`.

**`error`** — go to Step 6.

- `cabin_failed` names the cabin that did not load.

`no_held_seat`, `held_position_unknown`, `nothing_open` and `held_cabin_mismatch` exit non-zero. None is an answer about the seat. Report no verdict on any of them.

Ranking rules live in `skills/expertflyer/scripts/seat_quality.py`.

Finish here unless the operator accepts the alert.

## Step 4 — Review seats on upcoming flights

For "make sure I have the best seats", or after a new booking appears.

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/upcoming-flights.py \
    --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Outputs `{"flights": [{airline, flight, origin, destination, date, departs_utc, summary, uid}], "count": N}`, soonest first, already filtered to those far enough out to act on. The lead window is a named constant in `skills/expertflyer/scripts/upcoming-flights.py`.

Collect the held seat for every flight in one exchange before assessing any of them. Read each from byAir. Ask the operator once, in a single message, for every flight byAir has no seat for. Write each answer back to byAir.

Then run Step 3 once per flight, adding `--date-fallback`. Read `date_fallback_applied` to see which date answered. Do not pass it in Step 3 for a date the operator named.

Report only the flights that need something:

- `upgrade` → name the flight and `best_upgrade`
- `optimal` → one line that the seat holds up across `cabins_scanned`, or nothing when the operator asked only for problems
- `no_held_seat`, `held_position_unknown`, `nothing_open` or `held_cabin_mismatch` → name the flight as unanswered, never as fine
- `cabins_absent` covering the held cabin → report it; a seat cannot be in a cabin the aircraft lacks

A flight whose verdict never came back is not a flight with good seats. Say which ones were not answered.

Finish here unless the operator accepts an alert.

## Step 5 — Create an alert

Only after Step 1, 2, 3 or 4 reported the wanted thing absent, or the operator explicitly asked for the alert regardless.

```bash
# Seat alert — needs --cabin and --want
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py create-alert \
    --kind seat --airline DL --flight 2957 --date 2026-08-11 \
    --origin ATL --destination YYZ --cabin "comfort+" --want non-middle

# Fare-class alert — needs --class
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py create-alert \
    --kind fare-class --airline KL --flight 642 --date 2026-08-31 \
    --origin JFK --destination AMS --class Z
```

Route is required here. Steps 1 to 4 all report it. Outputs `{"created": true, "alert_id": ..., "status": "ACTIVE", "verified_in_account": true}`.

The service refuses to duplicate an active alert of the same kind on the same flight and class, returning `{"created": false, "reason": "already_exists", "alert_id": ...}`. A seat alert and a fare-class alert on one flight are different watches, so having one never blocks the other. Relay the refusal; do not retry with `--force` unless the operator asks.

`verified_in_account` comes from the service re-reading the account after submitting. Report a failure there as **not created**, never as success.

Finish here.

## Step 6 — Diagnose access

Run when a step above reports an `error` field.

`unrankable` is **not** an access fault. On it:

- Relay `detail`. It names the seat the ranking refused.
- Offer no alert.
- Do not run the command below.
- Finish here.

On every other value:

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py alerts
```

The `error` value names the fault and is not re-derivable here — relay it verbatim:

- `unreachable` — the service is down or `EXPERTFLYER_API_URL` is wrong. Nothing to retry until it is up.
- `tls` — the service answered but its certificate could not be verified. The endpoint is fine; the trust store is not. The detail names the fix.
- `auth` — the service could not authenticate; its `detail` carries ExpertFlyer's own message. It re-tries a login itself before reporting, so this means the credentials in the service need attention.
- `blocked` — ExpertFlyer's bot wall rejected the request. Never retry it in a loop.

Finish here.
