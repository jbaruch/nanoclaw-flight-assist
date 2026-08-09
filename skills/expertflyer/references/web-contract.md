# ExpertFlyer web contract

Everything here was verified against the live account on 2026-08-09. ExpertFlyer
publishes no API; this is browser automation against the authenticated web UI.

## Seat-map legend — availability is state, not shading

The legend carries two **orthogonal** axes. Conflating them reports a full cabin
when seats are free.

| Axis | Values |
|---|---|
| **State** (decides availability) | Available `·` · Occupied (person icon) · Blocked `✕` |
| **Decoration** (never implies unavailable) | Wing (grey), Exit Row (red), Paid `$`, Premium (shield), Paid Premium (star), Accessible, Selected (blue outline), Highlighted (orange outline) |

A grey wing-shaded cell with no person icon is **available**. Observed on
DL2957 ATL–YYZ 2026-08-11: 13B and 14B rendered grey and were open on
delta.com, while every aisle and window in Comfort+ was taken. Classify each
cell on the state axis only.

Middle seats are the columns between the aisles — on a 3-3 narrowbody, B and E.
"Non-middle" means aisle **or** window, which on the alert form is two separate
criteria, not one control.

## Fare-class inventory

Buckets render as `LETTER + DIGITS`, e.g. `Z4`, `J9`, `C0`.

- `Z0` — the bucket exists and is empty. An answer, not a missing value.
- `Z9` — display caps at 9, so this means **at least** 9.
- Inventory lives on the **operating** carrier. Resolve marketing → operating
  before querying, or a codeshare reads the wrong bucket. Same split byAir has
  between `list_trips` (marketing) and `get_flight` (operating).

Cabin codes, as the seat-map form exposes them:

| Code | Form label | Operator says |
|---|---|---|
| `F` | First | first, first class |
| `C` | Business | business, Delta One |
| `A` | Premium Select | **premium economy**, premium select, premium |
| `W` | Economy - Comfort Plus | comfort+, comfort plus, economy comfort |
| `Y` | Economy | economy, main cabin, coach |

Premium economy is `A`, **not** `W`. They are adjacent cabins with similar-sounding
names, and searching the wrong one returns seats the operator cannot book.
`cabin_code()` in `scripts/expertflyer_parse.py` owns the alias table and raises
on anything unrecognised rather than defaulting.

## Access

Stealth is **mandatory**. Vanilla headless Playwright returns HTTP 403 on every
expertflyer.com request, including unauthenticated ones — so a 403 says nothing
about session validity, and auth failure must be distinguished from the bot wall
by what the page renders, not by status code. `create_stealth_context()` from
`jbaruch/fifty-tabs-of-fares` (`src/fifty_tabs/browser.py`) clears the wall
unmodified; a blocked request is never retried in a loop.

Auth is Auth0 OIDC + PKCE at `auth.expertflyer.com`; the password posts to
`/usernamepassword/login`. The session is `__session__0` (~3500 B) plus
`__session__1` (~1208 B), httpOnly/Secure/SameSite=Lax, expiring in **~7 days**.
A captured `storage_state` replays cleanly into a fresh headless context.

OneCLI cannot carry this credential: its generic-secret `injectionConfig`
supports header, query param and URL path only — no request-body injection — so
the login POST can never be gateway-mediated.

## Results URLs are fully parameterized

Navigate straight to these; do not drive the search forms.

```
/air/availability/results?origin=&destination=&departureDateTime=YYYY-MM-DDT00:00
    &alliance=none&airLineCodes=&excludeCodeshares=&classFilter=
    &pcc=USA (Default)&resultsDisplay=tabbed

/air/status/results?departureDateTime=YYYY-MM-DD&airlineCode=&flightNumber=

/air/seat-map/results?departingAirport=&arrivingAirport=&departDate=YYYY-MM-DD
    &airline=&flightNumber=&paxID=passenger1&ptc=ADT&withRawXML=false&cabinClass=
```

