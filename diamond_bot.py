#!/usr/bin/env python3
import csv
import json
import logging
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import ccxt
import pandas as pd
import yaml
from dotenv import load_dotenv
from diamond_selective_execution_adapter import new_execution_contracts
from diamond_liquidity_gate import evaluate_buy_liquidity

LOG = logging.getLogger("diamond_trader")

TRADE_CSV_COLUMNS = [
    "ts", "market", "side", "price", "base_amount", "quote_amount",
    "fees_quote", "spread_pct", "net_pnl_quote", "holding_time_min", "reason", "dry_run",
]

CANARY_EXECUTION_CSV_COLUMNS = [
    "ts",
    "event",
    "canary_trade_number",
    "market",
    "side",
    "reason",
    "reference_ask",
    "buy_fill_price",
    "buy_slippage_pct",
    "buy_slippage_status",
    "buy_spread_pct",
    "buy_fee_quote",
    "buy_order_id",
    "buy_client_order_id",
    "reference_bid",
    "sell_fill_price",
    "sell_slippage_pct",
    "sell_slippage_status",
    "sell_spread_pct",
    "sell_fee_quote",
    "sell_order_id",
    "sell_client_order_id",
    "base_amount",
    "entry_quote_actual",
    "exit_quote_actual",
    "expected_net_pnl_quote",
    "actual_net_pnl_quote",
    "pnl_difference_quote",
    "total_fees_quote",
    "holding_time_min",
    "recovery_used",
    "overall_status",
    "dry_run",
]

SHORT_EXECUTION_CSV_COLUMNS = [
    "ts",
    "event",
    "strategy_version",
    "market",
    "entry_trigger",
    "entry_price",
    "signal_close",
    "atr",
    "atr_pct",
    "spread_pct",
    "planned_stop_loss",
    "planned_take_profit",
    "base_take_profit",
    "planned_tp_atr_mult",
    "expected_net_reward",
    "expected_net_risk",
    "expected_net_rr",
    "exit_reason",
    "exit_price",
    "market_ask_at_close",
    "exit_candle_open",
    "exit_candle_high",
    "exit_candle_low",
    "exit_candle_close",
    "stop_overshoot_pct",
    "take_profit_overshoot_pct",
    "net_pnl_quote",
    "holding_time_min",
    "paper_only",
    "dry_run",
]

ALIASES = {
    "dry_run": ["risk.dry_run"],
    "fixed_stake_quote": ["risk.fixed_stake_quote"],
    "max_open_positions": ["risk.max_open_positions", "trading.max_total_positions"],
    "max_spread_pct": ["risk.max_spread_pct"],
    "eur_reserve": ["risk.eur_reserve"],
    "skip_log_every_seconds": ["risk.skip_log_every_seconds"],
    "min_profit_eur": ["risk.min_profit_eur"],
    "cooldown_minutes": ["risk.cooldown_minutes"],
    "taker_fee_pct": ["fees.taker_fee_pct"],
    "log_level": ["logging.level"],
    "loop_sleep_seconds": ["logging.loop_sleep_seconds"],
    "candles_limit": ["logging.candles_limit"],
}

SYMBOL_NAME_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "ADA": "cardano",
    "XRP": "ripple", "DOGE": "dogecoin", "LINK": "chainlink", "AVAX": "avalanche",
    "DOT": "polkadot", "PEPE": "pepe coin", "WIF": "dogwifhat", "BONK": "bonk",
    "SHIB": "shiba inu", "TRUMP": "official trump coin", "ENA": "ethena",
    "FET": "fetch ai", "TAO": "bittensor", "WLD": "worldcoin", "HYPE": "hyperliquid",
    "ZKJ": "polyhedra", "ONDO": "ondo finance", "ORCA": "orca crypto",
    "CHIP": "chip crypto", "XVG": "verge crypto", "ARB": "arbitrum",
}

POSITIVE_TITLE_WORDS = {
    "approval": 1.8, "approved": 1.8, "etf": 1.2, "launch": 0.8, "launches": 0.8,
    "listing": 0.8, "listed": 0.8, "partnership": 0.8, "partners": 0.8,
    "integrates": 0.7, "integration": 0.7, "adoption": 0.7, "upgrade": 0.5,
    "upgrades": 0.5, "surge": 0.5, "rally": 0.5, "bullish": 0.5,
    "record": 0.4, "breakout": 0.5, "inflow": 0.6,
}

NEGATIVE_TITLE_WORDS = {
    "hack": -2.0, "hacked": -2.0, "exploit": -2.0, "breach": -1.8,
    "lawsuit": -1.6, "sued": -1.6, "delist": -1.8, "delisting": -1.8,
    "scam": -2.2, "fraud": -2.0, "rug": -2.2, "rugpull": -2.2,
    "outage": -1.2, "bankruptcy": -2.4, "liquidation": -1.4, "liquidations": -1.4,
    "unlock": -1.0, "selloff": -1.2, "dump": -1.2, "bearish": -0.6,
    "investigation": -1.2, "probe": -1.2, "charges": -1.6,
}

SEVERE_NEGATIVE_WORDS = {
    "hack", "hacked", "exploit", "breach", "bankruptcy", "scam", "fraud",
    "rug", "rugpull", "delist", "delisting", "lawsuit", "charges",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def utc_now_ts() -> float:
    return time.time()

def minutes_since(ts: float) -> float:
    return max(0.0, (utc_now_ts() - ts) / 60.0)

def to_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "ja", "aan", "on", "waar"}:
        return True
    if s in {"0", "false", "no", "nee", "uit", "off", "onwaar"}:
        return False
    return default

def to_float(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return default

def ensure_parent(path_str: str) -> None:
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)

def default_state() -> Dict[str, Any]:
    return {
        "positions": {},
        "cooldown": {},
        "short_positions": {},
        "short_cooldown": {},
        "pnl_quote": 0.0,
        "short_pnl_quote": 0.0,
        "trades": 0,
        "wins": 0,
        "short_trades": 0,
        "short_wins": 0,
        "simulated_free_quote": None,
        # Live-order safety. Deze velden zijn backwards-compatible met oude statebestanden.
        "pending_orders": {},
        "recovery_required": False,
        "recovery_reason": "",
        "canary_trade_sequence": 0,
    }


def default_control() -> Dict[str, Any]:
    return {
        "paused": False,
        "pause_reason": "",
        "paused_at": None,
        "pause_date": None,
        "pause_btc_price": None,
    }


def load_control(path_str: str) -> Dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        return default_control()

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return default_control()
    except Exception:
        return default_control()

    control = default_control()
    control.update(data)
    control["paused"] = to_bool(control.get("paused"), False)
    return control


def load_state(path_str: str) -> Dict[str, Any]:
    p = Path(path_str)
    if not p.exists():
        return default_state()

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state is geen dictionary")
    except Exception as exc:
        # Bij een bestaand maar onleesbaar statebestand nooit stil terugvallen
        # naar een "lege" live-state. Nieuwe entries worden geblokkeerd totdat
        # de operator de recovery heeft beoordeeld.
        state = default_state()
        state["recovery_required"] = True
        state["recovery_reason"] = f"state_load_failed:{type(exc).__name__}"
        return state

    base = default_state()
    base.update(data)

    for key in [
        "positions",
        "cooldown",
        "short_positions",
        "short_cooldown",
        "pending_orders",
    ]:
        if not isinstance(base.get(key), dict):
            base[key] = {}

    base["recovery_required"] = to_bool(
        base.get("recovery_required"),
        False,
    )
    base["recovery_reason"] = str(
        base.get("recovery_reason")
        or ""
    )
    return base

def save_state(path_str: str, state: Dict[str, Any]) -> None:
    """Schrijft de bot-state atomair. Alleen diamond_bot.py schrijft dit bestand."""
    ensure_parent(path_str)
    path = Path(path_str)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as temporary:
        json.dump(state, temporary, indent=2, ensure_ascii=False)
        temporary_name = temporary.name

    os.replace(temporary_name, path)


def append_trade_csv(path_str: str, row: Dict[str, Any]) -> None:
    ensure_parent(path_str)
    exists = Path(path_str).exists()
    with open(path_str, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in TRADE_CSV_COLUMNS})


def append_canary_execution_csv(path_str: str, row: Dict[str, Any]) -> None:
    """Schrijft uitsluitend echte live/canary execution-events."""
    ensure_parent(path_str)
    exists = Path(path_str).exists()
    with open(path_str, "a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CANARY_EXECUTION_CSV_COLUMNS,
        )
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                key: row.get(key, "")
                for key in CANARY_EXECUTION_CSV_COLUMNS
            }
        )


def execution_slippage_pct(
    side: str,
    reference_price: float,
    fill_price: float,
) -> float:
    """Positief = slechter uitgevoerd dan de vlak-voor-order referentie."""
    reference = to_float(reference_price, 0.0)
    fill = to_float(fill_price, 0.0)
    if reference <= 0 or fill <= 0:
        return 0.0

    if str(side).strip().lower() == "sell":
        return ((reference - fill) / reference) * 100.0

    return ((fill - reference) / reference) * 100.0


def classify_slippage_status(slippage_pct: float) -> str:
    """
    Classificeert alleen nadelige execution-slippage.

    Negatieve slippage betekent een betere fill dan de referentie en is dus OK.
    """
    value = to_float(slippage_pct, 0.0)

    if value > 0.30:
        return "STOP_CANDIDATE"
    if value > 0.20:
        return "HIGH"
    if value > 0.10:
        return "WARNING"
    return "OK"


def combine_execution_status(*statuses: str) -> str:
    """Geeft de zwaarste executionstatus terug."""
    ranking = {
        "OK": 0,
        "WARNING": 1,
        "HIGH": 2,
        "STOP_CANDIDATE": 3,
    }

    normalized = [
        str(status or "OK").strip().upper()
        for status in statuses
    ]

    if not normalized:
        return "OK"

    return max(
        normalized,
        key=lambda value: ranking.get(value, 0),
    )


def expected_canary_net_pnl_quote(
    amount: float,
    reference_ask: float,
    reference_bid: float,
    taker_fee_pct: float,
) -> float:
    """Verwachte netto PnL zonder execution-slippage, met ingestelde taker fee."""
    amount_value = max(0.0, to_float(amount, 0.0))
    ask = max(0.0, to_float(reference_ask, 0.0))
    bid = max(0.0, to_float(reference_bid, 0.0))
    fee_rate = max(0.0, to_float(taker_fee_pct, 0.0)) / 100.0

    if amount_value <= 0 or ask <= 0 or bid <= 0:
        return 0.0

    expected_entry_quote = amount_value * ask
    expected_exit_quote = amount_value * bid
    expected_buy_fee = expected_entry_quote * fee_rate
    expected_sell_fee = expected_exit_quote * fee_rate

    return (
        expected_exit_quote
        - expected_sell_fee
        - expected_entry_quote
        - expected_buy_fee
    )


def append_short_execution_csv(path_str: str, row: Dict[str, Any]) -> None:
    """Schrijft uitsluitend uitgebreide paper-shortdiagnostiek."""
    ensure_parent(path_str)
    exists = Path(path_str).exists()
    with open(path_str, "a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=SHORT_EXECUTION_CSV_COLUMNS,
        )
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                key: row.get(key, "")
                for key in SHORT_EXECUTION_CSV_COLUMNS
            }
        )

def load_yaml(path_str: str) -> Dict[str, Any]:
    with open(path_str, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"YAML fout in {path_str}: {e}")
    if not isinstance(data, dict):
        raise ValueError("Config moet een YAML dictionary zijn.")
    return data


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

def _get_path(cfg: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur

def get_cfg(cfg: Dict[str, Any], path: str, default: Any = None) -> Any:
    value = _get_path(cfg, path, None)
    if value is not None:
        return value
    for alias in ALIASES.get(path, []):
        alias_value = _get_path(cfg, alias, None)
        if alias_value is not None:
            return alias_value
    return default

def normalize_symbol(symbol: str, quote: str) -> str:
    s = str(symbol).strip().upper()
    q = str(quote).strip().upper()
    if "/" in s:
        return s
    if "-" in s:
        parts = s.split("-", 1)
        return f"{parts[0]}/{parts[1]}"
    return f"{s}/{q}"

def compute_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-12)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)

def compute_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

def enrich_indicators(df: pd.DataFrame, sma_fast: int, sma_slow: int, rsi_len: int, atr_len: int) -> pd.DataFrame:
    out = df.copy()
    out["sma_fast"] = out["close"].rolling(sma_fast).mean()
    out["sma_slow"] = out["close"].rolling(sma_slow).mean()
    out["rsi"] = compute_rsi(out["close"], rsi_len)
    out["atr"] = compute_atr(out, atr_len)
    out["atr_pct"] = (out["atr"] / out["close"]) * 100.0
    return out

def http_get_json(url: str, timeout: int = 20) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "diamond-news-bot/1.0", "Accept": "application/json,text/plain,*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        content_type = str(resp.headers.get("Content-Type", "")).lower()
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("lege response van nieuwsbron")
    if text.startswith("<"):
        raise ValueError(f"html response van nieuwsbron ({content_type})")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        snippet = text[:200].replace("\n", " ")
        raise ValueError(f"ongeldige json response: {snippet}") from e


class NewsEngine:
    def __init__(self, cfg: Dict[str, Any], exchange: ccxt.Exchange):
        self.cfg = cfg
        self.exchange = exchange
        self.enabled = to_bool(get_cfg(cfg, "news.enabled", True), True)
        self.cache_ttl_sec = int(to_float(get_cfg(cfg, "news.cache_minutes", 180), 180) * 60)
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.fng_cache: Dict[str, Any] = {"ts": 0.0, "value": None, "classification": "unknown"}

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        item = self.cache.get(key)
        if not item:
            return None
        if utc_now_ts() - float(item.get("ts", 0.0)) > self.cache_ttl_sec:
            return None
        return item.get("value")

    def _cache_set(self, key: str, value: Dict[str, Any]) -> Dict[str, Any]:
        self.cache[key] = {"ts": utc_now_ts(), "value": value}
        return value

    def news_term_for_symbol(self, symbol: str) -> str:
        market = self.exchange.market(symbol)
        base = str(market.get("base", "")).upper()
        custom = get_cfg(self.cfg, f"news.aliases.{base}", None)
        if custom:
            return str(custom)
        return SYMBOL_NAME_MAP.get(base, base.lower())

    def fear_greed(self) -> Dict[str, Any]:
        if not self.enabled or not to_bool(get_cfg(self.cfg, "news.use_fear_greed", True), True):
            return {"value": None, "classification": "disabled"}
        if utc_now_ts() - float(self.fng_cache.get("ts", 0.0)) < self.cache_ttl_sec and self.fng_cache.get("value") is not None:
            return {"value": self.fng_cache.get("value"), "classification": self.fng_cache.get("classification", "unknown")}
        try:
            data = http_get_json("https://api.alternative.me/fng/?limit=1&format=json", timeout=15)
            row = ((data or {}).get("data") or [{}])[0]
            value = int(row.get("value"))
            classification = str(row.get("value_classification", "unknown"))
            self.fng_cache = {"ts": utc_now_ts(), "value": value, "classification": classification}
            return {"value": value, "classification": classification}
        except Exception as e:
            LOG.warning("Fear & Greed ophalen mislukt: %s", e)
            return {"value": None, "classification": "unknown"}

    def gdelt_articles(self, term: str) -> List[Dict[str, Any]]:
        # Kleine vertraging om 429 rate-limit te voorkomen
        time.sleep(2.0)
        hours = int(to_float(get_cfg(self.cfg, "news.timespan_hours", 24), 24))
        max_records = int(to_float(get_cfg(self.cfg, "news.max_records", 5), 5))
        params = {
            "query": f"\"{term}\"", "mode": "ArtList", "format": "json",
            "timespan": f"{hours}h", "maxrecords": str(max_records), "sort": "DateDesc",
        }
        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)
        data = http_get_json(url, timeout=20)
        if isinstance(data, dict):
            for key in ["articles", "data", "results"]:
                value = data.get(key)
                if isinstance(value, list):
                    return value
        if isinstance(data, list):
            return data
        raise ValueError("nieuwsbron gaf geen bruikbare artikellijst terug")

    def title_sentiment_score(self, text: str) -> float:
        t = str(text or "").lower()
        score = 0.0
        for word, weight in POSITIVE_TITLE_WORDS.items():
            if word in t:
                score += weight
        for word, weight in NEGATIVE_TITLE_WORDS.items():
            if word in t:
                score += weight
        return score

    def severe_negative_count(self, titles: List[str]) -> int:
        count = 0
        for title in titles:
            t = str(title or "").lower()
            if any(word in t for word in SEVERE_NEGATIVE_WORDS):
                count += 1
        return count

    def coin_news(self, symbol: str) -> Dict[str, Any]:
        if not self.enabled:
            return {"term": None, "article_count": 0, "news_score": 0.0, "severe_negative_count": 0, "titles": [], "ok": True}
        cached = self._cache_get(symbol)
        if cached is not None:
            return cached
        term = self.news_term_for_symbol(symbol)
        stale_item = self.cache.get(symbol)
        stale_value = stale_item.get("value") if isinstance(stale_item, dict) else None
        try:
            articles = self.gdelt_articles(term)
            titles: List[str] = []
            score = 0.0
            for article in articles:
                title = str((article or {}).get("title") or "")
                if title:
                    titles.append(title)
                    score += self.title_sentiment_score(title)
            result = {
                "term": term, "article_count": len(titles),
                "news_score": round(score, 4), "severe_negative_count": self.severe_negative_count(titles),
                "titles": titles[:5], "ok": True,
            }
            return self._cache_set(symbol, result)
        except Exception as e:
            LOG.warning("Nieuws ophalen mislukt voor %s: %s", symbol, e)
            if isinstance(stale_value, dict):
                LOG.info("Gebruik oude nieuwscache voor %s", symbol)
                return stale_value
            result = {"term": term, "article_count": 0, "news_score": 0.0, "severe_negative_count": 0, "titles": [], "ok": False}
            return self._cache_set(symbol, result)

    def buy_gate(self, symbol: str) -> Dict[str, Any]:
        if not self.enabled:
            return {"allow": True, "reason": "news_disabled"}
        fail_open = to_bool(get_cfg(self.cfg, "news.fail_open", False), False)
        allow_without_news = to_bool(get_cfg(self.cfg, "news.allow_buy_without_news", True), True)
        min_score = to_float(get_cfg(self.cfg, "news.min_news_score_to_buy", -0.5), -0.5)
        block_on_severe = to_bool(get_cfg(self.cfg, "news.block_on_severe_negative", True), True)
        max_fng = int(to_float(get_cfg(self.cfg, "news.fear_greed_buy_max", 85), 85))
        fng = self.fear_greed()
        if fng.get("value") is not None and int(fng["value"]) > max_fng:
            return {"allow": False, "reason": f"fear_greed_too_high:{fng['value']}"}
        news = self.coin_news(symbol)
        if not news.get("ok", False) and not fail_open:
            return {"allow": False, "reason": "news_fetch_failed"}
        if int(news.get("severe_negative_count", 0)) > 0 and block_on_severe:
            return {"allow": False, "reason": "severe_negative_news"}
        if int(news.get("article_count", 0)) == 0 and not allow_without_news:
            return {"allow": False, "reason": "no_news"}
        if to_float(news.get("news_score", 0.0), 0.0) < min_score:
            return {"allow": False, "reason": f"news_score_below_min:{news.get('news_score')}"}
        return {"allow": True, "reason": f"news_ok:{news.get('news_score')}", "news": news, "fear_greed": fng}

    def forced_exit_reason(self, symbol: str) -> Optional[str]:
        if not self.enabled:
            return None
        news = self.coin_news(symbol)
        min_bad = to_float(get_cfg(self.cfg, "news.bad_news_score_to_exit", -2.0), -2.0)
        severe_force = to_bool(get_cfg(self.cfg, "news.force_exit_on_severe_negative", True), True)
        if severe_force and int(news.get("severe_negative_count", 0)) > 0:
            return "bad_news"
        if to_float(news.get("news_score", 0.0), 0.0) <= min_bad and int(news.get("article_count", 0)) > 0:
            return "bad_news"
        return None


