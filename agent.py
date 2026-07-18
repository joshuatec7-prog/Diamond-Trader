#!/usr/bin/env python3
"""
Diamond Agent v5

Belangrijk:
- Leest diamond_state.json en diamond_transactions.csv.
- Schrijft NOOIT naar diamond_state.json.
- Schrijft veiligheidsstatus uitsluitend naar diamond_control.json.
- Stuurt dagrapporten om 08:00 en 20:00 Nederlandse tijd.
- Stuurt zondag om 09:00 een weekrapport.
- Pauzeert nieuwe aankopen bij te groot dagverlies of sterke BTC-daling.
- Open posities blijven door diamond_bot.py bewaakt tijdens een pauze.
"""

import csv
import imaplib
import json
import logging
import os
import smtplib
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import ccxt
from dotenv import load_dotenv


load_dotenv()

LOG = logging.getLogger("diamond_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

STATE_FILE = os.getenv(
    "STATE_FILE",
    "/var/data/diamond_state.json",
).strip()

TRADES_FILE = os.getenv(
    "TRADES_FILE",
    "/var/data/diamond_transactions.csv",
).strip()

AGENT_STATE_FILE = os.getenv(
    "AGENT_STATE_FILE",
    "/var/data/diamond_agent_state.json",
).strip()

CONTROL_FILE = os.getenv(
    "CONTROL_FILE",
    "/var/data/diamond_control.json",
).strip()

GMAIL_USER = os.getenv(
    "GMAIL_USER",
    "joshuatec7@gmail.com",
).strip()

GMAIL_PASS = os.getenv(
    "GMAIL_APP_PASSWORD",
    "",
).strip()

BITVAVO_API_KEY = os.getenv(
    "BITVAVO_API_KEY",
    "",
).strip()

BITVAVO_API_SECRET = os.getenv(
    "BITVAVO_API_SECRET",
    "",
).strip()

DAILY_REPORT_HOURS = {8, 20}
WEEKLY_REPORT_WEEKDAY = 6
WEEKLY_REPORT_HOUR = 9

ANALYZE_INTERVAL_SECONDS = 15 * 60
LOOP_SLEEP_SECONDS = 60

MAX_DAY_LOSS_PCT = 1.5
BTC_DROP_LIMIT_PCT = -8.0
BTC_RECOVERY_PCT = 4.0
DEFAULT_TOTAL_CAPITAL = 3000.0


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "ja", "aan", "on"}:
        return True
    if normalized in {"0", "false", "no", "nee", "uit", "off"}:
        return False
    return default


def ensure_parent(path_str: str) -> None:
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)


def load_json(path_str: str, default: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        return default.copy()

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            result = default.copy()
            result.update(data)
            return result
    except Exception as exc:
        LOG.error("JSON lezen mislukt voor %s: %s", path_str, exc)

    return default.copy()


def save_json_atomic(path_str: str, data: Dict[str, Any]) -> None:
    ensure_parent(path_str)
    target = Path(path_str)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
    ) as temporary:
        json.dump(data, temporary, indent=2, ensure_ascii=False)
        temporary_name = temporary.name

    os.replace(temporary_name, target)


def default_bot_state() -> Dict[str, Any]:
    return {
        "positions": {},
        "short_positions": {},
        "pnl_quote": 0.0,
        "short_pnl_quote": 0.0,
        "trades": 0,
        "wins": 0,
        "short_trades": 0,
        "short_wins": 0,
        "simulated_free_quote": None,
    }


def default_control() -> Dict[str, Any]:
    return {
        "paused": False,
        "pause_reason": "",
        "paused_at": None,
        "pause_date": None,
        "pause_btc_price": None,
    }


def default_agent_state() -> Dict[str, Any]:
    return {
        "last_analysis_ts": 0.0,
        "sent_daily_reports": [],
        "sent_weekly_reports": [],
    }


