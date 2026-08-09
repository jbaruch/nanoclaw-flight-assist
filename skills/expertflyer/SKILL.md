---
name: expertflyer
description: Check seat availability or fare-class (upgrade) inventory on a specific flight via the operator's ExpertFlyer account, and create an ExpertFlyer alert only when the wanted thing is not already available. Use when the operator asks whether a seat is open, whether upgrade space exists (Z class, upgrade certificate, SkyTeam partner), asks to be alerted when a seat or fare class opens up, or names a flight and asks about Comfort+ / business availability.
---

# ExpertFlyer

This skill is an action router — pick the step that matches the operator's intent and execute only that step. Do not run other steps; do not parallelize.

Every alert request is **check first, alert only if absent**. An alert for something already bookable is worse than useless: it delays the booking while the operator waits for an email describing space they could have taken on the spot. Report the check result either way, so it is visible why no alert was set. Only skip the check when the operator explicitly says to set the alert regardless.

Run the scripts. Do not drive the site yourself and do not re-derive the parsing — seat-state and inventory semantics are easy to get backwards, and `references/web-contract.md` records exactly how.

## Step 1 — Check fare-class (upgrade) inventory

For "is there Z on KL642", "can I use an upgrade certificate", "check business availability".

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/check-fare-class.py \
    --origin JFK --destination AMS --date 2026-08-31 \
    --airline KL --flight 642 --class Z
```

Outputs JSON:
```json
{"flight": "KL642", "class": "Z", "seats": 0, "available": false,
 "display_capped": false, "alternatives": [{"flight": "KL646", "seats": 1}],
 "recommend_alert": true}
```

`seats` is the bucket count; `display_capped` is true at 9, which means *at least* 9. `available` is `seats > 0` — `Z0` means the bucket exists and is empty, which is an answer, not a missing value.

Report the count plainly. When `available` is true, say so and **do not** offer an alert. When it is false, name any `alternatives` on the same route and date, then offer the alert (Step 3). Resolve the operating carrier before querying — inventory lives on the operating flight, not the marketing one.

Finish here unless the operator accepts the alert.

## Step 2 — Check seat availability

For "is there a non-middle seat in Comfort+ on DL2957", "any window left".

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/check-seats.py \
    --airline DL --flight 2957 --date 2026-08-11 --cabin "comfort+" --want aisle,window
```

`--cabin` takes the cabin the operator named — `premium economy`, `comfort+`,
`business`, `first`, `economy` — or a bare code. Premium economy is Delta's
**Premium Select** (`A`) and is a different cabin from Comfort+ (`W`); the
script resolves the name and rejects anything it does not recognise rather than
falling back to economy. `--want non-middle` expands to aisle **and** window.

Outputs JSON:
```json
{"flight": "DL2957", "cabin": "W", "matching": ["13A"],
 "available_total": 2, "recommend_alert": false}
```

`matching` lists free seats meeting the wanted criteria. A seat is free when its **state** is available — grey wing shading, exit-row red, paid, premium and accessible marks are decoration and never mean unavailable (`references/web-contract.md`). Getting this backwards reports a full cabin while a bookable seat sits open.

When `matching` is non-empty, tell the operator which seat is open and **do not** create an alert. When it is empty, offer the alert (Step 3).

Omit `--origin`/`--destination` and the script resolves the route from the flight number itself. Finish here unless the operator accepts the alert.

## Step 3 — Create an alert

Only after Step 1 or Step 2 reported the wanted thing absent, or the operator explicitly asked for the alert regardless.

```bash
# Fare-class alert
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/create-alert.py \
    --kind fare-class --airline KL --flight 642 --date 2026-08-31 \
    --origin JFK --destination AMS --class Z

# Seat alert (any cabin — name it as the operator did)
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/create-alert.py \
    --kind seat --airline DL --flight 2957 --date 2026-08-11 \
    --cabin "premium economy" --want aisle,window
```

Outputs `{"created": true, "alert_name": "...", "verified_in_my_alerts": true}`.

The script re-checks availability and refuses to create a redundant alert unless `--force` is passed; a refusal exits 0 with `{"created": false, "reason": "already_available", ...}`. Relay that reason rather than retrying with `--force`.

`verified_in_my_alerts` comes from re-reading the operator's alert list — report a `false` there as a failure, not a success. Finish here.

## Step 4 — Diagnose access

Run when any step above reports `{"error": "auth"}` or `{"error": "blocked"}`.

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/check-access.py
```

`{"error": "auth"}` means the ExpertFlyer session expired (they last ~7 days) and needs re-establishing. `{"error": "blocked"}` means the bot wall rejected the request — never retry it in a loop; report it. The two are distinguishable only by the script, because an unauthenticated request and a blocked one both surface as HTTP 403 to a naive caller. Report the diagnostic verbatim and finish here.