class Bot:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.quote = str(get_cfg(cfg, "quote", "EUR")).upper()
        self.dry_run = to_bool(get_cfg(cfg, "dry_run", True), True)
        self.selective_execution_enabled = to_bool(
            get_cfg(cfg, "execution.selective_contracts_enabled", False),
            False,
        )
        self.selective_signals_file = str(
            get_cfg(cfg, "files.market_signals_file",
                    "/var/data/diamond_market_signals.csv")
        )
        self.selective_execution_cursor_file = str(
            get_cfg(cfg, "files.selective_execution_cursor_file",
                    "/var/data/diamond_selective_execution_cursor.json")
        )
        self.state_file = str(get_cfg(cfg, "files.state_file", "state.json"))
        self.trades_file = str(get_cfg(cfg, "files.trades_file", "transactions.csv"))
        self.canary_execution_file = str(
            get_cfg(
                cfg,
                "files.canary_execution_file",
                "/var/data/diamond_canary_execution.csv",
            )
        )
        self.control_file = str(get_cfg(cfg, "files.control_file", "/var/data/diamond_control.json"))
        self.live_approval_file = str(
            get_cfg(cfg, "files.live_approval_file", "/var/data/diamond_live_approval.json")
        )
        self.short_test_baseline_file = str(
            get_cfg(
                cfg,
                "files.short_test_baseline_file",
                "/var/data/diamond_short_test_baseline.json",
            )
        )
        self.short_test_report_file = str(
            get_cfg(
                cfg,
                "files.short_test_report_file",
                "/var/data/diamond_short_test_report.json",
            )
        )
        self.short_execution_file = str(
            get_cfg(
                cfg,
                "files.short_execution_file",
                "/var/data/diamond_short_execution.csv",
            )
        )
        self.short_test_archive_dir = str(
            get_cfg(
                cfg,
                "files.short_test_archive_dir",
                "/var/data/short_test_archive",
            )
        )

        self.state = load_state(self.state_file)
        self.short_strategy_baseline_mismatch = False

        if self.state.get("simulated_free_quote") is None:
            self.state["simulated_free_quote"] = to_float(
                get_cfg(self.cfg, "risk.simulated_quote_balance", 3000), 3000.0
            )

        # Een corrupt bestaand statebestand wordt niet stil overschreven met
        # een lege state. De recovery-gate blijft zichtbaar en blokkeert
        # nieuwe entries totdat de operator de situatie heeft beoordeeld.
        if not to_bool(
            self.state.get("recovery_required"),
            False,
        ):
            save_state(self.state_file, self.state)
        else:
            LOG.error(
                "RECOVERY_REQUIRED bij opstart | reden=%s",
                self.state.get("recovery_reason") or "onbekend",
            )

        self.ensure_short_test_baseline()

        self.last_status_log_ts = 0.0
        self.last_hold_log_ts: Dict[str, float] = {}
        self.last_skip_log_ts: Dict[str, float] = {}
        self.balance_cache: Dict[str, Any] = {"free": {}, "total": {}}

        load_dotenv()
        self.api_key = os.getenv("BITVAVO_API_KEY", "").strip()
        self.api_secret = os.getenv("BITVAVO_API_SECRET", "").strip()
        self.operator_id = os.getenv("BITVAVO_OPERATOR_ID", "").strip()

        if not self.dry_run:
            if not self.api_key or not self.api_secret:
                raise ValueError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt.")
            if not self.operator_id:
                raise ValueError("BITVAVO_OPERATOR_ID ontbreekt.")

        self.exchange = ccxt.bitvavo({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {"fetchMarkets": {"types": ["spot"]}},
        })
        self.exchange.load_markets()
        self.news = NewsEngine(cfg, self.exchange)
        # Sync uitgeschakeld - bestaande coins worden niet als posities geladen
        # Bot beheert alleen posities die hij zelf opent

    def _sync_positions_from_balance(self) -> None:
        """Bij opstart: laad echte Bitvavo saldi in state als posities ontbreken."""
        if self.state.get("positions"):
            return  # state heeft al posities, niet overschrijven
        try:
            balance = self.exchange.fetch_balance()
            dust = to_float(get_cfg(self.cfg, "risk.existing_balance_dust", 5), 5.0)
            tickers = self.exchange.fetch_tickers()
            synced = 0
            for asset, bal in (balance.get("total") or {}).items():
                if asset == self.quote:
                    continue
                sym = f"{asset}/{self.quote}"
                if sym not in self.exchange.markets:
                    continue
                amount = to_float(bal, 0.0)
                if amount <= 0:
                    continue
                ticker = tickers.get(sym) or {}
                price = to_float(ticker.get("last"), 0.0)
                if price <= 0:
                    continue
                eur_value = amount * price
                if eur_value < dust:
                    continue
                # Voeg toe als bestaande positie (opened_by_bot=False zodat hij niet verkoopt)
                self.state["positions"][sym] = {
                    "opened_by_bot": False,
                    "opened_at": utc_now_ts(),
                    "entry_price": price,
                    "amount": amount,
                    "quote_amount": eur_value,
                    "fees_buy_quote": 0.0,
                    "stop_loss": 0.0,
                    "take_profit": 0.0,
                    "highest_price": price,
                    "synced_from_balance": True,
                }
                synced += 1
            if synced > 0:
                save_state(self.state_file, self.state)
                LOG.info("SYNC: %s bestaande posities geladen uit Bitvavo saldo", synced)
        except Exception as e:
            LOG.warning("Kon posities niet synchroniseren: %s", e)

    def order_params(
        self,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if self.operator_id:
            params["operatorId"] = self.operator_id
        if client_order_id:
            # Bitvavo ondersteunt een eigen clientOrderId. Dit maakt een
            # live-order na een crash/restart eenduidig terugvindbaar.
            params["clientOrderId"] = client_order_id
        return params

    def legacy_spot_entry_route_enabled(self) -> bool:
        return not self.selective_execution_enabled

    def selective_execution_candidates(self) -> List[Dict[str, Any]]:
        if not self.selective_execution_enabled:
            return []
        contracts = new_execution_contracts(
            Path(self.selective_signals_file),
            Path(self.selective_execution_cursor_file),
        )

        for item in contracts:
            item["execution_mode"] = (
                "DRY_RUN" if self.dry_run else "CANARY_GATED"
            )
            LOG.info(
                "SELECTIVE EXECUTION | key=%s | %s | %s",
                item.get("candidate_key"),
                item.get("symbol"),
                item.get("strategy"),
            )

        return contracts

    def selective_contract_to_long_signal(
        self,
        contract: Dict[str, Any],
    ) -> Dict[str, Any]:
        if str(contract.get("side") or "").upper() != "LONG":
            raise ValueError("SELECTIVE_EXECUTION_LONG_ONLY")

        entry = to_float(contract.get("entry_price"), 0.0)
        stop = to_float(contract.get("stop_loss"), 0.0)
        target = to_float(contract.get("take_profit"), 0.0)

        if entry <= 0 or stop <= 0 or target <= 0:
            raise ValueError("SELECTIVE_EXECUTION_INVALID_PRICES")

        return {
            "candidate_key": str(contract.get("candidate_key") or ""),
            "signal_candle_ts": str(
                contract.get("candle_timestamp") or ""
            ),
            "candle_timestamp": str(
                contract.get("candle_timestamp") or ""
            ),
            "close": entry,
            "stop_loss": stop,
            "take_profit": target,
            "tech_score": to_float(contract.get("score"), 0.0),
            "rsi": 0.0,
            "atr_pct": 0.0,
            "strategy": str(contract.get("strategy") or ""),
            "market_regime": str(
                contract.get("market_regime") or ""
            ),
            "selection_reason": str(
                contract.get("selection_reason") or "UNKNOWN"
            ),
        }

    def execute_selective_contracts(self) -> int:
        contracts = self.selective_execution_candidates()

        for contract in contracts:
            signal = self.selective_contract_to_long_signal(
                contract
            )

            self.try_buy_symbol(
                str(contract.get("symbol") or ""),
                precomputed_signal=signal,
                precomputed_news_gate={
                    "allow": True,
                    "reason": "SELECTIVE_CONTRACT",
                },
            )

        return len(contracts)

    def long_order_key(
        self,
        symbol: str,
        signal: Dict[str, Any],
    ) -> str:
        candidate_key = str(
            signal.get("candidate_key") or ""
        ).strip()

        if candidate_key:
            return candidate_key

        candle_key = str(
            signal.get("signal_candle_ts")
            or signal.get("candle_timestamp")
            or signal.get("close")
            or ""
        )
        return f"LONG|{symbol.upper()}|{candle_key}"

    def sell_order_key(
        self,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
        sell_amount: float,
    ) -> str:
        """Maakt een stabiele identiteit voor één concrete live-verkoop."""
        opened_at = to_float(
            position.get("opened_at"),
            0.0,
        )
        amount_key = f"{float(sell_amount):.16g}"
        reason_key = str(reason or "sell").strip().lower()
        return (
            f"SELL|{symbol.upper()}|{opened_at:.6f}|"
            f"{reason_key}|{amount_key}"
        )

    def pending_sell_for_symbol(
        self,
        symbol: str,
    ) -> Optional[Dict[str, Any]]:
        for record in (
            self.state.get("pending_orders")
            or {}
        ).values():
            if not isinstance(record, dict):
                continue
            if (
                str(record.get("side") or "").lower() == "sell"
                and str(record.get("symbol") or "").upper()
                == symbol.upper()
            ):
                return record
        return None

    def client_order_id_for_key(
        self,
        order_key: str,
    ) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"diamond-trader:{order_key}",
            )
        )

    def live_approval(self) -> Dict[str, Any]:
        path = Path(getattr(
            self,
            "live_approval_file",
            "/var/data/diamond_live_approval.json",
        ))
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def canary_new_entry_gate(
        self,
        stake_quote: float,
        canary_trade_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Fail-closed approval gate voor iedere nieuwe echte BUY.

        CANARY:
        - uitsluitend historische/afgebakende canaryfase;
        - maximaal het goedgekeurde aantal canary-trades;
        - aparte harde stake-limiet.

        LIVE:
        - alleen toegestaan nadat de canaryfase is afgerond;
        - vereist expliciete LIVE approval;
        - vereist expliciete graduated_from_canary bevestiging;
        - behoudt expiry en harde stake-limiet.
        """
        if self.dry_run:
            return {
                "allow": True,
                "reason": "dry_run",
                "mode": "DRY_RUN",
            }

        approval = self.live_approval()

        approved = bool(
            str(
                approval.get("status", "")
            ).upper() == "APPROVED"
            or approval.get("approved") is True
        )

        if not approved:
            return {
                "allow": False,
                "reason": "approval_missing_or_revoked",
            }

        mode = str(
            approval.get("mode") or ""
        ).strip().upper()

        if mode not in {
            "CANARY",
            "LIVE",
        }:
            return {
                "allow": False,
                "reason": "approval_mode_invalid",
            }

        if not to_bool(
            approval.get("allow_new_entries"),
            False,
        ):
            return {
                "allow": False,
                "reason": "new_entries_not_allowed",
            }

        expires_at = str(
            approval.get("expires_at") or ""
        ).strip()

        if not expires_at:
            return {
                "allow": False,
                "reason": "approval_expiry_missing",
            }

        try:
            expiry = datetime.fromisoformat(
                expires_at.replace(
                    "Z",
                    "+00:00",
                )
            )

            if expiry.tzinfo is None:
                expiry = expiry.replace(
                    tzinfo=timezone.utc
                )

            if (
                expiry.astimezone(timezone.utc)
                <= datetime.now(timezone.utc)
            ):
                return {
                    "allow": False,
                    "reason": "approval_expired",
                }

        except Exception:
            return {
                "allow": False,
                "reason": "approval_expiry_invalid",
            }

        if mode == "CANARY":
            hard_max = to_float(
                get_cfg(
                    self.cfg,
                    "risk.canary_hard_max_stake_quote",
                    30.0,
                ),
                30.0,
            )
        else:
            hard_max = to_float(
                get_cfg(
                    self.cfg,
                    "risk.live_hard_max_stake_quote",
                    130.0,
                ),
                130.0,
            )

        approved_max = to_float(
            approval.get("max_stake_quote"),
            0.0,
        )

        allowed_stake = min(
            hard_max,
            approved_max,
        )

        if allowed_stake <= 0:
            return {
                "allow": False,
                "reason": "invalid_max_stake",
            }

        if float(stake_quote) > (
            allowed_stake + 1e-9
        ):
            return {
                "allow": False,
                "reason": (
                    "stake_above_canary_limit"
                    if mode == "CANARY"
                    else "stake_above_live_limit"
                ),
            }

        if mode == "CANARY":
            max_trades = int(
                to_float(
                    approval.get(
                        "max_canary_trades"
                    ),
                    0,
                )
            )

            if (
                max_trades < 1
                or max_trades > 5
            ):
                return {
                    "allow": False,
                    "reason": "invalid_max_canary_trades",
                }

            trade_number = (
                canary_trade_number
            )

            if trade_number is None:
                trade_number = int(
                    to_float(
                        self.state.get(
                            "canary_trade_sequence"
                        ),
                        0,
                    )
                ) + 1

            if (
                int(trade_number) < 1
                or int(trade_number)
                > max_trades
            ):
                return {
                    "allow": False,
                    "reason": "canary_trade_limit_reached",
                }

            return {
                "allow": True,
                "reason": "approved_canary_entry",
                "mode": "CANARY",
                "trade_number": int(
                    trade_number
                ),
                "allowed_stake": allowed_stake,
            }

        # Vanaf hier uitsluitend normale LIVE-modus.

        if not to_bool(
            get_cfg(
                self.cfg,
                "execution.live_mode_enabled",
                False,
            ),
            False,
        ):
            return {
                "allow": False,
                "reason": "live_mode_disabled",
            }

        required_canary = max(
            5,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "execution.require_canary_graduation_trades",
                        5,
                    ),
                    5,
                )
            ),
        )

        completed_canary = int(
            to_float(
                self.state.get(
                    "canary_trade_sequence"
                ),
                0,
            )
        )

        if completed_canary < required_canary:
            return {
                "allow": False,
                "reason": "canary_graduation_incomplete",
            }

        if not to_bool(
            approval.get(
                "graduated_from_canary"
            ),
            False,
        ):
            return {
                "allow": False,
                "reason": "live_graduation_not_approved",
            }

        # ONE_SHOT_LIVE_APPROVAL
        #
        # Iedere LIVE approval heeft een sequence-start en een hard
        # maximum aantal nieuwe entries. De huidige productieconfig
        # begrenst dit op precies één entry per approval.
        sequence_start = int(
            to_float(
                approval.get("entry_sequence_start"),
                -1,
            )
        )

        max_live_entries = int(
            to_float(
                approval.get("max_live_entries"),
                0,
            )
        )

        hard_entry_cap = int(
            to_float(
                get_cfg(
                    self.cfg,
                    "execution.live_max_entries_per_approval",
                    1,
                ),
                1,
            )
        )

        if (
            max_live_entries < 1
            or max_live_entries > hard_entry_cap
        ):
            return {
                "allow": False,
                "reason": "invalid_max_live_entries",
            }

        if (
            sequence_start < required_canary
            or sequence_start > completed_canary
        ):
            return {
                "allow": False,
                "reason": "invalid_live_entry_sequence_start",
            }

        candidate_sequence = (
            int(canary_trade_number)
            if canary_trade_number is not None
            else completed_canary + 1
        )

        first_allowed = sequence_start + 1
        last_allowed = (
            sequence_start + max_live_entries
        )

        if (
            candidate_sequence < first_allowed
            or candidate_sequence > last_allowed
        ):
            return {
                "allow": False,
                "reason": "live_entry_limit_reached",
            }

        return {
            "allow": True,
            "reason": "approved_live_entry",
            "mode": "LIVE",
            "allowed_stake": allowed_stake,
            "completed_canary": completed_canary,
            "entry_sequence": candidate_sequence,
            "max_live_entries": max_live_entries,
        }

    def entries_blocked_by_recovery(self) -> bool:
        pending = self.state.get("pending_orders") or {}
        return bool(
            to_bool(
                self.state.get("recovery_required"),
                False,
            )
            or pending
        )

    def set_recovery_required(
        self,
        reason: str,
    ) -> None:
        self.state["recovery_required"] = True
        self.state["recovery_reason"] = str(reason or "onbekend")
        save_state(self.state_file, self.state)

    def clear_recovery_if_safe(self) -> None:
        pending = self.state.get("pending_orders") or {}
        if pending:
            return
        self.state["recovery_required"] = False
        self.state["recovery_reason"] = ""

    def prepare_pending_long_order(
        self,
        order_key: str,
        symbol: str,
        signal: Dict[str, Any],
        stake_quote: float,
        spread_pct: float,
        protected_base_amount: float,
    ) -> Dict[str, Any]:
        pending = self.state.setdefault(
            "pending_orders",
            {},
        )
        if order_key in pending:
            return pending[order_key]

        canary_trade_number = int(
            to_float(
                self.state.get("canary_trade_sequence"),
                0,
            )
        ) + 1
        self.state["canary_trade_sequence"] = canary_trade_number

        record = {
            "order_key": order_key,
            "canary_trade_number": canary_trade_number,
            "clientOrderId": self.client_order_id_for_key(order_key),
            "symbol": symbol,
            "side": "buy",
            "status": "PREPARED",
            "created_at": now_iso(),
            "created_at_ts": utc_now_ts(),
            "submitted_at": None,
            "exchange_order_id": None,
            "stake_quote": float(stake_quote),
            "spread_pct": float(spread_pct),
            "protected_base_amount": float(protected_base_amount),
            "signal": {
                "candidate_key": str(
                    signal.get("candidate_key") or ""
                ),
                "strategy": str(signal.get("strategy") or ""),
                "market_regime": str(
                    signal.get("market_regime") or ""
                ),
                "selection_reason": str(
                    signal.get("selection_reason") or ""
                ),
                "signal_candle_ts": str(
                    signal.get("signal_candle_ts")
                    or ""
                ),
                "close": to_float(signal.get("close"), 0.0),
                "stop_loss": to_float(signal.get("stop_loss"), 0.0),
                "take_profit": to_float(signal.get("take_profit"), 0.0),
                "tech_score": to_float(signal.get("tech_score"), 0.0),
                "rsi": to_float(signal.get("rsi"), 0.0),
                "atr_pct": to_float(signal.get("atr_pct"), 0.0),
            },
        }
        pending[order_key] = record
        save_state(self.state_file, self.state)
        return record

    def prepare_pending_sell_order(
        self,
        order_key: str,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
        sell_amount: float,
    ) -> Dict[str, Any]:
        """Slaat een live SELL atomair op vóór create_order kan starten."""
        pending = self.state.setdefault(
            "pending_orders",
            {},
        )
        if order_key in pending:
            return pending[order_key]

        record = {
            "order_key": order_key,
            "clientOrderId": self.client_order_id_for_key(order_key),
            "symbol": symbol,
            "side": "sell",
            "status": "PREPARED",
            "created_at": now_iso(),
            "created_at_ts": utc_now_ts(),
            "submitted_at": None,
            "exchange_order_id": None,
            "reason": str(reason or "sell"),
            "intended_amount": float(sell_amount),
            "canary_trade_number": int(
                to_float(
                    position.get("canary_trade_number"),
                    0,
                )
            ),
            "position_snapshot": {
                "opened_at": to_float(
                    position.get("opened_at"),
                    0.0,
                ),
                "tracked_amount": to_float(
                    position.get("amount"),
                    0.0,
                ),
                "entry_quote_total": to_float(
                    position.get("quote_amount"),
                    0.0,
                ),
                "fee_buy_total": to_float(
                    position.get("fees_buy_quote"),
                    0.0,
                ),
                "protected_base_amount": to_float(
                    position.get("protected_base_amount"),
                    0.0,
                ),
            },
        }
        pending[order_key] = record
        save_state(self.state_file, self.state)
        return record

    def canary_open_event(
        self,
        symbol: str,
        position: Dict[str, Any],
        recovered: bool = False,
    ) -> None:
        if self.dry_run:
            return

        append_canary_execution_csv(
            self.canary_execution_file,
            {
                "ts": now_iso(),
                "event": "OPEN",
                "canary_trade_number": int(
                    to_float(
                        position.get("canary_trade_number"),
                        0,
                    )
                ),
                "market": symbol,
                "side": "BUY",
                "reason": "live_entry",
                "reference_ask": position.get("entry_reference_ask"),
                "buy_fill_price": position.get("entry_price"),
                "buy_slippage_pct": position.get("entry_slippage_pct"),
                "buy_slippage_status": classify_slippage_status(
                    to_float(
                        position.get("entry_slippage_pct"),
                        0.0,
                    )
                ),
                "buy_spread_pct": position.get("entry_spread_pct"),
                "buy_fee_quote": position.get("fees_buy_quote"),
                "buy_order_id": position.get("exchange_order_id"),
                "buy_client_order_id": position.get("client_order_id"),
                "base_amount": position.get("amount"),
                "entry_quote_actual": position.get("quote_amount"),
                "recovery_used": bool(recovered),
                "overall_status": classify_slippage_status(
                    to_float(
                        position.get("entry_slippage_pct"),
                        0.0,
                    )
                ),
                "dry_run": False,
            },
        )

    def canary_close_event(
        self,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
        order: Dict[str, Any],
        filled_amount: float,
        exit_quote_actual: float,
        sell_fee_quote: float,
        actual_net_pnl_quote: float,
        holding_time_min: float,
        reference_bid: float,
        sell_spread_pct: float,
        recovered: bool,
    ) -> None:
        if self.dry_run:
            return

        reference_ask = to_float(
            position.get("entry_reference_ask"),
            to_float(position.get("entry_price"), 0.0),
        )
        buy_fill = to_float(
            position.get("entry_price"),
            0.0,
        )
        buy_slippage = to_float(
            position.get("entry_slippage_pct"),
            execution_slippage_pct(
                "buy",
                reference_ask,
                buy_fill,
            ),
        )
        sell_fill = to_float(
            order.get("average")
            or order.get("price"),
            0.0,
        )
        sell_slippage = execution_slippage_pct(
            "sell",
            reference_bid,
            sell_fill,
        )

        buy_slippage_status = classify_slippage_status(
            buy_slippage
        )
        sell_slippage_status = classify_slippage_status(
            sell_slippage
        )
        overall_status = combine_execution_status(
            buy_slippage_status,
            sell_slippage_status,
        )

        fee_pct = to_float(
            get_cfg(self.cfg, "taker_fee_pct", 0.25),
            0.25,
        )
        expected_net = expected_canary_net_pnl_quote(
            filled_amount,
            reference_ask,
            reference_bid,
            fee_pct,
        )
        pnl_difference = actual_net_pnl_quote - expected_net

        entry_amount = max(
            to_float(position.get("amount"), 0.0),
            1e-12,
        )
        fraction = min(
            1.0,
            max(0.0, filled_amount) / entry_amount,
        )
        allocated_buy_fee = (
            to_float(
                position.get("fees_buy_quote"),
                0.0,
            )
            * fraction
        )

        append_canary_execution_csv(
            self.canary_execution_file,
            {
                "ts": now_iso(),
                "event": "CLOSE",
                "canary_trade_number": int(
                    to_float(
                        position.get("canary_trade_number"),
                        0,
                    )
                ),
                "market": symbol,
                "side": "SELL",
                "reason": reason,
                "reference_ask": reference_ask,
                "buy_fill_price": buy_fill,
                "buy_slippage_pct": buy_slippage,
                "buy_slippage_status": buy_slippage_status,
                "buy_spread_pct": position.get("entry_spread_pct"),
                "buy_fee_quote": allocated_buy_fee,
                "buy_order_id": position.get("exchange_order_id"),
                "buy_client_order_id": position.get("client_order_id"),
                "reference_bid": reference_bid,
                "sell_fill_price": sell_fill,
                "sell_slippage_pct": sell_slippage,
                "sell_slippage_status": sell_slippage_status,
                "sell_spread_pct": sell_spread_pct,
                "sell_fee_quote": sell_fee_quote,
                "sell_order_id": order.get("id"),
                "sell_client_order_id": (
                    order.get("clientOrderId")
                    or (order.get("info") or {}).get("clientOrderId")
                    or ""
                ),
                "base_amount": filled_amount,
                "entry_quote_actual": (
                    to_float(
                        position.get("quote_amount"),
                        0.0,
                    )
                    * fraction
                ),
                "exit_quote_actual": exit_quote_actual,
                "expected_net_pnl_quote": expected_net,
                "actual_net_pnl_quote": actual_net_pnl_quote,
                "pnl_difference_quote": pnl_difference,
                "total_fees_quote": allocated_buy_fee + sell_fee_quote,
                "holding_time_min": holding_time_min,
                "recovery_used": bool(recovered),
                "overall_status": overall_status,
                "dry_run": False,
            },
        )

    def mark_pending_submitting(
        self,
        order_key: str,
    ) -> None:
        record = (
            self.state.get("pending_orders", {})
            .get(order_key)
        )
        if not isinstance(record, dict):
            raise RuntimeError(
                f"Pending order ontbreekt vóór submit: {order_key}"
            )
        record["status"] = "SUBMITTING"
        record["submitted_at"] = now_iso()
        save_state(self.state_file, self.state)

    def update_pending_from_order(
        self,
        order_key: str,
        order: Dict[str, Any],
        status: Optional[str] = None,
    ) -> None:
        record = (
            self.state.get("pending_orders", {})
            .get(order_key)
        )
        if not isinstance(record, dict):
            return

        order_status = str(
            status
            or order.get("status")
            or record.get("status")
            or "SUBMITTED"
        )
        record["status"] = order_status.upper()
        record["exchange_order_id"] = (
            order.get("id")
            or record.get("exchange_order_id")
        )
        record["clientOrderId"] = str(
            order.get("clientOrderId")
            or (order.get("info") or {}).get("clientOrderId")
            or record.get("clientOrderId")
            or ""
        )

        for source, target in (
            ("average", "fill_price"),
            ("filled", "filled_amount"),
            ("cost", "fill_cost"),
        ):
            value = order.get(source)
            if value is not None:
                record[target] = to_float(value, 0.0)

        record["updated_at"] = now_iso()
        save_state(self.state_file, self.state)

    def abandon_pending_after_confirmed_rejection(
        self,
        order_key: str,
        reason: str,
    ) -> None:
        pending = self.state.get("pending_orders") or {}
        if order_key in pending:
            pending.pop(order_key, None)
        self.clear_recovery_if_safe()
        save_state(self.state_file, self.state)
        LOG.warning(
            "PENDING ORDER AFGESLOTEN | key=%s | reden=%s",
            order_key,
            reason,
        )

    def fetch_order_by_client_order_id(
        self,
        symbol: str,
        client_order_id: str,
    ) -> Optional[Dict[str, Any]]:
        last_not_found: Optional[Exception] = None

        for attempt in range(3):
            try:
                # Bitvavo gebruikt clientOrderId wanneer zowel orderId als
                # clientOrderId zijn meegegeven. Zo kunnen we via de unified
                # CCXT fetch_order dezelfde order na een restart terugvinden.
                order = self.exchange.fetch_order(
                    client_order_id,
                    symbol,
                    {"clientOrderId": client_order_id},
                )
                if isinstance(order, dict):
                    return order
            except ccxt.OrderNotFound as exc:
                last_not_found = exc
                if attempt < 2:
                    time.sleep(1.0 + attempt)
                    continue
            except Exception:
                raise

        if last_not_found is not None:
            return None
        return None

    def recover_position_from_pending(
        self,
        order_key: str,
        record: Dict[str, Any],
        order: Dict[str, Any],
    ) -> bool:
        symbol = str(record.get("symbol") or "")
        if not symbol:
            return False

        signal = record.get("signal") or {}
        price = to_float(
            order.get("average")
            or order.get("price")
            or record.get("fill_price"),
            0.0,
        )
        amount = to_float(
            order.get("filled")
            or record.get("filled_amount"),
            0.0,
        )
        quote_amount = to_float(
            order.get("cost")
            or record.get("fill_cost"),
            amount * price,
        )

        if min(price, amount, quote_amount) <= 0:
            return False

        fee_quote = self.order_fee_quote(
            order,
            quote_amount,
            symbol,
            price,
        )

        self.state.setdefault(
            "positions",
            {},
        )[symbol] = {
            "opened_by_bot": True,
            "candidate_key": str(
                signal.get("candidate_key")
                or order_key
                or ""
            ),
            "strategy": str(signal.get("strategy") or ""),
            "market_regime": str(
                signal.get("market_regime") or ""
            ),
            "selection_reason": str(
                signal.get("selection_reason") or ""
            ),
            "signal_candle_ts": str(
                signal.get("signal_candle_ts") or ""
            ),
            "opened_at": to_float(
                record.get("created_at_ts"),
                utc_now_ts(),
            ),
            "entry_price": price,
            "amount": amount,
            "quote_amount": quote_amount,
            "fees_buy_quote": fee_quote,
            "stop_loss": to_float(signal.get("stop_loss"), 0.0),
            "take_profit": to_float(signal.get("take_profit"), 0.0),
            "highest_price": price,
            "news_score_at_entry": 0.0,
            "fear_greed_at_entry": None,
            "tech_score_at_entry": to_float(
                signal.get("tech_score"),
                0.0,
            ),
            "protected_base_amount": to_float(
                record.get("protected_base_amount"),
                0.0,
            ),
            "recovered_from_pending": True,
            "client_order_id": str(
                record.get("clientOrderId")
                or ""
            ),
            "exchange_order_id": (
                order.get("id")
                or record.get("exchange_order_id")
            ),
            "canary_trade_number": int(
                to_float(
                    record.get("canary_trade_number"),
                    0,
                )
            ),
            "entry_reference_ask": to_float(
                record.get("reference_ask"),
                price,
            ),
            "entry_slippage_pct": execution_slippage_pct(
                "buy",
                to_float(record.get("reference_ask"), price),
                price,
            ),
            "entry_slippage_status": classify_slippage_status(
                execution_slippage_pct(
                    "buy",
                    to_float(record.get("reference_ask"), price),
                    price,
                )
            ),
            "entry_spread_pct": to_float(
                record.get("execution_spread_pct"),
                to_float(record.get("spread_pct"), 0.0),
            ),
        }

        self.state.get(
            "pending_orders",
            {},
        ).pop(order_key, None)
        self.clear_recovery_if_safe()
        save_state(self.state_file, self.state)

        if not self.dry_run:
            self.canary_open_event(
                symbol,
                self.state["positions"][symbol],
                recovered=True,
            )

        LOG.warning(
            "RECOVERY POSITIE HERSTELD | %s | key=%s | amount=%s | prijs=%.8f",
            symbol,
            order_key,
            amount,
            price,
        )
        return True

    def apply_confirmed_sell_fill(
        self,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
        order: Dict[str, Any],
        fallback_price: float,
        fallback_amount: float,
        pending_order_key: Optional[str] = None,
        spread_pct: Optional[float] = None,
        reference_bid: Optional[float] = None,
        recovered: bool = False,
    ) -> Dict[str, Any]:
        """
        Verwerkt één bevestigde SELL-fill exact één keer.

        Positie-aanpassing, PnL en het verwijderen van pending worden samen
        atomair opgeslagen. Daardoor kan een restart dezelfde SELL niet nog
       maals administratief of op de exchange uitvoeren.
        """
        tracked_amount = to_float(
            position.get("amount"),
            0.0,
        )
        if tracked_amount <= 0:
            raise RuntimeError(
                f"Geen geldige bot-hoeveelheid voor {symbol}"
            )

        price = to_float(
            order.get("average")
            or order.get("price"),
            fallback_price,
        )
        filled_amount = to_float(
            order.get("filled"),
            fallback_amount,
        )
        quote_amount = to_float(
            order.get("cost"),
            filled_amount * price,
        )

        if min(price, filled_amount, quote_amount) <= 0:
            raise RuntimeError(
                f"Bevestigde SELL-fill ongeldig voor {symbol}: "
                f"price={price}, filled={filled_amount}, cost={quote_amount}"
            )

        tolerance = max(
            1e-12,
            tracked_amount * 1e-8,
        )
        if filled_amount > tracked_amount + tolerance:
            self.set_recovery_required(
                f"RECOVERY_REQUIRED:sell_fill_exceeds_position:{symbol}"
            )
            raise RuntimeError(
                f"SELL-fill groter dan botpositie voor {symbol}: "
                f"filled={filled_amount}, tracked={tracked_amount}"
            )

        fee_sell_quote = self.order_fee_quote(
            order,
            quote_amount,
            symbol,
            price,
        )

        entry_quote_total = to_float(
            position.get("quote_amount"),
            0.0,
        )
        fee_buy_total = to_float(
            position.get("fees_buy_quote"),
            0.0,
        )
        fraction = min(
            1.0,
            filled_amount / max(tracked_amount, 1e-12),
        )
        allocated_entry_quote = entry_quote_total * fraction
        allocated_buy_fee = fee_buy_total * fraction

        net_pnl_quote = (
            quote_amount
            - fee_sell_quote
            - allocated_entry_quote
            - allocated_buy_fee
        )
        holding_time_min = minutes_since(
            float(
                position.get(
                    "opened_at",
                    utc_now_ts(),
                )
            )
        )

        resolved_reference_bid = to_float(
            reference_bid,
            fallback_price,
        )
        resolved_spread_pct = to_float(
            spread_pct,
            0.0,
        )

        if not self.dry_run:
            self.canary_close_event(
                symbol=symbol,
                position=position,
                reason=reason,
                order=order,
                filled_amount=filled_amount,
                exit_quote_actual=quote_amount,
                sell_fee_quote=fee_sell_quote,
                actual_net_pnl_quote=net_pnl_quote,
                holding_time_min=holding_time_min,
                reference_bid=resolved_reference_bid,
                sell_spread_pct=resolved_spread_pct,
                recovered=recovered,
            )

        self.state["pnl_quote"] = (
            to_float(
                self.state.get("pnl_quote"),
                0.0,
            )
            + net_pnl_quote
        )
        self.state["trades"] = int(
            self.state.get("trades", 0)
        ) + 1
        if net_pnl_quote > 0:
            self.state["wins"] = int(
                self.state.get("wins", 0)
            ) + 1

        remaining_amount = max(
            0.0,
            tracked_amount - filled_amount,
        )

        if (
            remaining_amount > tolerance
            and fraction < 0.999999
        ):
            position["amount"] = remaining_amount
            position["quote_amount"] = max(
                0.0,
                entry_quote_total - allocated_entry_quote,
            )
            position["fees_buy_quote"] = max(
                0.0,
                fee_buy_total - allocated_buy_fee,
            )
            self.state.setdefault(
                "positions",
                {},
            )[symbol] = position
            LOG.warning(
                "GEDEELTELIJKE VERKOOP %s | verkocht=%s | resterend=%s | recovery=%s",
                symbol,
                filled_amount,
                remaining_amount,
                recovered,
            )
        else:
            self.state.setdefault(
                "positions",
                {},
            ).pop(symbol, None)
            self.state.setdefault(
                "cooldown",
                {},
            )[symbol] = utc_now_ts()

        if self.dry_run:
            current_simulated = to_float(
                self.state.get("simulated_free_quote"),
                0.0,
            )
            self.state["simulated_free_quote"] = (
                current_simulated
                + quote_amount
                - fee_sell_quote
            )

        if pending_order_key:
            self.state.get(
                "pending_orders",
                {},
            ).pop(
                pending_order_key,
                None,
            )
            self.clear_recovery_if_safe()

        # Positie/PnL + pending verwijderen vormen één atomische state-save.
        save_state(
            self.state_file,
            self.state,
        )
        self.refresh_balance_cache()

        if spread_pct is None:
            spread_pct = 0.0

        append_trade_csv(
            self.trades_file,
            {
                "ts": now_iso(),
                "market": symbol,
                "side": "SELL",
                "price": round(price, 12),
                "base_amount": filled_amount,
                "quote_amount": round(quote_amount, 8),
                "fees_quote": round(fee_sell_quote, 8),
                "spread_pct": round(float(spread_pct), 6),
                "net_pnl_quote": round(net_pnl_quote, 8),
                "holding_time_min": round(holding_time_min, 2),
                "reason": reason,
                "dry_run": self.dry_run,
            },
        )

        LOG.info(
            "VERKOOP %s | reden=%s prijs=%.8f amount=%s "
            "netto_pnl=%+.4f %s dry=%s recovery=%s",
            symbol,
            reason,
            price,
            filled_amount,
            net_pnl_quote,
            self.quote,
            self.dry_run,
            recovered,
        )

        return {
            "price": price,
            "filled_amount": filled_amount,
            "quote_amount": quote_amount,
            "fee_sell_quote": fee_sell_quote,
            "net_pnl_quote": net_pnl_quote,
            "remaining_amount": remaining_amount,
        }

    def recover_sell_from_pending(
        self,
        order_key: str,
        record: Dict[str, Any],
        order: Dict[str, Any],
    ) -> bool:
        symbol = str(record.get("symbol") or "")
        if not symbol:
            return False

        position = (
            self.state.get("positions", {})
            .get(symbol)
        )
        if (
            not isinstance(position, dict)
            or not to_bool(
                position.get("opened_by_bot"),
                False,
            )
        ):
            self.state["recovery_required"] = True
            self.state["recovery_reason"] = (
                f"RECOVERY_REQUIRED:sell_position_missing:{symbol}"
            )
            save_state(self.state_file, self.state)
            return False

        intended_amount = to_float(
            record.get("intended_amount"),
            0.0,
        )
        fallback_price = to_float(
            record.get("reference_bid"),
            to_float(
                order.get("average")
                or order.get("price"),
                0.0,
            ),
        )

        self.apply_confirmed_sell_fill(
            symbol=symbol,
            position=position,
            reason=str(record.get("reason") or "sell_recovery"),
            order=order,
            fallback_price=fallback_price,
            fallback_amount=intended_amount,
            pending_order_key=order_key,
            spread_pct=to_float(
                record.get("execution_spread_pct"),
                to_float(record.get("spread_pct"), 0.0),
            ),
            reference_bid=to_float(
                record.get("reference_bid"),
                fallback_price,
            ),
            recovered=True,
        )

        LOG.warning(
            "RECOVERY VERKOOP HERSTELD | %s | key=%s | order=%s",
            symbol,
            order_key,
            order.get("id") or record.get("exchange_order_id"),
        )
        return True

    def reconcile_pending_orders(self) -> None:
        pending = self.state.get("pending_orders") or {}
        if not pending:
            return

        # Iedere pending live-order is een recovery gate. Nieuwe entries blijven
        # geblokkeerd totdat BUY of SELL eenduidig met Bitvavo is vergeleken.
        self.state["recovery_required"] = True
        self.state["recovery_reason"] = "RECOVERY_REQUIRED:pending_order"
        save_state(self.state_file, self.state)

        if not self.api_key or not self.api_secret:
            LOG.error(
                "RECOVERY_REQUIRED | pending order aanwezig maar API-credentials ontbreken"
            )
            return

        changed = False

        for order_key, record in list(pending.items()):
            if not isinstance(record, dict):
                self.state["recovery_reason"] = (
                    f"RECOVERY_REQUIRED:pending_invalid:{order_key}"
                )
                changed = True
                continue

            symbol = str(record.get("symbol") or "")
            side = str(record.get("side") or "buy").lower()
            status = str(record.get("status") or "").upper()

            if side not in {"buy", "sell"}:
                self.state["recovery_reason"] = (
                    f"RECOVERY_REQUIRED:pending_side_invalid:{order_key}"
                )
                changed = True
                continue

            # PREPARED is vóór SUBMITTING atomair opgeslagen. create_order was
            # dus aantoonbaar nog niet gestart en de record mag veilig weg.
            if status == "PREPARED":
                pending.pop(order_key, None)
                changed = True
                continue

            existing = (
                self.state.get("positions", {})
                .get(symbol)
            )

            # Alleen voor BUY bewijst een bestaande botpositie dat de lokale
            # position-save al klaar was. Voor SELL is juist het verdwijnen of
            # verkleinen van de positie onderdeel van de te herstellen actie.
            if (
                side == "buy"
                and isinstance(existing, dict)
                and to_bool(
                    existing.get("opened_by_bot"),
                    False,
                )
            ):
                pending.pop(order_key, None)
                changed = True
                continue

            client_order_id = str(
                record.get("clientOrderId")
                or ""
            )
            if not symbol or not client_order_id:
                self.state["recovery_reason"] = (
                    f"RECOVERY_REQUIRED:pending_invalid:{order_key}"
                )
                changed = True
                continue

            try:
                order = self.fetch_order_by_client_order_id(
                    symbol,
                    client_order_id,
                )
            except Exception as exc:
                self.state["recovery_reason"] = (
                    "RECOVERY_REQUIRED:exchange_unavailable:"
                    f"{type(exc).__name__}"
                )
                LOG.error(
                    "RECOVERY ordercontrole mislukt | %s | %s | %s",
                    side.upper(),
                    symbol,
                    exc,
                )
                changed = True
                continue

            if order is None:
                # SUBMITTING blijft bewust ambigu: nooit opnieuw verzenden als
                # Bitvavo de request mogelijk wel heeft ontvangen.
                self.state["recovery_reason"] = (
                    "RECOVERY_REQUIRED:order_not_found:"
                    f"{client_order_id}"
                )
                changed = True
                continue

            record["exchange_order_id"] = (
                order.get("id")
                or record.get("exchange_order_id")
            )
            order_status = str(
                order.get("status")
                or ""
            ).lower()
            filled = to_float(order.get("filled"), 0.0)

            terminal = order_status in {
                "closed",
                "filled",
                "canceled",
                "cancelled",
                "rejected",
                "expired",
            }

            if terminal and filled > 0:
                recovered_ok = False
                if side == "sell":
                    recovered_ok = self.recover_sell_from_pending(
                        order_key,
                        record,
                        order,
                    )
                else:
                    recovered_ok = self.recover_position_from_pending(
                        order_key,
                        record,
                        order,
                    )

                if recovered_ok:
                    changed = True
                continue

            if terminal and filled <= 0:
                pending.pop(order_key, None)
                changed = True
                continue

            # Open/partieel uitgevoerde BUY of SELL blijft pending. Daardoor
            # kunnen noch een dubbele BUY noch een tweede SELL ontstaan.
            record["status"] = (
                order_status.upper()
                if order_status
                else "OPEN"
            )
            record["filled_amount"] = filled
            record["updated_at"] = now_iso()
            self.state["recovery_reason"] = (
                "RECOVERY_REQUIRED:order_open:"
                f"{client_order_id}"
            )
            changed = True

        if not self.state.get("pending_orders"):
            self.clear_recovery_if_safe()
            changed = True

        if changed:
            save_state(self.state_file, self.state)

    def safe_fetch_balance(self) -> Dict[str, Any]:
        if self.dry_run:
            simulated_quote = to_float(
                self.state.get("simulated_free_quote"),
                to_float(get_cfg(self.cfg, "risk.simulated_quote_balance", 3000), 3000.0),
            )
            return {
                "free": {self.quote: simulated_quote},
                "total": {self.quote: simulated_quote},
            }
        last_error = None
        for i in range(3):
            try:
                return self.exchange.fetch_balance()
            except Exception as e:
                last_error = e
                LOG.debug("fetch_balance poging %s mislukt: %s", i + 1, e)
                time.sleep(1.5 * (i + 1))
        LOG.warning("fetch_balance mislukt na 3 pogingen: %s", last_error)
        raise RuntimeError(f"Kon saldo niet ophalen: {last_error}")

    def refresh_balance_cache(self) -> None:
        try:
            self.balance_cache = self.safe_fetch_balance()
        except Exception as e:
            LOG.warning("Kon balanscache niet verversen: %s", e)

    def asset_balance(self, asset: str) -> float:
        asset = str(asset).upper()
        free = self.balance_cache.get("free") or {}
        total = self.balance_cache.get("total") or {}
        if asset in free and free[asset] is not None:
            return float(free[asset])
        if asset in total and total[asset] is not None:
            return float(total[asset])
        return 0.0

    def free_quote_balance(self) -> float:
        return self.asset_balance(self.quote)

    def fetch_ohlcv_df(self, symbol: str) -> pd.DataFrame:
        timeframe = str(get_cfg(self.cfg, "timeframe", "15m"))
        limit = int(to_float(get_cfg(self.cfg, "candles_limit", 400), 400))
        last_error = None
        for i in range(4):
            try:
                rows = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
                if df.empty:
                    raise ValueError(f"Geen candles voor {symbol}")
                df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                return df
            except Exception as e:
                last_error = e
                LOG.warning("fetch_ohlcv poging %s mislukt voor %s: %s", i + 1, symbol, e)
                time.sleep(2 * (i + 1))
        raise RuntimeError(f"Kon candles niet ophalen voor {symbol}: {last_error}")

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        last_error = None
        for i in range(4):
            try:
                return self.exchange.fetch_ticker(symbol)
            except Exception as e:
                last_error = e
                LOG.warning("fetch_ticker poging %s mislukt voor %s: %s", i + 1, symbol, e)
                time.sleep(2 * (i + 1))
        raise RuntimeError(f"Kon ticker niet ophalen voor {symbol}: {last_error}")

    def market_min_notional(self, symbol: str) -> float:
        m = self.exchange.market(symbol)
        limit_cost = (((m.get("limits") or {}).get("cost") or {}).get("min"))
        if limit_cost:
            return float(limit_cost)
        info = m.get("info") or {}
        raw = info.get("minOrderInQuoteAsset") or info.get("minOrderInBaseAsset")
        return to_float(raw, 5.0)

    def amount_to_precision_safe(self, symbol: str, amount: float) -> float:
        return float(self.exchange.amount_to_precision(symbol, amount))

    def estimate_spread_pct(self, ticker: Dict[str, Any]) -> float:
        bid = to_float(ticker.get("bid"), 0.0)
        ask = to_float(ticker.get("ask"), 0.0)
        if bid <= 0 or ask <= 0:
            return 999.0
        mid = (bid + ask) / 2.0
        return ((ask - bid) / mid) * 100.0

    def scanned_symbols(self) -> List[str]:
        manual = get_cfg(self.cfg, "symbols", []) or []
        if manual:
            return [normalize_symbol(str(s), self.quote) for s in manual]
        auto_scan = to_bool(get_cfg(self.cfg, "scanner.auto_scan", False), False)
        if not auto_scan:
            return []
        top_n = int(to_float(get_cfg(self.cfg, "scanner.top_n_markets", 8), 8))
        min_quote_volume = to_float(get_cfg(self.cfg, "scanner.min_quote_volume", 200000), 200000.0)
        max_spread_pct = to_float(get_cfg(self.cfg, "max_spread_pct", 0.25), 0.25)
        exclude_bases = {str(x).upper() for x in (get_cfg(self.cfg, "scanner.exclude_bases", ["EUR", "USDT", "USDC"]) or [])}
        candidates = []
        tickers = self.exchange.fetch_tickers()
        for symbol, ticker in tickers.items():
            try:
                market = self.exchange.market(symbol)
            except Exception:
                continue
            if not market.get("spot", True):
                continue
            if str(market.get("quote", "")).upper() != self.quote:
                continue
            base = str(market.get("base", "")).upper()
            if base in exclude_bases:
                continue
            last = to_float(ticker.get("last"), 0.0)
            bid = to_float(ticker.get("bid"), 0.0)
            ask = to_float(ticker.get("ask"), 0.0)
            if min(last, bid, ask) <= 0:
                continue
            qv = ticker.get("quoteVolume")
            if qv is None:
                info = ticker.get("info") or {}
                qv = info.get("quoteVolume") or info.get("volumeQuote")
            qv = to_float(qv, 0.0)
            spread_pct = self.estimate_spread_pct(ticker)
            if qv < min_quote_volume:
                continue
            if spread_pct > max_spread_pct:
                continue
            candidates.append((symbol, qv, spread_pct))
        candidates.sort(key=lambda x: (-x[1], x[2], x[0]))
        return [c[0] for c in candidates[:top_n]]

    def open_positions_count(self) -> int:
        return len(self.state.get("positions", {}))

    def short_positions_count(self) -> int:
        return len(self.state.get("short_positions", {}))

    def total_positions_count(self) -> int:
        return (
            self.open_positions_count()
            + self.short_positions_count()
        )

    def max_total_positions(self) -> int:
        return max(
            1,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "trading.max_total_positions",
                        5,
                    ),
                    5,
                )
            ),
        )

    def max_open_short_positions(self) -> int:
        return max(
            0,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "short.max_open_positions",
                        1,
                    ),
                    1,
                )
            ),
        )

    def bot_invested_quote(self) -> float:
        return sum(to_float(pos.get("quote_amount"), 0.0) for pos in self.state.get("positions", {}).values())

    def symbol_in_cooldown(self, symbol: str) -> bool:
        ts = self.state.get("cooldown", {}).get(symbol)
        if not ts:
            return False
        cooldown = to_float(get_cfg(self.cfg, "cooldown_minutes", 45), 45.0)
        return minutes_since(float(ts)) < cooldown

    def short_symbol_in_cooldown(self, symbol: str) -> bool:
        ts = self.state.get("short_cooldown", {}).get(symbol)
        if not ts:
            return False
        cooldown = to_float(get_cfg(self.cfg, "short.cooldown_minutes", 60), 60.0)
        return minutes_since(float(ts)) < cooldown

    def short_enabled(self) -> bool:
        signals_enabled = to_bool(
            get_cfg(
                self.cfg,
                "trading.enable_short_signals",
                False,
            ),
            False,
        )

        module_enabled = to_bool(
            get_cfg(
                self.cfg,
                "short.enabled",
                False,
            ),
            False,
        )

        paper_only = to_bool(
            get_cfg(
                self.cfg,
                "short.paper_only",
                True,
            ),
            True,
        )

        return (
            signals_enabled
            and module_enabled
            and paper_only
            and not self.short_strategy_baseline_mismatch
        )

    def short_test_target_trades(self) -> int:
        return max(
            0,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "short.test_target_trades",
                        0,
                    ),
                    0,
                )
            ),
        )

    def configured_short_strategy_version(self) -> str:
        return str(
            get_cfg(
                self.cfg,
                "short.strategy_version",
                "short_breakout_v3",
            )
            or "short_breakout_v3"
        ).strip()

    def archive_completed_short_test_artifacts(
        self,
        existing_version: str,
    ) -> List[Path]:
        """
        Kopieert een afgeronde shorttest naar een archiefmap.

        Pas nadat de nieuwe baseline veilig is geschreven, worden de oude
        rapportbestanden uit hun actieve locatie verwijderd. De historische
        transacties en bot-state blijven altijd onaangeraakt.
        """
        archive_root = Path(
            self.short_test_archive_dir
        )
        archive_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        version_slug = "".join(
            character
            if character.isalnum() or character in {"-", "_"}
            else "_"
            for character in (existing_version or "onbekend")
        )
        timestamp = datetime.now(
            timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")
        archive_dir = archive_root / (
            f"{timestamp}_{version_slug}"
        )
        archive_dir.mkdir(
            parents=True,
            exist_ok=False,
        )

        baseline_path = Path(
            self.short_test_baseline_file
        )
        report_path = Path(
            self.short_test_report_file
        )
        execution_path = Path(
            self.short_execution_file
        )

        candidates: List[Path] = [
            baseline_path,
            report_path,
            report_path.with_suffix(".txt"),
            execution_path,
        ]

        parent = baseline_path.parent
        candidates.extend(
            sorted(
                parent.glob(
                    "diamond_short_test_interim_*.json"
                )
            )
        )
        candidates.extend(
            sorted(
                parent.glob(
                    "diamond_short_test_interim_*.txt"
                )
            )
        )

        copied: List[Path] = []
        stale_active_files: List[Path] = []
        seen: set[str] = set()

        for source in candidates:
            key = str(source)
            if key in seen or not source.exists():
                continue
            seen.add(key)

            destination = archive_dir / source.name
            shutil.copy2(
                source,
                destination,
            )
            copied.append(source)

            if source != baseline_path:
                stale_active_files.append(source)

        manifest = {
            "archived_at": now_iso(),
            "strategy_version": existing_version or "onbekend",
            "files": [
                source.name
                for source in copied
            ],
            "source_baseline": str(baseline_path),
            "current_short_trades": int(
                self.state.get("short_trades", 0)
                or 0
            ),
        }
        with (archive_dir / "manifest.json").open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                manifest,
                file,
                indent=2,
                ensure_ascii=False,
            )

        LOG.info(
            "PAPER-SHORTTEST GEARCHIVEERD | strategie=%s | map=%s | bestanden=%d",
            existing_version or "onbekend",
            archive_dir,
            len(copied),
        )

        return stale_active_files

    def ensure_short_test_baseline(self) -> None:
        """
        Legt vóór de eerste paper-short automatisch de nulmeting vast.

        Een afgeronde test mag veilig worden opgevolgd door een nieuwe
        strategieversie. De oude baseline en rapporten worden eerst gekopieerd
        naar het archief. Een lopende test of open short wordt nooit gemengd
        met een andere strategie.
        """
        self.short_strategy_baseline_mismatch = False

        if not self.short_enabled():
            return

        target_new = self.short_test_target_trades()

        if target_new <= 0:
            return

        path = Path(
            self.short_test_baseline_file
        )

        ensure_parent(
            self.short_test_baseline_file
        )

        start_short_trades = int(
            self.state.get(
                "short_trades",
                0,
            )
            or 0
        )

        strategy_version = (
            self.configured_short_strategy_version()
        )

        settings = {
            "strategy_version": strategy_version,
            "paper_only": True,
            "symbols": self.scanned_symbols(),
            "timeframe": get_cfg(
                self.cfg,
                "timeframe",
                "15m",
            ),
            "max_open_positions": (
                self.max_open_short_positions()
            ),
            "margin_per_trade": to_float(
                get_cfg(
                    self.cfg,
                    "short.margin_per_trade",
                    30,
                ),
                30.0,
            ),
            "leverage": to_float(
                get_cfg(
                    self.cfg,
                    "short.leverage",
                    1,
                ),
                1.0,
            ),
            "allow_crossover_entry": to_bool(
                get_cfg(
                    self.cfg,
                    "short.allow_crossover_entry",
                    True,
                ),
                True,
            ),
            "allow_breakout_entry": to_bool(
                get_cfg(
                    self.cfg,
                    "short.allow_breakout_entry",
                    True,
                ),
                True,
            ),
            "breakout_lookback_candles": int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "short.breakout_lookback_candles",
                        8,
                    ),
                    8,
                )
            ),
            "require_fast_sma_falling": to_bool(
                get_cfg(
                    self.cfg,
                    "short.require_fast_sma_falling",
                    True,
                ),
                True,
            ),
            "rsi_sell_min": to_float(
                get_cfg(
                    self.cfg,
                    "short.rsi_sell_min",
                    25,
                ),
                25.0,
            ),
            "rsi_sell_max": to_float(
                get_cfg(
                    self.cfg,
                    "short.rsi_sell_max",
                    45,
                ),
                45.0,
            ),
            "min_profit_eur": to_float(
                get_cfg(
                    self.cfg,
                    "short.min_profit_eur",
                    0.05,
                ),
                0.05,
            ),
            "min_atr_pct": to_float(
                get_cfg(
                    self.cfg,
                    "short.min_atr_pct",
                    0.30,
                ),
                0.30,
            ),
            "atr_tp_mult": to_float(
                get_cfg(
                    self.cfg,
                    "short.atr_tp_mult",
                    2.4,
                ),
                2.4,
            ),
            "atr_sl_mult": to_float(
                get_cfg(
                    self.cfg,
                    "short.atr_sl_mult",
                    1.2,
                ),
                1.2,
            ),
            "min_net_reward_risk": to_float(
                get_cfg(
                    self.cfg,
                    "short.min_net_reward_risk",
                    1.0,
                ),
                1.0,
            ),
            "max_cost_adjusted_tp_atr_mult": to_float(
                get_cfg(
                    self.cfg,
                    "short.max_cost_adjusted_tp_atr_mult",
                    4.0,
                ),
                4.0,
            ),
            "use_intrabar_thresholds": to_bool(
                get_cfg(
                    self.cfg,
                    "short.use_intrabar_thresholds",
                    True,
                ),
                True,
            ),
            "simulate_threshold_execution": to_bool(
                get_cfg(
                    self.cfg,
                    "short.simulate_threshold_execution",
                    True,
                ),
                True,
            ),
        }

        replace_existing = False
        stale_active_files: List[Path] = []

        if path.exists():
            try:
                with path.open(
                    "r",
                    encoding="utf-8",
                ) as file:
                    existing = json.load(file)

                existing_settings = (
                    existing.get("settings")
                    or {}
                )

                existing_version = str(
                    existing_settings.get(
                        "strategy_version"
                    )
                    or ""
                ).strip()

                existing_start = int(
                    existing.get(
                        "start_short_trades",
                        start_short_trades,
                    )
                    or 0
                )
                existing_target_total = int(
                    existing.get(
                        "target_total_short_trades",
                        0,
                    )
                    or 0
                )

                test_has_started = (
                    start_short_trades > existing_start
                    or self.short_positions_count() > 0
                )
                previous_test_complete = bool(
                    existing_target_total > existing_start
                    and start_short_trades >= existing_target_total
                )

                if existing_version == strategy_version:
                    return

                if (
                    previous_test_complete
                    and self.short_positions_count() == 0
                ):
                    stale_active_files = (
                        self.archive_completed_short_test_artifacts(
                            existing_version
                        )
                    )
                    replace_existing = True

                elif test_has_started:
                    self.short_strategy_baseline_mismatch = True

                    LOG.error(
                        "PAPER-SHORTTEST GEBLOKKEERD | "
                        "baseline strategie=%s | config strategie=%s | "
                        "gesloten shorts=%d | open shorts=%d",
                        existing_version or "onbekend",
                        strategy_version,
                        start_short_trades - existing_start,
                        self.short_positions_count(),
                    )

                    return

                else:
                    replace_existing = True

            except Exception as exc:
                self.short_strategy_baseline_mismatch = True

                LOG.error(
                    "PAPER-SHORTTEST GEBLOKKEERD | "
                    "bestaande baseline kon niet veilig worden gelezen: %s",
                    exc,
                )

                return

        baseline = {
            "started_at": now_iso(),
            "start_short_trades": start_short_trades,
            "target_new_trades": target_new,
            "target_total_short_trades": (
                start_short_trades
                + target_new
            ),
            "settings": settings,
        }

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as temporary:
            json.dump(
                baseline,
                temporary,
                indent=2,
                ensure_ascii=False,
            )
            temporary_name = temporary.name

        try:
            if not path.exists() or replace_existing:
                os.replace(
                    temporary_name,
                    path,
                )

                for stale_path in stale_active_files:
                    try:
                        stale_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        LOG.warning(
                            "Oud shorttestrapport kon niet worden verwijderd: %s | %s",
                            stale_path,
                            exc,
                        )

                LOG.info(
                    "PAPER-SHORT NULMETING %s | "
                    "strategie=%s | start=%d | doel=%d | bestand=%s",
                    (
                        "VERNIEUWD"
                        if replace_existing
                        else "OPGESLAGEN"
                    ),
                    strategy_version,
                    start_short_trades,
                    start_short_trades + target_new,
                    self.short_test_baseline_file,
                )
            else:
                os.unlink(
                    temporary_name
                )

        except Exception:
            try:
                os.unlink(
                    temporary_name
                )
            except OSError:
                pass

            raise

    def short_test_status(self) -> Dict[str, Any]:
        path = Path(
            self.short_test_baseline_file
        )

        if not path.exists():
            return {
                "enabled": False,
                "target_reached": False,
            }

        try:
            with path.open(
                "r",
                encoding="utf-8",
            ) as file:
                baseline = json.load(file)

            start = int(
                baseline.get(
                    "start_short_trades",
                    0,
                )
                or 0
            )

            target_total = int(
                baseline.get(
                    "target_total_short_trades",
                    0,
                )
                or 0
            )

            current = int(
                self.state.get(
                    "short_trades",
                    0,
                )
                or 0
            )

            valid = (
                start >= 0
                and target_total > start
            )

            return {
                "enabled": valid,
                "start_short_trades": start,
                "target_total_short_trades": target_total,
                "current_short_trades": current,
                "new_short_trades": max(
                    0,
                    current - start,
                ),
                "remaining_short_trades": max(
                    0,
                    target_total - current,
                ),
                "target_reached": (
                    valid
                    and current >= target_total
                ),
            }

        except Exception as exc:
            LOG.error(
                "Paper-short nulmeting lezen mislukt: %s",
                exc,
            )

            return {
                "enabled": False,
                "target_reached": False,
            }

    def short_test_complete(self) -> bool:
        return bool(
            self.short_test_status().get(
                "target_reached",
                False,
            )
        )

    def allow_long_and_short_same_symbol(self) -> bool:
        return to_bool(get_cfg(self.cfg, "trading.allow_long_and_short_same_symbol", False), False)

    def spot_enabled(self) -> bool:
        return to_bool(get_cfg(self.cfg, "trading.enable_spot", True), True)

    def buy_budget_available(self) -> float:
        reserve = to_float(get_cfg(self.cfg, "eur_reserve", 50), 50.0)
        free_quote = self.free_quote_balance()
        return max(0.0, free_quote - reserve)

    def rate_limited_info(self, bucket: Dict[str, float], key: str, seconds: int, message: str, *args) -> None:
        now_ts = utc_now_ts()
        last_ts = float(bucket.get(key, 0.0))
        if now_ts - last_ts >= seconds:
            LOG.info(message, *args)
            bucket[key] = now_ts

    def skip_symbol_due_to_existing_balance(self, symbol: str) -> bool:
        """
        Beschermt bestaande coins: als er al een saldo is van dit coin
        (buiten de bot om gekocht), slaan we dit symbol over voor kopen.
        Bij verkopen wordt dit ook gerespecteerd via opened_by_bot flag.
        """
        avoid_existing = to_bool(get_cfg(self.cfg, "risk.avoid_symbols_with_existing_balance", False), False)
        if not avoid_existing:
            return False
        market = self.exchange.market(symbol)
        base = str(market.get("base", "")).upper()
        dust = to_float(get_cfg(self.cfg, "risk.existing_balance_dust", 5), 5.0)
        base_balance = self.asset_balance(base)
        if base_balance > dust:
            self.rate_limited_info(
                self.last_skip_log_ts,
                f"existing:{symbol}",
                3600,
                "OVERSLAAN KOPEN %s | bestaand saldo %s=%.8f (niet door bot gekocht)",
                symbol, base, base_balance,
            )
            return True
        return False

    def long_entry_signal(self, symbol: str) -> Optional[Dict[str, Any]]:
        df = self.fetch_ohlcv_df(symbol)
        sma_fast = int(to_float(get_cfg(self.cfg, "signals.sma_fast", 20), 20))
        sma_slow = int(to_float(get_cfg(self.cfg, "signals.sma_slow", 60), 60))
        rsi_len = int(to_float(get_cfg(self.cfg, "signals.rsi_len", 14), 14))
        atr_len = int(to_float(get_cfg(self.cfg, "signals.atr_len", 14), 14))
        df = enrich_indicators(df, sma_fast, sma_slow, rsi_len, atr_len)
        if len(df) < max(sma_slow + 2, 80):
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        fast_now = to_float(last["sma_fast"], 0.0)
        slow_now = to_float(last["sma_slow"], 0.0)
        fast_prev = to_float(prev["sma_fast"], 0.0)
        slow_prev = to_float(prev["sma_slow"], 0.0)
        close_now = to_float(last["close"], 0.0)
        rsi_now = to_float(last["rsi"], 50.0)
        atr_now = to_float(last["atr"], 0.0)
        atr_pct = to_float(last["atr_pct"], 0.0)

        use_sma = to_bool(get_cfg(self.cfg, "signals.use_sma", True), True)
        use_rsi = to_bool(get_cfg(self.cfg, "signals.use_rsi", True), True)
        use_atr_filter = to_bool(get_cfg(self.cfg, "signals.use_atr_filter", True), True)

        cross_up = fast_prev <= slow_prev and fast_now > slow_now
        trend_ok = fast_now > slow_now and close_now > fast_now
        require_crossover = to_bool(get_cfg(self.cfg, "signals.require_crossover", False), False)
        if use_sma:
            sma_ok = (cross_up and trend_ok) if require_crossover else trend_ok
        else:
            sma_ok = True

        rsi_min = to_float(get_cfg(self.cfg, "signals.rsi_buy_min", 55), 55.0)
        rsi_max = to_float(get_cfg(self.cfg, "signals.rsi_buy_max", 75), 75.0)
        rsi_ok = (rsi_min <= rsi_now <= rsi_max) if use_rsi else True

        min_atr_pct = to_float(get_cfg(self.cfg, "signals.min_atr_pct", 0.20), 0.20)
        atr_filter_ok = (atr_pct >= min_atr_pct) if use_atr_filter else True

        if sma_ok and rsi_ok and atr_filter_ok and atr_now > 0:
            tp_mult = to_float(get_cfg(self.cfg, "signals.atr_tp_mult", 2.6), 2.6)
            sl_mult = to_float(get_cfg(self.cfg, "signals.atr_sl_mult", 1.2), 1.2)

            # ATR-gebaseerde stop-loss
            atr_stop = close_now - atr_now * sl_mult

            # Harde stop-loss: maximaal 7% onder aankoopprijs
            hard_sl_pct = to_float(get_cfg(self.cfg, "signals.hard_stop_loss_pct", 7.0), 7.0)
            hard_stop = close_now * (1.0 - hard_sl_pct / 100.0)

            # Gebruik de hoogste (meest beschermende) stop-loss
            stop_loss = max(atr_stop, hard_stop)

            tech_score = atr_pct + max(0.0, 1.0 - abs(rsi_now - 60.0) / 20.0)
            return {
                "close": close_now,
                "atr": atr_now,
                "rsi": rsi_now,
                "atr_pct": atr_pct,
                "stop_loss": stop_loss,
                "take_profit": close_now + atr_now * tp_mult,
                "tech_score": round(tech_score, 4),
                "signal_candle_ts": str(last.get("ts", "")),
            }
        return None

    def collect_buy_candidates(self, symbols: List[str]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        if self.entries_blocked_by_recovery():
            return candidates
        max_open = int(to_float(get_cfg(self.cfg, "max_open_positions", 5), 5))
        if (
            self.open_positions_count() >= max_open
            or self.total_positions_count() >= self.max_total_positions()
        ):
            return candidates

        for symbol in symbols:
            if symbol in self.state["positions"]:
                continue
            if not self.allow_long_and_short_same_symbol() and symbol in self.state.get("short_positions", {}):
                continue
            if self.symbol_in_cooldown(symbol):
                continue
            if self.skip_symbol_due_to_existing_balance(symbol):
                continue
            try:
                signal = self.long_entry_signal(symbol)
                if not signal:
                    continue
                ticker = self.get_ticker(symbol)
                spread_pct = self.estimate_spread_pct(ticker)
                max_spread_pct = to_float(get_cfg(self.cfg, "max_spread_pct", 0.25), 0.25)
                if spread_pct > max_spread_pct:
                    continue
                candidates.append({
                    "symbol": symbol, "signal": signal, "ticker": ticker,
                    "spread_pct": spread_pct, "tech_score": to_float(signal.get("tech_score", 0.0), 0.0),
                })
            except Exception as e:
                LOG.warning("Kandidaat overgeslagen voor %s door marktdatafout: %s", symbol, e)

        candidates.sort(key=lambda x: x["tech_score"], reverse=True)
        return candidates

    def long_exit_signal(self, symbol: str, position: Dict[str, Any]) -> Optional[str]:
        bad_news_reason = self.news.forced_exit_reason(symbol)
        if bad_news_reason:
            return bad_news_reason

        df = self.fetch_ohlcv_df(symbol)
        sma_fast = int(to_float(get_cfg(self.cfg, "signals.sma_fast", 20), 20))
        sma_slow = int(to_float(get_cfg(self.cfg, "signals.sma_slow", 60), 60))
        rsi_len = int(to_float(get_cfg(self.cfg, "signals.rsi_len", 14), 14))
        atr_len = int(to_float(get_cfg(self.cfg, "signals.atr_len", 14), 14))
        df = enrich_indicators(df, sma_fast, sma_slow, rsi_len, atr_len)
        last = df.iloc[-1]

        price = to_float(last["close"], 0.0)

        # Strategie blijft closed-candle gebaseerd, maar echte
        # prijsdrempels mogen nooit tegen een candle van vóór de BUY
        # worden getest. Gebruik daarvoor de actuele uitvoerbare bid.
        live_ticker = self.get_ticker(symbol)
        live_bid = to_float(live_ticker.get("bid"), 0.0)
        if live_bid <= 0:
            raise RuntimeError(f"Geen geldige live bid voor {symbol}")

        atr = to_float(last["atr"], 0.0)
        fast = to_float(last["sma_fast"], 0.0)
        slow = to_float(last["sma_slow"], 0.0)
        stop_loss = to_float(position.get("stop_loss"), 0.0)
        take_profit = to_float(position.get("take_profit"), 0.0)
        entry_price = to_float(position.get("entry_price"), 0.0)
        highest = max(to_float(position.get("highest_price", 0.0), 0.0), price)
        position["highest_price"] = highest

        profit_pct = (
            ((price - entry_price) / entry_price) * 100.0
            if entry_price > 0
            else 0.0
        )
        min_profit_eur = max(
            0.0,
            to_float(get_cfg(self.cfg, "min_profit_eur", 0.50), 0.50),
        )
        min_profitable_exit_price = self.minimum_profitable_exit_price(
            position,
            min_profit_eur,
        )
        estimated_net_profit = self.estimated_exit_pnl_quote(
            symbol,
            position,
            price,
        )

        # Een trailing stop mag pas worden verhoogd naar winstgebied wanneer
        # de stopprijs na koop- en verkoopkosten minimaal min_profit_eur overlaat.
        profit_trailing_pct = to_float(
            get_cfg(self.cfg, "signals.profit_trailing_trigger_pct", 20.0),
            20.0,
        )
        profit_trailing_pullback = to_float(
            get_cfg(self.cfg, "signals.profit_trailing_pullback_pct", 5.0),
            5.0,
        )

        if profit_pct >= profit_trailing_pct and highest > 0:
            tight_trailing_stop = highest * (
                1.0 - profit_trailing_pullback / 100.0
            )
            if (
                tight_trailing_stop >= min_profitable_exit_price
                and tight_trailing_stop > stop_loss
            ):
                position["stop_loss"] = tight_trailing_stop
                stop_loss = tight_trailing_stop
                locked_net_profit = self.estimated_exit_pnl_quote(
                    symbol,
                    position,
                    tight_trailing_stop,
                )
                LOG.info(
                    "WINST-TRAILING actief voor %s | koerswinst=%.2f%% | "
                    "netto_winst_nu=%.4f %s | stop=%.8f | "
                    "min_netto_bij_stop=%.4f %s",
                    symbol,
                    profit_pct,
                    estimated_net_profit,
                    self.quote,
                    tight_trailing_stop,
                    locked_net_profit,
                    self.quote,
                )

        # Normale ATR-trailing wordt alleen actief wanneer de berekende
        # stopprijs minimaal de ingestelde nettowinst veiligstelt.
        trailing_enabled = to_bool(
            get_cfg(self.cfg, "signals.trailing_enabled", True),
            True,
        )
        trailing_atr_mult = to_float(
            get_cfg(self.cfg, "signals.trailing_atr_mult", 1.2),
            1.2,
        )
        if trailing_enabled and atr > 0 and highest > 0:
            trailing_stop = highest - atr * trailing_atr_mult
            if (
                trailing_stop >= min_profitable_exit_price
                and trailing_stop > stop_loss
            ):
                position["stop_loss"] = trailing_stop
                stop_loss = trailing_stop

        # Harde stop-loss blijft altijd actief als veiligheidsgrens.
        hard_sl_pct = to_float(
            get_cfg(self.cfg, "signals.hard_stop_loss_pct", 7.0),
            7.0,
        )
        if entry_price > 0:
            hard_stop = entry_price * (1.0 - hard_sl_pct / 100.0)
            if live_bid <= hard_stop:
                LOG.warning(
                    "HARDE STOP-LOSS geraakt voor %s | live_bid=%.8f | "
                    "hard_stop=%.8f (-%.2f%%)",
                    symbol,
                    live_bid,
                    hard_stop,
                    hard_sl_pct,
                )
                return "hard_stop_loss"

        if stop_loss > 0 and live_bid <= stop_loss:
            if (
                stop_loss >= min_profitable_exit_price
                and highest > entry_price
            ):
                return "trailing_stop"
            return "stop_loss"

        profit_trailing_active = (
            stop_loss >= min_profitable_exit_price
            and min_profitable_exit_price < float("inf")
        )
        if (
            take_profit > 0
            and live_bid >= take_profit
            and not profit_trailing_active
        ):
            return "take_profit"

        if (
            to_bool(
                get_cfg(self.cfg, "signals.exit_on_trend_break", False),
                False,
            )
            and fast < slow
        ):
            return "trend_break"
        return None

    def short_entry_diagnostics(
        self,
        symbol: str,
    ) -> Dict[str, Any]:
        """
        Geeft alle paper-shortvoorwaarden terug.

        Een short kan starten bij:
        1. een nieuwe bearish SMA-kruising; of
        2. een bestaande dalende trend met een nieuwe neerwaartse
           uitbraak onder het recente dieptepunt.

        Alleen volledig afgesloten candles worden gebruikt wanneer de bot
        via closed_candle_runner.py draait.
        """
        df = self.fetch_ohlcv_df(
            symbol
        )

        sma_fast = max(
            2,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "short.sma_fast",
                        20,
                    ),
                    20,
                )
            ),
        )

        sma_slow = max(
            sma_fast + 1,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "short.sma_slow",
                        60,
                    ),
                    60,
                )
            ),
        )

        rsi_len = max(
            2,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "short.rsi_len",
                        14,
                    ),
                    14,
                )
            ),
        )

        atr_len = max(
            2,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "short.atr_len",
                        14,
                    ),
                    14,
                )
            ),
        )

        breakout_lookback = max(
            2,
            min(
                50,
                int(
                    to_float(
                        get_cfg(
                            self.cfg,
                            "short.breakout_lookback_candles",
                            8,
                        ),
                        8,
                    )
                ),
            ),
        )

        df = enrich_indicators(
            df,
            sma_fast,
            sma_slow,
            rsi_len,
            atr_len,
        )

        required_rows = max(
            sma_slow + 2,
            breakout_lookback + 2,
            80,
        )

        if len(df) < required_rows:
            return {
                "symbol": symbol,
                "signal": False,
                "entry_trigger": "",
                "blockers": [
                    (
                        f"onvoldoende candles: "
                        f"{len(df)}/{required_rows}"
                    )
                ],
            }

        last = df.iloc[-1]
        prev = df.iloc[-2]

        fast_now = to_float(
            last["sma_fast"],
            0.0,
        )

        slow_now = to_float(
            last["sma_slow"],
            0.0,
        )

        fast_prev = to_float(
            prev["sma_fast"],
            0.0,
        )

        slow_prev = to_float(
            prev["sma_slow"],
            0.0,
        )

        close_now = to_float(
            last["close"],
            0.0,
        )

        rsi_now = to_float(
            last["rsi"],
            50.0,
        )

        atr_now = to_float(
            last["atr"],
            0.0,
        )

        atr_pct = to_float(
            last["atr_pct"],
            0.0,
        )

        recent_window = df.iloc[
            -(breakout_lookback + 1):-1
        ]

        recent_low = to_float(
            pd.to_numeric(
                recent_window["low"],
                errors="coerce",
            ).min(),
            0.0,
        )

        use_sma = to_bool(
            get_cfg(
                self.cfg,
                "short.use_sma",
                True,
            ),
            True,
        )

        use_rsi = to_bool(
            get_cfg(
                self.cfg,
                "short.use_rsi",
                True,
            ),
            True,
        )

        use_atr_filter = to_bool(
            get_cfg(
                self.cfg,
                "short.use_atr_filter",
                True,
            ),
            True,
        )

        allow_crossover = to_bool(
            get_cfg(
                self.cfg,
                "short.allow_crossover_entry",
                True,
            ),
            True,
        )

        allow_breakout = to_bool(
            get_cfg(
                self.cfg,
                "short.allow_breakout_entry",
                True,
            ),
            True,
        )

        require_fast_falling = to_bool(
            get_cfg(
                self.cfg,
                "short.require_fast_sma_falling",
                True,
            ),
            True,
        )

        cross_down = (
            fast_prev >= slow_prev
            and fast_now < slow_now
        )

        trend_ok = (
            fast_now < slow_now
            and close_now < fast_now
        )

        fast_falling = (
            fast_now < fast_prev
        )

        breakout_down = (
            recent_low > 0
            and close_now < recent_low
        )

        crossover_trigger = (
            allow_crossover
            and cross_down
            and trend_ok
        )

        breakout_trigger = (
            allow_breakout
            and trend_ok
            and breakout_down
            and (
                fast_falling
                or not require_fast_falling
            )
        )

        entry_trigger = ""

        if not use_sma:
            entry_trigger = (
                "sma_filter_disabled"
            )
        elif crossover_trigger:
            entry_trigger = (
                "bearish_crossover"
            )
        elif breakout_trigger:
            entry_trigger = (
                "bearish_breakout"
            )

        rsi_min = to_float(
            get_cfg(
                self.cfg,
                "short.rsi_sell_min",
                25,
            ),
            25.0,
        )

        rsi_max = to_float(
            get_cfg(
                self.cfg,
                "short.rsi_sell_max",
                45,
            ),
            45.0,
        )

        if rsi_min > rsi_max:
            rsi_min, rsi_max = (
                rsi_max,
                rsi_min,
            )

        rsi_ok = (
            rsi_min <= rsi_now <= rsi_max
            if use_rsi
            else True
        )

        min_atr_pct = to_float(
            get_cfg(
                self.cfg,
                "short.min_atr_pct",
                0.30,
            ),
            0.30,
        )

        atr_filter_ok = (
            atr_pct >= min_atr_pct
            if use_atr_filter
            else True
        )

        atr_valid = atr_now > 0

        blockers: List[str] = []

        if use_sma and not entry_trigger:
            if not trend_ok:
                blockers.append(
                    "trend is niet volledig bearish"
                )

            if (
                allow_crossover
                and not cross_down
            ):
                blockers.append(
                    "geen nieuwe bearish SMA-kruising"
                )

            if (
                allow_breakout
                and not breakout_down
            ):
                blockers.append(
                    (
                        "geen slot onder recent dieptepunt "
                        f"({recent_low:.8f})"
                    )
                )

            if (
                allow_breakout
                and require_fast_falling
                and not fast_falling
            ):
                blockers.append(
                    "snelle SMA daalt niet"
                )

        if not rsi_ok:
            blockers.append(
                (
                    f"RSI {rsi_now:.2f} buiten "
                    f"{rsi_min:.2f}-{rsi_max:.2f}"
                )
            )

        if not atr_filter_ok:
            blockers.append(
                (
                    f"ATR {atr_pct:.3f}% lager dan "
                    f"{min_atr_pct:.3f}%"
                )
            )

        if not atr_valid:
            blockers.append(
                "ATR is ongeldig"
            )

        signal_ok = bool(
            entry_trigger
            and rsi_ok
            and atr_filter_ok
            and atr_valid
        )

        return {
            "symbol": symbol,
            "signal": signal_ok,
            "entry_trigger": entry_trigger,
            "close": close_now,
            "atr": atr_now,
            "atr_pct": atr_pct,
            "rsi": rsi_now,
            "rsi_min": rsi_min,
            "rsi_max": rsi_max,
            "sma_fast": fast_now,
            "sma_slow": slow_now,
            "cross_down": cross_down,
            "trend_ok": trend_ok,
            "fast_sma_falling": fast_falling,
            "breakout_down": breakout_down,
            "breakout_lookback_candles": breakout_lookback,
            "breakout_level": recent_low,
            "min_atr_pct": min_atr_pct,
            "blockers": blockers,
            "last_candle": str(
                last.get(
                    "ts",
                    "",
                )
            ),
        }

    def short_entry_signal(
        self,
        symbol: str,
    ) -> Optional[Dict[str, Any]]:
        diagnostics = (
            self.short_entry_diagnostics(
                symbol
            )
        )

        if not diagnostics.get(
            "signal",
            False,
        ):
            return None

        close_now = to_float(
            diagnostics.get(
                "close"
            ),
            0.0,
        )

        atr_now = to_float(
            diagnostics.get(
                "atr"
            ),
            0.0,
        )

        tp_mult = to_float(
            get_cfg(
                self.cfg,
                "short.atr_tp_mult",
                2.4,
            ),
            2.4,
        )

        sl_mult = to_float(
            get_cfg(
                self.cfg,
                "short.atr_sl_mult",
                1.2,
            ),
            1.2,
        )

        return {
            "close": close_now,
            "atr": atr_now,
            "rsi": to_float(
                diagnostics.get(
                    "rsi"
                ),
                0.0,
            ),
            "atr_pct": to_float(
                diagnostics.get(
                    "atr_pct"
                ),
                0.0,
            ),
            "entry_trigger": str(
                diagnostics.get(
                    "entry_trigger"
                )
                or "paper_short_entry"
            ),
            "breakout_level": to_float(
                diagnostics.get(
                    "breakout_level"
                ),
                0.0,
            ),
            "stop_loss": (
                close_now
                + atr_now * sl_mult
            ),
            "take_profit": (
                close_now
                - atr_now * tp_mult
            ),
        }

    def short_trade_plan(
        self,
        symbol: str,
        signal: Dict[str, Any],
        ticker: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Bouwt een kostenbewust paper-shortplan op basis van de echte biedprijs.

        De stopafstand blijft ATR-gebaseerd. Het take-profitdoel wordt zo nodig
        verder gezet totdat de verwachte nettowinst minimaal gelijk is aan de
        ingestelde netto risico/winstverhouding. Wanneer daarvoor een onredelijk
        groot ATR-doel nodig is, wordt de instap afgewezen.
        """
        ticker = ticker or self.get_ticker(symbol)
        bid = to_float(ticker.get("bid"), 0.0)
        ask = to_float(ticker.get("ask"), 0.0)
        atr_now = to_float(signal.get("atr"), 0.0)
        signal_close = to_float(signal.get("close"), 0.0)
        spread_pct = self.estimate_spread_pct(ticker)

        blockers: List[str] = []

        if bid <= 0 or ask <= 0:
            blockers.append("geldige bied- of laatprijs ontbreekt")

        if atr_now <= 0:
            blockers.append("ATR is ongeldig")

        leverage = max(
            1.0,
            to_float(
                get_cfg(self.cfg, "short.leverage", 1),
                1.0,
            ),
        )
        margin_per_trade = max(
            0.0,
            to_float(
                get_cfg(
                    self.cfg,
                    "short.margin_per_trade",
                    30,
                ),
                30.0,
            ),
        )
        notional = margin_per_trade * leverage

        amount = 0.0
        if bid > 0 and notional > 0:
            amount = self.amount_to_precision_safe(
                symbol,
                notional / bid,
            )

        if amount <= 0:
            blockers.append("berekende shortomvang is ongeldig")

        entry_quote = amount * bid
        fee_rate = max(
            0.0,
            to_float(
                get_cfg(self.cfg, "taker_fee_pct", 0.25),
                0.25,
            )
            / 100.0,
        )
        fee_open_quote = entry_quote * fee_rate

        tp_mult = max(
            0.0,
            to_float(
                get_cfg(self.cfg, "short.atr_tp_mult", 2.4),
                2.4,
            ),
        )
        sl_mult = max(
            0.0,
            to_float(
                get_cfg(self.cfg, "short.atr_sl_mult", 1.2),
                1.2,
            ),
        )
        min_net_rr = max(
            0.0,
            to_float(
                get_cfg(
                    self.cfg,
                    "short.min_net_reward_risk",
                    1.0,
                ),
                1.0,
            ),
        )
        min_profit_quote = max(
            0.0,
            to_float(
                get_cfg(
                    self.cfg,
                    "short.min_profit_eur",
                    0.05,
                ),
                0.05,
            ),
        )
        max_tp_atr_mult = max(
            tp_mult,
            to_float(
                get_cfg(
                    self.cfg,
                    "short.max_cost_adjusted_tp_atr_mult",
                    4.0,
                ),
                4.0,
            ),
        )

        stop_loss = bid + atr_now * sl_mult
        base_take_profit = bid - atr_now * tp_mult
        cover_markup = (
            max(1.0, ask / bid)
            if bid > 0 and ask > 0
            else 1.0
        )

        expected_stop_ask = stop_loss * cover_markup
        expected_stop_quote = amount * expected_stop_ask
        expected_stop_fee = expected_stop_quote * fee_rate
        expected_stop_pnl = (
            entry_quote
            - fee_open_quote
            - expected_stop_quote
            - expected_stop_fee
        )
        expected_net_risk = max(
            0.0,
            -expected_stop_pnl,
        )

        desired_net_reward = max(
            min_profit_quote,
            expected_net_risk * min_net_rr,
        )

        required_cover_ask = 0.0
        required_take_profit = 0.0
        if amount > 0 and (1.0 + fee_rate) > 0:
            required_cover_ask = (
                entry_quote
                - fee_open_quote
                - desired_net_reward
            ) / (amount * (1.0 + fee_rate))
            required_take_profit = (
                required_cover_ask / cover_markup
                if cover_markup > 0
                else 0.0
            )

        take_profit = min(
            base_take_profit,
            required_take_profit,
        )
        planned_tp_atr_mult = (
            (bid - take_profit) / atr_now
            if atr_now > 0 and take_profit > 0
            else 0.0
        )

        expected_tp_ask = take_profit * cover_markup
        expected_tp_quote = amount * expected_tp_ask
        expected_tp_fee = expected_tp_quote * fee_rate
        expected_net_reward = (
            entry_quote
            - fee_open_quote
            - expected_tp_quote
            - expected_tp_fee
        )
        expected_net_rr = (
            expected_net_reward / expected_net_risk
            if expected_net_risk > 0
            else 0.0
        )

        if stop_loss <= bid:
            blockers.append("geplande stop-loss ligt niet boven de instap")

        if take_profit <= 0 or take_profit >= bid:
            blockers.append("gepland take-profitdoel is ongeldig")

        if expected_net_risk <= 0:
            blockers.append("verwacht nettoverlies bij stop-loss is ongeldig")

        if planned_tp_atr_mult > max_tp_atr_mult + 1e-9:
            blockers.append(
                "kostenbewust take-profitdoel vereist "
                f"{planned_tp_atr_mult:.2f} ATR; maximum is "
                f"{max_tp_atr_mult:.2f} ATR"
            )

        if expected_net_reward + 1e-9 < min_profit_quote:
            blockers.append(
                "verwachte nettowinst is lager dan "
                f"{min_profit_quote:.4f} {self.quote}"
            )

        if expected_net_rr + 1e-9 < min_net_rr:
            blockers.append(
                "verwachte netto risico/winst is "
                f"{expected_net_rr:.2f}; minimum is {min_net_rr:.2f}"
            )

        return {
            "allowed": not blockers,
            "blockers": blockers,
            "strategy_version": self.configured_short_strategy_version(),
            "bid": bid,
            "ask": ask,
            "signal_close": signal_close,
            "spread_pct": spread_pct,
            "cover_markup": cover_markup,
            "atr": atr_now,
            "atr_pct": to_float(signal.get("atr_pct"), 0.0),
            "amount": amount,
            "margin_quote": margin_per_trade,
            "leverage": leverage,
            "quote_amount": entry_quote,
            "fee_open_quote": fee_open_quote,
            "stop_loss": stop_loss,
            "base_take_profit": base_take_profit,
            "take_profit": take_profit,
            "planned_tp_atr_mult": planned_tp_atr_mult,
            "expected_net_reward": expected_net_reward,
            "expected_net_risk": expected_net_risk,
            "expected_net_rr": expected_net_rr,
            "min_net_reward_risk": min_net_rr,
            "max_cost_adjusted_tp_atr_mult": max_tp_atr_mult,
        }

    def short_exit_diagnostics(
        self,
        symbol: str,
        position: Dict[str, Any],
    ) -> Dict[str, Any]:
        df = self.fetch_ohlcv_df(symbol)
        sma_fast = int(
            to_float(
                get_cfg(self.cfg, "short.sma_fast", 20),
                20,
            )
        )
        sma_slow = int(
            to_float(
                get_cfg(self.cfg, "short.sma_slow", 60),
                60,
            )
        )
        rsi_len = int(
            to_float(
                get_cfg(self.cfg, "short.rsi_len", 14),
                14,
            )
        )
        atr_len = int(
            to_float(
                get_cfg(self.cfg, "short.atr_len", 14),
                14,
            )
        )
        df = enrich_indicators(
            df,
            sma_fast,
            sma_slow,
            rsi_len,
            atr_len,
        )
        last = df.iloc[-1]

        candle_open = to_float(last.get("open"), 0.0)
        candle_high = to_float(last.get("high"), 0.0)
        candle_low = to_float(last.get("low"), 0.0)
        candle_close = to_float(last.get("close"), 0.0)
        fast = to_float(last.get("sma_fast"), 0.0)
        slow = to_float(last.get("sma_slow"), 0.0)
        stop_loss = to_float(position.get("stop_loss"), 0.0)
        take_profit = to_float(position.get("take_profit"), 0.0)
        entry_price = to_float(position.get("entry_price"), 0.0)

        use_intrabar = to_bool(
            position.get(
                "use_intrabar_thresholds",
                get_cfg(
                    self.cfg,
                    "short.use_intrabar_thresholds",
                    False,
                ),
            ),
            False,
        )

        stop_hit = bool(
            stop_loss > 0
            and (
                candle_high >= stop_loss
                if use_intrabar
                else candle_close >= stop_loss
            )
        )
        take_profit_hit = bool(
            take_profit > 0
            and (
                candle_low <= take_profit
                if use_intrabar
                else candle_close <= take_profit
            )
        )
        both_thresholds_hit = bool(
            stop_hit and take_profit_hit
        )

        reason: Optional[str] = None
        execution_reference_price = candle_close

        # Wanneer beide niveaus binnen dezelfde afgesloten candle zijn geraakt,
        # kiest de papersimulatie conservatief de stop-loss.
        if stop_hit:
            reason = "short_stop_loss"
            execution_reference_price = max(
                stop_loss,
                candle_open,
            )
        elif take_profit_hit:
            reason = "short_take_profit"
            execution_reference_price = take_profit
        elif (
            to_bool(
                get_cfg(
                    self.cfg,
                    "signals.exit_on_trend_break",
                    False,
                ),
                False,
            )
            and fast > slow
        ):
            reason = "short_trend_break"
            execution_reference_price = candle_close

        stop_overshoot_pct = (
            max(0.0, candle_high - stop_loss)
            / entry_price
            * 100.0
            if entry_price > 0 and stop_loss > 0
            else 0.0
        )
        take_profit_overshoot_pct = (
            max(0.0, take_profit - candle_low)
            / entry_price
            * 100.0
            if entry_price > 0 and take_profit > 0
            else 0.0
        )

        return {
            "reason": reason,
            "execution_reference_price": execution_reference_price,
            "use_intrabar_thresholds": use_intrabar,
            "both_thresholds_hit": both_thresholds_hit,
            "candle_ts": str(last.get("ts", "")),
            "candle_open": candle_open,
            "candle_high": candle_high,
            "candle_low": candle_low,
            "candle_close": candle_close,
            "stop_hit": stop_hit,
            "take_profit_hit": take_profit_hit,
            "stop_overshoot_pct": stop_overshoot_pct,
            "take_profit_overshoot_pct": take_profit_overshoot_pct,
        }

    def short_exit_signal(
        self,
        symbol: str,
        position: Dict[str, Any],
    ) -> Optional[str]:
        return self.short_exit_diagnostics(
            symbol,
            position,
        ).get("reason")

    def order_fee_quote(
        self,
        order: Dict[str, Any],
        fallback_quote_amount: float,
        symbol: str,
        execution_price: float,
    ) -> float:
        """
        Zet orderkosten veilig om naar de quotevaluta (EUR).

        Bitvavo/CCXT kan kosten in EUR of in de basismunt teruggeven.
        Bij ontbrekende kosten wordt de ingestelde taker fee gebruikt.
        """
        base_asset, quote_asset = symbol.split("/")
        base_asset = base_asset.upper()
        quote_asset = quote_asset.upper()

        fee_items: List[Dict[str, Any]] = []

        # CCXT kan dezelfde Bitvavo-fee zowel in "fee" als in "fees"
        # teruggeven. Gebruik "fees" wanneer die beschikbaar is.
        fees = order.get("fees")
        if isinstance(fees, list) and any(
            isinstance(item, dict) for item in fees
        ):
            fee_items.extend(
                item for item in fees
                if isinstance(item, dict)
            )
        else:
            fee = order.get("fee")
            if isinstance(fee, dict):
                fee_items.append(fee)

        total_quote_fee = 0.0
        found_valid_fee = False

        for item in fee_items:
            cost = to_float(item.get("cost"), 0.0)
            if cost <= 0:
                continue

            currency = str(
                item.get("currency") or quote_asset
            ).upper()

            if currency == quote_asset:
                total_quote_fee += cost
                found_valid_fee = True
            elif currency == base_asset and execution_price > 0:
                total_quote_fee += cost * execution_price
                found_valid_fee = True
            else:
                LOG.warning(
                    "Onbekende feevaluta voor %s: %s; "
                    "fallback fee wordt gebruikt",
                    symbol,
                    currency,
                )

        if found_valid_fee:
            return total_quote_fee

        taker_fee_pct = to_float(
            get_cfg(self.cfg, "taker_fee_pct", 0.25),
            0.25,
        )
        return fallback_quote_amount * (taker_fee_pct / 100.0)

    def estimated_exit_pnl_quote(
        self,
        symbol: str,
        position: Dict[str, Any],
        bid_price: Optional[float] = None,
    ) -> float:
        if bid_price is None:
            ticker = self.get_ticker(symbol)
            bid_price = to_float(ticker.get("bid"), 0.0)

        amount = to_float(position.get("amount"), 0.0)
        gross_quote = amount * max(bid_price or 0.0, 0.0)
        taker_fee_pct = to_float(
            get_cfg(self.cfg, "taker_fee_pct", 0.25),
            0.25,
        )
        est_sell_fee = gross_quote * (taker_fee_pct / 100.0)
        entry_quote = to_float(position.get("quote_amount"), 0.0)
        fee_buy_quote = to_float(position.get("fees_buy_quote"), 0.0)
        return gross_quote - est_sell_fee - entry_quote - fee_buy_quote

    def minimum_profitable_exit_price(
        self,
        position: Dict[str, Any],
        min_profit_quote: float,
    ) -> float:
        """
        Berekent de minimale verkoopprijs waarbij na beide handelskosten
        minimaal min_profit_quote in de quotevaluta overblijft.
        """
        amount = to_float(position.get("amount"), 0.0)
        if amount <= 0:
            return float("inf")

        entry_quote = to_float(position.get("quote_amount"), 0.0)
        fee_buy_quote = to_float(position.get("fees_buy_quote"), 0.0)
        taker_fee_pct = max(
            0.0,
            to_float(get_cfg(self.cfg, "taker_fee_pct", 0.25), 0.25),
        )
        sell_multiplier = 1.0 - taker_fee_pct / 100.0
        if sell_multiplier <= 0:
            return float("inf")

        required_net_quote = (
            entry_quote
            + fee_buy_quote
            + max(0.0, min_profit_quote)
        )
        return required_net_quote / (amount * sell_multiplier)

    def estimated_short_exit_pnl_quote(self, symbol: str, position: Dict[str, Any], ask_price: Optional[float] = None) -> float:
        if ask_price is None:
            ticker = self.get_ticker(symbol)
            ask_price = to_float(ticker.get("ask"), 0.0)
        amount = to_float(position.get("amount"), 0.0)
        cover_quote = amount * max(ask_price or 0.0, 0.0)
        taker_fee_pct = to_float(get_cfg(self.cfg, "taker_fee_pct", 0.25), 0.25)
        est_cover_fee = cover_quote * (taker_fee_pct / 100.0)
        entry_quote = to_float(position.get("quote_amount"), 0.0)
        fee_open_quote = to_float(position.get("fees_open_quote"), 0.0)
        return entry_quote - fee_open_quote - cover_quote - est_cover_fee

    def simulated_short_exit_ask(
        self,
        position: Dict[str, Any],
        reason: str,
        ticker: Dict[str, Any],
        exit_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Bepaalt de conservatieve paper-uitvoeringsprijs voor shortsluiting."""
        market_ask = to_float(ticker.get("ask"), 0.0)
        market_bid = to_float(ticker.get("bid"), 0.0)

        simulate_threshold = to_bool(
            position.get(
                "simulate_threshold_execution",
                get_cfg(
                    self.cfg,
                    "short.simulate_threshold_execution",
                    False,
                ),
            ),
            False,
        )

        if (
            not simulate_threshold
            or reason not in {
                "short_stop_loss",
                "short_take_profit",
            }
            or not exit_diagnostics
        ):
            return market_ask

        reference_price = to_float(
            exit_diagnostics.get(
                "execution_reference_price"
            ),
            0.0,
        )
        if reference_price <= 0:
            return market_ask

        current_markup = (
            max(1.0, market_ask / market_bid)
            if market_bid > 0 and market_ask > 0
            else 1.0
        )
        entry_markup = max(
            1.0,
            to_float(
                position.get("entry_cover_markup"),
                1.0,
            ),
        )
        cover_markup = max(
            current_markup,
            entry_markup,
        )
        slippage_pct = max(
            0.0,
            to_float(
                get_cfg(
                    self.cfg,
                    "short.paper_slippage_pct",
                    0.0,
                ),
                0.0,
            ),
        )

        return (
            reference_price
            * cover_markup
            * (1.0 + slippage_pct / 100.0)
        )

    def rate_limited_hold_log(self, key: str, message: str, *args) -> None:
        now_ts = utc_now_ts()
        last_ts = float(self.last_hold_log_ts.get(key, 0.0))
        if now_ts - last_ts >= 600:
            LOG.info(message, *args)
            self.last_hold_log_ts[key] = now_ts

    def sell_allowed_by_profit(
        self,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
    ) -> bool:
        ticker = self.get_ticker(symbol)
        bid = to_float(ticker.get("bid"), 0.0)
        if bid <= 0:
            return False

        est_pnl = self.estimated_exit_pnl_quote(symbol, position, bid)

        # Beschermende exits mogen altijd doorgaan.
        # Een trailing-stop moet ook bij een snelle koerssprong
        # uitgevoerd kunnen worden wanneer de actuele prijs al
        # onder het oorspronkelijk beschermde winstniveau ligt.
        if reason in {
            "stop_loss",
            "bad_news",
            "hard_stop_loss",
            "trailing_stop",
        }:
            return True

        # Take-profit, trendbreuk en trailing-stop mogen pas verkopen wanneer
        # de geschatte nettowinst na beide handelskosten voldoende is.
        min_profit_eur = max(
            0.0,
            to_float(get_cfg(self.cfg, "min_profit_eur", 0.50), 0.50),
        )
        if est_pnl >= min_profit_eur:
            return True

        self.rate_limited_hold_log(
            f"{symbol}:{reason}",
            "HOLD %s | reason=%s | est_pnl=%.4f %s < min_profit=%.4f %s",
            symbol,
            reason,
            est_pnl,
            self.quote,
            min_profit_eur,
            self.quote,
        )
        return False

    def close_short_allowed_by_profit(
        self,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
        exit_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> bool:
        ticker = self.get_ticker(symbol)
        ask = self.simulated_short_exit_ask(
            position,
            reason,
            ticker,
            exit_diagnostics,
        )
        if ask <= 0:
            return False

        est_pnl = self.estimated_short_exit_pnl_quote(
            symbol,
            position,
            ask,
        )
        if reason == "short_stop_loss":
            return True

        min_profit_eur = to_float(
            get_cfg(
                self.cfg,
                "short.min_profit_eur",
                0.05,
            ),
            0.05,
        )
        if min_profit_eur <= 0:
            return True
        if est_pnl >= min_profit_eur:
            return True

        self.rate_limited_hold_log(
            f"short:{symbol}:{reason}",
            "HOLD SHORT %s | reason=%s | est_pnl=%.4f %s < min_profit=%.4f %s",
            symbol,
            reason,
            est_pnl,
            self.quote,
            min_profit_eur,
            self.quote,
        )
        return False

    def resolve_order_fill(
        self,
        symbol: str,
        order: Dict[str, Any],
        fallback_price: float,
        fallback_amount: float,
    ) -> Dict[str, Any]:
        """
        Controleert de werkelijk uitgevoerde hoeveelheid.

        In live-modus wordt nooit aangenomen dat een order gevuld is wanneer
        Bitvavo geen positieve 'filled'-waarde heeft teruggegeven.
        """
        resolved = dict(order or {})

        if self.dry_run:
            price = to_float(
                resolved.get("average") or resolved.get("price"),
                fallback_price,
            )
            amount = to_float(
                resolved.get("filled") or resolved.get("amount"),
                fallback_amount,
            )
            cost = to_float(resolved.get("cost"), amount * price)

            if price <= 0 or amount <= 0 or cost <= 0:
                raise RuntimeError(
                    f"Dry-run order voor {symbol} is ongeldig: "
                    f"price={price}, amount={amount}, cost={cost}"
                )

            resolved["average"] = price
            resolved["filled"] = amount
            resolved["cost"] = cost
            return resolved

        order_id = resolved.get("id")

        for attempt in range(6):
            filled = to_float(resolved.get("filled"), 0.0)
            status = str(resolved.get("status") or "").lower()

            if filled > 0 and status in {"closed", "filled"}:
                break

            if status in {"canceled", "cancelled", "rejected", "expired"}:
                break

            if not order_id:
                break

            try:
                time.sleep(1.0 + attempt)
                fetched = self.exchange.fetch_order(order_id, symbol)
                if isinstance(fetched, dict):
                    resolved.update(fetched)
            except Exception as exc:
                LOG.warning(
                    "Ordercontrole poging %s mislukt voor %s: %s",
                    attempt + 1,
                    symbol,
                    exc,
                )

        filled = to_float(resolved.get("filled"), 0.0)
        status = str(resolved.get("status") or "").lower()

        if filled <= 0:
            raise RuntimeError(
                f"Live order voor {symbol} niet als uitgevoerd bevestigd "
                f"(status={status or 'onbekend'}, id={order_id or 'onbekend'})"
            )

        price = to_float(
            resolved.get("average") or resolved.get("price"),
            fallback_price,
        )
        cost = to_float(resolved.get("cost"), filled * price)

        if price <= 0 or cost <= 0:
            raise RuntimeError(
                f"Live order voor {symbol} heeft ongeldige uitvoering: "
                f"price={price}, filled={filled}, cost={cost}"
            )

        resolved["average"] = price
        resolved["filled"] = filled
        resolved["cost"] = cost
        return resolved

    def place_market_buy(
        self,
        symbol: str,
        stake_quote: float,
        client_order_id: Optional[str] = None,
        order_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        ticker = self.get_ticker(symbol)
        ask = to_float(ticker.get("ask"), 0.0)
        if ask <= 0:
            raise ValueError(f"Geen geldige ask voor {symbol}")
        amount = self.amount_to_precision_safe(symbol, stake_quote / ask)
        est_quote = amount * ask
        min_notional = self.market_min_notional(symbol)
        if est_quote < min_notional:
            raise ValueError(
                f"{symbol} te klein voor minimale orderwaarde. "
                f"Nodig: {min_notional:.2f} {self.quote}"
            )
        if self.dry_run:
            return {
                "id": f"drybuy-{int(time.time())}",
                "symbol": symbol,
                "price": ask,
                "amount": amount,
                "filled": amount,
                "cost": est_quote,
                "fee": {
                    "cost": est_quote * (
                        to_float(
                            get_cfg(
                                self.cfg,
                                "taker_fee_pct",
                                0.25,
                            ),
                            0.25,
                        )
                        / 100.0
                    ),
                    "currency": self.quote,
                },
                "_diamond_reference_ask": ask,
                "_diamond_execution_spread_pct": self.estimate_spread_pct(ticker),
            }

        execution_spread_pct = self.estimate_spread_pct(ticker)

        if not order_key:
            raise RuntimeError("LIVE_BUY_BLOCKED:missing_pending_order")

        pending_record = (
            self.state.get("pending_orders", {}).get(order_key)
        )
        canary_trade_number = int(
            to_float(
                (pending_record or {}).get("canary_trade_number"),
                0,
            )
        )

        gate = self.canary_new_entry_gate(
            stake_quote,
            canary_trade_number=canary_trade_number,
        )
        if not gate.get("allow", False):
            raise RuntimeError(
                "LIVE_BUY_BLOCKED:" + str(gate.get("reason"))
            )

        # Referentie wordt vlak vóór submit persistent opgeslagen.
        if order_key:
            record = (
                self.state.get("pending_orders", {})
                .get(order_key)
            )
            if isinstance(record, dict):
                record["reference_ask"] = ask
                record["execution_spread_pct"] = execution_spread_pct
                record["reference_ask_at"] = now_iso()
                save_state(self.state_file, self.state)

            # Alle lokale ordervalidatie is nu afgerond. Pas vlak vóór de
            # exchange-call wordt PREPARED -> SUBMITTING atomair opgeslagen.
            self.mark_pending_submitting(order_key)

        order = self.exchange.create_order(
            symbol,
            "market",
            "buy",
            amount,
            None,
            self.order_params(client_order_id),
        )
        if isinstance(order, dict):
            order = dict(order)
            order["_diamond_reference_ask"] = ask
            order["_diamond_execution_spread_pct"] = execution_spread_pct
        return order

    def place_market_sell(
        self,
        symbol: str,
        amount: float,
        client_order_id: Optional[str] = None,
        order_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        amount = self.amount_to_precision_safe(
            symbol,
            amount,
        )
        if amount <= 0:
            raise ValueError("Verkoop amount is 0.")

        ticker = self.get_ticker(symbol)
        bid = to_float(ticker.get("bid"), 0.0)
        if bid <= 0:
            raise ValueError(f"Geen geldige bid voor {symbol}")

        if self.dry_run:
            est_quote = amount * bid
            return {
                "id": f"drysell-{int(time.time())}",
                "symbol": symbol,
                "price": bid,
                "amount": amount,
                "filled": amount,
                "cost": est_quote,
                "fee": {
                    "cost": est_quote * (
                        to_float(
                            get_cfg(
                                self.cfg,
                                "taker_fee_pct",
                                0.25,
                            ),
                            0.25,
                        )
                        / 100.0
                    ),
                    "currency": self.quote,
                },
                "_diamond_reference_bid": bid,
                "_diamond_execution_spread_pct": self.estimate_spread_pct(ticker),
            }

        execution_spread_pct = self.estimate_spread_pct(ticker)

        # Referentie wordt vlak vóór submit persistent opgeslagen.
        if order_key:
            record = (
                self.state.get("pending_orders", {})
                .get(order_key)
            )
            if isinstance(record, dict):
                record["reference_bid"] = bid
                record["execution_spread_pct"] = execution_spread_pct
                record["reference_bid_at"] = now_iso()
                save_state(self.state_file, self.state)

            # Net als BUY: pas direct vóór de exchange-call PREPARED ->
            # SUBMITTING atomair opslaan. Een crash daarna wordt recovery.
            self.mark_pending_submitting(order_key)

        order = self.exchange.create_order(
            symbol,
            "market",
            "sell",
            amount,
            None,
            self.order_params(client_order_id),
        )
        if isinstance(order, dict):
            order = dict(order)
            order["_diamond_reference_bid"] = bid
            order["_diamond_execution_spread_pct"] = execution_spread_pct
        return order

    def try_buy_symbol(
        self,
        symbol: str,
        precomputed_signal: Optional[Dict[str, Any]] = None,
        precomputed_news_gate: Optional[Dict[str, Any]] = None,
        precomputed_ticker: Optional[Dict[str, Any]] = None,
        precomputed_spread_pct: Optional[float] = None,
    ) -> None:
        if not self.spot_enabled():
            return

        if self.entries_blocked_by_recovery():
            self.rate_limited_info(
                self.last_skip_log_ts,
                "recovery_block",
                300,
                "NIEUWE KOOP GEBLOKKEERD | recovery_required=%s | "
                "reden=%s | pending=%d",
                self.state.get("recovery_required"),
                self.state.get("recovery_reason") or "",
                len(self.state.get("pending_orders") or {}),
            )
            return

        if symbol in self.state["positions"]:
            return
        if (
            not self.allow_long_and_short_same_symbol()
            and symbol in self.state.get("short_positions", {})
        ):
            return
        if self.symbol_in_cooldown(symbol):
            return

        max_open = int(
            to_float(
                get_cfg(
                    self.cfg,
                    "max_open_positions",
                    5,
                ),
                5,
            )
        )
        if (
            self.open_positions_count() >= max_open
            or self.total_positions_count()
            >= self.max_total_positions()
        ):
            return
        if self.skip_symbol_due_to_existing_balance(symbol):
            return

        signal = (
            precomputed_signal
            or self.long_entry_signal(symbol)
        )
        if not signal:
            return

        ticker = (
            precomputed_ticker
            or self.get_ticker(symbol)
        )
        spread_pct = precomputed_spread_pct
        if spread_pct is None:
            spread_pct = self.estimate_spread_pct(ticker)

        max_spread_pct = to_float(
            get_cfg(
                self.cfg,
                "max_spread_pct",
                0.25,
            ),
            0.25,
        )
        if spread_pct > max_spread_pct:
            self.rate_limited_info(
                self.last_skip_log_ts,
                f"spread:{symbol}",
                1800,
                "OVERSLAAN KOPEN %s | spread %.3f%% > %.3f%%",
                symbol,
                spread_pct,
                max_spread_pct,
            )
            return

        news_gate = (
            precomputed_news_gate
            or self.news.buy_gate(symbol)
        )
        if not news_gate.get("allow", False):
            self.rate_limited_info(
                self.last_skip_log_ts,
                (
                    f"news:{symbol}:"
                    f"{news_gate.get('reason')}"
                ),
                1800,
                "OVERSLAAN KOPEN %s | news_reason=%s",
                symbol,
                news_gate.get("reason"),
            )
            return

        stake = min(
            to_float(
                get_cfg(
                    self.cfg,
                    "fixed_stake_quote",
                    40,
                ),
                40.0,
            ),
            self.buy_budget_available(),
        )
        if stake <= 0:
            return

        if not self.dry_run:
            gate = self.canary_new_entry_gate(stake)
            if not gate.get("allow", False):
                self.rate_limited_info(
                    self.last_skip_log_ts,
                    f"approval_gate:{symbol}:{gate.get('reason')}",
                    300,
                    "LIVE BUY GEBLOKKEERD %s | %s",
                    symbol,
                    gate.get("reason"),
                )
                return

        order_key: Optional[str] = None
        client_order_id: Optional[str] = None

        try:
            base_asset = symbol.split("/")[0].upper()
            protected_base_amount = 0.0

            if not self.dry_run:
                self.refresh_balance_cache()
                protected_base_amount = self.asset_balance(
                    base_asset
                )

                order_key = self.long_order_key(
                    symbol,
                    signal,
                )
                pending = (
                    self.state.get("pending_orders")
                    or {}
                )
                if order_key in pending:
                    self.set_recovery_required(
                        (
                            "RECOVERY_REQUIRED:"
                            f"duplicate_pending:{order_key}"
                        )
                    )
                    return

                # LIVE_BUY_ORDERBOOK_LIQUIDITY_GATE
                #
                # Alleen nieuwe echte BUYs worden hier gefilterd.
                # Protective SELLs worden NOOIT door deze gate geblokkeerd.
                liquidity_gate_enabled = to_bool(
                    get_cfg(
                        self.cfg,
                        "execution.liquidity_gate_enabled",
                        True,
                    ),
                    True,
                )

                if (
                    not self.dry_run
                    and liquidity_gate_enabled
                ):
                    try:
                        book_depth = max(
                            5,
                            min(
                                1000,
                                int(
                                    to_float(
                                        get_cfg(
                                            self.cfg,
                                            "execution.liquidity_orderbook_depth",
                                            50,
                                        ),
                                        50,
                                    )
                                ),
                            ),
                        )

                        order_book = (
                            self.exchange.fetch_order_book(
                                symbol,
                                book_depth,
                            )
                        )

                        liquidity = evaluate_buy_liquidity(
                            order_book,
                            stake,
                            max_price_impact_pct=to_float(
                                get_cfg(
                                    self.cfg,
                                    "execution.liquidity_max_price_impact_pct",
                                    0.15,
                                ),
                                0.15,
                            ),
                            depth_band_pct=to_float(
                                get_cfg(
                                    self.cfg,
                                    "execution.liquidity_depth_band_pct",
                                    0.25,
                                ),
                                0.25,
                            ),
                            min_depth_multiple=to_float(
                                get_cfg(
                                    self.cfg,
                                    "execution.liquidity_min_depth_multiple",
                                    2.0,
                                ),
                                2.0,
                            ),
                        )
                    except Exception as exc:
                        self.rate_limited_info(
                            self.last_skip_log_ts,
                            f"liquidity_error:{symbol}",
                            300,
                            "OVERSLAAN KOPEN %s | "
                            "liquidity gate fout: %s",
                            symbol,
                            type(exc).__name__,
                        )
                        return

                    if not liquidity.get("allow", False):
                        self.rate_limited_info(
                            self.last_skip_log_ts,
                            f"liquidity_block:{symbol}",
                            300,
                            "OVERSLAAN KOPEN %s | "
                            "liquidity=%s | "
                            "impact=%.4f%% | depth=%.2fx",
                            symbol,
                            liquidity.get("reason"),
                            to_float(
                                liquidity.get(
                                    "estimated_price_impact_pct"
                                ),
                                0.0,
                            ),
                            to_float(
                                liquidity.get("depth_multiple"),
                                0.0,
                            ),
                        )
                        return

                record = self.prepare_pending_long_order(
                    order_key,
                    symbol,
                    signal,
                    stake,
                    spread_pct,
                    protected_base_amount,
                )
                client_order_id = str(
                    record.get("clientOrderId")
                    or ""
                )

            try:
                raw_order = self.place_market_buy(
                    symbol,
                    stake,
                    client_order_id=client_order_id,
                    order_key=order_key,
                )
            except Exception:
                if not self.dry_run and order_key:
                    record = (
                        self.state.get("pending_orders", {})
                        .get(order_key)
                    )
                    status = str(
                        (record or {}).get("status")
                        or ""
                    ).upper()

                    if status == "PREPARED":
                        # create_order is aantoonbaar nog niet gestart.
                        self.abandon_pending_after_confirmed_rejection(
                            order_key,
                            "lokale_validatie_voor_submit",
                        )
                    else:
                        # SUBMITTING is bewust ambigu: Bitvavo kan de order
                        # al hebben ontvangen terwijl de response wegviel.
                        self.set_recovery_required(
                            (
                                "RECOVERY_REQUIRED:"
                                f"submit_ambiguous:{order_key}"
                            )
                        )
                raise

            if not self.dry_run and order_key:
                self.update_pending_from_order(
                    order_key,
                    raw_order,
                    status="SUBMITTED",
                )

            reference_ask = to_float(
                raw_order.get("_diamond_reference_ask"),
                to_float(ticker.get("ask"), 0.0),
            )
            execution_spread_pct = to_float(
                raw_order.get("_diamond_execution_spread_pct"),
                spread_pct,
            )
            canary_trade_number = 0
            if not self.dry_run and order_key:
                pending_record = (
                    self.state.get("pending_orders", {})
                    .get(order_key)
                )
                if isinstance(pending_record, dict):
                    reference_ask = to_float(
                        pending_record.get("reference_ask"),
                        reference_ask,
                    )
                    execution_spread_pct = to_float(
                        pending_record.get("execution_spread_pct"),
                        execution_spread_pct,
                    )
                    canary_trade_number = int(
                        to_float(
                            pending_record.get("canary_trade_number"),
                            0,
                        )
                    )

            fallback_price = to_float(
                raw_order.get("average")
                or raw_order.get("price")
                or signal["close"],
                signal["close"],
            )
            fallback_amount = self.amount_to_precision_safe(
                symbol,
                stake / max(
                    fallback_price,
                    1e-12,
                ),
            )

            order = self.resolve_order_fill(
                symbol,
                raw_order,
                fallback_price=fallback_price,
                fallback_amount=fallback_amount,
            )

            price = to_float(
                order.get("average"),
                fallback_price,
            )
            amount = to_float(
                order.get("filled"),
                fallback_amount,
            )
            quote_amount = to_float(
                order.get("cost"),
                amount * price,
            )
            fee_quote = self.order_fee_quote(
                order,
                quote_amount,
                symbol,
                price,
            )
            buy_slippage_pct = execution_slippage_pct(
                "buy",
                reference_ask,
                price,
            )

            if not self.dry_run and order_key:
                # Eerst de bevestigde fill persistent opslaan. Crasht Render
                # hierna vóór de positie-save, dan kan recovery de positie
                # reconstrueren uit pending + Bitvavo.
                self.update_pending_from_order(
                    order_key,
                    order,
                    status="FILLED_CONFIRMED",
                )

            if self.dry_run:
                current_simulated = to_float(
                    self.state.get("simulated_free_quote"),
                    to_float(
                        get_cfg(
                            self.cfg,
                            "risk.simulated_quote_balance",
                            3000,
                        ),
                        3000.0,
                    ),
                )
                required = quote_amount + fee_quote
                if required > current_simulated + 1e-9:
                    raise RuntimeError(
                        "Onvoldoende gesimuleerd saldo: "
                        f"nodig={required:.2f}, "
                        f"beschikbaar={current_simulated:.2f} "
                        f"{self.quote}"
                    )
                self.state["simulated_free_quote"] = (
                    current_simulated
                    - required
                )

            news_snapshot = self.news.coin_news(symbol)
            fear_greed = self.news.fear_greed()

            self.state["positions"][symbol] = {
                "opened_by_bot": True,
                "candidate_key": str(
                    signal.get("candidate_key") or ""
                ),
                "strategy": str(signal.get("strategy") or ""),
                "market_regime": str(
                    signal.get("market_regime") or ""
                ),
                "selection_reason": str(
                    signal.get("selection_reason") or ""
                ),
                "signal_candle_ts": str(
                    signal.get("signal_candle_ts") or ""
                ),
                "opened_at": utc_now_ts(),
                "entry_price": price,
                "amount": amount,
                "quote_amount": quote_amount,
                "fees_buy_quote": fee_quote,
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "highest_price": price,
                "news_score_at_entry": (
                    news_snapshot.get(
                        "news_score",
                        0.0,
                    )
                ),
                "fear_greed_at_entry": (
                    fear_greed.get("value")
                ),
                "tech_score_at_entry": (
                    signal.get(
                        "tech_score",
                        0.0,
                    )
                ),
                "protected_base_amount": (
                    protected_base_amount
                ),
                "client_order_id": (
                    client_order_id
                    if not self.dry_run
                    else None
                ),
                "exchange_order_id": (
                    order.get("id")
                    if not self.dry_run
                    else None
                ),
                "canary_trade_number": (
                    canary_trade_number
                    if not self.dry_run
                    else 0
                ),
                "entry_reference_ask": reference_ask,
                "entry_slippage_pct": buy_slippage_pct,
                "entry_slippage_status": classify_slippage_status(
                    buy_slippage_pct
                ),
                "entry_spread_pct": execution_spread_pct,
            }

            if not self.dry_run and order_key:
                self.state.get(
                    "pending_orders",
                    {},
                ).pop(
                    order_key,
                    None,
                )
                self.clear_recovery_if_safe()

            # Positie + verwijderen van pending worden samen atomair opgeslagen.
            save_state(
                self.state_file,
                self.state,
            )
            self.refresh_balance_cache()

            append_trade_csv(
                self.trades_file,
                {
                    "ts": now_iso(),
                    "market": symbol,
                    "side": "BUY",
                    "price": round(price, 12),
                    "base_amount": amount,
                    "quote_amount": round(
                        quote_amount,
                        8,
                    ),
                    "fees_quote": round(
                        fee_quote,
                        8,
                    ),
                    "spread_pct": round(
                        spread_pct,
                        6,
                    ),
                    "net_pnl_quote": "",
                    "holding_time_min": "",
                    "reason": (
                        "entry_signal_news_"
                        f"{news_snapshot.get('news_score', 0.0)}"
                    ),
                    "dry_run": self.dry_run,
                },
            )

            if not self.dry_run:
                self.canary_open_event(
                    symbol,
                    self.state["positions"][symbol],
                    recovered=False,
                )

            LOG.info(
                "KOOP %s | prijs=%.8f amount=%s quote=%.2f %s "
                "nieuws=%.2f fg=%s tech=%.2f rsi=%.2f "
                "atr%%=%.3f stop=%.8f tp=%.8f dry=%s",
                symbol,
                price,
                amount,
                quote_amount,
                self.quote,
                to_float(
                    news_snapshot.get(
                        "news_score",
                        0.0,
                    ),
                    0.0,
                ),
                fear_greed.get("value"),
                to_float(
                    signal.get(
                        "tech_score",
                        0.0,
                    ),
                    0.0,
                ),
                signal["rsi"],
                signal["atr_pct"],
                signal["stop_loss"],
                signal["take_profit"],
                self.dry_run,
            )

        except Exception as exc:
            LOG.exception(
                "KOOP mislukt voor %s: %s",
                symbol,
                exc,
            )

    def try_sell_symbol(
        self,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
    ) -> None:
        # Coins die niet door de bot zijn gekocht worden nooit verkocht.
        if not to_bool(position.get("opened_by_bot"), False):
            LOG.info(
                "OVERSLAAN VERKOOP %s | niet door bot gekocht, wordt beschermd",
                symbol,
            )
            return

        order_key: Optional[str] = None
        client_order_id: Optional[str] = None

        try:
            tracked_amount = to_float(
                position.get("amount"),
                0.0,
            )
            if tracked_amount <= 0:
                raise RuntimeError(
                    f"Geen geldige bot-hoeveelheid voor {symbol}"
                )

            sell_amount = tracked_amount

            if not self.dry_run:
                # Een bestaande pending SELL voor dit symbool is altijd eerst
                # recovery. Nooit een tweede verkoop sturen.
                existing_pending_sell = self.pending_sell_for_symbol(
                    symbol
                )
                if existing_pending_sell is not None:
                    self.set_recovery_required(
                        "RECOVERY_REQUIRED:duplicate_pending_sell:"
                        f"{symbol}"
                    )
                    LOG.error(
                        "VERKOOP GEBLOKKEERD %s | pending SELL bestaat al | key=%s",
                        symbol,
                        existing_pending_sell.get("order_key"),
                    )
                    return

                self.refresh_balance_cache()
                base_asset = symbol.split("/")[0].upper()
                free_base = self.asset_balance(base_asset)
                protected_base = to_float(
                    position.get("protected_base_amount"),
                    0.0,
                )

                # Alleen de hoeveelheid boven het vóór de botkoop aanwezige
                # saldo mag worden verkocht. Handmatig bezit blijft beschermd.
                bot_owned_available = max(
                    0.0,
                    free_base - protected_base,
                )
                sell_amount = min(
                    tracked_amount,
                    bot_owned_available,
                )
                sell_amount = self.amount_to_precision_safe(
                    symbol,
                    sell_amount,
                )

                if sell_amount <= 0:
                    LOG.error(
                        "VERKOOP GEBLOKKEERD %s | vrij=%s | beschermd=%s | "
                        "botpositie=%s",
                        symbol,
                        free_base,
                        protected_base,
                        tracked_amount,
                    )
                    return

                order_key = self.sell_order_key(
                    symbol,
                    position,
                    reason,
                    sell_amount,
                )
                pending = self.state.get("pending_orders") or {}
                if order_key in pending:
                    self.set_recovery_required(
                        "RECOVERY_REQUIRED:duplicate_pending_sell:"
                        f"{order_key}"
                    )
                    return

                record = self.prepare_pending_sell_order(
                    order_key,
                    symbol,
                    position,
                    reason,
                    sell_amount,
                )
                client_order_id = str(
                    record.get("clientOrderId")
                    or ""
                )

            try:
                raw_order = self.place_market_sell(
                    symbol,
                    sell_amount,
                    client_order_id=client_order_id,
                    order_key=order_key,
                )
            except Exception:
                if not self.dry_run and order_key:
                    record = (
                        self.state.get("pending_orders", {})
                        .get(order_key)
                    )
                    status = str(
                        (record or {}).get("status")
                        or ""
                    ).upper()

                    if status == "PREPARED":
                        # create_order is aantoonbaar nog niet gestart.
                        self.abandon_pending_after_confirmed_rejection(
                            order_key,
                            "lokale_validatie_voor_sell_submit",
                        )
                    else:
                        # SUBMITTING is ambigu: niet opnieuw verkopen.
                        self.set_recovery_required(
                            "RECOVERY_REQUIRED:sell_submit_ambiguous:"
                            f"{order_key}"
                        )
                raise

            if not self.dry_run and order_key:
                self.update_pending_from_order(
                    order_key,
                    raw_order,
                    status="SUBMITTED",
                )

            reference_bid = to_float(
                raw_order.get("_diamond_reference_bid"),
                0.0,
            )
            execution_spread_pct = to_float(
                raw_order.get("_diamond_execution_spread_pct"),
                0.0,
            )

            if not self.dry_run and order_key:
                record = (
                    self.state.get("pending_orders", {})
                    .get(order_key)
                )
                if isinstance(record, dict):
                    reference_bid = to_float(
                        record.get("reference_bid"),
                        reference_bid,
                    )
                    execution_spread_pct = to_float(
                        record.get("execution_spread_pct"),
                        execution_spread_pct,
                    )

            if reference_bid <= 0:
                ticker = self.get_ticker(symbol)
                reference_bid = to_float(
                    ticker.get("bid"),
                    0.0,
                )
                if execution_spread_pct <= 0:
                    execution_spread_pct = self.estimate_spread_pct(
                        ticker
                    )

            fallback_price = reference_bid
            spread_pct = execution_spread_pct

            order = self.resolve_order_fill(
                symbol,
                raw_order,
                fallback_price=fallback_price,
                fallback_amount=sell_amount,
            )

            if not self.dry_run and order_key:
                # Eerst bevestigde SELL-fill bewaren. Crasht Render vóór de
                # positie/PnL-save, dan reconstrueert recovery exact deze fill.
                self.update_pending_from_order(
                    order_key,
                    order,
                    status="FILLED_CONFIRMED",
                )

            self.apply_confirmed_sell_fill(
                symbol=symbol,
                position=position,
                reason=reason,
                order=order,
                fallback_price=fallback_price,
                fallback_amount=sell_amount,
                pending_order_key=(
                    order_key
                    if not self.dry_run
                    else None
                ),
                spread_pct=spread_pct,
                reference_bid=reference_bid,
                recovered=False,
            )

        except Exception as exc:
            LOG.exception(
                "VERKOOP mislukt voor %s: %s",
                symbol,
                exc,
            )

    def fast_long_exit_signal(
        self,
        symbol: str,
        position: Dict[str, Any],
    ) -> Optional[str]:
        """
        Lichtgewicht bescherming van een open LONG.

        Gebruikt de actuele uitvoerbare bid-prijs en haalt geen OHLCV,
        indicatoren of nieuwe entry-signalen op. Hierdoor kunnen een
        hard-stop en winst-trailing veel vaker worden bewaakt dan de
        normale strategiecyclus.
        """
        ticker = self.get_ticker(symbol)
        bid = to_float(ticker.get("bid"), 0.0)
        if bid <= 0:
            return None

        entry_price = to_float(
            position.get("entry_price"),
            0.0,
        )
        if entry_price <= 0:
            return None

        previous_highest = to_float(
            position.get("highest_price"),
            entry_price,
        )
        highest = max(
            previous_highest,
            bid,
        )
        position["highest_price"] = highest

        stop_loss = to_float(
            position.get("stop_loss"),
            0.0,
        )
        take_profit = to_float(
            position.get("take_profit"),
            0.0,
        )

        min_profit_eur = max(
            0.0,
            to_float(
                get_cfg(
                    self.cfg,
                    "min_profit_eur",
                    0.50,
                ),
                0.50,
            ),
        )

        min_profitable_exit_price = (
            self.minimum_profitable_exit_price(
                position,
                min_profit_eur,
            )
        )

        highest_profit_pct = (
            ((highest - entry_price) / entry_price)
            * 100.0
        )

        trigger_pct = max(
            0.0,
            to_float(
                get_cfg(
                    self.cfg,
                    "signals.profit_trailing_trigger_pct",
                    1.0,
                ),
                1.0,
            ),
        )
        pullback_pct = max(
            0.0,
            to_float(
                get_cfg(
                    self.cfg,
                    "signals.profit_trailing_pullback_pct",
                    0.5,
                ),
                0.5,
            ),
        )

        if (
            highest_profit_pct >= trigger_pct
            and highest > 0
        ):
            tight_stop = highest * (
                1.0 - pullback_pct / 100.0
            )

            if (
                tight_stop >= min_profitable_exit_price
                and tight_stop > stop_loss
            ):
                position["stop_loss"] = tight_stop
                stop_loss = tight_stop

                self.rate_limited_info(
                    self.last_skip_log_ts,
                    f"fast_trail:{symbol}",
                    60,
                    "FAST WINST-TRAIL %s | "
                    "bid=%.8f | hoogste=%.8f | "
                    "stop=%.8f",
                    symbol,
                    bid,
                    highest,
                    stop_loss,
                )

        hard_sl_pct = max(
            0.0,
            to_float(
                get_cfg(
                    self.cfg,
                    "signals.hard_stop_loss_pct",
                    7.0,
                ),
                7.0,
            ),
        )
        hard_stop = entry_price * (
            1.0 - hard_sl_pct / 100.0
        )

        if bid <= hard_stop:
            LOG.warning(
                "FAST HARDE STOP %s | bid=%.8f | stop=%.8f",
                symbol,
                bid,
                hard_stop,
            )
            return "hard_stop_loss"

        if stop_loss > 0 and bid <= stop_loss:
            if (
                stop_loss >= min_profitable_exit_price
                and highest > entry_price
            ):
                LOG.info(
                    "FAST TRAILING STOP %s | "
                    "bid=%.8f | stop=%.8f | hoogste=%.8f",
                    symbol,
                    bid,
                    stop_loss,
                    highest,
                )
                return "trailing_stop"

            return "stop_loss"

        profit_trailing_active = (
            stop_loss >= min_profitable_exit_price
            and min_profitable_exit_price
            < float("inf")
        )

        if (
            take_profit > 0
            and bid >= take_profit
            and not profit_trailing_active
        ):
            return "take_profit"

        return None

    def manage_open_positions_fast(self) -> None:
        positions = list(
            (self.state.get("positions") or {}).items()
        )

        state_changed = False

        for symbol, position in positions:
            if not to_bool(
                position.get("opened_by_bot"),
                False,
            ):
                continue

            before_high = to_float(
                position.get("highest_price"),
                0.0,
            )
            before_stop = to_float(
                position.get("stop_loss"),
                0.0,
            )

            try:
                reason = self.fast_long_exit_signal(
                    symbol,
                    position,
                )

                after_high = to_float(
                    position.get("highest_price"),
                    0.0,
                )
                after_stop = to_float(
                    position.get("stop_loss"),
                    0.0,
                )

                if (
                    after_high != before_high
                    or after_stop != before_stop
                ):
                    state_changed = True

                if reason:
                    # Nieuwe peak/stop eerst persistent opslaan.
                    save_state(
                        self.state_file,
                        self.state,
                    )
                    state_changed = False

                    if self.sell_allowed_by_profit(
                        symbol,
                        position,
                        reason,
                    ):
                        self.try_sell_symbol(
                            symbol,
                            position,
                            reason,
                        )

            except Exception as exc:
                LOG.warning(
                    "FAST positiecontrole overgeslagen "
                    "voor %s: %s",
                    symbol,
                    exc,
                )

        if state_changed:
            save_state(
                self.state_file,
                self.state,
            )

    def manage_open_positions(self) -> None:
        positions = list((self.state.get("positions") or {}).items())
        for symbol, position in positions:
            # Sla posities over die niet door de bot zijn gekocht
            if not to_bool(position.get("opened_by_bot"), False):
                continue
            try:
                reason = self.long_exit_signal(symbol, position)
                save_state(self.state_file, self.state)
                if reason and self.sell_allowed_by_profit(symbol, position, reason):
                    self.try_sell_symbol(symbol, position, reason)
            except Exception as e:
                LOG.warning("Positiebeheer overgeslagen voor %s door marktdatafout: %s", symbol, e)

    def open_paper_short(
        self,
        symbol: str,
        signal: Dict[str, Any],
    ) -> None:
        if not self.short_enabled():
            return

        if self.short_test_complete():
            return

        if (
            self.short_positions_count()
            >= self.max_open_short_positions()
        ):
            return

        if (
            self.total_positions_count()
            >= self.max_total_positions()
        ):
            return

        ticker = self.get_ticker(symbol)
        bid = to_float(ticker.get("bid"), 0.0)

        if bid <= 0:
            return

        spread_pct = self.estimate_spread_pct(
            ticker
        )

        max_spread_pct = to_float(
            get_cfg(
                self.cfg,
                "max_spread_pct",
                0.25,
            ),
            0.25,
        )

        if spread_pct > max_spread_pct:
            self.rate_limited_info(
                self.last_skip_log_ts,
                f"short_spread:{symbol}",
                600,
                "PAPER SHORT OVERSLAAN %s | "
                "spread=%.4f%% > max=%.4f%%",
                symbol,
                spread_pct,
                max_spread_pct,
            )

            return

        plan = self.short_trade_plan(
            symbol,
            signal,
            ticker,
        )

        if not plan.get("allowed", False):
            blockers = list(
                plan.get("blockers", [])
                or []
            )
            self.rate_limited_info(
                self.last_skip_log_ts,
                f"short_cost_plan:{symbol}",
                600,
                "PAPER SHORT V3 OVERSLAAN %s | %s",
                symbol,
                "; ".join(blockers) or "kostenplan afgewezen",
            )
            append_short_execution_csv(
                self.short_execution_file,
                {
                    "ts": now_iso(),
                    "event": "PLAN_REJECT",
                    "strategy_version": plan.get("strategy_version"),
                    "market": symbol,
                    "entry_trigger": signal.get("entry_trigger"),
                    "entry_price": plan.get("bid"),
                    "signal_close": plan.get("signal_close"),
                    "atr": plan.get("atr"),
                    "atr_pct": plan.get("atr_pct"),
                    "spread_pct": plan.get("spread_pct"),
                    "planned_stop_loss": plan.get("stop_loss"),
                    "planned_take_profit": plan.get("take_profit"),
                    "base_take_profit": plan.get("base_take_profit"),
                    "planned_tp_atr_mult": plan.get("planned_tp_atr_mult"),
                    "expected_net_reward": plan.get("expected_net_reward"),
                    "expected_net_risk": plan.get("expected_net_risk"),
                    "expected_net_rr": plan.get("expected_net_rr"),
                    "exit_reason": "; ".join(blockers),
                    "paper_only": True,
                    "dry_run": True,
                },
            )
            return

        amount = to_float(plan.get("amount"), 0.0)
        quote_amount = to_float(
            plan.get("quote_amount"),
            0.0,
        )
        fee_open_quote = to_float(
            plan.get("fee_open_quote"),
            0.0,
        )
        leverage = to_float(
            plan.get("leverage"),
            1.0,
        )
        margin_per_trade = to_float(
            plan.get("margin_quote"),
            30.0,
        )
        strategy_version = str(
            plan.get("strategy_version")
            or self.configured_short_strategy_version()
        )

        self.state["short_positions"][symbol] = {
            "paper_only": True,
            "strategy_version": strategy_version,
            "opened_at": utc_now_ts(),
            "entry_price": bid,
            "signal_close": to_float(plan.get("signal_close"), 0.0),
            "amount": amount,
            "margin_quote": margin_per_trade,
            "leverage": leverage,
            "quote_amount": quote_amount,
            "fees_open_quote": fee_open_quote,
            "stop_loss": to_float(plan.get("stop_loss"), 0.0),
            "take_profit": to_float(plan.get("take_profit"), 0.0),
            "base_take_profit": to_float(
                plan.get("base_take_profit"),
                0.0,
            ),
            "planned_tp_atr_mult": to_float(
                plan.get("planned_tp_atr_mult"),
                0.0,
            ),
            "expected_net_reward": to_float(
                plan.get("expected_net_reward"),
                0.0,
            ),
            "expected_net_risk": to_float(
                plan.get("expected_net_risk"),
                0.0,
            ),
            "expected_net_rr": to_float(
                plan.get("expected_net_rr"),
                0.0,
            ),
            "entry_cover_markup": to_float(
                plan.get("cover_markup"),
                1.0,
            ),
            "entry_spread_pct": spread_pct,
            "entry_trigger": signal.get(
                "entry_trigger",
                "paper_short_entry",
            ),
            "entry_rsi": signal.get("rsi"),
            "entry_atr": signal.get("atr"),
            "entry_atr_pct": signal.get("atr_pct"),
            "breakout_level": signal.get("breakout_level"),
            "use_intrabar_thresholds": to_bool(
                get_cfg(
                    self.cfg,
                    "short.use_intrabar_thresholds",
                    True,
                ),
                True,
            ),
            "simulate_threshold_execution": to_bool(
                get_cfg(
                    self.cfg,
                    "short.simulate_threshold_execution",
                    True,
                ),
                True,
            ),
        }
        save_state(self.state_file, self.state)

        append_trade_csv(
            self.trades_file,
            {
                "ts": now_iso(),
                "market": symbol,
                "side": "SHORT_OPEN",
                "price": round(bid, 12),
                "base_amount": amount,
                "quote_amount": round(quote_amount, 8),
                "fees_quote": round(fee_open_quote, 8),
                "spread_pct": round(spread_pct, 6),
                "net_pnl_quote": "",
                "holding_time_min": "",
                "reason": (
                    "paper_short_"
                    + str(
                        signal.get(
                            "entry_trigger",
                            "entry",
                        )
                    )
                ),
                "dry_run": True,
            },
        )

        append_short_execution_csv(
            self.short_execution_file,
            {
                "ts": now_iso(),
                "event": "OPEN",
                "strategy_version": strategy_version,
                "market": symbol,
                "entry_trigger": signal.get("entry_trigger"),
                "entry_price": bid,
                "signal_close": plan.get("signal_close"),
                "atr": plan.get("atr"),
                "atr_pct": plan.get("atr_pct"),
                "spread_pct": spread_pct,
                "planned_stop_loss": plan.get("stop_loss"),
                "planned_take_profit": plan.get("take_profit"),
                "base_take_profit": plan.get("base_take_profit"),
                "planned_tp_atr_mult": plan.get("planned_tp_atr_mult"),
                "expected_net_reward": plan.get("expected_net_reward"),
                "expected_net_risk": plan.get("expected_net_risk"),
                "expected_net_rr": plan.get("expected_net_rr"),
                "paper_only": True,
                "dry_run": True,
            },
        )

        LOG.info(
            "PAPER SHORT V3 OPEN %s | trigger=%s | prijs=%.8f | "
            "amount=%s | notional=%.2f %s | lev=%.2f | "
            "RSI=%.2f | ATR=%.3f%% | spread=%.4f%% | "
            "SL=%.8f | TP=%.8f | net_rr=%.2f",
            symbol,
            signal.get("entry_trigger", "onbekend"),
            bid,
            amount,
            quote_amount,
            self.quote,
            leverage,
            to_float(signal.get("rsi"), 0.0),
            to_float(signal.get("atr_pct"), 0.0),
            spread_pct,
            to_float(plan.get("stop_loss"), 0.0),
            to_float(plan.get("take_profit"), 0.0),
            to_float(plan.get("expected_net_rr"), 0.0),
        )

    def close_paper_short(
        self,
        symbol: str,
        position: Dict[str, Any],
        reason: str,
        exit_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        ticker = self.get_ticker(symbol)
        market_ask = to_float(ticker.get("ask"), 0.0)
        if market_ask <= 0:
            return

        ask = self.simulated_short_exit_ask(
            position,
            reason,
            ticker,
            exit_diagnostics,
        )
        if ask <= 0:
            return

        amount = to_float(position.get("amount"), 0.0)
        cover_quote = amount * ask
        fee_close_quote = cover_quote * (
            to_float(
                get_cfg(self.cfg, "taker_fee_pct", 0.25),
                0.25,
            )
            / 100.0
        )
        entry_quote = to_float(
            position.get("quote_amount"),
            0.0,
        )
        fee_open_quote = to_float(
            position.get("fees_open_quote"),
            0.0,
        )
        net_pnl_quote = (
            entry_quote
            - fee_open_quote
            - cover_quote
            - fee_close_quote
        )
        holding_time_min = minutes_since(
            float(
                position.get(
                    "opened_at",
                    utc_now_ts(),
                )
            )
        )

        self.state["short_pnl_quote"] = (
            to_float(
                self.state.get("short_pnl_quote", 0.0),
                0.0,
            )
            + net_pnl_quote
        )
        self.state["short_trades"] = int(
            self.state.get("short_trades", 0)
        ) + 1
        if net_pnl_quote > 0:
            self.state["short_wins"] = int(
                self.state.get("short_wins", 0)
            ) + 1

        self.state["short_positions"].pop(
            symbol,
            None,
        )
        self.state["short_cooldown"][symbol] = (
            utc_now_ts()
        )
        save_state(self.state_file, self.state)

        append_trade_csv(
            self.trades_file,
            {
                "ts": now_iso(),
                "market": symbol,
                "side": "SHORT_CLOSE",
                "price": round(ask, 12),
                "base_amount": amount,
                "quote_amount": round(cover_quote, 8),
                "fees_quote": round(fee_close_quote, 8),
                "spread_pct": round(
                    self.estimate_spread_pct(ticker),
                    6,
                ),
                "net_pnl_quote": round(net_pnl_quote, 8),
                "holding_time_min": round(holding_time_min, 2),
                "reason": reason,
                "dry_run": True,
            },
        )

        details = exit_diagnostics or {}
        append_short_execution_csv(
            self.short_execution_file,
            {
                "ts": now_iso(),
                "event": "CLOSE",
                "strategy_version": position.get("strategy_version"),
                "market": symbol,
                "entry_trigger": position.get("entry_trigger"),
                "entry_price": position.get("entry_price"),
                "signal_close": position.get("signal_close"),
                "atr": position.get("entry_atr"),
                "atr_pct": position.get("entry_atr_pct"),
                "spread_pct": self.estimate_spread_pct(ticker),
                "planned_stop_loss": position.get("stop_loss"),
                "planned_take_profit": position.get("take_profit"),
                "base_take_profit": position.get("base_take_profit"),
                "planned_tp_atr_mult": position.get("planned_tp_atr_mult"),
                "expected_net_reward": position.get("expected_net_reward"),
                "expected_net_risk": position.get("expected_net_risk"),
                "expected_net_rr": position.get("expected_net_rr"),
                "exit_reason": reason,
                "exit_price": ask,
                "market_ask_at_close": market_ask,
                "exit_candle_open": details.get("candle_open"),
                "exit_candle_high": details.get("candle_high"),
                "exit_candle_low": details.get("candle_low"),
                "exit_candle_close": details.get("candle_close"),
                "stop_overshoot_pct": details.get("stop_overshoot_pct"),
                "take_profit_overshoot_pct": details.get(
                    "take_profit_overshoot_pct"
                ),
                "net_pnl_quote": net_pnl_quote,
                "holding_time_min": holding_time_min,
                "paper_only": True,
                "dry_run": True,
            },
        )

        LOG.info(
            "PAPER SHORT V3 CLOSE %s | prijs=%.8f | markt_ask=%.8f | "
            "amount=%s | pnl=%.4f %s | reden=%s",
            symbol,
            ask,
            market_ask,
            amount,
            net_pnl_quote,
            self.quote,
            reason,
        )

    def try_open_paper_short(self, symbol: str) -> None:
        if not self.short_enabled():
            return

        if self.short_test_complete():
            self.rate_limited_info(
                self.last_skip_log_ts,
                "short_test_complete",
                3600,
                "PAPER-SHORTTEST KLAAR | "
                "geen nieuwe paper-shorts",
            )
            return

        if (
            self.short_positions_count()
            >= self.max_open_short_positions()
        ):
            return

        if (
            self.total_positions_count()
            >= self.max_total_positions()
        ):
            return

        if symbol in self.state["short_positions"]:
            return

        if self.short_symbol_in_cooldown(symbol):
            return

        if (
            not self.allow_long_and_short_same_symbol()
            and symbol in self.state.get(
                "positions",
                {},
            )
        ):
            return

        signal = self.short_entry_signal(symbol)
        if not signal:
            return
        try:
            self.open_paper_short(symbol, signal)
        except Exception as e:
            LOG.exception("SHORT OPEN mislukt voor %s: %s", symbol, e)

    def manage_open_short_positions(self) -> None:
        positions = list(
            (self.state.get("short_positions") or {}).items()
        )
        for symbol, position in positions:
            try:
                exit_diagnostics = self.short_exit_diagnostics(
                    symbol,
                    position,
                )
                reason = exit_diagnostics.get("reason")
                if reason and self.close_short_allowed_by_profit(
                    symbol,
                    position,
                    str(reason),
                    exit_diagnostics,
                ):
                    self.close_paper_short(
                        symbol,
                        position,
                        str(reason),
                        exit_diagnostics,
                    )
            except Exception as exc:
                LOG.warning(
                    "Short positiebeheer overgeslagen voor %s door marktdatafout: %s",
                    symbol,
                    exc,
                )

    def print_status(self, symbols: List[str]) -> None:
        every_seconds = int(to_float(get_cfg(self.cfg, "skip_log_every_seconds", 600), 600.0))
        now_ts = utc_now_ts()
        if every_seconds > 0 and self.last_status_log_ts > 0 and (now_ts - self.last_status_log_ts) < every_seconds:
            return
        self.last_status_log_ts = now_ts

        pnl = to_float(self.state.get("pnl_quote", 0.0), 0.0)
        trades = int(self.state.get("trades", 0))
        wins = int(self.state.get("wins", 0))
        winrate = (wins / trades * 100.0) if trades > 0 else 0.0
        short_pnl = to_float(self.state.get("short_pnl_quote", 0.0), 0.0)
        short_trades = int(self.state.get("short_trades", 0))
        short_wins = int(self.state.get("short_wins", 0))
        short_winrate = (short_wins / short_trades * 100.0) if short_trades > 0 else 0.0
        fg = self.news.fear_greed()

        LOG.info(
            "STATUS | droog=%s | symbolen=%s | spot_open=%s | korte_open=%s | vooraf=%.2f | spot_pnl=%.2f | korte_pnl=%.2f | spot_trades=%s | short_trades=%s | spot_winrate=%.1f%% | short_winrate=%.1f%% | angst_greed=%s",
            self.dry_run, len(symbols), self.open_positions_count(), self.short_positions_count(),
            self.bot_invested_quote(), pnl, short_pnl, trades, short_trades, winrate, short_winrate, fg.get("value"),
        )

    def run_once(self) -> None:
        # Alleen deze bot schrijft diamond_state.json.
        self.state = load_state(
            self.state_file
        )

        # Pending live-orders worden vóór nieuwe entries gereconcilieerd.
        # Bestaande posities blijven daarna gewoon bewaakt.
        if self.state.get("pending_orders"):
            self.reconcile_pending_orders()

        self.ensure_short_test_baseline()

        control = load_control(
            self.control_file
        )

        self.refresh_balance_cache()

        # Open posities blijven altijd bewaakt, ook tijdens een pauze of
        # recovery. Alleen nieuwe spot-entries worden door recovery geblokkeerd.
        self.manage_open_positions()
        self.manage_open_short_positions()

        symbols = self.scanned_symbols()
        self.print_status(symbols)

        paused = to_bool(
            control.get("paused"),
            False,
        )

        pause_reason = str(
            control.get("pause_reason")
            or ""
        )

        # De longteststop blokkeert alleen nieuwe spotposities.
        # Veiligheidspauzes blokkeren zowel spot als paper-shorts.
        long_test_pause = (
            paused
            and pause_reason.startswith(
                "testdoel_"
            )
            and pause_reason.endswith(
                "_trades_bereikt"
            )
        )

        recovery_block = self.entries_blocked_by_recovery()

        block_spot_entries = (
            paused
            or recovery_block
        )
        block_short_entries = (
            paused
            and not long_test_pause
        )

        if recovery_block:
            self.rate_limited_info(
                self.last_skip_log_ts,
                "recovery_gate",
                300,
                "RECOVERY_REQUIRED | nieuwe spot-aankopen geblokkeerd | "
                "reden=%s | pending=%d",
                self.state.get("recovery_reason") or "pending_order",
                len(self.state.get("pending_orders") or {}),
            )

        if paused:
            self.rate_limited_info(
                self.last_skip_log_ts,
                "agent_pause",
                600,
                "BOT GEPAUZEERD | reden=%s | "
                "open posities blijven bewaakt | "
                "paper-shorts toegestaan=%s",
                pause_reason or "onbekend",
                long_test_pause,
            )

        if self.selective_execution_enabled and not block_spot_entries:
            self.execute_selective_contracts()

        max_open_spot = int(
            to_float(
                get_cfg(
                    self.cfg,
                    "max_open_positions",
                    5,
                ),
                5,
            )
        )

        if (
            not block_spot_entries
            and self.spot_enabled()
            and self.legacy_spot_entry_route_enabled()
            and self.open_positions_count()
            < max_open_spot
            and self.total_positions_count()
            < self.max_total_positions()
        ):
            candidates = self.collect_buy_candidates(
                symbols
            )

            if candidates:
                top_n_news = int(
                    to_float(
                        get_cfg(
                            self.cfg,
                            "news.top_n_for_news_check",
                            3,
                        ),
                        3,
                    )
                )

                if top_n_news <= 0:
                    top_n_news = 1

                for item in candidates[
                    :top_n_news
                ]:
                    if (
                        self.open_positions_count()
                        >= max_open_spot
                        or self.total_positions_count()
                        >= self.max_total_positions()
                    ):
                        break

                    symbol = item["symbol"]

                    news_gate = self.news.buy_gate(
                        symbol
                    )

                    self.try_buy_symbol(
                        symbol,
                        precomputed_signal=item["signal"],
                        precomputed_news_gate=news_gate,
                        precomputed_ticker=item["ticker"],
                        precomputed_spread_pct=item["spread_pct"],
                    )

        if (
            not block_short_entries
            and self.short_enabled()
            and not self.short_test_complete()
        ):
            for symbol in symbols:
                if (
                    self.short_positions_count()
                    >= self.max_open_short_positions()
                    or self.total_positions_count()
                    >= self.max_total_positions()
                ):
                    break

                self.try_open_paper_short(
                    symbol
                )

    def run_forever(self) -> None:
        full_cycle_s = max(
            30,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "loop_sleep_seconds",
                        300,
                    ),
                    300,
                )
            ),
        )

        position_check_s = max(
            5,
            int(
                to_float(
                    get_cfg(
                        self.cfg,
                        "execution.position_check_seconds",
                        20,
                    ),
                    20,
                )
            ),
        )

        LOG.info(
            "LOOP | volledige cyclus=%ss | "
            "open-positiecontrole=%ss",
            full_cycle_s,
            position_check_s,
        )

        next_full_cycle = 0.0

        while True:
            try:
                now = time.monotonic()

                if now >= next_full_cycle:
                    self.run_once()
                    next_full_cycle = (
                        time.monotonic()
                        + full_cycle_s
                    )

                elif self.state.get("positions"):
                    self.manage_open_positions_fast()

            except Exception as exc:
                LOG.exception(
                    "Hoofdloop fout: %s",
                    exc,
                )

            seconds_until_full = max(
                0.0,
                next_full_cycle - time.monotonic(),
            )

            if seconds_until_full <= 0:
                continue

            if self.state.get("positions"):
                sleep_s = min(
                    float(position_check_s),
                    seconds_until_full,
                )
            else:
                sleep_s = seconds_until_full

            time.sleep(
                max(1.0, sleep_s)
            )


def main() -> None:
    cfg_path = os.getenv("CFG_FILE", "config.yaml")
    cfg = load_yaml(cfg_path)
    setup_logging(str(get_cfg(cfg, "log_level", "INFO")))
    bot = Bot(cfg)
    LOG.info("Diamond Bot v6.8 gestart | dry_run=%s | state=%s | trades=%s | control=%s", bot.dry_run, bot.state_file, bot.trades_file, bot.control_file)
    bot.run_forever()


if __name__ == "__main__":
    main()