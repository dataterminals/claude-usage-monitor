"""Pricing table + per-record cost math.

Rates live in pricing.json next to this file. Costs are NOTIONAL on a
Max/Pro subscription (equivalent-API dollars) — an intensity gauge, not a bill.
"""
import json
import os
import re
import sys

# When frozen by PyInstaller, bundled data files live under sys._MEIPASS.
_BASE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_BASE, "pricing.json")

with open(_PATH, encoding="utf-8") as _f:
    RATES = json.load(_f)

_DEFAULT = RATES.get("_default", {
    "input": 5.0, "output": 25.0,
    "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5,
})


# Transcripts carry whatever id the request used, and some are dated —
# `claude-haiku-4-5-20251001` next to a bare `claude-sonnet-5`. An exact-match
# lookup misses the dated one and silently bills it at _default's Opus rates,
# which is what made Haiku read 5x its real cost. Strip the suffix and retry.
_DATED = re.compile(r"-\d{8}$")


def rates_for(model):
    """Return the rate dict for a model, falling back to _default.

    Matches the id exactly, then with a trailing -YYYYMMDD removed.
    """
    if not isinstance(model, str):
        return _DEFAULT
    r = RATES.get(model)
    if not isinstance(r, dict):
        r = RATES.get(_DATED.sub("", model))
    return r if isinstance(r, dict) else _DEFAULT


def cost_for_record(model, tokens):
    """USD cost for one assistant message given its token breakdown."""
    r = rates_for(model)
    return (
        tokens["input"] * r["input"]
        + tokens["output"] * r["output"]
        + tokens["cache_read"] * r["cache_read"]
        + tokens["cache_write_5m"] * r["cache_write_5m"]
        + tokens["cache_write_1h"] * r["cache_write_1h"]
    ) / 1_000_000.0