def load_bot_state() -> Dict[str, Any]:
    state = load_json(STATE_FILE, default_bot_state())

    if not isinstance(state.get("positions"), dict):
        state["positions"] = {}

    if not isinstance(state.get("short_positions"), dict):
        state["short_positions"] = {}

    return state


def load_control() -> Dict[str, Any]:
    control = load_json(CONTROL_FILE, default_control())
    control["paused"] = to_bool(control.get("paused"), False)
    return control


def save_control(
    paused: bool,
    reason: str = "",
    extra_values: Optional[Dict[str, Any]] = None,
) -> None:
    control = load_control()
    control["paused"] = paused
    control["pause_reason"] = reason
    control["paused_at"] = now_utc().isoformat() if paused else None

    if extra_values:
        control.update(extra_values)

    save_json_atomic(CONTROL_FILE, control)


def load_agent_state() -> Dict[str, Any]:
    state = load_json(AGENT_STATE_FILE, default_agent_state())

    if not isinstance(state.get("sent_daily_reports"), list):
        state["sent_daily_reports"] = []

    if not isinstance(state.get("sent_weekly_reports"), list):
        state["sent_weekly_reports"] = []

    return state


def save_agent_state(state: Dict[str, Any]) -> None:
    save_json_atomic(AGENT_STATE_FILE, state)


def load_trades() -> List[Dict[str, str]]:
    path = Path(TRADES_FILE)
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8", newline="") as file:
            return list(csv.DictReader(file))
    except Exception as exc:
        LOG.error("Transactiebestand lezen mislukt: %s", exc)
        return []


def trade_pnl(row: Dict[str, str]) -> float:
    if row.get("net_pnl_quote") not in {None, ""}:
        return to_float(row.get("net_pnl_quote"), 0.0)
    return to_float(row.get("pnl"), 0.0)


def is_closed_spot_trade(row: Dict[str, str]) -> bool:
    return str(row.get("side", "")).upper() == "SELL"


def is_closed_short_trade(row: Dict[str, str]) -> bool:
    return str(row.get("side", "")).upper() == "SHORT_CLOSE"


def parse_trade_datetime(row: Dict[str, str]) -> Optional[datetime]:
    raw = str(row.get("ts", "")).strip()
    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(LOCAL_TZ)
    except ValueError:
        pass

    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return parsed.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    except ValueError:
        return None


def get_day_pnl(trades: List[Dict[str, str]]) -> float:
    today = now_local().date()
    total = 0.0

    for row in trades:
        if not is_closed_spot_trade(row):
            continue
        trade_time = parse_trade_datetime(row)
        if trade_time and trade_time.date() == today:
            total += trade_pnl(row)

    return total


def get_week_trades(trades: List[Dict[str, str]]) -> List[Dict[str, str]]:
    cutoff = now_local() - timedelta(days=7)
    result = []

    for row in trades:
        if not is_closed_spot_trade(row):
            continue
        trade_time = parse_trade_datetime(row)
        if trade_time and trade_time >= cutoff:
            result.append(row)

    return result


def create_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo({
        "apiKey": BITVAVO_API_KEY,
        "secret": BITVAVO_API_SECRET,
        "enableRateLimit": True,
        "options": {
            "fetchMarkets": {
                "types": ["spot"],
            },
        },
    })
    exchange.load_markets()
    return exchange


def fetch_free_eur(exchange: ccxt.Exchange) -> float:
    try:
        balance = exchange.fetch_balance()
        free = balance.get("free") or {}
        return to_float(free.get("EUR"), 0.0)
    except Exception as exc:
        LOG.warning("Vrij EUR-saldo ophalen mislukt: %s", exc)
        return 0.0


def fetch_btc_price(exchange: ccxt.Exchange) -> float:
    try:
        ticker = exchange.fetch_ticker("BTC/EUR")
        return to_float(ticker.get("last") or ticker.get("close"), 0.0)
    except Exception as exc:
        LOG.warning("BTC-prijs ophalen mislukt: %s", exc)
        return 0.0


