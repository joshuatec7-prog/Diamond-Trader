import unittest
from pathlib import Path


class TreeTests(unittest.TestCase):
    def test_only_expected_project_files_exist(self):
        root = Path(__file__).resolve().parents[1]
        allowed_top = {
            '.env.example','.gitignore','.python-version','.github','CLEANROOM.md','README.md',
            'backtest.py','bitvavo_public.py','config.py','main.py','market_data.py','models.py',
            'offline_check.py','paper_trader.py','readiness.py','report.py','requirements.txt',
            'start.sh','status.py','storage.py','strategy.py','tests'
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
