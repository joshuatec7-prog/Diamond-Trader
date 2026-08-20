#!/usr/bin/env python3
"""
Diamond Trader SELECTIVE Prospective Candidate Tracker v1.0

Doel
----
Vanaf een VASTE baseline alleen NIEUWE gesloten SELECTIVE shadow-trades volgen
en drie kandidaatregels prospectief naast de ongewijzigde CURRENT SELECTIVE
vergelijken:

1. CURRENT_ALL
2. GUARDED_MIX
   - alle LONG
   - SHORT alleen wanneer R/R 1.40-1.59
3. RR_GE_140
   - alle LONG/SHORT met R/R >= 1.40
4. LONG_ALL
   - diagnostische referentie, niet automatisch voorkeur

Eerste run:
- legt alleen baseline vast;
- bestaande historische trades tellen NIET mee in prospectieve score.

Volgende runs:
- gebruiken uitsluitend nieuwe gesloten SELECTIVE trades na die baseline;
- wijzigen niets aan strategie/filter/config/live.

Bron:
  /var/data/diamond_scanner_selective_shadow_trades.csv

State:
  /var/data/diamond_selective_prospective_candidate_state.json

Rapport:
  /var/data/diamond_selective_prospective_candidate_report.json
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

VERSION = "2.0"

DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SOURCE = DATA / "diamond_scanner_selective_shadow_trades.csv"
STATE = DATA / "diamond_selective_binance_1m_state.json"
REPORT = DATA / "diamond_selective_binance_1m_report.json"

MILESTONE = 20

SAFETY = {
    "historical_baseline_excluded": True,
    "orders": False,
    "private_api": False,
    "network": False,
    "strategy_change": False,
    "filter_change": False,
    "config_change": False,
    "stake_change": False,
    "live_change": False,
    "automatic_shadow_change": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def row_key(raw: Dict[str, Any], index: int) -> str:
    key = str(raw.get("candidate_key") or "").strip()
    if key:
        return key

    # Fallback alleen voor uitzonderlijke oude rijen zonder candidate_key.
    return "|".join([
        str(raw.get("symbol") or ""),
        str(raw.get("opened_at") or ""),
        str(raw.get("closed_at") or ""),
        str(index),
    ])


def load_closed_selective(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)

    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "variant",
            "candidate_key",
            "closed_at",
            "side",
            "reward_risk",
            "net_pnl_eur",
            "symbol",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                "CSV mist kolommen: " + ", ".join(sorted(missing))
            )

        for index, raw in enumerate(reader, start=1):
            if str(raw.get("variant") or "").strip().upper() != "SELECTIVE":
                continue
            if not str(raw.get("closed_at") or "").strip():
                continue

            row = dict(raw)
            row["_key"] = row_key(raw, index)
            row["side"] = str(raw.get("side") or "").strip().upper()
            row["reward_risk"] = f(raw.get("reward_risk"))
            row["net_pnl_eur"] = f(raw.get("net_pnl_eur"))
            row["symbol"] = str(raw.get("symbol") or "UNKNOWN")
            rows.append(row)

    return rows


def pf(rows: Iterable[Dict[str, Any]]) -> float | None:
    pnl = [f(row.get("net_pnl_eur")) for row in rows]
    gp = sum(x for x in pnl if x > 0)
    gl = abs(sum(x for x in pnl if x < 0))

    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnl = [f(row.get("net_pnl_eur")) for row in rows]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    value = pf(rows)

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows), 4) if rows else None,
        "pnl_eur": round(sum(pnl), 4),
        "profit_factor": (
            None if value is None
            else math.inf if math.isinf(value)
            else round(value, 4)
        ),
        "average_trade_eur": (
            round(sum(pnl) / len(rows), 4) if rows else None
        ),
        "milestone_target": MILESTONE,
        "milestone_reached": len(rows) >= MILESTONE,
    }


def rr_between(row: Dict[str, Any], low: float, high: float) -> bool:
    rr = f(row.get("reward_risk"))
    return low <= rr < high


def lead_ok(r):
    try:
        events = json.loads(
            Path("/var/data/diamond_binance_1m_lead_state.json").read_text()
        ).get("events", [])

        raw_time = r.get("opened_at") or r.get("detected_at")
        if not raw_time:
            return False

        t = datetime.fromisoformat(
            str(raw_time).replace("Z", "+00:00")
        ).timestamp() * 1000

        side = str(r.get("side") or "").upper()

        # Market Lead-assets zijn referentie-assets.
        # Het SELECTIVE-symbool hoeft dus niet dezelfde munt te zijn.
        return any(
            str(x.get("direction") or "").upper() == side
            and 0 <= t - float(x.get("ts_ms", 0)) <= 120000
            for x in reversed(events)
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False

def rules():
 return [
  ("CURRENT_ALL","Referentie.",lambda r:True),
  ("RR_GE_160","R/R >=1.60.",lambda r:f(r.get("reward_risk"))>=1.60),
  ("RR_GE_160_BINANCE_1M","R/R + Binance.",lambda r:f(r.get("reward_risk"))>=1.60 and lead_ok(r)),
  ("TREND_BREAKOUT","Trend.",lambda r:r.get("strategy")=="trend_breakout"),
  ("TREND_BREAKOUT_BINANCE_1M","Trend + Binance.",lambda r:r.get("strategy")=="trend_breakout" and lead_ok(r)),
 ]

def initialize_state(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    baseline_keys = sorted({str(row["_key"]) for row in rows})

    return {
        "version": VERSION,
        "created_at": now_iso(),
        "baseline_closed_selective": len(rows),
        "baseline_keys": baseline_keys,
        "note": (
            "Alle rijen die bij baseline al gesloten waren zijn uitgesloten "
            "van de prospectieve vergelijking."
        ),
    }


def build_report(
    all_rows: List[Dict[str, Any]],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    baseline_keys = set(str(x) for x in state.get("baseline_keys") or [])
    prospective = [
        row for row in all_rows
        if str(row["_key"]) not in baseline_keys
    ]

    result_rows = []
    for name, description, predicate in rules():
        selected = [row for row in prospective if predicate(row)]
        result_rows.append({
            "rule": name,
            "description": description,
            **summarize(selected),
        })

    return {
        "version": VERSION,
        "generated_at": now_iso(),
        "baseline_created_at": state.get("created_at"),
        "baseline_closed_selective": int(
            state.get("baseline_closed_selective") or 0
        ),
        "prospective_closed_selective": len(prospective),
        "rules": result_rows,
        "safety": SAFETY,
    }


def pf_text(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except Exception:
        return "n/a"
    return "INF" if math.isinf(number) else f"{number:.4f}"


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 96)
    print(f" DIAMOND SELECTIVE PROSPECTIVE CANDIDATE TRACKER v{VERSION}")
    print("=" * 96)
    print(f"Baseline gesloten     : {report['baseline_closed_selective']}")
    print(f"Nieuw prospectief     : {report['prospective_closed_selective']}")

    print("\n=== PROSPECTIEVE VERGELIJKING ===")
    for row in report["rules"]:
        print(
            f"{row['rule']:<18} "
            f"n={row['n']:>2}/{MILESTONE} "
            f"W/L={row['wins']}/{row['losses']} "
            f"PnL=€{row['pnl_eur']:+.4f} "
            f"PF={pf_text(row['profit_factor'])}"
        )

    print("\n=== STATUS ===")
    if report["prospective_closed_selective"] == 0:
        print("Baseline staat. Vanaf nu tellen alleen NIEUWE gesloten SELECTIVE trades.")
    elif report["prospective_closed_selective"] < MILESTONE:
        print(
            f"Doorlopen zonder wijzigingen tot minimaal "
            f"{MILESTONE} nieuwe CURRENT_ALL closes."
        )
    else:
        print("20+ nieuwe CURRENT_ALL closes: kandidaat voor eindreview.")

    print("\n=== VEILIGHEID ===")
    print("Historische baseline uitgesloten : JA")
    print("Filters/strategie gewijzigd       : NEE")
    print("Auto shadow gewijzigd             : NEE")
    print("Stake/config/live                 : NEE")
    print("Orders/private API                : NEE")


def main() -> int:
    try:
        rows = load_closed_selective(SOURCE)
    except Exception as exc:
        print("=" * 96)
        print(f" DIAMOND SELECTIVE PROSPECTIVE CANDIDATE TRACKER v{VERSION}")
        print("=" * 96)
        print(f"STATUS: BRONFOUT | {type(exc).__name__}: {exc}")
        print("Live/config/orders/private API: NEE")
        return 2

    state = load_json(STATE)
    if not state.get("baseline_keys"):
        state = initialize_state(rows)
        atomic_json(STATE, state)

    report = build_report(rows, state)
    atomic_json(REPORT, report)
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
