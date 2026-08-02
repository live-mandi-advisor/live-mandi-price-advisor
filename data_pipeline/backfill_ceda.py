"""
One-time historical backfill from CEDA Agri Market Data
(agmarknet.ceda.ashoka.edu.in) — see docs/build-plan.md, Section 3.3.

Run ONCE per scoped state to seed enough history for Prophet to learn
weekly/yearly seasonality, before daily live collection takes over.

TODO:
- Confirm CEDA's actual export format (CSV structure/columns) against
  the live Agmarknet API's schema before writing the real parser
- Load into the same state-partitioned tables used by fetch_agmarknet.py
"""

def backfill_state(state: str):
    raise NotImplementedError("Phase 1 — implement CEDA historical backfill")


if __name__ == "__main__":
    pass