def fetch_btc_24h_change(exchange: ccxt.Exchange) -> float:
    try:
        candles = exchange.fetch_ohlcv(
            "BTC/EUR",
            timeframe="1h",
            limit=25,
        )

        if len(candles) < 2:
            return 0.0

        first_close = to_float(candles[0][4], 0.0)
        last_close = to_float(candles[-1][4], 0.0)

        if first_close <= 0:
            return 0.0

        return ((last_close - first_close) / first_close) * 100.0
    except Exception as exc:
        LOG.warning("BTC 24-uursverandering ophalen mislukt: %s", exc)
        return 0.0


def send_email(subject: str, body: str) -> bool:
    if not GMAIL_PASS:
        LOG.warning("GMAIL_APP_PASSWORD ontbreekt")
        return False

    try:
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = subject
        message["From"] = GMAIL_USER
        message["To"] = GMAIL_USER

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.send_message(message)

        LOG.info("E-mail verstuurd: %s", subject)
        return True
    except Exception as exc:
        LOG.error("E-mail versturen mislukt: %s", exc)
        return False


def cleanup_report_emails(keep: int = 2) -> None:
    if not GMAIL_PASS:
        return

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(GMAIL_USER, GMAIL_PASS)

            selected = False
            for mailbox in (
                '"[Gmail]/Alle e-mail"',
                '"[Gmail]/All Mail"',
                "INBOX",
            ):
                status, _ = imap.select(mailbox)
                if status == "OK":
                    selected = True
                    break

            if not selected:
                return

            status, data = imap.search(
                None,
                f'(FROM "{GMAIL_USER}" SUBJECT "Diamond Bot")',
            )
            if status != "OK":
                return

            message_ids = data[0].split()
            if len(message_ids) <= keep:
                return

            for message_id in message_ids[:-keep]:
                imap.store(message_id, "+FLAGS", "\\Deleted")

            imap.expunge()
    except Exception as exc:
        LOG.warning("Rapportmails opruimen mislukt: %s", exc)


def position_value(position: Dict[str, Any]) -> float:
    return to_float(position.get("quote_amount"), 0.0)


def build_per_coin_statistics(
    trades: List[Dict[str, str]],
) -> Dict[str, Dict[str, float]]:
    statistics: Dict[str, Dict[str, float]] = {}

    for row in trades:
        if not is_closed_spot_trade(row):
            continue

        symbol = row.get("market", "?")
        pnl = trade_pnl(row)

        if symbol not in statistics:
            statistics[symbol] = {
                "trades": 0,
                "wins": 0,
                "pnl": 0.0,
            }

        statistics[symbol]["trades"] += 1
        statistics[symbol]["pnl"] += pnl

        if pnl > 0:
            statistics[symbol]["wins"] += 1

    return statistics


