from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional

from models import Candle, Decision, Position


class Storage:
    def __init__(self, db_path: str, paper_start_eur: float) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self._init_schema()
        self._ensure_state('cash_eur', paper_start_eur)

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript('''
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
        CREATE TABLE IF NOT EXISTS decisions (
            market TEXT NOT NULL,
            timestamp_ms INTEGER NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            PRIMARY KEY (market, timestamp_ms)
        );
        CREATE TABLE IF NOT EXISTS positions (
            market TEXT PRIMARY KEY,
            opened_at_ms INTEGER NOT NULL,
            entry_candle_ts INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            amount REAL NOT NULL,
            entry_notional REAL NOT NULL,
            entry_fee REAL NOT NULL,
            stop_price REAL NOT NULL,
            take_price REAL NOT NULL,
            bars_held INTEGER NOT NULL
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
        ''')
        self.conn.commit()

    def _ensure_state(self, key: str, value: float | int | str) -> None:
        self.conn.execute('INSERT OR IGNORE INTO state(key,value) VALUES(?,?)', (key, str(value)))
        self.conn.commit()

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return default if row is None else str(row['value'])

    def set_state(self, key: str, value: float | int | str) -> None:
        self.conn.execute(
            'INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)),
        )
        self.conn.commit()

    def cash_eur(self) -> float:
        return float(self.get_state('cash_eur', '0') or 0)

    def universe(self) -> List[str]:
        raw = self.get_state('universe_json')
        if not raw:
            return []
        value = json.loads(raw)
        return [str(x) for x in value]

    def set_universe(self, markets: List[str]) -> None:
        if self.universe():
            raise RuntimeError('universe is al vastgezet')
        self.set_state('universe_json', json.dumps(markets))
        self.set_state('universe_selected_at_ms', __import__('time').time_ns() // 1_000_000)

    def last_processed(self, market: str) -> int:
        return int(self.get_state(f'last_candle:{market}', '0') or 0)

    def set_last_processed(self, market: str, timestamp_ms: int) -> None:
        self.set_state(f'last_candle:{market}', timestamp_ms)

    def save_candles(self, market: str, interval: str, candles: Iterable[Candle]) -> None:
        self.conn.executemany(
            'INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?)',
            [(market, interval, c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume) for c in candles],
        )
        self.conn.commit()

    def save_decision(self, market: str, timestamp_ms: int, decision: Decision) -> None:
        self.conn.execute(
            '''INSERT INTO decisions(market,timestamp_ms,action,reason,metrics_json)
               VALUES(?,?,?,?,?)
               ON CONFLICT(market,timestamp_ms) DO UPDATE SET
                 action=excluded.action, reason=excluded.reason, metrics_json=excluded.metrics_json''',
            (market, timestamp_ms, decision.action, decision.reason, json.dumps(decision.metrics, sort_keys=True)),
        )
        self.conn.commit()

    def decision_reason_counts(self) -> List[tuple[str, str, int]]:
        rows = self.conn.execute(
            'SELECT action, reason, COUNT(*) n FROM decisions GROUP BY action,reason ORDER BY n DESC'
        ).fetchall()
        return [(str(r['action']), str(r['reason']), int(r['n'])) for r in rows]

    def get_position(self, market: str) -> Optional[Position]:
        row = self.conn.execute('SELECT * FROM positions WHERE market=?', (market,)).fetchone()
        return None if row is None else self._row_to_position(row)

    def all_positions(self) -> List[Position]:
        rows = self.conn.execute('SELECT * FROM positions ORDER BY opened_at_ms').fetchall()
        return [self._row_to_position(row) for row in rows]

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            market=str(row['market']), opened_at_ms=int(row['opened_at_ms']),
            entry_candle_ts=int(row['entry_candle_ts']), entry_price=float(row['entry_price']),
            amount=float(row['amount']), entry_notional=float(row['entry_notional']),
            entry_fee=float(row['entry_fee']), stop_price=float(row['stop_price']),
            take_price=float(row['take_price']), bars_held=int(row['bars_held']),
        )

    def update_position(self, p: Position) -> None:
        self.conn.execute('UPDATE positions SET bars_held=? WHERE market=?', (p.bars_held, p.market))
        self.conn.commit()

    def open_position_atomic(self, p: Position, total_cost: float) -> None:
        with self.conn:
            row = self.conn.execute("SELECT value FROM state WHERE key='cash_eur'").fetchone()
            cash = 0.0 if row is None else float(row['value'])
            if cash + 1e-9 < total_cost:
                raise RuntimeError('onvoldoende paper cash')
            if self.conn.execute('SELECT 1 FROM positions WHERE market=?', (p.market,)).fetchone():
                raise RuntimeError('positie bestaat al')
            self.conn.execute(
                "INSERT INTO state(key,value) VALUES('cash_eur',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f'{cash-total_cost:.12f}',),
            )
            self.conn.execute(
                'INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (p.market,p.opened_at_ms,p.entry_candle_ts,p.entry_price,p.amount,p.entry_notional,
                 p.entry_fee,p.stop_price,p.take_price,p.bars_held),
            )

    def close_position_atomic(self, p: Position, closed_at_ms: int, exit_price: float,
                              exit_fee: float, net_exit: float, pnl_eur: float,
                              pnl_pct: float, reason: str) -> None:
        with self.conn:
            row = self.conn.execute("SELECT value FROM state WHERE key='cash_eur'").fetchone()
            cash = 0.0 if row is None else float(row['value'])
            self.conn.execute(
                "INSERT INTO state(key,value) VALUES('cash_eur',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f'{cash+net_exit:.12f}',),
            )
            self.conn.execute(
                '''INSERT INTO trades(market,opened_at_ms,closed_at_ms,entry_price,exit_price,amount,
                                      entry_fee,exit_fee,pnl_eur,pnl_pct,exit_reason)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (p.market,p.opened_at_ms,closed_at_ms,p.entry_price,exit_price,p.amount,p.entry_fee,
                 exit_fee,pnl_eur,pnl_pct,reason),
            )
            self.conn.execute('DELETE FROM positions WHERE market=?', (p.market,))

    def trade_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute('SELECT * FROM trades ORDER BY closed_at_ms,id').fetchall()
