"""
Data-driven selection of the top 6 commodities per state — see
docs/build-plan.md, Section 3.3.1.

Selection is based on actual historical reporting CONSISTENCY
(how many markets/days a commodity appears across), not general
knowledge or assumption. Output is stored in a state_top_crops table,
consumed by the /states/{state}/top-crops backend endpoint.

TODO: implement frequency analysis over the backfilled historical data
"""
