import unittest
from pathlib import Path


class TreeTests(unittest.TestCase):
    def test_only_expected_project_files_exist(self):
        root = Path(__file__).resolve().parents[1]
        allowed_top = {
            '.env.example','.gitignore','.python-version','.github','CLEANROOM.md','README.md',
            'adaptive_ls_main.py','adaptive_ls_strategy.py','adaptive_ls_trader.py',
            'adaptive_ls_strict_main.py','adaptive_ls_strict_strategy.py','adaptive_ls_strict_replay.py',
            'adaptive_trend_main.py','adaptive_trend_strategy.py','adaptive_trend_trader.py',
            'audit_all.py','auto_research_controller.py','auto_research_controller_d2.py','backtest.py','bitvavo_public.py','config.py',
            'continuation_main.py','continuation_strategy.py','continuation_v2_main.py',
            'continuation_v3_main.py','continuation_v4_main.py','continuation_v5_main.py',
            'continuation_v6_main.py','crypto_scanner.py','exit_capture_lab.py','exit_capture_1m_lab.py','low_frequency_lab.py','main.py','market_data.py','models.py','missed_trade_audit.py',
            'offline_check.py','paper_trader.py','profit_protect_trader.py','readiness.py',
            'regime_strategy_lab.py','report.py','requirements.txt','research_report_publisher.py','signal_excursion_lab.py','staged_runner_trader.py',
            'start.sh','status.py','storage.py','strategy.py','supervisor.py','trend_main.py',
            'trend_strategy.py','trend_v3_main.py','trend_v4_main.py','trend_v5_main.py',
            'trend_v6_main.py','trend_v7_main.py','tests'
        }
        actual = {p.name for p in root.iterdir() if p.name not in {'__pycache__', '.git', '.venv'}}
        self.assertEqual(actual, allowed_top)

    def test_no_private_trading_capability_in_runtime_code(self):
        root = Path(__file__).resolve().parents[1]
        prohibited = (
            'BITVAVO_API_KEY', 'BITVAVO_API_SECRET', '/order', 'place_order',
            'create_order', 'cancel_order', 'private_post', 'private_get',
        )
        hits = []
        for path in sorted(root.glob('*.py')):
            text = path.read_text(encoding='utf-8')
            for token in prohibited:
                if token.lower() in text.lower():
                    hits.append(f'{path.name}:{token}')
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
