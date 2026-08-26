import unittest
from pathlib import Path


class TreeTests(unittest.TestCase):
    def test_only_expected_project_files_exist(self):
        root = Path(__file__).resolve().parents[1]
        allowed_top = {
            '.env.example','.gitignore','.python-version','.github','CLEANROOM.md','README.md',
            'backtest.py','bitvavo_public.py','config.py','main.py','models.py','paper_trader.py',
            'report.py','requirements.txt','start.sh','status.py','storage.py','strategy.py','tests'
        }
        actual = {p.name for p in root.iterdir() if p.name != '__pycache__'}
        self.assertEqual(actual, allowed_top)


if __name__ == '__main__':
    unittest.main()
