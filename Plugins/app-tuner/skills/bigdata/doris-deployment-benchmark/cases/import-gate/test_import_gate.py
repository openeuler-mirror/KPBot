"""Verify Stream Load rows and eight table counts gate query timing."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verify_tpch_import.py"
spec = importlib.util.spec_from_file_location("verify_tpch_import", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

COUNTS = {
    "region": 5,
    "nation": 25,
    "supplier": 10,
    "part": 20,
    "partsupp": 30,
    "customer": 15,
    "orders": 40,
    "lineitem": 60,
}


class ImportGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.expected = self.root / "expected.json"
        self.expected.write_text(json.dumps(COUNTS), encoding="utf-8")
        self.load = self.root / "load.stdout"
        self.load.write_text(self.output(), encoding="utf-8")
        self.calls = []
        self.counts = dict(COUNTS)

    def output(self, **overrides):
        blocks = ["load started\n"]
        for table, count in COUNTS.items():
            value = overrides.get(table, {})
            status = value.get("status", "Success")
            loaded = value.get("loaded", count)
            filtered = value.get("filtered", 0)
            blocks.append(
                "----loaded file\n"
                + json.dumps(
                    {
                        "Status": status,
                        "Label": f"123_{table}_1",
                        "NumberLoadedRows": loaded,
                        "NumberFilteredRows": filtered,
                    }
                )
                + "\n"
            )
        return "".join(blocks)

    def client(self, argv, **kwargs):
        self.calls.append(argv)
        table = argv[-1].rsplit(" ", 1)[-1]
        return subprocess.CompletedProcess(
            argv, 0, f"COUNT(*)\n{self.counts[table]}\n", ""
        )

    def args(self, **overrides):
        values = {
            "load_output": self.load,
            "responses": None,
            "expected_json": self.expected,
            "data_dir": None,
            "database": "tpch_test",
            "host": "localhost",
            "port": 9030,
            "user": "root",
            "client": "mysql",
            "protocol": "TCP",
            "config": None,
            "timeout": 30.0,
            "output": None,
        }
        values.update(overrides)
        options = [
            "--database",
            values["database"],
            "--host",
            values["host"],
            "--port",
            str(values["port"]),
            "--user",
            values["user"],
            "--client",
            values["client"],
            "--protocol",
            values["protocol"],
        ]
        if values["data_dir"] is not None:
            options += ["--data-dir", str(values["data_dir"])]
        elif values["expected_json"] is not None:
            options += ["--expected-json", str(values["expected_json"])]
        args = module.parser().parse_args(options)
        args.load_output = values["load_output"]
        args.responses = values["responses"]
        args.config = values["config"]
        args.timeout = values["timeout"]
        args.output = values["output"]
        return args

    def make_data_dir(self):
        data = self.root / "tpch-data"
        data.mkdir()
        for table, count in COUNTS.items():
            if table in ("partsupp", "orders", "lineitem"):
                first = count // 2
                (data / f"{table}.tbl.1").write_text(
                    "row\n" * first, encoding="utf-8"
                )
                (data / f"{table}.tbl.2").write_text(
                    "row\n" * (count - first), encoding="utf-8"
                )
            else:
                (data / f"{table}.tbl").write_text(
                    "row\n" * count, encoding="utf-8"
                )
        return data

    def check(self, args=None, side_effect=None):
        with patch.object(
            module.subprocess, "run", side_effect=side_effect or self.client
        ):
            return module.check(args or self.args())

    def test_all_responses_and_counts_pass(self):
        result = self.check()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["errors"], [])
        for table, count in COUNTS.items():
            self.assertEqual(result["tables"][table]["loaded"], count)
            self.assertEqual(result["tables"][table]["filtered"], 0)
            self.assertEqual(result["tables"][table]["count"], count)
            self.assertEqual(result["tables"][table]["expected"], count)

    def test_data_dir_sums_sharded_and_single_tbl_files(self):
        data = self.make_data_dir()
        result = self.check(self.args(expected_json=None, data_dir=data))
        self.assertEqual(result["status"], "passed")
        for table, count in COUNTS.items():
            self.assertEqual(result["tables"][table]["expected"], count)

    def test_sharded_responses_are_aggregated_per_table(self):
        blocks = []
        for table, count in COUNTS.items():
            if table in ("partsupp", "orders", "lineitem"):
                first = count // 2
                shards = ((1, first), (2, count - first))
            else:
                shards = ((0, count),)
            for index, rows in shards:
                blocks.append(
                    json.dumps(
                        {
                            "Status": "Success",
                            "Label": f"123_{table}_{index}",
                            "NumberLoadedRows": rows,
                            "NumberFilteredRows": 0,
                        }
                    )
                    + "\n"
                )
        self.load.write_text("".join(blocks), encoding="utf-8")
        data = self.make_data_dir()
        result = self.check(self.args(expected_json=None, data_dir=data))
        self.assertEqual(result["status"], "passed")
        for table, count in COUNTS.items():
            self.assertEqual(result["tables"][table]["loaded"], count)
            self.assertGreaterEqual(result["tables"][table]["responses"], 1)

    def test_data_dir_missing_table_is_rejected(self):
        data = self.make_data_dir()
        (data / "nation.tbl").unlink()
        with self.assertRaises(ValueError) as caught:
            self.check(self.args(expected_json=None, data_dir=data))
        self.assertIn("no generated files for nation", str(caught.exception))

    def test_filtered_rows_fail_even_when_status_succeeds(self):
        self.load.write_text(
            self.output(region={"filtered": 1}), encoding="utf-8"
        )
        result = self.check()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            any("filtered 1 rows" in error for error in result["errors"])
        )

    def test_non_success_status_fails(self):
        self.load.write_text(
            self.output(orders={"status": "Fail"}), encoding="utf-8"
        )
        result = self.check()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            any("orders Status is Fail" in error for error in result["errors"])
        )

    def test_missing_table_fails(self):
        text = self.output()
        self.load.write_text(
            text.replace('"123_nation_1"', '"unknown_1"'), encoding="utf-8"
        )
        result = self.check()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            any(
                "nation has no Stream Load response" in error
                for error in result["errors"]
            )
        )
        self.assertTrue(
            any(
                "cannot map response label" in error
                for error in result["errors"]
            )
        )

    def test_count_mismatch_fails_after_queries(self):
        self.counts["lineitem"] = 59
        result = self.check()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            any("lineitem COUNT(*)" in error for error in result["errors"])
        )
        self.assertEqual(len(self.calls), 8)

    def test_protocol_is_explicitly_tcp(self):
        self.check()
        for argv in self.calls:
            self.assertIn("--protocol=TCP", argv)

    def test_config_file_stays_the_first_mysql_option(self):
        config = self.root / "client.conf"
        config.write_text("[client]\nprotocol=TCP\n", encoding="utf-8")
        result = self.check(self.args(config=config))
        self.assertEqual(result["status"], "passed")
        for argv in self.calls:
            self.assertEqual(argv[1], f"--defaults-extra-file={config}")
            self.assertEqual(argv[2], "--protocol=TCP")

    def test_response_directory_mode_collects_https_outputs(self):
        for table, count in COUNTS.items():
            target = self.root / f"https-{table}"
            target.mkdir()
            (target / "stream-load-response.json").write_text(
                json.dumps(
                    {
                        "Status": "Success",
                        "Label": f"123_{table}_1",
                        "NumberLoadedRows": count,
                        "NumberFilteredRows": 0,
                    }
                ),
                encoding="utf-8",
            )
        result = self.check(self.args(load_output=None, responses=self.root))
        self.assertEqual(result["status"], "passed")

    def test_directory_without_stream_load_responses_is_rejected(self):
        empty = self.root / "empty"
        empty.mkdir()
        (empty / "result.json").write_text(
            json.dumps({"status": "passed"}), encoding="utf-8"
        )
        with self.assertRaises(ValueError) as caught:
            self.check(self.args(load_output=None, responses=empty))
        self.assertIn("no Stream Load responses", str(caught.exception))

    def test_no_responses_is_rejected(self):
        self.load.write_text("nothing to parse\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.check()
        self.assertIn("no Stream Load responses", str(caught.exception))

    def test_object_scanning_tolerates_noise(self):
        values = module.objects_in(
            'noise {"Status":"Success","Label":"x_region",'
            '"NumberLoadedRows":1}'
        )
        self.assertEqual(values[0]["Status"], "Success")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parent / "results"
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(
            {
                "success": result.wasSuccessful(),
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(0 if result.wasSuccessful() else 1)
