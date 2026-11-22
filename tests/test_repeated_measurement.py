"""Repository discovery shim for the self-contained skill contract suite."""
import importlib.util
from pathlib import Path
CASE = Path(__file__).resolve().parents[1] / 'Plugins/code-optimizer/skills/verify-optimization/cases/repeated-measurement-contract/test_repeated_measurement.py'
spec = importlib.util.spec_from_file_location('repeated_measurement_contract', CASE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
RepeatedMeasurementTests = module.RepeatedMeasurementTests
