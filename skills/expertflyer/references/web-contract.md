# Reading the ExpertFlyer API responses

The service (`jbaruch/expertflyer-api`) owns the browser automation, the
credential, the session, and every rule about parsing ExpertFlyer's pages. Its
`docs/web-contract.md` is the authority for all of that; nothing here restates
it. This file covers only what an agent needs to read an answer correctly.

## Availability is a number, not a presence check

`seats: 0` means the fare bucket exists and is empty — a real answer. Do not
report it as "no data" or "unknown".

`display_capped: true` means ExpertFlyer stopped counting at 9, so the true
figure is **at least** that many. Say "at least 9", never "9".

Inventory lives on the **operating** carrier. The service excludes codeshares by
default for exactly that reason; a codeshare row would report the marketing
carrier's flight number against the operating carrier's bucket.

## Cabins are named, and two of them sound alike

| Operator says | Code |
|---|---|
| first | `F` |
| business, Delta One | `C` |
| **premium economy**, premium select | `A` |
| comfort+, comfort plus, economy comfort | `W` |
| economy, main cabin, coach | `Y` |

Premium economy is `A`, **not** `W`. Pass the words the operator used; the
service resolves them and rejects anything it does not recognise rather than
defaulting to economy.

## cabin_present distinguishes "full" from "not on this aircraft"

A cabin the aircraft does not have returns an empty seat map, which looks
identical to a full one. `cabin_present: false` means the aircraft has no such
cabin — report that, and never offer an alert for it, because it can never
open. `recommend_alert` already accounts for this.

## Errors name a fault the agent cannot re-derive

- `unreachable` — the service is down or `EXPERTFLYER_API_URL` is wrong.
- `auth` — the service could not authenticate. It retries a login itself before
  reporting, so this means its credentials need attention.
- `blocked` — ExpertFlyer's bot wall. Never retry in a loop.

Upstream returns 403 for both a bot-walled request and an unauthenticated one,
so only the service can tell `auth` from `blocked`. Relay its verdict.
