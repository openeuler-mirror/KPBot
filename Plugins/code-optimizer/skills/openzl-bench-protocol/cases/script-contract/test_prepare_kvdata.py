#!/usr/bin/env python3
"""Tier-A unit tests for prepare_kvdata.py (stdlib only; numpy parts optional).

torch is stubbed so the tests run without a torch install; numpy-dependent
tests (distribution stats, chunked histogram) are skipped when numpy is
missing. Run via run.sh, or directly:

    python3 test_prepare_kvdata.py
"""

import importlib.util
import math
import re
from pathlib import Path
import subprocess
import sys
import types
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prepare_kvdata.py"

# ---- torch stub (module-level imports in the script are deferred) ----
torch = types.ModuleType("torch")


class _FakeDtype:
    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return self._name


torch.bfloat16 = _FakeDtype("torch.bfloat16")
torch.uint8 = _FakeDtype("torch.uint8")
torch.int16 = _FakeDtype("torch.int16")
sys.modules.setdefault("torch", torch)


def load_module():
    spec = importlib.util.spec_from_file_location("prepare_kvdata", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTensor:
    def __init__(self, dtype_name):
        self.dtype = _FakeDtype(dtype_name)


try:
    import numpy  # noqa: F401
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


class DetectDtypeTest(unittest.TestCase):
    def setUp(self):
        self.pk = load_module()

    def test_detect_all_formats(self):
        self.assertEqual(self.pk.detect_dtype(FakeTensor("torch.bfloat16")), "bf16")
        self.assertEqual(self.pk.detect_dtype(FakeTensor("torch.float8_e4m3fn")), "e4m3")
        self.assertEqual(self.pk.detect_dtype(FakeTensor("torch.float8_e5m2")), "e5m2")

    def test_detect_unsupported_exits(self):
        with self.assertRaises(SystemExit):
            self.pk.detect_dtype(FakeTensor("torch.float32"))

    def test_exp_layout_matches_ieee_bit_positions(self):
        # bf16: [sign:1][exp:8][frac:7] -> exp at bit 7, width 8
        # e4m3: [sign:1][exp:4][frac:3] -> exp at bit 3, width 4
        # e5m2: [sign:1][exp:5][frac:2] -> exp at bit 2, width 5
        self.assertEqual(self.pk.EXP_LAYOUT,
                         {"bf16": (7, 8), "e4m3": (3, 4), "e5m2": (2, 5)})


class EntropyTest(unittest.TestCase):
    def setUp(self):
        self.pk = load_module()

    def test_uniform_16_symbols_is_4_bits(self):
        self.assertAlmostEqual(self.pk.entropy([100] * 16), 4.0, places=9)

    def test_skewed_and_zero_counts(self):
        counts = [9996, 4] + [0] * 254
        self.assertLess(self.pk.entropy(counts), 0.02)

    def test_single_symbol_is_zero_bits(self):
        self.assertEqual(self.pk.entropy([42, 0, 0]), 0.0)


class CliContractTest(unittest.TestCase):
    def test_help_exits_zero(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--n-true", result.stdout)

    def test_bad_input_exits_nonzero(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "/nonexistent/missing.pt"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


class NoPythonObjectMaterializationTest(unittest.TestCase):
    """Regression guard: distribution stats must not materialize per-element
    Python objects (.tolist()/Counter/zip over the full stream)."""

    def test_no_tolist_in_stats(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(".tolist()", source,
                         "stats must use chunked bincount, not .tolist()")
        self.assertNotIn("Counter(", source)


class LoadInputSecurityTest(unittest.TestCase):
    """P2 regression guard: .pt deserialization must be pinned to the
    restricted weights_only unpickler and refuse torch < 2.6 (whose default
    weights_only=False allows pickle-based arbitrary code execution)."""

    def setUp(self):
        self.pk = load_module()
        self.calls = []
        self._saved_version = torch.__dict__.get("__version__")
        self._saved_load = torch.__dict__.get("load")

    def tearDown(self):
        for name, value in (("__version__", self._saved_version),
                            ("load", self._saved_load)):
            if value is None:
                torch.__dict__.pop(name, None)
            else:
                setattr(torch, name, value)

    def fake_load(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return FakeTensor("torch.bfloat16")

    def test_torch_below_2_6_exits_without_loading(self):
        torch.__version__ = "2.5.1"
        torch.load = self.fake_load
        with self.assertRaises(SystemExit) as ctx:
            self.pk.load_input("kv.pt")
        self.assertIn("2.6", str(ctx.exception))
        self.assertEqual(self.calls, [], "must not torch.load on old versions")

    def test_torch_2_6_loads_with_weights_only(self):
        torch.__version__ = "2.6.0"
        torch.load = self.fake_load
        self.pk.load_input("kv.pt")
        self.assertEqual(self.calls,
                         [("kv.pt", {"map_location": "cpu", "weights_only": True})])

    def test_version_with_local_suffix_parses(self):
        torch.__version__ = "2.7.0+cu126"
        torch.load = self.fake_load
        self.pk.load_input("kv.pt")
        self.assertEqual(len(self.calls), 1)

    def test_source_has_no_unpinned_torch_load(self):
        for line in SCRIPT.read_text(encoding="utf-8").splitlines():
            if "torch.load(" in line:
                self.assertIn("weights_only=True", line,
                              f"unpinned torch.load: {line.strip()}")


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class StatsNumericsTest(unittest.TestCase):
    """Validate distribution stats against a straightforward reference
    implementation (collections.Counter over pairs) on small synthetic data."""

    def setUp(self):
        self.pk = load_module()
        import numpy as np
        self.np = np

    def make_exp(self, dtype, n=5000, seed=0):
        _, width = self.pk.EXP_LAYOUT[dtype]
        rng = self.np.random.default_rng(seed)
        n_sym = 1 << width
        symbols = self.np.arange(n_sym, dtype=self.np.uint16)
        base = self.np.array([.30, .18, .12, .09, .07, .06, .05, .04,
                              .03, .02, .015, .01, .008, .006, .006, .005])
        probs = self.np.full(n_sym, 1e-4)
        probs[: min(len(base), n_sym)] = base[: min(len(base), n_sym)]
        probs = probs / probs.sum()
        return rng.choice(symbols, size=n, p=probs).astype(self.np.uint16)

    def reference_counts(self, exp):
        from collections import Counter
        marginal = Counter(exp.tolist())
        joint = Counter(zip(exp[:-1].tolist(), exp[1:].tolist()))
        return marginal, joint

    def test_marginal_matches_reference(self):
        for dtype in ("bf16", "e4m3", "e5m2"):
            exp = self.make_exp(dtype)
            counts = self.pk._bincount_chunked(exp, 1 << self.pk.EXP_LAYOUT[dtype][1])
            marginal, _ = self.reference_counts(exp)
            for symbol, expected in marginal.items():
                self.assertEqual(int(counts[symbol]), expected, f"{dtype} symbol {symbol}")

    def test_joint_pairs_match_reference_and_survive_chunk_boundaries(self):
        for dtype in ("bf16", "e4m3", "e5m2"):
            width = self.pk.EXP_LAYOUT[dtype][1]
            exp = self.make_exp(dtype, n=97)  # odd size crossing tiny chunks
            real_chunk = self.pk._BINCOUNT_CHUNK
            self.pk._BINCOUNT_CHUNK = 7  # force many chunks incl. boundary cases
            try:
                joint = self.pk._pair_joint_counts(exp, width)
            finally:
                self.pk._BINCOUNT_CHUNK = real_chunk
            _, ref_joint = self.reference_counts(exp)
            for (prev, cur), expected in ref_joint.items():
                self.assertEqual(int(joint[(prev << width) | cur]), expected,
                                 f"{dtype} pair ({prev},{cur})")

    def test_entropy_from_bincount_equals_reference(self):
        from collections import Counter
        exp = self.make_exp("e4m3")
        counts = self.pk._bincount_chunked(exp, 1 << 4)
        ref = Counter(exp.tolist())
        self.assertAlmostEqual(self.pk.entropy(counts),
                               self.pk.entropy(ref.values()), places=9)

    def test_print_stats_output_lines(self):
        import contextlib
        import io
        exp = self.make_exp("e4m3")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.pk.print_stats(exp, "e4m3")
        out = buffer.getvalue()
        self.assertIn("[stats] dtype=e4m3 elements=5000 exp_bits=4", out)
        self.assertIn("exp top8 coverage:", out)
        self.assertIn("exp marginal entropy:", out)
        self.assertIn("exp pairwise joint entropy:", out)
        # joint entropy per pair >= marginal entropy is not guaranteed, but
        # per-element joint (jp/2) must be <= marginal for correlated streams
        marginal = float(re.search(r"marginal entropy: ([0-9.]+)", out).group(1))
        per_elt = float(re.search(r"joint entropy: ([0-9.]+)", out).group(1)) / 2
        self.assertLessEqual(per_elt, marginal + 1e-9)

    def test_exponent_stream_extracts_layout_bits(self):
        # e4m3 byte 0b1011_1001: sign=1, exp=(b>>3)&0xF=0b0111=7, frac=001
        raw = bytes([0b1011_1001])
        exp = self.pk.exponent_stream(raw, "e4m3")
        self.assertEqual(exp.tolist(), [7])
        # bf16 u16 0x3F80 (1.0): exp=(u>>7)&0xFF=127
        exp16 = self.pk.exponent_stream((0x3F80).to_bytes(2, "little"), "bf16")
        self.assertEqual(exp16.tolist(), [127])


if __name__ == "__main__":
    import json
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("PYTEST_SUMMARY:" + json.dumps({
        "ran": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "numpy": HAS_NUMPY,
    }))
    sys.exit(0 if result.wasSuccessful() else 1)
