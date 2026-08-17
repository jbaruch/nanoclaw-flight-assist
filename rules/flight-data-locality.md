---
alwaysApply: true
---

# Flight Data Locality

## Single Upstream

- byAir is the source of truth for flight status, gate, delay, baggage carousel, and inbound aircraft chain
- A second flight-data API is forbidden. AeroAPI, Flighty, FlightAware direct, and airline-specific APIs do not enter the plugin
- Missing fields are reported upstream or descoped. A second API is not the remedy

## Out of Scope

- Maps and traffic data live on a separate axis. Google Maps Distance Matrix is the source for time-to-leave
- Calendar and TripIt are the source-of-record for which flights exist
- byAir is the source-of-record for what those flights are doing now

## Travel Already Booked or Flown

- `jbaruch/tripit-api` is the source-of-record for travel history: past trips, lodging stays, confirmation numbers, costs, loyalty balances
- Its `using-tripit` skill reaches the service over `TRIPIT_API_URL` / `TRIPIT_API_TOKEN`
- The plugin loads as a co-loaded overlay tile
- The plugin is never vendored here
- The TripIt iCal feed carries a rolling ~90-day window and no confirmation numbers
- The feed stays the input to `travel-schedule.json` and `travel-db.json` for upcoming travel
- Route a history question to `using-tripit`, never to a hand-parsed `.ics`
- A second history source is forbidden on the same terms as a second flight-data API

## How to Apply

- New flight-data integration request starts with: "does byAir already expose this?"
- A PR adding a second flight upstream is `REQUEST_CHANGES` by default
- The PR description must name the specific byAir gap that justifies the addition
- Existing surfaces (precheck script, MCP client, state files) stay byAir-only
