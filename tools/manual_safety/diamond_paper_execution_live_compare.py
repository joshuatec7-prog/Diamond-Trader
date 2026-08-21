#!/usr/bin/env python3
# Diamond Trader Paper vs Execution vs Live Compare v1.0
#
# Read-only vergelijker:
# - PAPER: SELECTIVE
# - EXECUTION: BASELINE
# - LIVE: echte canary trades uit diamond_canary_log_analysis.json
#
# Geen orders, geen private API, geen automatische livegang.

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional


VERSION = "1.0"
DEFAULT_ANALYZER = Path("diamond_prospective_final_analyzer.py")
DEFAULT_LIVE_JSON = Path("/var/data/diamond_canary_log_analysis.json")


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fmt_eur(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"€{sign}{value:.4f}"


def fmt_pf(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "INF"
    return f"{value:.4f}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.4f}%"


def run_analyzer(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        result = subprocess.run(
            ["python3", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    except Exception:
        return ""


def clean_num(text: str) -> str:
    return (
        str(text)
        .replace("€", "")
        .replace("+", "")
        .replace(",", ".")
        .strip()
    )


def parse_strategy_line(text: str, label: str) -> Dict[str, Any]:
    """
    Flexibele parser voor regels zoals:
    SELECTIVE: 29/20 W/L 14/15 pnl €+42.2666 PF 1.882
    BASELINE n=15/20 W/L=6/9 pnl=€+1.4754 PF=1.0583
    """
    candidates = [
        line.strip()
        for line in text.splitlines()
        if label.upper() in line.upper()
    ]

    if not candidates:
        return {"available": False, "label": label}

    # Kies de eerste regel die ook PnL/PF of W/L bevat.
    line = next(
        (
            c for c in candidates
            if ("PF" in c.upper() or "PNL" in c.upper() or "W/L" in c.upper())
        ),
        candidates[0],
    )

    closed = None
    target = None
    wins = None
    losses = None
    pnl = None
    pf = None

    m = re.search(r"(?<!\d)(\d+)\s*/\s*(\d+)", line)
    if m:
        closed = int(m.group(1))
        target = int(m.group(2))

    m = re.search(
        r"W\s*/\s*L\s*=?\s*(\d+)\s*/\s*(\d+)",
        line,
        flags=re.IGNORECASE,
    )
    if m:
        wins = int(m.group(1))
        losses = int(m.group(2))

    m = re.search(
        r"PNL\s*=?\s*€?\s*([+-]?\d+(?:[.,]\d+)?)",
        line,
        flags=re.IGNORECASE,
    )
    if m:
        pnl = float(clean_num(m.group(1)))

    m = re.search(
        r"\bPF\s*=?\s*([+-]?\d+(?:[.,]\d+)?|INF)",
        line,
        flags=re.IGNORECASE,
    )
    if m:
        raw = m.group(1).upper()
        pf = math.inf if raw == "INF" else float(clean_num(raw))

    return {
        "available": any(v is not None for v in (closed, wins, pnl, pf)),
        "label": label,
        "source_line": line,
        "closed": closed,
        "target": target,
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
        "pf": pf,
    }


def load_live(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"available": False, "label": "LIVE", "closed": 0}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "available": False,
            "label": "LIVE",
            "closed": 0,
            "error": "LIVE_JSON_LEESFOUT",
        }

    if not isinstance(data, dict):
        return {"available": False, "label": "LIVE", "closed": 0}

    closed = to_int(data.get("closed_trades"), 0)
    wins = to_int(data.get("wins"), 0)
    losses = to_int(data.get("losses"), 0)

    pf_raw = data.get("profit_factor")
    pf = None
    if pf_raw is not None:
        try:
            pf = float(pf_raw)
        except (TypeError, ValueError):
            pf = None

    return {
        "available": closed > 0,
        "label": "LIVE",
        "closed": closed,
        "target": 5,
        "wins": wins,
        "losses": losses,
        "pnl": to_float(data.get("actual_net_pnl_quote"), 0.0),
        "pf": pf,
        "fees": to_float(data.get("total_fees_quote"), 0.0),
        "buy_slippage": to_float(data.get("avg_buy_slippage_pct"), 0.0),
        "sell_slippage": to_float(data.get("avg_sell_slippage_pct"), 0.0),
        "max_slippage": to_float(data.get("max_adverse_slippage_pct"), 0.0),
        "execution_difference": to_float(data.get("pnl_difference_quote"), 0.0),
        "status": str(data.get("status") or "READY"),
    }


def winrate(stats: Dict[str, Any]) -> Optional[float]:
    wins = stats.get("wins")
    losses = stats.get("losses")
    if wins is None or losses is None:
        return None
    total = int(wins) + int(losses)
    if total <= 0:
        return None
    return (int(wins) / total) * 100.0


def per_trade_pnl(stats: Dict[str, Any]) -> Optional[float]:
    closed = stats.get("closed")
    pnl = stats.get("pnl")
    if closed in (None, 0) or pnl is None:
        return None
    return float(pnl) / int(closed)


def ratio_or_none(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or abs(b) < 1e-12:
        return None
    return a / b


def build_comparison(
    paper: Dict[str, Any],
    execution: Dict[str, Any],
    live: Dict[str, Any],
) -> Dict[str, Any]:
    result = {
        "paper": paper,
        "execution": execution,
        "live": live,
        "paper_winrate": winrate(paper),
        "execution_winrate": winrate(execution),
        "live_winrate": winrate(live),
        "paper_pnl_per_trade": per_trade_pnl(paper),
        "execution_pnl_per_trade": per_trade_pnl(execution),
        "live_pnl_per_trade": per_trade_pnl(live),
        "live_vs_paper_edge_retention": None,
        "live_vs_execution_edge_retention": None,
        "status": "WAIT_LIVE",
    }

    if live.get("available"):
        result["live_vs_paper_edge_retention"] = ratio_or_none(
            result["live_pnl_per_trade"],
            result["paper_pnl_per_trade"],
        )
        result["live_vs_execution_edge_retention"] = ratio_or_none(
            result["live_pnl_per_trade"],
            result["execution_pnl_per_trade"],
        )

        live_status = str(live.get("status") or "").upper()
        if live_status in {"FAIL", "STOP_CANDIDATE"}:
            result["status"] = "PAUSE_CANDIDATE"
        else:
            result["status"] = "LIVE_DATA_AVAILABLE"

    return result


def ratio_text(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100.0:.1f}%"


def row_text(name: str, stats: Dict[str, Any]) -> str:
    if not stats.get("available"):
        closed = to_int(stats.get("closed"), 0)
        return f"{name:<10} closed={closed:<3} | wacht op data"

    wr = winrate(stats)
    return (
        f"{name:<10} "
        f"closed={to_int(stats.get('closed')):<3} "
        f"W/L={to_int(stats.get('wins'))}/{to_int(stats.get('losses'))} "
        f"WR={fmt_pct(wr)} "
        f"PnL={fmt_eur(stats.get('pnl'))} "
        f"PF={fmt_pf(stats.get('pf'))} "
        f"avg/trade={fmt_eur(per_trade_pnl(stats))}"
    )


def print_report(result: Dict[str, Any]) -> None:
    paper = result["paper"]
    execution = result["execution"]
    live = result["live"]

    print("=" * 78)
    print(f" DIAMOND PAPER vs EXECUTION vs LIVE v{VERSION}")
    print("=" * 78)

    print(row_text("PAPER", paper))
    print(row_text("EXECUTION", execution))
    print(row_text("LIVE", live))

    print("\n=== EDGE VERGELIJKING ===")
    print(
        "Paper avg/trade     : "
        f"{fmt_eur(result['paper_pnl_per_trade'])}"
    )
    print(
        "Execution avg/trade : "
        f"{fmt_eur(result['execution_pnl_per_trade'])}"
    )
    print(
        "Live avg/trade      : "
        f"{fmt_eur(result['live_pnl_per_trade'])}"
    )

    if live.get("available"):
        print(
            "Live/Paper edge     : "
            f"{ratio_text(result['live_vs_paper_edge_retention'])}"
        )
        print(
            "Live/Execution edge : "
            f"{ratio_text(result['live_vs_execution_edge_retention'])}"
        )

        print("\n=== LIVE EXECUTION KOSTEN ===")
        print(
            f"Totale fees         : {fmt_eur(live.get('fees'))}"
        )
        print(
            f"Gem. BUY slippage   : {fmt_pct(live.get('buy_slippage'))}"
        )
        print(
            f"Gem. SELL slippage  : {fmt_pct(live.get('sell_slippage'))}"
        )
        print(
            f"Max slippage        : {fmt_pct(live.get('max_slippage'))}"
        )
        print(
            f"Expected/actual diff: "
            f"{fmt_eur(live.get('execution_difference'))}"
        )
    else:
        print("\n[WAIT] Nog geen gesloten echte canary-trades.")
        print("       Live vergelijking wordt automatisch bruikbaar zodra die bestaan.")

    print("\n=== STATUS ===")
    print(result["status"])
    print("Automatische livegang : NEE")
    print("Automatisch opschalen : NEE")
    print("Orders/private API    : NEE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Vergelijk SELECTIVE paper, Execution BASELINE en echte live canary."
    )
    parser.add_argument(
        "--analyzer",
        default=str(DEFAULT_ANALYZER),
        help="Pad naar diamond_prospective_final_analyzer.py",
    )
    parser.add_argument(
        "--analyzer-output",
        default="",
        help="Optioneel bestaand tekstbestand i.p.v. analyzer uitvoeren.",
    )
    parser.add_argument(
        "--live-json",
        default=str(DEFAULT_LIVE_JSON),
        help=f"Live analyzer JSON (standaard: {DEFAULT_LIVE_JSON})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.analyzer_output:
        path = Path(args.analyzer_output)
        analyzer_text = (
            path.read_text(encoding="utf-8")
            if path.exists()
            else ""
        )
    else:
        analyzer_text = run_analyzer(Path(args.analyzer))

    paper = parse_strategy_line(analyzer_text, "SELECTIVE")
    execution = parse_strategy_line(analyzer_text, "BASELINE")
    live = load_live(Path(args.live_json))

    result = build_comparison(paper, execution, live)
    print_report(result)

    return 2 if result["status"] == "PAUSE_CANDIDATE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
