import ast
import unittest
from pathlib import Path


class DailyReportPartsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'jalwe_research_watcher.py'
        tree = ast.parse(path.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'telegram_report_parts')
        ns = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
        cls.split = staticmethod(ns['telegram_report_parts'])

    def test_long_report_preserved_and_all_parts_fit_telegram_units(self):
        message = '🎯 الأسباب\n' * 1000
        parts = self.split(message)
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(parts), message)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 3800 for p in parts))

    def test_short_report_stays_one_message(self):
        self.assertEqual(self.split('تقرير اليوم'), ['تقرير اليوم'])
        self.assertEqual(self.split(''), [''])
