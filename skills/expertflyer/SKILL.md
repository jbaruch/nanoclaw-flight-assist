---
name: expertflyer
description: Check seat availability or fare-class (upgrade) inventory on a specific flight via the operator's ExpertFlyer account, and create an ExpertFlyer seat alert only when the wanted thing is not already available. Use when the operator asks whether a seat is open, whether upgrade space exists (Z class, upgrade certificate, SkyTeam partner), asks to be alerted when a seat opens up, or names a flight and asks about Comfort+ / premium economy / business availability.
---

# ExpertFlyer

This skill is an action router — pick the step that matches the operator's intent and execute only that step. Do not run other steps; do not parallelize.

Every alert request is **check first, alert only if absent**. An alert for something already bookable is worse than useless: it delays the booking while the operator waits for an email describing space they could have taken on the spot. Report the check result either way, so it is visible why no alert was set. Only skip the check when the operator explicitly says to set the alert regardless.

Run the scripts. Do not drive the site yourself — the pages ship structured payloads that the scripts read, and scraping the rendered text invents flights that do not exist. Reference contract: `references/web-contract.md`.

Both `EXPERTFLYER_STORAGE_STATE` (path to the captured session) and `FIFTY_TABS_SRC` (the stealth layer) must be set; without stealth every request returns 403.

## Step 1 — Check fare-class (upgrade) inventory

For "is there Z on KL642", "can I use an upgrade certificate", "check business availability".

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/check-fare-class.py \
    --origin JFK --destination AMS --date 2026-08-31 \
    --airline KL --flight 642 --class Z
```

Outputs JSON: `flight`, `seats`, `available`, `display_capped`, `alternatives` (other flights that day with space), `recommend_alert`.

`seats: 0` means the bucket exists and is empty — an answer, not a missing value. `display_capped: true` means *at least* that many. Codeshares are excluded by default because inventory lives on the operating carrier; pass `--include-codeshares` to see them.

Report the count plainly. When `available` is true, say so and **do not** offer an alert. When false, name the `alternatives` and tell the operator ExpertFlyer cannot watch a fare class from this skill yet (Step 3 covers seat alerts only) — they can set a Quick Alert by hand at `/alerts/create/quick-alert`.

Finish here.

## Step 2 — Check seat availability

For "is there a non-middle seat in Comfort+ on DL2957", "any window left in premium economy".

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/check-seats.py \
    --airline DL --flight 2957 --date 2026-08-11 \
    --cabin "comfort+" --want non-middle
```

`--cabin` takes the cabin the operator named — `premium economy`, `comfort+`, `business`, `first`, `economy` — or a bare code. Premium economy is Delta's **Premium Select** (`A`) and is a different cabin from Comfort+ (`W`); the script rejects an unrecognised cabin rather than falling back to economy. `--want` accepts `non-middle` (expands to aisle and window), `aisle,window`, `middle`, or `any`. `--origin`/`--destination` are optional — omit them and the route is resolved from the flight number.

Outputs `matching` (free seats meeting the criteria), `available_total`, `seats_in_cabin`, `cabin_present`, `recommend_alert`.

When `matching` is non-empty, tell the operator which seat is open and **do not** create an alert. When `cabin_present` is false the aircraft has no such cabin — say that; do not offer an alert for a cabin that can never open. Otherwise offer the alert (Step 3).

Finish here unless the operator accepts the alert.

## Step 3 — Create a seat alert

Only after Step 2 reported nothing matching, or the operator explicitly asked for the alert regardless.

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/create-alert.py \
    --kind seat --airline DL --flight 2957 --date 2026-08-11 \
    --origin ATL --destination YYZ --cabin "comfort+" --want non-middle
```

Route is required here (Step 2 reports it). Outputs `{"created": true, "alert_id": ..., "criteria": ["AISLE","WINDOW"], "verified_in_account": true}`.

The script refuses to duplicate an existing active alert for the same flight and cabin, exiting 0 with `{"created": false, "reason": "already_exists", "alert_id": ...}`. Relay that reason; do not retry with `--force` unless the operator asks.

`verified_in_account` comes from re-reading the account's alert objects after submitting. Report a failure there as **not created**, never as success. Fare-class alerts are not wired — `--kind fare-class` exits with an error rather than pretending.

Finish here.

## Step 4 — Diagnose access

Run when any step above reports `{"error": "auth"}` or `{"error": "blocked"}`.

```bash
python3 /home/node/.claude/skills/tessl__expertflyer/scripts/check-access.py
```

`{"error": "auth"}` means the session expired — they last ~7 days and must be re-captured by logging in with a headed browser and saving the context's `storage_state`. `{"error": "blocked"}` means the bot wall rejected the request; never retry it in a loop, report it. The two are indistinguishable by status code, which is why this script exists. Report the diagnostic verbatim and finish here.
