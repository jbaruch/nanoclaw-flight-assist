---
name: expertflyer
description: Check seat availability or fare-class (upgrade) inventory on a specific flight via the operator's ExpertFlyer account, and create an ExpertFlyer alert only when the wanted thing is not already available. Use when the operator asks whether a seat is open, whether upgrade space exists (Z class, upgrade certificate, SkyTeam partner), asks to be alerted when a seat or fare class opens up, or names a flight and asks about Comfort+ / premium economy / business availability.
---

# ExpertFlyer

This skill is an action router — pick the step that matches the operator's intent and execute only that step. Do not run other steps; do not parallelize.

Every alert request is **check first, alert only if absent**. An alert for something already bookable is worse than useless: it delays the booking while the operator waits for an email describing space they could have taken on the spot. Report the check result either way, so it is visible why no alert was set. Only skip the check when the operator explicitly says to set the alert regardless.

The browser automation, the ExpertFlyer credential and the session live in the `jbaruch/expertflyer-api` service. This container holds none of them — every step below is one HTTP call through `scripts/expertflyer.py`, which reads `EXPERTFLYER_API_URL` and `EXPERTFLYER_API_TOKEN`.

## Step 1 — Check fare-class (upgrade) inventory

For "is there Z on KL642", "can I use an upgrade certificate", "check business availability".

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py fare-class \
    --origin JFK --destination AMS --date 2026-08-31 \
    --airline KL --flight 642 --class Z
```

Outputs `flight`, `seats`, `available`, `display_capped`, `alternatives` (other flights that day with space), `recommend_alert`.

`seats: 0` means the bucket exists and is empty — an answer, not a missing value. `display_capped: true` means *at least* that many. Codeshares are excluded by default because inventory lives on the operating carrier; pass `--include-codeshares` to see them.

Report the count plainly. When `available` is true, say so and **do not** offer an alert. When false, name any `alternatives` and offer the alert (Step 3).

Finish here unless the operator accepts the alert.

## Step 2 — Check seat availability

For "is there a non-middle seat in Comfort+ on DL2957", "any window left in premium economy".

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py seats \
    --airline DL --flight 2957 --date 2026-08-11 \
    --cabin "comfort+" --want non-middle
```

`--cabin` takes the cabin the operator named — `premium economy`, `comfort+`, `business`, `first`, `economy` — or a bare code. Premium economy is Delta's **Premium Select** (`A`) and is a different cabin from Comfort+ (`W`); the service rejects an unrecognised cabin rather than falling back to economy. `--want` accepts `non-middle` (aisle and window), `aisle,window`, `middle`, or `any`. `--origin`/`--destination` are optional — omit them and the route is resolved from the flight number.

Outputs `matching` (the service's own criteria filter), `available_total`, `seats_in_cabin`, `cabin_present`, `recommend_alert`, plus three fields the client adds by ranking the response against the operator's seat preferences:

- `ranked` — bookable seats, best first, each with a `why` such as `12A (window)`
- `best` — the top seat's description, or `null` when nothing is worth taking
- `acceptable_total` — how many seats are actually worth taking

`acceptable_total` can be `0` while `available_total` is not: a middle is never offered, so a cabin whose only free seats are middles ranks to nothing. Report `best` when it is set; when it is `null` say the cabin has nothing worth moving to rather than listing the middles. Ranking rules live in `scripts/seat_quality.py`.

When `matching` is non-empty, tell the operator which seat is open and **do not** create an alert. When `cabin_present` is false the aircraft has no such cabin — say that; do not offer an alert for a cabin that can never open. Otherwise offer the alert (Step 3).

Finish here unless the operator accepts the alert.

## Step 3 — Create an alert

Only after Step 1 or Step 2 reported the wanted thing absent, or the operator explicitly asked for the alert regardless.

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

Route is required here; Steps 1 and 2 both report it. Outputs `{"created": true, "alert_id": ..., "status": "ACTIVE", "verified_in_account": true}`.

The service refuses to duplicate an active alert of the same kind on the same flight and class, returning `{"created": false, "reason": "already_exists", "alert_id": ...}`. A seat alert and a fare-class alert on one flight are different watches, so having one never blocks the other. Relay the refusal; do not retry with `--force` unless the operator asks.

`verified_in_account` comes from the service re-reading the account after submitting. Report a failure there as **not created**, never as success.

Finish here.

## Step 4 — Diagnose access

Run when any step above reports an `error` field.

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/expertflyer.py alerts
```

The `error` value names the fault and is not re-derivable here — relay it verbatim:

- `unreachable` — the service is down or `EXPERTFLYER_API_URL` is wrong. Nothing to retry until it is up.
- `auth` — the service could not authenticate; its `detail` carries ExpertFlyer's own message. It re-tries a login itself before reporting, so this means the credentials in the service need attention.
- `blocked` — ExpertFlyer's bot wall rejected the request. Never retry it in a loop.

Finish here.