def build_report(exchange: ccxt.Exchange) -> str:
    state = load_bot_state()
    control = load_control()
    trades = load_trades()

    spot_sells = [row for row in trades if is_closed_spot_trade(row)]
    short_closes = [row for row in trades if is_closed_short_trade(row)]

    total_spot_pnl = sum(trade_pnl(row) for row in spot_sells)
    total_short_pnl = sum(trade_pnl(row) for row in short_closes)

    spot_wins = sum(1 for row in spot_sells if trade_pnl(row) > 0)
    short_wins = sum(1 for row in short_closes if trade_pnl(row) > 0)

    spot_winrate = (
        spot_wins / len(spot_sells) * 100.0
        if spot_sells else 0.0
    )
    short_winrate = (
        short_wins / len(short_closes) * 100.0
        if short_closes else 0.0
    )

    positions = state.get("positions") or {}
    short_positions = state.get("short_positions") or {}

    invested = sum(
        position_value(position)
        for position in positions.values()
        if to_bool(position.get("opened_by_bot"), True)
    )

    free_eur = fetch_free_eur(exchange)
    btc_change = fetch_btc_24h_change(exchange)
    day_pnl = get_day_pnl(trades)

    per_coin = build_per_coin_statistics(trades)

    lines = [
        "=" * 58,
        "DIAMOND BOT RAPPORT",
        now_local().strftime("%d-%m-%Y %H:%M Nederlandse tijd"),
        "=" * 58,
        "",
        f"Status             : {'GEPAUZEERD' if control.get('paused') else 'ACTIEF'}",
        f"Reden pauze        : {control.get('pause_reason') or '-'}",
        f"Vrij EUR-saldo     : €{free_eur:.2f}",
        f"Bot geïnvesteerd   : €{invested:.2f}",
        f"BTC laatste 24 uur : {btc_change:+.2f}%",
        f"Dagresultaat       : €{day_pnl:+.2f}",
        "",
        "SPOT:",
        f"Open posities      : {len(positions)}",
        f"Gesloten trades    : {len(spot_sells)}",
        f"Winsttrades        : {spot_wins}",
        f"Verliestrades      : {len(spot_sells) - spot_wins}",
        f"Winrate            : {spot_winrate:.1f}%",
        f"Gerealiseerde PnL  : €{total_spot_pnl:+.2f}",
        "",
        "PAPER SHORT:",
        f"Open shorts        : {len(short_positions)}",
        f"Gesloten shorts    : {len(short_closes)}",
        f"Short-winrate      : {short_winrate:.1f}%",
        f"Short PnL          : €{total_short_pnl:+.2f}",
        "",
        "OPEN SPOTPOSITIES:",
    ]

    if positions:
        for symbol, position in positions.items():
            lines.append(
                f"  {symbol:<12} "
                f"€{position_value(position):>8.2f} | "
                f"entry={to_float(position.get('entry_price')):.8f} | bot"
            )
    else:
        lines.append("  Geen open spotposities")

    lines.extend(["", "RESULTAAT PER COIN:"])

    if per_coin:
        for symbol, values in sorted(per_coin.items()):
            trades_count = int(values["trades"])
            wins = int(values["wins"])
            winrate = (
                wins / trades_count * 100.0
                if trades_count else 0.0
            )
            lines.append(
                f"  {symbol:<12} "
                f"trades={trades_count:<4} "
                f"winrate={winrate:>5.1f}% "
                f"pnl=€{values['pnl']:+.2f}"
            )
    else:
        lines.append("  Nog geen gesloten trades")

    lines.append("=" * 58)
    return "\n".join(lines)


def build_weekly_report(exchange: ccxt.Exchange) -> str:
    state = load_bot_state()
    control = load_control()
    trades = load_trades()
    week_trades = get_week_trades(trades)

    week_pnl = sum(trade_pnl(row) for row in week_trades)
    week_wins = sum(1 for row in week_trades if trade_pnl(row) > 0)
    week_winrate = (
        week_wins / len(week_trades) * 100.0
        if week_trades else 0.0
    )

    all_spot_sells = [row for row in trades if is_closed_spot_trade(row)]
    all_pnl = sum(trade_pnl(row) for row in all_spot_sells)
    all_wins = sum(1 for row in all_spot_sells if trade_pnl(row) > 0)
    all_winrate = (
        all_wins / len(all_spot_sells) * 100.0
        if all_spot_sells else 0.0
    )

    positions = state.get("positions") or {}
    invested = sum(position_value(position) for position in positions.values())
    free_eur = fetch_free_eur(exchange)

    lines = [
        "=" * 58,
        "DIAMOND BOT WEEKRAPPORT",
        now_local().strftime("%d-%m-%Y"),
        "=" * 58,
        "",
        "AFGELOPEN 7 DAGEN:",
        f"Gesloten trades    : {len(week_trades)}",
        f"Winsttrades        : {week_wins}",
        f"Verliestrades      : {len(week_trades) - week_wins}",
        f"Winrate            : {week_winrate:.1f}%",
        f"Weekresultaat      : €{week_pnl:+.2f}",
        "",
        "TOTAAL:",
        f"Gesloten trades    : {len(all_spot_sells)}",
        f"Winrate            : {all_winrate:.1f}%",
        f"Gerealiseerde PnL  : €{all_pnl:+.2f}",
        "",
        "HUIDIGE STAND:",
        f"Vrij EUR-saldo     : €{free_eur:.2f}",
        f"Bot geïnvesteerd   : €{invested:.2f}",
        f"Open posities      : {len(positions)}",
        f"Botstatus          : {'GEPAUZEERD' if control.get('paused') else 'ACTIEF'}",
        f"Pauzereden         : {control.get('pause_reason') or '-'}",
        "=" * 58,
    ]

    return "\n".join(lines)


