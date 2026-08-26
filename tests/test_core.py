import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bitvavo_public import BitvavoPublic, PermanentHTTPError
from config import Settings
from models import Book, Candle, Position
from paper_trader import PaperTrader
from report import performance, verdict
from storage import Storage
from strategy import BandReentryStrategy


class FakeResponse:
    def __init__(self, payload, status=200, text=''):
        self.payload = payload
        self.status_code = status
        self.headers = {}
        self.text = text
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))
    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = 0
    def get(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class CleanRoomTests(unittest.TestCase):
    def test_market_ranking_is_dynamic(self):
        market_payload = [
            {'market':'AAA-EUR','status':'trading','quote':'EUR'},
            {'market':'BBB-EUR','status':'trading','quote':'EUR'},
            {'market':'CCC-USDC','status':'trading','quote':'USDC'},
        ]
        ticker_payload = [
            {'market':'AAA-EUR','volumeQuote':'100'},
            {'market':'BBB-EUR','volumeQuote':'250'},
            {'market':'CCC-USDC','volumeQuote':'999'},
        ]
        api = BitvavoPublic('https://x', session=FakeSession([FakeResponse(market_payload), FakeResponse(ticker_payload)]))
        self.assertEqual(api.top_markets_by_quote_volume('EUR', 2), ['BBB-EUR','AAA-EUR'])

    def test_closed_candle_filter(self):
        payload = [[0,'10','11','9','10','2'],[3_600_000,'11','12','10','11','2']]
        api = BitvavoPublic('https://x', session=FakeSession([FakeResponse(payload)]))
        result = api.closed_candles('AAA-EUR','1h',10,now_ms=5_000_000)
        self.assertEqual([c.timestamp_ms for c in result],[0])

    def test_invalid_exchange_candle_is_discarded(self):
        payload = [[0,'10','9','11','10','2'],[3_600_000,'11','12','10','11','2']]
        api = BitvavoPublic('https://x', session=FakeSession([FakeResponse(payload)]))
        result = api.candles('AAA-EUR','1h',10)
        self.assertEqual([c.timestamp_ms for c in result],[3_600_000])

    def test_permanent_4xx_not_retried(self):
        sess = FakeSession([FakeResponse({},403)])
        api = BitvavoPublic('https://x', retries=3, session=sess)
        with self.assertRaises(PermanentHTTPError):
            api.trading_markets('EUR')
        self.assertEqual(sess.calls,1)

    def test_public_probe_reports_status_without_raising(self):
        sess = FakeSession([
            FakeResponse({},403,'blocked'),
            FakeResponse([],200,'[]'),
            FakeResponse([],200,'[]'),
        ])
        api = BitvavoPublic('https://x', session=sess)
        results = api.probe_public_endpoints()
        self.assertEqual([r['status'] for r in results], [403,200,200])
        self.assertEqual(sess.calls,3)

    def test_strategy_reentry(self):
        s = replace(Settings(), band_window=5, band_stddev=1.0)
        closes = [10,10,10,10,10,8,9]
        candles = [Candle(i*3_600_000,v,v+0.2,v-0.2,v,1) for i,v in enumerate(closes)]
        d = BandReentryStrategy(s).evaluate(candles)
        self.assertEqual(d.action,'BUY')

    def test_universe_is_immutable_after_first_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp)/'x.db'),5000)
            db.set_universe(['AAA-EUR','BBB-EUR'])
            with self.assertRaises(RuntimeError):
                db.set_universe(['CCC-EUR'])
            db.close()

    def test_position_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'x.db')
            db = Storage(path,5000)
            p = Position('AAA-EUR',1,1,100,2,200,0.5,98.5,102.5,0)
            db.open_position_atomic(p,200.5)
            db.close()
            db = Storage(path,5000)
            self.assertIsNotNone(db.get_position('AAA-EUR'))
            db.close()

    def test_stop_has_priority_if_both_hit_same_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp)/'x.db'), slippage_pct=0)
            db = Storage(s.db_path,s.paper_start_eur)
            trader = PaperTrader(s,db)
            trader.open_long('AAA-EUR',Book(99.9,100),1,now_ms=1)
            p = db.get_position('AAA-EUR')
            event = trader.process_candle('AAA-EUR',Candle(2,100,p.take_price+1,p.stop_price-1,100,1),now_ms=2)
            self.assertEqual(event.reason,'stop_loss')
            db.close()

    def test_evaluation_waits_for_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings()
            db = Storage(str(Path(tmp)/'x.db'),s.paper_start_eur)
            p = performance(db,s.paper_start_eur)
            result,_ = verdict(p,s)
            self.assertEqual(result,'WAIT')
            db.close()


if __name__ == '__main__':
    unittest.main()
