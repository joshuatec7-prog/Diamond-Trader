#!/usr/bin/env python3
"""Mail nieuwe LIVE-geschikte AUTO LIVE 5-kandidaten.

Veiligheid:
- plaatst nooit orders;
- wijzigt geen trading state of approvals;
- gebruikt alleen publieke Bitvavo-marktdata voor crash/liquidity checks;
- verstuurt maximaal één mail per kandidaat;
- bestaande signalen worden bij eerste start alleen als gezien gemarkeerd.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import smtplib
import tempfile
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List

import ccxt
import yaml
from dotenv import load_dotenv

from diamond_liquidity_gate import evaluate_buy_liquidity
from diamond_market_crash_guard import poll_market_crash_guard
from diamond_selective_rules import selective_accepts, selective_candidate_key

load_dotenv()

LOG = logging.getLogger("diamond_live_candidate_mailer")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

SIGNALS_FILE = Path("/var/data/diamond_market_signals.csv")
STATE_FILE = Path("/var/data/diamond_live_candidate_mailer_state.json")
BOT_STATE_FILE = Path("/var/data/diamond_state.json")
CFG_FILE = Path(os.getenv("CFG_FILE", "/opt/render/project/src/config.yaml").strip())

GMAIL_USER = os.getenv("GMAIL_USER", "joshuatec7@gmail.com").strip()
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "").strip()

MAX_SIGNAL_AGE_MINUTES = 20.0
LOOP_SECONDS = 60
HISTORY_LIMIT = 30000


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_time(value: Any):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default.copy()
    except Exception:
        return default.copy()


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        tmp = handle.name
    os.replace(tmp, path)


def load_config() -> Dict[str, Any]:
    try:
        data = yaml.safe_load(CFG_FILE.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        LOG.warning("Config lezen mislukt: %s", exc)
        return {}


def load_rows() -> List[Dict[str, Any]]:
    if not SIGNALS_FILE.exists():
        return []
    try:
        with SIGNALS_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
            return [row for row in csv.DictReader(handle) if isinstance(row, dict)]
    except Exception as exc:
        LOG.warning("Signalen lezen mislukt: %s", exc)
        return []


def is_live_candidate(row: Dict[str, Any]) -> bool:
    if not selective_accepts(row):
        return False
    if str(row.get("side") or "").strip().upper() != "LONG":
        return False
    if str(row.get("strategy") or "").strip() != "trend_breakout":
        return False
    regime = str(row.get("market_regime") or "").strip().upper()
    if not regime or regime in {"BEARISH", "BEARISH_WEAK"}:
        return False
    return True


def bot_has_open_position() -> bool:
    state = read_json(BOT_STATE_FILE, {})

    # diamond_bot.py gebruikt 'positions'. Houd 'open_positions' alleen als
    # backwards-compatible fallback voor oudere statebestanden.
    positions = state.get("positions")
    if isinstance(positions, dict):
        return bool(positions)
    if isinstance(positions, list):
        return bool(positions)
    if positions:
        return True

    legacy = state.get("open_positions")
    if isinstance(legacy, dict):
        return bool(legacy)
    if isinstance(legacy, list):
        return bool(legacy)
    return bool(legacy)


def liquidity_settings(cfg: Dict[str, Any]) -> Dict[str, float]:
    risk = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
    execution = cfg.get("execution") if isinstance(cfg.get("execution"), dict) else {}
    return {
        "stake": to_float(risk.get("fixed_stake_quote"), 130.0),
        "depth": max(1, int(to_float(execution.get("liquidity_orderbook_depth"), 50))),
        "max_impact": to_float(execution.get("liquidity_max_price_impact_pct"), 0.15),
        "band": to_float(execution.get("liquidity_depth_band_pct"), 0.25),
        "min_multiple": to_float(execution.get("liquidity_min_depth_multiple"), 2.0),
    }


def check_liquidity(exchange: ccxt.Exchange, row: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    settings = liquidity_settings(cfg)
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return {"allow": False, "reason": "missing_symbol"}
    try:
        order_book = exchange.fetch_order_book(symbol, limit=settings["depth"])
    except Exception as exc:
        return {"allow": False, "reason": f"orderbook_error:{type(exc).__name__}"}
    return evaluate_buy_liquidity(
        order_book,
        settings["stake"],
        max_price_impact_pct=settings["max_impact"],
        depth_band_pct=settings["band"],
        min_depth_multiple=settings["min_multiple"],
    )


def send_email(row: Dict[str, Any], crash: Dict[str, Any], liquidity: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    if not GMAIL_USER or not GMAIL_PASS:
        LOG.warning("Gmail-instellingen ontbreken; kandidaatmail niet verzonden")
        return False

    settings = liquidity_settings(cfg)
    symbol = str(row.get("symbol") or "ONBEKEND").upper()
    subject = f"Diamond Trader LIVE kandidaat | {symbol}"

    body = "\n".join([
        "DIAMOND TRADER - NIEUWE AUTO LIVE KANDIDAAT",
        "=" * 52,
        f"Markt          : {symbol}",
        f"Richting       : {row.get('side') or '-'}",
        f"Strategie      : {row.get('strategy') or '-'}",
        f"Regime         : {row.get('market_regime') or '-'}",
        f"Score          : {row.get('score') or '-'}",
        f"Signaaltijd    : {row.get('detected_at') or '-'}",
        f"Entry          : {row.get('entry_price') or '-'}",
        f"Take-profit    : {row.get('take_profit') or '-'}",
        f"Stop-loss      : {row.get('stop_loss') or '-'}",
        f"Spread signaal : {row.get('spread_pct') or '-'}%",
        f"R/R            : {row.get('reward_risk') or '-'}",
        "",
        "LIVE VEILIGHEIDSCONTROLES",
        f"Crash guard    : {crash.get('status') or '-'} | {crash.get('reason') or '-'}",
        f"Liquidity      : {liquidity.get('reason') or '-'}",
        f"Prijsimpact    : {to_float(liquidity.get('estimated_price_impact_pct'), 0.0):.4f}%",
        f"Depth multiple : {to_float(liquidity.get('depth_multiple'), 0.0):.2f}x",
        f"Geplande inzet : €{settings['stake']:.2f}",
        "",
        "Deze mail plaatst zelf GEEN Bitvavo-order.",
        "AUTO LIVE kan deze kandidaat automatisch kopen zolang het signaal vers is.",
        "Vlak voor een echte BUY worden spread, liquidity, budget, approval en recovery opnieuw gecontroleerd.",
        "=" * 52,
    ])

    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = GMAIL_USER
    message["To"] = GMAIL_USER

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, [GMAIL_USER], message.as_string())
        return True
    except Exception as exc:
        LOG.warning("Kandidaatmail mislukt | %s | %s", symbol, exc)
        return False


def default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "initialized": False,
        "seen_keys": [],
        "sent_count": 0,
        "last_sent_at": None,
        "last_symbol": None,
    }


def process_once(exchange: ccxt.Exchange) -> None:
    rows = load_rows()
    state = read_json(STATE_FILE, default_state())
    for key, value in default_state().items():
        state.setdefault(key, value)

    candidate_rows = [row for row in rows if is_live_candidate(row)]

    if not bool(state.get("initialized")):
        state["seen_keys"] = [selective_candidate_key(row) for row in candidate_rows][-HISTORY_LIMIT:]
        state["initialized"] = True
        state["initialized_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(STATE_FILE, state)
        LOG.info("LIVE kandidaatmail baseline gestart | bestaande kandidaten=%d", len(candidate_rows))
        return

    seen_order = list(dict.fromkeys(str(x) for x in state.get("seen_keys", []) if str(x)))
    seen = set(seen_order)
    now = datetime.now(timezone.utc)
    cfg = load_config()

    for row in candidate_rows:
        key = selective_candidate_key(row)
        if not key or key in seen:
            continue

        detected = parse_time(row.get("detected_at"))
        if detected is None:
            seen.add(key)
            seen_order.append(key)
            continue

        age_minutes = (now - detected).total_seconds() / 60.0
        if age_minutes > MAX_SIGNAL_AGE_MINUTES:
            seen.add(key)
            seen_order.append(key)
            continue
        if age_minutes < -1.0:
            continue

        if bot_has_open_position():
            LOG.info("LIVE kandidaat wacht: bestaande positie open | %s", row.get("symbol"))
            continue

        crash = poll_market_crash_guard()
        if not bool(crash.get("allow_long")):
            LOG.info("LIVE kandidaat wacht: crash guard blokkeert | %s | %s", row.get("symbol"), crash.get("reason"))
            continue

        liquidity = check_liquidity(exchange, row, cfg)
        if not bool(liquidity.get("allow")):
            LOG.info("LIVE kandidaat wacht: liquidity blokkeert | %s | %s", row.get("symbol"), liquidity.get("reason"))
            continue

        if not send_email(row, crash, liquidity, cfg):
            continue

        seen.add(key)
        seen_order.append(key)
        state["sent_count"] = int(to_float(state.get("sent_count"), 0.0)) + 1
        state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
        state["last_symbol"] = str(row.get("symbol") or "")
        LOG.info("LIVE kandidaatmail verzonden | %s | score=%s", row.get("symbol"), row.get("score"))

    state["seen_keys"] = seen_order[-HISTORY_LIMIT:]
    state["last_check_at"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(STATE_FILE, state)


def self_test() -> None:
    good = {
        "symbol": "XRP/EUR",
        "strategy": "trend_breakout",
        "side": "LONG",
        "market_regime": "BULLISH",
        "shadow_eligible": "true",
        "candle_timestamp": "2026-08-23T03:00:00+00:00",
    }
    bad_regime = dict(good)
    bad_regime["market_regime"] = "BEARISH"
    bad_strategy = dict(good)
    bad_strategy["strategy"] = "momentum"
    assert is_live_candidate(good)
    assert not is_live_candidate(bad_regime)
    assert not is_live_candidate(bad_strategy)
    print("DIAMOND_LIVE_CANDIDATE_MAILER_SELF_TEST_OK")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    exchange = ccxt.bitvavo({"enableRateLimit": True})
    LOG.info("Diamond LIVE kandidaatmailer gestart | check=%ss", LOOP_SECONDS)

    while True:
        try:
            process_once(exchange)
        except Exception as exc:
            LOG.exception("LIVE kandidaatmailer fout: %s", exc)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