def get_total_capital(state: Dict[str, Any], exchange: ccxt.Exchange) -> float:
    simulated_free = to_float(state.get("simulated_free_quote"), 0.0)
    invested = sum(
        position_value(position)
        for position in (state.get("positions") or {}).values()
    )

    if simulated_free > 0:
        return simulated_free + invested

    free_eur = fetch_free_eur(exchange)
    if free_eur > 0:
        return free_eur + invested

    return DEFAULT_TOTAL_CAPITAL


def analyze_and_act(exchange: ccxt.Exchange) -> None:
    state = load_bot_state()
    control = load_control()
    trades = load_trades()

    day_pnl = get_day_pnl(trades)
    btc_change = fetch_btc_24h_change(exchange)

    total_capital = get_total_capital(state, exchange)
    max_day_loss = total_capital * (MAX_DAY_LOSS_PCT / 100.0)

    paused = to_bool(control.get("paused"), False)
    reason = str(control.get("pause_reason", ""))

    if not paused:
        if day_pnl <= -max_day_loss:
            save_control(
                paused=True,
                reason=f"dagverlies_{day_pnl:.2f}_EUR",
                extra_values={
                    "pause_date": now_local().date().isoformat(),
                    "pause_btc_price": None,
                },
            )

            LOG.warning(
                "Nieuwe aankopen gepauzeerd door dagverlies: %.2f EUR",
                day_pnl,
            )

            send_email(
                "Diamond Bot GEPAUZEERD - dagverlies",
                (
                    f"Nieuwe aankopen zijn gepauzeerd.\n\n"
                    f"Dagverlies: €{day_pnl:.2f}\n"
                    f"Daglimiet: €{max_day_loss:.2f}\n\n"
                    f"{build_report(exchange)}"
                ),
            )

        elif btc_change <= BTC_DROP_LIMIT_PCT:
            btc_price = fetch_btc_price(exchange)

            save_control(
                paused=True,
                reason=f"btc_daling_{btc_change:.2f}_pct",
                extra_values={
                    "pause_btc_price": btc_price,
                    "pause_date": now_local().date().isoformat(),
                },
            )

            LOG.warning(
                "Nieuwe aankopen gepauzeerd door BTC-daling: %.2f%%",
                btc_change,
            )

            send_email(
                "Diamond Bot GEPAUZEERD - BTC-daling",
                (
                    f"Nieuwe aankopen zijn gepauzeerd.\n\n"
                    f"BTC 24-uursverandering: {btc_change:.2f}%\n"
                    f"BTC-prijs bij pauze: €{btc_price:.2f}\n\n"
                    f"{build_report(exchange)}"
                ),
            )

    else:
        if reason.startswith("dagverlies_"):
            pause_date = str(control.get("pause_date", ""))

            if pause_date and pause_date != now_local().date().isoformat():
                save_control(
                    paused=False,
                    reason="",
                    extra_values={
                        "pause_date": None,
                        "pause_btc_price": None,
                    },
                )

                LOG.info("Nieuwe aankopen hervat: nieuwe kalenderdag")
                send_email(
                    "Diamond Bot HERVAT",
                    (
                        "Nieuwe aankopen zijn hervat na de dagverliespauze.\n\n"
                        f"{build_report(exchange)}"
                    ),
                )

        elif reason.startswith("btc_daling_"):
            pause_price = to_float(control.get("pause_btc_price"), 0.0)
            current_price = fetch_btc_price(exchange)

            if pause_price > 0 and current_price > 0:
                recovery = (
                    (current_price - pause_price)
                    / pause_price
                    * 100.0
                )

                if recovery >= BTC_RECOVERY_PCT:
                    save_control(
                        paused=False,
                        reason="",
                        extra_values={
                            "pause_btc_price": None,
                            "pause_date": None,
                        },
                    )

                    LOG.info(
                        "Nieuwe aankopen hervat na BTC-herstel van %.2f%%",
                        recovery,
                    )

                    send_email(
                        "Diamond Bot HERVAT na BTC-herstel",
                        (
                            f"BTC is {recovery:.2f}% hersteld sinds de pauze.\n"
                            "Nieuwe aankopen zijn weer vrijgegeven.\n\n"
                            f"{build_report(exchange)}"
                        ),
                    )

    current_control = load_control()
    LOG.info(
        "Veiligheidsanalyse | dag_pnl=%+.2f EUR | btc_24u=%+.2f%% | "
        "limiet=-%.2f EUR | paused=%s",
        day_pnl,
        btc_change,
        max_day_loss,
        current_control.get("paused", False),
    )


