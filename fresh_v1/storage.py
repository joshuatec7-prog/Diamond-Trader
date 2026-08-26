from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from models import Candle, Position, Signal


class Storage:
    def __init__(self, db_path: str, paper_start_eur: float) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        self._ensure_state("cash_eur", paper_start_eur)

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candles (
                market TEXT NOT NULL,
                interval TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY (market, interval, timestamp_ms)
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                UNIQUE (market, timestamp_ms)
            );

            CREATE TABLE IF NOT EXISTS positions (
                market TEXT PRIMARY KEY,
                opened_at_ms INTEGER NOT NULL,
                entry_candle_ts INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                amount REAL NOT NULL,
                entry_notional REAL NOT NULL,
                entry_fee REAL NOT NULL,
                atr_at_entry REAL NOT NULL,
                stop_price REAL NOT NULL,
                take_price REAL NOT NULL,
                highest_price REAL NOT NULL,
                trailing_active INTEGER NOT NULL,
                trailing_stop REAL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                opened_at_ms INTEGER NOT NULL,
                closed_at_ms INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                amount REAL NOT NULL,
                entry_fee REAL NOT NULL,
                exit_fee REAL NOT NULL,
                pnl_eur REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                exit_reason TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def _ensure_state(self, key: str, value: float | int | str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO state(key, value) VALUES(?, ?)", (key, str(value)))
        self.conn.commit()

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def set_state(self, key: str, value: float | int | str) -> None:
        self.conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def cash_eur(self) -> float:
        return float(self.get_state("cash_eur", "0") or "0")

    def set_cash_eur(self, amount: float) -> None:
        self.set_state("cash_eur", f"{amount:.12f}")

    def open_position_atomic(self, p: Position, total_cost: float) -> None:
        with self.conn:
            row = self.conn.execute("SELECT value FROM state WHERE key='cash_eur'").fetchone()
            cash = 0.0 if row is None else float(row["value"])
            if cash + 1e-9 < total_cost:
                raise RuntimeError("Onvoldoende paper cash tijdens atomic open")
            new_cash = cash - total_cost
            self.conn.execute(
                "INSERT INTO state(key, value) VALUES('cash_eur', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"{new_cash:.12f}",),
            )
            self.conn.execute(
                """INSERT INTO positions (
                    market, opened_at_ms, entry_candle_ts, entry_price, amount,
                    entry_notional, entry_fee, atr_at_entry, stop_price, take_price,
                    highest_price, trailing_active, trailing_stop
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (p.market, p.opened_at_ms, p.entry_candle_ts, p.entry_price, p.amount,
                 p.entry_notional, p.entry_fee, p.atr_at_entry, p.stop_price, p.take_price,
                 p.highest_price, int(p.trailing_active), p.trailing_stop),
            )

    def close_position_atomic(self, p: Position, closed_at_ms: int, exit_price: float, exit_fee: float,
                              net_exit: float, pnl_eur: float, pnl_pct: float, exit_reason: str) -> None:
        with self.conn:
            row = self.conn.execute("SELECT value FROM state WHERE key='cash_eur'").fetchone()
            cash = 0.0 if row is None else float(row["value"])
            new_cash = cash + net_exit
            self.conn.execute(
                "INSERT INTO state(key, value) VALUES('cash_eur', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"{new_cash:.12f}",),
            )
            self.conn.execute(
                """INSERT INTO trades (
                    market, opened_at_ms, closed_at_ms, entry_price, exit_price,
                    amount, entry_fee, exit_fee, pnl_eur, pnl_pct, exit_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (p.market, p.opened_at_ms, closed_at_ms, p.entry_price, exit_price,
                 p.amount, p.entry_fee, exit_fee, pnl_eur, pnl_pct, exit_reason),
            )
            self.conn.execute("DELETE FROM positions WHERE market=?", (p.market,))

    def last_processed_candle(self, market: str) -> int:
        return int(self.get_state(f"last_candle:{market}", "0") or "0")

    def set_last_processed_candle(self, market: str, timestamp_ms: int) -> None:
        self.set_state(f"last_candle:{market}", timestamp_ms)

    def save_candles(self, market: str, interval: str, candles: Iterable[Candle]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO candles (market, interval, timestamp_ms, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(market, interval, c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume) for c in candles],
        )
        self.conn.commit()

    def save_signal(self, market: str, timestamp_ms: int, signal: Signal) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO signals (market, timestamp_ms, action, reason, metrics_json) VALUES (?, ?, ?, ?, ?)",
            (market, timestamp_ms, signal.action, signal.reason, json.dumps(signal.metrics, sort_keys=True)),
        )
        self.conn.commit()

    def get_position(self, market: str) -> Optional[Position]:
        row = self.conn.execute("SELECT * FROM positions WHERE market=?", (market,)).fetchone()
        return None if row is None else self._row_to_position(row)

    def all_positions(self) -> List[Position]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY opened_at_ms").fetchall()
        return [self._row_to_position(row) for row in rows]

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            market=row["market"], opened_at_ms=int(row["opened_at_ms"]), entry_candle_ts=int(row["entry_candle_ts"]),
            entry_price=float(row["entry_price"]), amount=float(row["amount"]), entry_notional=float(row["entry_notional"]),
            entry_fee=float(row["entry_fee"]), atr_at_entry=float(row["atr_at_entry"]), stop_price=float(row["stop_price"]),
            take_price=float(row["take_price"]), highest_price=float(row["highest_price"]),
            trailing_active=bool(row["trailing_active"]),
            trailing_stop=None if row["trailing_stop"] is None else float(row["trailing_stop"]),
        )

    def upsert_position(self, p: Position) -> None:
        self.conn.execute(
            """INSERT INTO positions (
                market, opened_at_ms, entry_candle_ts, entry_price, amount,
                entry_notional, entry_fee, atr_at_entry, stop_price, take_price,
                highest_price, trailing_active, trailing_stop
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market) DO UPDATE SET
                opened_at_ms=excluded.opened_at_ms, entry_candle_ts=excluded.entry_candle_ts,
                entry_price=excluded.entry_price, amount=excluded.amount, entry_notional=excluded.entry_notional,
                entry_fee=excluded.entry_fee, atr_at_entry=excluded.atr_at_entry, stop_price=excluded.stop_price,
                take_price=excluded.take_price, highest_price=excluded.highest_price,
                trailing_active=excluded.trailing_active, trailing_stop=excluded.trailing_stop""",
            (p.market, p.opened_at_ms, p.entry_candle_ts, p.entry_price, p.amount,
             p.entry_notional, p.entry_fee, p.atr_at_entry, p.stop_price, p.take_price,
             p.highest_price, int(p.trailing_active), p.trailing_stop),
        )
        self.conn.commit()

    def summary(self) -> Dict[str, float]:
        row = self.conn.execute(
            """SELECT COUNT(*) AS trades,
                COALESCE(SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN pnl_eur < 0 THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(pnl_eur), 0) AS pnl,
                COALESCE(SUM(CASE WHEN pnl_eur > 0 THEN pnl_eur ELSE 0 END), 0) AS gross_profit,
                ABS(COALESCE(SUM(CASE WHEN pnl_eur < 0 THEN pnl_eur ELSE 0 END), 0)) AS gross_loss
            FROM trades"""
        ).fetchone()
        gross_loss = float(row["gross_loss"])
        gross_profit = float(row["gross_profit"])
        pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        return {
            "cash_eur": self.cash_eur(), "open_positions": float(len(self.all_positions())),
            "trades": float(row["trades"]), "wins": float(row["wins"]), "losses": float(row["losses"]),
            "pnl_eur": float(row["pnl"]), "profit_factor": pf,
        }