A flight-number-only request ("DL2957 on the 11th") needs a
`/air/status/results` hop first — availability and seat map both require a city
pair. That hop resolves the route and the scheduled times.

## Structured payloads beat the rendered page

Every results page ships its data as JSON inside the Next.js RSC payload. Read
that; the rendered text is a trap.

| Data | Key in payload | Shape |
|---|---|---|
| Seat map | `seatMap` | `sections[].rows[].seats[]` with `status`, `isWindow`/`isAisle`/`isMiddle`, `type` (`seat` or `aisle` gap) |
| Fare-class inventory | `bookingClassAvailability` | `[{code, cabin, availability, hasAvailability}]`, preceded by `marketingAirlineCode` / `operatingAirlineCode` / `flightNumber` / `departureAirport` / `airEquipType` |
| Account alerts | array starting `[{"alertType"` | `id`, `status`, `airlineCode`, `flightNumber`, `classCode`, `seatMapLocations`, `name` |

Seat statuses observed: `available`, `occupied`. Non-seat cells have
`type: "aisle"` and no `status`.

Text scraping the availability grid pairs an aircraft code with the `AM`/`PM`
of a departure time and invents flights like `PM787`. Parse the payload with a
real JSON decoder — hand-rolled brace counting desynchronises on punctuation
inside an alert name.

## Alert creation and verification

The seat-alert panel opens from the **Seat Alert** button on the seat-map
results page. Its criteria checkboxes carry stable `value` attributes:

| value | Label | Note |
|---|---|---|
| `ANY` | Any Seat | disabled on a single-cabin search |
| `AISLE` | Any Aisle Seats | |
| `WINDOW` | Any Window Seats | |
| `EXIT` | Any Exit Row Seats | disabled when the cabin has no exit row |
| `TWO_TOGETHER` | Any 2 Seats Together | |

There is no middle-seat criterion — a middle is searchable but not alertable.
`#alertName` prefills (e.g. `ATL to YYZ DL2957 8.11.26`) and **Create Alert**
stays disabled until a criterion is ticked. Success shows a
"Seat Alert created successfully!" toast.

**Verify against the payload, never the alerts page text.** `/alerts` defaults
to the **Flight Alerts** tab, which renders "No alerts found" even when seat
alerts exist — reading that text reports a successful creation as a failure.
Deleting uses the per-row `button[title="Delete Alert"]`, whose confirmation is
a fixed overlay (`div.fixed.inset-0.z-50`) that intercepts clicks on anything
behind it, so the confirm button must be located inside the overlay.

## DOM gotchas

Only the alert-creation form still needs driving; these apply there.

- **Ids are stable but not unique.** Every tab panel renders at once, so
  `#flightNumber` and `#seatOptions-N` resolve 4–10 times. Scope with `:visible`.
- **React checkboxes ignore Playwright `.check()`.** React state stays untouched
  and the value silently serializes `false` — this is how `excludeCodeshares`
  went out wrong. Click, then assert `is_checked()`.
- **Autocomplete first-option is nondeterministic.** "Delta" resolved to
  `Delta (DL)` on one run and `DELTA` on the next. Pin the option by matching
  the parenthesised IATA code.
- **Date format differs per page.** `/air/status` validates `mm/dd/yy`.
- **Submit must be `button[type=submit]:visible`.** A `name=/search/i` match
  also hits nav and OneTrust consent buttons, which silently do nothing.
- The alert form's criteria checkboxes have no associated `<label>`; the
  ordering observed was `seatOptions-0` Any Seat, `-1` Any Aisle, `-2` Any
  Window, `-3` Any Exit Row, `-4` Any 2 Together, with 0 and 3 disabled on a
  single-cabin search. **Create Alert** stays disabled until a criterion is
  ticked. Verify the mapping at runtime rather than trusting the index.