def cleanup_agent_history(agent_state: Dict[str, Any]) -> None:
    agent_state["sent_daily_reports"] = (
        agent_state.get("sent_daily_reports", [])[-30:]
    )
    agent_state["sent_weekly_reports"] = (
        agent_state.get("sent_weekly_reports", [])[-12:]
    )


def handle_scheduled_reports(
    exchange: ccxt.Exchange,
    agent_state: Dict[str, Any],
) -> None:
    current = now_local()

    if current.hour in DAILY_REPORT_HOURS:
        report_key = current.strftime("%Y-%m-%d-%H")

        if report_key not in agent_state["sent_daily_reports"]:
            sent = send_email(
                f"Diamond Bot Rapport {current.strftime('%d-%m-%Y %H:%M')}",
                build_report(exchange),
            )

            if sent:
                agent_state["sent_daily_reports"].append(report_key)
                cleanup_report_emails(keep=2)
                cleanup_agent_history(agent_state)
                save_agent_state(agent_state)

    if (
        current.weekday() == WEEKLY_REPORT_WEEKDAY
        and current.hour == WEEKLY_REPORT_HOUR
    ):
        week_key = current.strftime("%G-W%V")

        if week_key not in agent_state["sent_weekly_reports"]:
            sent = send_email(
                f"Diamond Bot WEEKRAPPORT {current.strftime('%d-%m-%Y')}",
                build_weekly_report(exchange),
            )

            if sent:
                agent_state["sent_weekly_reports"].append(week_key)
                cleanup_agent_history(agent_state)
                save_agent_state(agent_state)


def main() -> None:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError(
            "BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt"
        )

    for path in (
        STATE_FILE,
        TRADES_FILE,
        AGENT_STATE_FILE,
        CONTROL_FILE,
    ):
        ensure_parent(path)

    if not Path(CONTROL_FILE).exists():
        save_json_atomic(CONTROL_FILE, default_control())

    exchange = create_exchange()
    agent_state = load_agent_state()

    LOG.info("Diamond Agent v5 gestart")
    LOG.info("State-bestand: %s", STATE_FILE)
    LOG.info("Transactiebestand: %s", TRADES_FILE)
    LOG.info("Controlebestand: %s", CONTROL_FILE)

    while True:
        try:
            handle_scheduled_reports(exchange, agent_state)

            last_analysis = to_float(
                agent_state.get("last_analysis_ts"),
                0.0,
            )

            if time.time() - last_analysis >= ANALYZE_INTERVAL_SECONDS:
                analyze_and_act(exchange)
                agent_state["last_analysis_ts"] = time.time()
                save_agent_state(agent_state)

        except Exception as exc:
            LOG.exception("Agent-hoofdloop fout: %s", exc)

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
