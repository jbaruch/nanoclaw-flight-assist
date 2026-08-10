---
name: expertflyer
description: Check seat availability or fare-class (upgrade) inventory on a flight, review seats across every upcoming flight at once, or create an ExpertFlyer alert when the wanted thing is not already available. Actions - check fare-class/upgrade inventory (Z class, upgrade certificate, SkyTeam partner); check seats on one flight; review upcoming flights for better seats; create a seat or fare-class alert; diagnose access. Use when the operator asks whether a seat is open, asks about Comfort+ / premium economy / business availability, says make sure I have the best seats or check my upcoming flights for better seats, asks to be alerted when a seat or fare class opens up, or a new booking has just appeared.
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

Report the count plainly. When `available` is true, say so and **do not** offer an alert. When false, name any `alternatives` and offer the alert (Step 4).

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

1. `cabin_present` is false — the aircraft has no such cabin. Say so. Offer no alert; a cabin that does not exist can never open.
2. `best` is set — name it and say it is open. Offer no alert.
3. `best` is `null` — nothing in the cabin is worth taking, whatever `available_total` says. Offer the alert (Step 4).

Ranking rules live in `skills/expertflyer/scripts/seat_quality.py`.

Finish here unless the operator accepts the alert.

## Step 3 — Review seats on upcoming flights

For "make sure I have the best seats", or after a new booking appears.

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/upcoming-flights.py \
    --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Outputs `{"flights": [{airline, flight, origin, destination, date, departs_utc, summary, uid}], "count": N}`, soonest first, already filtered to those far enough out to act on. The lead window is a named constant in `skills/expertflyer/scripts/upcoming-flights.py`.

Then run Step 2 once per flight, passing its `airline`, `flight`, `date`, `origin` and `destination`, with the cabin the operator flies (`comfort+` unless they say otherwise). Apply Step 2's decision rules per flight and report only the flights that need something:

- `best` set → name the seat and say it is open, so the operator can take it in the airline's app
- `best` null and `cabin_present` true → offer the alert (Step 4)
- `cabin_present` false → skip the flight silently

Say nothing about a flight whose cabin is absent from the aircraft. Every other flight gets one of the two lines above.

Pass `--date-fallback` on these schedule-derived calls: the schedule stamps UTC, so a late local departure lands on the next UTC day. The client then retries the previous day itself and reports `date_fallback_applied` when it did. Do not pass it in Step 2, where the operator named the date.

Finish here unless the operator accepts an alert.

## Step 4 — Create an alert

Only after Step 1, 2 or 3 reported the wanted thing absent, or the operator explicitly asked for the alert regardless.

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

Route is required here; Steps 1, 2 and 3 all report it. Outputs `{"created": true, "alert_id": ..., "status": "ACTIVE", "verified_in_account": true}`.

The service refuses to duplicate an active alert of the same kind on the same flight and class, returning `{"created": false, "reason": "already_exists", "alert_id": ...}`. A seat alert and a fare-class alert on one flight are different watches, so having one never blocks the other. Relay the refusal; do not retry with `--force` unless the operator asks.

`verified_in_account` comes from the service re-reading the account after submitting. Report a failure there as **not created**, never as success.

Finish here.

## Step 5 — Diagnose access

Run when any step above reports an `error` field.

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py alerts
```

The `error` value names the fault and is not re-derivable here — relay it verbatim:

- `unreachable` — the service is down or `EXPERTFLYER_API_URL` is wrong. Nothing to retry until it is up.
- `auth` — the service could not authenticate; its `detail` carries ExpertFlyer's own message. It re-tries a login itself before reporting, so this means the credentials in the service need attention.
- `blocked` — ExpertFlyer's bot wall rejected the request. Never retry it in a loop.

Finish here.
