#!/usr/bin/env python3
"""
Diamond Agent - stuurt elke 12 uur een rapport via email
"""
import csv
import json
import logging
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import yaml

LOG = logging.getLogger("diamond_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

TRADES_FILE = "/opt/render/project/src/grid_transactions.csv"
STATE_FILE  = "/opt/render/project/src/grid_state.json"

GMAIL_USER     = "joshuatec7@gmail.com"
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
REPORT_HOURS   = [8, 20]  # 08:00 en 20:00 UTC


def send_email(subject, body):
    if not GMAIL_PASSWORD:
        LOG.warning("Geen GMAIL_APP_PASSWORD, email niet verstuurd")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = GMAIL_USER
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASSWORD)
            smtp.send_message(msg)
        LOG.info("Email verstuurd: %s", subject)
    except Exception as e:
        LOG.error("Email mislukt: %s", e)


def build_report():
    state = {}
    if Path(STATE_FILE).exists():
        state = json.load(open(STATE_FILE))

    trades = []
    if Path(TRADES_FILE).exists():
        trades = list(csv.DictReader(open(TRADES_FILE)))

    sells = [t for t in trades if t.get("side", "").upper() == "SELL"]
    total  = len(sells)
    wins   = sum(1 for t in sells if float(t.get("pnl") or 0) > 0)
    pnl    = sum(float(t.get("pnl") or 0) for t in sells)
    winrate = (wins / total * 100) if total else 0

    # Per coin
    per_coin = {}
    for t in sells:
        sym = t.get("market", "?")
        if sym not in per_coin:
            per_coin[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
        per_coin[sym]["trades"] += 1
        p = float(t.get("pnl") or 0)
        per_coin[sym]["pnl"] += p
        if p > 0:
            per_coin[sym]["wins"] += 1

    grids = state.get("grids", {})

    lines = [
        "=" * 50,
        f"  DIAMOND GRID BOT RAPPORT",
        f"  {datetime.now().strftime('%d-%m-%Y %H:%M')}",
        "=" * 50,
        f"  Trades totaal : {total}",
        f"  Winst trades  : {wins}",
        f"  Verlies trades: {total - wins}",
        f"  Winrate       : {winrate:.1f}%",
        f"  Totaal PnL    : {pnl:+.2f} EUR",
        "",
        "  PER COIN:",
    ]

    for sym, data in per_coin.items():
        wr = (data["wins"] / data["trades"] * 100) if data["trades"] else 0
        lines.append(f"    {sym:<12} trades={data['trades']} winrate={wr:.0f}% pnl={data['pnl']:+.2f} EUR")

    lines += ["", "  GRID RANGES:"]
    for sym, grid in grids.items():
        low  = grid.get("low", 0)
        high = grid.get("high", 0)
        cur  = grid.get("current_price", 0)
        lines.append(f"    {sym:<12} range={low:.4f}-{high:.4f} | start={cur:.4f}")

    lines.append("=" * 50)
    return "\n".join(lines)


def main():
    LOG.info("Diamond Agent gestart - rapport elke 12 uur")
    last_report_hour = -1

    while True:
        now = datetime.now(timezone.utc)

        if now.hour in REPORT_HOURS and now.hour != last_report_hour:
            report = build_report()
            subject = f"Diamond Grid Bot Rapport {now.strftime('%d-%m-%Y %H:%M')}"
            send_email(subject, report)
            last_report_hour = now.hour

        time.sleep(60)


if __name__ == "__main__":
    main()
