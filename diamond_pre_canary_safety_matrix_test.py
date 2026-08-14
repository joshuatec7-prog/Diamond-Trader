import sys, types, importlib.util, tempfile, json, os
from pathlib import Path

# Minimal ccxt stub so the production module can be imported offline.
ccxt = types.ModuleType('ccxt')
class OrderNotFound(Exception): pass
class Exchange: pass
ccxt.OrderNotFound = OrderNotFound
ccxt.Exchange = Exchange
ccxt.bitvavo = lambda *a, **k: None
sys.modules['ccxt'] = ccxt

MODULE_PATH = '/mnt/data/punt3/diamond_bot.py'
spec = importlib.util.spec_from_file_location('diamond_bot_matrix', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.time.sleep = lambda *_: None

class FakeNews:
    def buy_gate(self, symbol): return {'allow': True, 'reason': 'test'}
    def coin_news(self, symbol): return {'news_score': 0.0}
    def fear_greed(self): return {'value': 50}

class FakeExchange:
    def __init__(self):
        self.create_calls = []
        self.fetch_calls = []
        self.orders = {}
        self.fetch_error = None
    def create_order(self, symbol, typ, side, amount, price, params):
        self.create_calls.append((symbol, typ, side, amount, price, dict(params or {})))
        cid = (params or {}).get('clientOrderId')
        order = {
            'id': f'ex-{len(self.create_calls)}', 'symbol': symbol, 'status': 'closed',
            'side': side, 'filled': amount, 'amount': amount,
            'average': 100.0 if side == 'buy' else 110.0,
            'price': 100.0 if side == 'buy' else 110.0,
            'cost': amount * (100.0 if side == 'buy' else 110.0),
            'clientOrderId': cid,
            'fee': {'cost': amount * (100.0 if side == 'buy' else 110.0) * 0.0025, 'currency': 'EUR'},
            'info': {'clientOrderId': cid},
        }
        if cid:
            self.orders[cid] = order
        return order
    def fetch_order(self, order_id, symbol=None, params=None):
        self.fetch_calls.append((order_id, symbol, dict(params or {})))
        if self.fetch_error:
            raise self.fetch_error
        cid = (params or {}).get('clientOrderId') or order_id
        if cid not in self.orders:
            raise OrderNotFound(cid)
        return dict(self.orders[cid])


def mkbot(root, exchange=None, state=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    b = mod.Bot.__new__(mod.Bot)
    b.cfg = {
        'quote': 'EUR',
        'risk': {
            'dry_run': False,
            'fixed_stake_quote': 35,
            'max_open_positions': 5,
            'eur_reserve': 250,
            'taker_fee_pct': 0.25,
            'avoid_symbols_with_existing_balance': False,
        },
        'trading': {'enable_spot': True, 'max_total_positions': 5, 'allow_long_and_short_same_symbol': False},
        'signals': {},
        'news': {},
        'fees': {'taker_fee_pct': 0.25},
    }
    b.quote = 'EUR'
    b.dry_run = False
    b.state_file = str(root/'state.json')
    b.trades_file = str(root/'transactions.csv')
    b.canary_execution_file = str(root/'canary.csv')
    b.control_file = str(root/'control.json')
    b.short_test_baseline_file = str(root/'short_base.json')
    b.short_test_report_file = str(root/'short_report.json')
    b.short_execution_file = str(root/'short_exec.csv')
    b.short_test_archive_dir = str(root/'short_archive')
    b.state = state if state is not None else mod.default_state()
    b.last_status_log_ts = 0.0
    b.last_hold_log_ts = {}
    b.last_skip_log_ts = {}
    b.balance_cache = {'free': {'EUR': 1000.0, 'BTC': 1.0}, 'total': {'EUR': 1000.0, 'BTC': 1.0}}
    b.api_key = 'key'
    b.api_secret = 'secret'
    b.operator_id = ''
    b.exchange = exchange or FakeExchange()
    b.news = FakeNews()
    b.short_strategy_baseline_mismatch = False
    # eliminate exchange balance/network paths
    b.refresh_balance_cache = lambda: None
    b.get_ticker = lambda symbol: {'bid': 110.0, 'ask': 100.0, 'last': 105.0}
    b.estimate_spread_pct = lambda ticker: 0.05
    b.market_min_notional = lambda symbol: 5.0
    b.amount_to_precision_safe = lambda symbol, amount: float(amount)
    b.asset_balance = lambda asset: {'EUR':1000.0, 'BTC':1.0}.get(str(asset).upper(),0.0)
    b.rate_limited_info = lambda *a, **k: None
    mod.save_state(b.state_file, b.state)
    return b

signal = {'signal_candle_ts':'2026-08-14T06:00:00Z','close':100.0,'stop_loss':95.0,'take_profit':110.0,'tech_score':2.0,'rsi':60.0,'atr_pct':1.0}

results=[]
def test(name, fn):
    try:
        detail=fn()
        results.append((name, True, detail or 'OK'))
    except Exception as e:
        results.append((name, False, f'{type(e).__name__}: {e}'))

# 1 save fails before submit => no create_order

def t1():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        real_save=mod.save_state
        calls={'n':0}
        def fail_save(path,state):
            calls['n']+=1
            raise OSError('simulated disk failure before submit')
        mod.save_state=fail_save
        try:
            b.try_buy_symbol('BTC/EUR', precomputed_signal=signal, precomputed_news_gate={'allow':True}, precomputed_ticker={'bid':99.9,'ask':100.0}, precomputed_spread_pct=0.05)
        finally:
            mod.save_state=real_save
        assert len(ex.create_calls)==0, ex.create_calls
        return 'state-save fout vóór submit -> 0 exchange orders'

test('1. Crash/state-save fout vóór BUY submit', t1)

# 2 PREPARED after crash before submit clears safely without exchange call

def t2():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        key=b.long_order_key('BTC/EUR', signal)
        b.prepare_pending_long_order(key,'BTC/EUR',signal,35,0.05,0.0)
        # restart
        b2=mkbot(td, ex, mod.load_state(b.state_file))
        b2.reconcile_pending_orders()
        assert not b2.state['pending_orders']
        assert not b2.state['recovery_required']
        assert len(ex.fetch_calls)==0
        return 'PREPARED wordt veilig verwijderd; geen exchange lookup/resend'

test('2. Crash na pending-save, vóór BUY submit', t2)

# 3 Accepted BUY, response lost: SUBMITTING + exchange closed fill recovers position

def t3():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        key=b.long_order_key('BTC/EUR', signal)
        rec=b.prepare_pending_long_order(key,'BTC/EUR',signal,35,0.05,0.0)
        rec['reference_ask']=100.0; rec['execution_spread_pct']=0.05; mod.save_state(b.state_file,b.state)
        b.mark_pending_submitting(key)
        cid=rec['clientOrderId']
        ex.orders[cid]={'id':'buy-accepted','status':'closed','filled':0.35,'average':100.1,'cost':35.035,'clientOrderId':cid,'fee':{'cost':0.0875875,'currency':'EUR'},'info':{'clientOrderId':cid}}
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        pos=b2.state['positions'].get('BTC/EUR')
        assert pos and pos.get('recovered_from_pending') is True
        assert not b2.state['pending_orders'] and not b2.state['recovery_required']
        assert len(ex.create_calls)==0
        return 'order via clientOrderId gevonden; positie hersteld; geen resend'

test('3. BUY geaccepteerd, response kwijt', t3)

# 4 fill confirmed persisted, crash before position save

def t4():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        key=b.long_order_key('BTC/EUR', signal)
        rec=b.prepare_pending_long_order(key,'BTC/EUR',signal,35,0.05,0.0)
        rec['reference_ask']=100.0; rec['execution_spread_pct']=0.05; mod.save_state(b.state_file,b.state)
        b.mark_pending_submitting(key)
        cid=rec['clientOrderId']
        order={'id':'buy-filled','status':'closed','filled':0.35,'average':100.2,'cost':35.07,'clientOrderId':cid,'fee':{'cost':0.087675,'currency':'EUR'},'info':{'clientOrderId':cid}}
        ex.orders[cid]=order
        b.update_pending_from_order(key, order, status='FILLED_CONFIRMED')
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        pos=b2.state['positions'].get('BTC/EUR')
        assert pos and abs(pos['amount']-0.35)<1e-12
        assert not b2.state['pending_orders']
        return 'FILLED_CONFIRMED pending reconstrueert positie na restart'

test('4. BUY fill gebeurd, crash vóór position-save', t4)

# 5 open order remains pending and blocks entries

def t5():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        key=b.long_order_key('BTC/EUR', signal); rec=b.prepare_pending_long_order(key,'BTC/EUR',signal,35,0.05,0.0); b.mark_pending_submitting(key)
        cid=rec['clientOrderId']; ex.orders[cid]={'id':'buy-open','status':'open','filled':0.0,'amount':0.35,'clientOrderId':cid,'info':{'clientOrderId':cid}}
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        assert key in b2.state['pending_orders'] and b2.state['recovery_required']
        before=len(ex.create_calls)
        b2.try_buy_symbol('ETH/EUR', precomputed_signal={**signal,'signal_candle_ts':'x2'}, precomputed_news_gate={'allow':True}, precomputed_ticker={'bid':99.9,'ask':100.0}, precomputed_spread_pct=0.05)
        assert len(ex.create_calls)==before
        return 'open order blijft pending; nieuwe entry geblokkeerd'

test('5. Open BUY order bij restart', t5)

# 6 exchange unavailable => recovery stays; entries blocked

def t6():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        key=b.long_order_key('BTC/EUR', signal); rec=b.prepare_pending_long_order(key,'BTC/EUR',signal,35,0.05,0.0); b.mark_pending_submitting(key)
        ex.fetch_error=RuntimeError('exchange down')
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        assert b2.state['recovery_required'] and key in b2.state['pending_orders']
        assert 'exchange_unavailable' in b2.state['recovery_reason']
        before=len(ex.create_calls)
        b2.try_buy_symbol('ETH/EUR', precomputed_signal={**signal,'signal_candle_ts':'x3'}, precomputed_news_gate={'allow':True}, precomputed_ticker={'bid':99.9,'ask':100.0}, precomputed_spread_pct=0.05)
        assert len(ex.create_calls)==before
        return 'exchange fout -> RECOVERY_REQUIRED; geen nieuwe BUY'

test('6. Exchange onbereikbaar tijdens recovery', t6)

# 7 unknown/non-bot position never sold

def t7():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        pos={'opened_by_bot':False,'amount':1.0,'opened_at':1.0,'quote_amount':100.0,'fees_buy_quote':0.0}
        b.try_sell_symbol('BTC/EUR',pos,'take_profit')
        assert len(ex.create_calls)==0
        return 'opened_by_bot=False -> 0 SELL orders'

test('7. Onbekende/niet-bot coin beschermen', t7)

# 8 state mismatch: pending SELL but bot position missing -> recovery required, no second sell

def t8():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        pos={'opened_by_bot':True,'opened_at':1000.0,'entry_price':100.0,'amount':0.35,'quote_amount':35.0,'fees_buy_quote':0.0875,'protected_base_amount':0.0,'canary_trade_number':1,'entry_reference_ask':100.0,'entry_slippage_pct':0.0,'entry_spread_pct':0.05}
        key=b.sell_order_key('BTC/EUR',pos,'take_profit',0.35); rec=b.prepare_pending_sell_order(key,'BTC/EUR',pos,'take_profit',0.35); b.mark_pending_submitting(key)
        cid=rec['clientOrderId']; ex.orders[cid]={'id':'sell-filled','status':'closed','filled':0.35,'average':110.0,'cost':38.5,'clientOrderId':cid,'fee':{'cost':0.09625,'currency':'EUR'},'info':{'clientOrderId':cid}}
        # intentionally no position in state
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        assert b2.state['recovery_required'] and key in b2.state['pending_orders']
        assert 'sell_position_missing' in b2.state['recovery_reason']
        return 'SELL fill + ontbrekende lokale positie -> recovery blijft geblokkeerd'

test('8. SELL state/position mismatch', t8)

# 9 duplicate pending BUY is never sent again

def t9():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        key=b.long_order_key('BTC/EUR', signal); b.prepare_pending_long_order(key,'BTC/EUR',signal,35,0.05,0.0)
        b.try_buy_symbol('BTC/EUR', precomputed_signal=signal, precomputed_news_gate={'allow':True}, precomputed_ticker={'bid':99.9,'ask':100.0}, precomputed_spread_pct=0.05)
        assert len(ex.create_calls)==0
        assert key in b.state['pending_orders']
        assert b.entries_blocked_by_recovery()
        return 'zelfde BUY key pending -> 0 resend; pending zelf blokkeert entries'

test('9. Duplicate BUY orderkey', t9)

# 10 completed SELL recovery applies exactly once

def t10():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        pos={'opened_by_bot':True,'opened_at':1000.0,'entry_price':100.0,'amount':0.35,'quote_amount':35.0,'fees_buy_quote':0.0875,'protected_base_amount':0.0,'canary_trade_number':1,'entry_reference_ask':100.0,'entry_slippage_pct':0.0,'entry_spread_pct':0.05,'exchange_order_id':'buy1','client_order_id':'cidbuy'}
        b.state['positions']['BTC/EUR']=pos; mod.save_state(b.state_file,b.state)
        key=b.sell_order_key('BTC/EUR',pos,'take_profit',0.35); rec=b.prepare_pending_sell_order(key,'BTC/EUR',pos,'take_profit',0.35); rec['reference_bid']=110.0; rec['execution_spread_pct']=0.05; mod.save_state(b.state_file,b.state); b.mark_pending_submitting(key)
        cid=rec['clientOrderId']; order={'id':'sell1','status':'closed','filled':0.35,'average':109.9,'cost':38.465,'clientOrderId':cid,'fee':{'cost':0.0961625,'currency':'EUR'},'info':{'clientOrderId':cid}}
        ex.orders[cid]=order
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        trades1=b2.state['trades']; pnl1=b2.state['pnl_quote']
        assert trades1==1 and 'BTC/EUR' not in b2.state['positions'] and not b2.state['pending_orders']
        b2.reconcile_pending_orders()
        assert b2.state['trades']==trades1 and b2.state['pnl_quote']==pnl1
        assert len(ex.create_calls)==0
        return 'SELL fill hersteld, positie gesloten, PnL/trade exact één keer geboekt'

test('10. SELL fill recovery exact-once', t10)

# 11 duplicate pending SELL blocks second create_order

def t11():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        pos={'opened_by_bot':True,'opened_at':1000.0,'entry_price':100.0,'amount':0.35,'quote_amount':35.0,'fees_buy_quote':0.0875,'protected_base_amount':0.0,'canary_trade_number':1}
        b.state['positions']['BTC/EUR']=pos; mod.save_state(b.state_file,b.state)
        key=b.sell_order_key('BTC/EUR',pos,'take_profit',0.35); b.prepare_pending_sell_order(key,'BTC/EUR',pos,'take_profit',0.35)
        b.try_sell_symbol('BTC/EUR',pos,'take_profit')
        assert len(ex.create_calls)==0 and b.state['recovery_required']
        return 'bestaande pending SELL -> tweede SELL geblokkeerd'

test('11. Duplicate SELL blokkering', t11)


# 12 crash after SELL pending save but before submit: PREPARED clears safely
def t12():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        pos={'opened_by_bot':True,'opened_at':1000.0,'entry_price':100.0,'amount':0.35,'quote_amount':35.0,'fees_buy_quote':0.0875,'protected_base_amount':0.0,'canary_trade_number':1}
        b.state['positions']['BTC/EUR']=pos; mod.save_state(b.state_file,b.state)
        key=b.sell_order_key('BTC/EUR',pos,'take_profit',0.35)
        b.prepare_pending_sell_order(key,'BTC/EUR',pos,'take_profit',0.35)
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        assert key not in b2.state['pending_orders'] and not b2.state['recovery_required']
        assert len(ex.fetch_calls)==0 and len(ex.create_calls)==0
        assert 'BTC/EUR' in b2.state['positions']
        return 'SELL PREPARED veilig verwijderd; positie blijft; geen exchange-call'
test('12. Crash na pending-save, vóór SELL submit', t12)

# 13 SELL accepted but response lost: recover by clientOrderId, exact once
def t13():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        pos={'opened_by_bot':True,'opened_at':1000.0,'entry_price':100.0,'amount':0.35,'quote_amount':35.0,'fees_buy_quote':0.0875,'protected_base_amount':0.0,'canary_trade_number':1,'entry_reference_ask':100.0,'entry_slippage_pct':0.0,'entry_spread_pct':0.05}
        b.state['positions']['BTC/EUR']=pos; mod.save_state(b.state_file,b.state)
        key=b.sell_order_key('BTC/EUR',pos,'take_profit',0.35); rec=b.prepare_pending_sell_order(key,'BTC/EUR',pos,'take_profit',0.35)
        rec['reference_bid']=110.0; rec['execution_spread_pct']=0.05; mod.save_state(b.state_file,b.state); b.mark_pending_submitting(key)
        cid=rec['clientOrderId']; ex.orders[cid]={'id':'sell-lost-response','status':'closed','filled':0.35,'average':109.95,'cost':38.4825,'clientOrderId':cid,'fee':{'cost':0.09620625,'currency':'EUR'},'info':{'clientOrderId':cid}}
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        assert 'BTC/EUR' not in b2.state['positions'] and not b2.state['pending_orders'] and not b2.state['recovery_required']
        assert b2.state['trades']==1 and len(ex.create_calls)==0
        return 'SELL via clientOrderId hersteld; geen tweede SELL'
test('13. SELL geaccepteerd, response kwijt', t13)

# 14 protected/manual balance cannot be sold
def t14():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        pos={'opened_by_bot':True,'opened_at':1000.0,'entry_price':100.0,'amount':0.35,'quote_amount':35.0,'fees_buy_quote':0.0875,'protected_base_amount':1.0,'canary_trade_number':1}
        b.state['positions']['BTC/EUR']=pos; mod.save_state(b.state_file,b.state)
        # asset_balance BTC is 1.0, exactly protected; bot-owned_available = 0
        b.try_sell_symbol('BTC/EUR',pos,'take_profit')
        assert len(ex.create_calls)==0 and not b.state['pending_orders']
        assert 'BTC/EUR' in b.state['positions']
        return 'vrij saldo == beschermd saldo -> 0 SELL; handmatig bezit beschermd'
test('14. Protected balance / onbekend bezit', t14)

# 15 open SELL remains pending and blocks another SELL
def t15():
    with tempfile.TemporaryDirectory() as td:
        ex=FakeExchange(); b=mkbot(td, ex)
        pos={'opened_by_bot':True,'opened_at':1000.0,'entry_price':100.0,'amount':0.35,'quote_amount':35.0,'fees_buy_quote':0.0875,'protected_base_amount':0.0,'canary_trade_number':1}
        b.state['positions']['BTC/EUR']=pos; mod.save_state(b.state_file,b.state)
        key=b.sell_order_key('BTC/EUR',pos,'take_profit',0.35); rec=b.prepare_pending_sell_order(key,'BTC/EUR',pos,'take_profit',0.35); b.mark_pending_submitting(key)
        cid=rec['clientOrderId']; ex.orders[cid]={'id':'sell-open','status':'open','filled':0.1,'amount':0.35,'average':110.0,'cost':11.0,'clientOrderId':cid,'info':{'clientOrderId':cid}}
        b2=mkbot(td, ex, mod.load_state(b.state_file)); b2.reconcile_pending_orders()
        assert key in b2.state['pending_orders'] and b2.state['recovery_required']
        before=len(ex.create_calls); b2.try_sell_symbol('BTC/EUR',b2.state['positions']['BTC/EUR'],'take_profit')
        assert len(ex.create_calls)==before
        return 'open/partiële SELL blijft pending; tweede SELL geblokkeerd'
test('15. Open/partiële SELL bij restart', t15)

# 16 stable clientOrderId for same order key
def t16():
    with tempfile.TemporaryDirectory() as td:
        b=mkbot(td, FakeExchange())
        key='LONG|BTC/EUR|stable-test'
        a=b.client_order_id_for_key(key); c=b.client_order_id_for_key(key)
        assert a==c and len(a)>10
        return f'stabiele clientOrderId {a}'
test('16. Stabiele order-identiteit', t16)

print('\n'.join(f"{'PASS' if ok else 'FAIL'} | {name} | {detail}" for name,ok,detail in results))
print(f"\nTOTAL {sum(ok for _,ok,_ in results)}/{len(results)} PASS")
if not all(ok for _,ok,_ in results):
    raise SystemExit(1)
