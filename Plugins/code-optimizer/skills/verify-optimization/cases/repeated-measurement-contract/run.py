#!/usr/bin/env python3
"""Run tier A contracts from repository or installed skill (standard library only)."""
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
suite = unittest.defaultTestLoader.discover(str(HERE), pattern='test_repeated_measurement.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
(HERE / 'results').mkdir(exist_ok=True)
(HERE / 'results/result.json').write_text(json.dumps({
    'success': result.wasSuccessful() and result.testsRun >= 10,
    'tests_run': result.testsRun,
    'failures': len(result.failures), 'errors': len(result.errors)
}, indent=2) + '\n', encoding='utf-8')
raise SystemExit(0 if result.wasSuccessful() and result.testsRun >= 10 else 1)
