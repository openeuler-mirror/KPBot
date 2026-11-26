"""Execute the guide's TPC-H import commands against local TLS/HTTP mocks.

The column lists, file row layouts and DDL column orders below are fixtures
copied from Apache Doris commit 09b0cc49a60ffdd444df3e40e5f3dc180299b561,
tools/tpch-tools/bin/load-tpch-data.sh and ddl/create-tpch-tables-sf1.sql.
The mock BE applies the declared columns mapping and rejects payloads whose
field count or column list does not match, so a documentation regression in
the guide itself fails this case instead of silently reporting a row count.
"""

import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parents[2]
SCRIPT = SKILL / "scripts/verify_tpch_import.py"
GUIDE = SKILL / "references/guide.md"
spec = importlib.util.spec_from_file_location("verify_tpch_import", SCRIPT)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

TABLES = (
    "region",
    "nation",
    "supplier",
    "part",
    "partsupp",
    "customer",
    "orders",
    "lineitem",
)
SHARDED = ("partsupp", "orders", "lineitem")

UPSTREAM_COLUMNS = {
    "region": ["r_regionkey", "r_name", "r_comment", "temp"],
    "nation": ["n_nationkey", "n_name", "n_regionkey", "n_comment", "temp"],
    "supplier": [
        "s_suppkey",
        "s_name",
        "s_address",
        "s_nationkey",
        "s_phone",
        "s_acctbal",
        "s_comment",
        "temp",
    ],
    "part": [
        "p_partkey",
        "p_name",
        "p_mfgr",
        "p_brand",
        "p_type",
        "p_size",
        "p_container",
        "p_retailprice",
        "p_comment",
        "temp",
    ],
    "partsupp": [
        "ps_partkey",
        "ps_suppkey",
        "ps_availqty",
        "ps_supplycost",
        "ps_comment",
        "temp",
    ],
    "customer": [
        "c_custkey",
        "c_name",
        "c_address",
        "c_nationkey",
        "c_phone",
        "c_acctbal",
        "c_mktsegment",
        "c_comment",
        "temp",
    ],
    "orders": [
        "o_orderkey",
        "o_custkey",
        "o_orderstatus",
        "o_totalprice",
        "o_orderdate",
        "o_orderpriority",
        "o_clerk",
        "o_shippriority",
        "o_comment",
        "temp",
    ],
    "lineitem": [
        "l_orderkey",
        "l_partkey",
        "l_suppkey",
        "l_linenumber",
        "l_quantity",
        "l_extendedprice",
        "l_discount",
        "l_tax",
        "l_returnflag",
        "l_linestatus",
        "l_shipdate",
        "l_commitdate",
        "l_receiptdate",
        "l_shipinstruct",
        "l_shipmode",
        "l_comment",
        "temp",
    ],
}

DDL_ORDER = {
    "lineitem": [
        "l_shipdate",
        "l_orderkey",
        "l_linenumber",
        "l_partkey",
        "l_suppkey",
        "l_quantity",
        "l_extendedprice",
        "l_discount",
        "l_tax",
        "l_returnflag",
        "l_linestatus",
        "l_commitdate",
        "l_receiptdate",
        "l_shipinstruct",
        "l_shipmode",
        "l_comment",
    ],
    "orders": [
        "o_orderkey",
        "o_orderdate",
        "o_custkey",
        "o_orderstatus",
        "o_totalprice",
        "o_orderpriority",
        "o_clerk",
        "o_shippriority",
        "o_comment",
    ],
}

ROWS = {
    "region": [
        "0|AFRICA|lar deposits. blithely final packages cajole. "
        "regular waters are final requests.|"
    ],
    "nation": [
        "0|ALGERIA|0| haggle. carefully final deposits detect slyly agai|"
    ],
    "supplier": [
        "1|Supplier#000000001| N kD4on9OM Ipw3,gf0JBoQDd7tgrzrddZ|17|"
        "27-918-335-1736|5755.94|each slyly above the careful|"
    ],
    "part": [
        "1|goldenrod lavender spring chocolate lace|Manufacturer#1|"
        "Brand#13|PROMO BURNISHED COPPER|7|JUMBO PKG|901.00|"
        "ly. slyly ironi|"
    ],
    "partsupp": [
        "1|2|3325|771.64|, even theodolites. regular, final theodolites eat|",
        "1|3|1200|500.00|carefully silent|",
    ],
    "customer": [
        "1|Customer#000000001|IVhzIApeRb ot,c,E|15|25-989-741-2988|711.56|"
        "BUILDING|to the even, regular platelets. "
        "regular, ironic epitaphs nag e|"
    ],
    "orders": [
        "1|36901|O|173665.47|1996-01-02|5-LOW|Clerk#000000951|0|"
        "nstructions sleep furiously among |",
        "2|78002|O|123456.78|1996-01-03|1-URGENT|Clerk#000000880|0|"
        "carefully final requests wake |",
    ],
    "lineitem": [
        "1|155190|7706|1|17|21168.23|0.04|0.02|N|O|1996-03-13|"
        "1996-02-12|1996-03-22|DELIVER IN PERSON|TRUCK|"
        "egular courts above the|",
        "1|155190|7706|2|17|21168.23|0.04|0.02|N|O|1996-03-13|"
        "1996-02-12|1996-03-22|DELIVER IN PERSON|TRUCK|blithely.?|",
    ],
}

COUNTS = {table: len(rows) for table, rows in ROWS.items()}


def guide_blocks():
    return re.findall(
        r"```bash\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.S
    )


def guide_block(token):
    found = [block for block in guide_blocks() if token in block]
    if len(found) != 1:
        raise AssertionError(f"expected one guide block containing {token!r}")
    return found[0]


def table_from_path(path):
    return path.split("/")[-2]


def split_columns(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def send(handler, status, headers, payload):
    handler.send_response(status)
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    if payload:
        handler.wfile.write(payload)


class Recorder(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "path": self.path,
                "body": body,
                "headers": {
                    name.lower(): value for name, value in self.headers.items()
                },
            }
        )
        table = table_from_path(self.path)
        if self.server.role == "fe":
            location = (
                "http://root:secret@127.0.0.1:8040" + self.path
            )
            send(self, 307, {"Location": location}, b"")
            return
        self.server.respond_be(self, table, body)


class SecureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def make_cert(directory, name):
    cert = directory / f"{name}.crt"
    key = directory / f"{name}.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class GuideImportTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required for the local HTTPS mock")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ca_cert, self.ca_key = make_cert(self.root, "ca")
        self.data = self.root / "tpch-data"
        self.data.mkdir()
        self.write_data()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.servers = []
        self.be = self.start_server("be", self.be_responder)
        self.fe = self.start_server("fe", None)
        self.fe.be_port = self.be.server_port
        self.addCleanup(self.stop_servers)

    def write_data(self):
        for table, rows in ROWS.items():
            if table in SHARDED:
                for index, row in enumerate(rows, start=1):
                    (self.data / f"{table}.tbl.{index}").write_text(
                        row + "\n", encoding="utf-8"
                    )
            else:
                (self.data / f"{table}.tbl").write_text(
                    "\n".join(rows) + "\n", encoding="utf-8"
                )

    def start_server(self, role, responder):
        server = SecureServer(("127.0.0.1", 0), Recorder)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.ca_cert), str(self.ca_key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.requests = []
        server.role = role
        server.respond_be = responder or self.be_responder
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        return server

    def be_responder(self, handler, table, body):
        headers = handler.server.requests[-1]["headers"]
        columns = split_columns(headers.get("columns"))
        label = headers.get("label")
        separator = headers.get("column_separator")
        lines = [line for line in body.decode("utf-8").split("\n") if line]
        expected = UPSTREAM_COLUMNS[table]
        valid = (
            columns == expected
            and separator == "|"
            and bool(label)
            and bool(lines)
            and all(len(line.split("|")) == len(expected) for line in lines)
        )
        payload = json.dumps(
            {
                "Status": "Success" if valid else "Fail",
                "Label": label or "",
                "NumberLoadedRows": len(lines) if valid else 0,
                "NumberFilteredRows": 0 if valid else max(len(lines), 1),
                "Message": "" if valid else "columns or row width mismatch",
            }
        ).encode()
        send(handler, 200, {"Content-Type": "application/json"}, payload)

    def stop_servers(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def write_fake_mysql(self):
        fake = self.root / "fakebin"
        fake.mkdir()
        counts = self.root / "counts.json"
        counts.write_text(json.dumps(COUNTS), encoding="utf-8")
        script = fake / "mysql"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "expected = '--defaults-extra-file=' + "
            "os.environ['DORIS_CLIENT_CONFIG']\n"
            "if not args or args[0] != expected:\n"
            "    raise SystemExit('missing client config as first option')\n"
            "if '--protocol=TCP' not in args:\n"
            "    raise SystemExit('missing explicit TCP protocol')\n"
            "if args[args.index('--user') + 1] != os.environ['EXPECTED_USER']:\n"
            "    raise SystemExit('wrong Doris user')\n"
            "sql = ''\n"
            "for index, arg in enumerate(args):\n"
            "    if arg == '--execute' and index + 1 < len(args):\n"
            "        sql = args[index + 1]\n"
            "    elif arg.startswith('--execute='):\n"
            "        sql = arg.split('=', 1)[1]\n"
            "counts = json.load(open(os.environ['GUIDE_COUNTS']))\n"
            "print(counts[sql.rsplit(' ', 1)[-1]])\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return fake, counts

    def guide_env(self, fake, counts):
        env = dict(os.environ)
        env.update(
            {
                "SKILL_DIR": str(SKILL),
                "TPCH_DATA_DIR": str(self.data),
                "RUN_DIR": str(self.run_dir),
                "FE_HOST": "127.0.0.1",
                "FE_HTTPS_PORT": str(self.fe.server_port),
                "FE_QUERY_PORT": "9030",
                "DB": "guide_test",
                "USER": "root",
                "EXPECTED_USER": "root",
                "DORIS_USER": "wrong-user",
                "BE_HOSTS": "127.0.0.1",
                "BE_HTTPS_PORT": str(self.be.server_port),
                "DORIS_CA_BUNDLE": str(self.ca_cert),
                "DORIS_CLIENT_CONFIG": str(self.root / "client.conf"),
                "DORIS_PASSWORD": "secret",
                "GUIDE_COUNTS": str(counts),
                "PATH": str(fake) + os.pathsep + env.get("PATH", ""),
            }
        )
        return env

    def run_https_guide(self):
        fake, counts = self.write_fake_mysql()
        (self.root / "client.conf").write_text(
            "[client]\nprotocol=TCP\n", encoding="utf-8"
        )
        block = guide_block("stream_load_https.py")
        return subprocess.run(
            ["bash", "-c", block],
            cwd=self.root,
            env=self.guide_env(fake, counts),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_https_loop_sends_upstream_columns_for_all_tables_and_shards(self):
        completed = self.run_https_guide()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(
            (self.run_dir / "import-check-https/result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(result["status"], "passed", result["errors"])
        for table, count in COUNTS.items():
            self.assertEqual(result["tables"][table]["loaded"], count)
            self.assertEqual(result["tables"][table]["expected"], count)

        expected_requests = len(COUNTS) + len(SHARDED)
        self.assertEqual(len(self.be.requests), expected_requests)
        self.assertEqual(len(self.fe.requests), expected_requests)
        for request in self.fe.requests:
            self.assertEqual(request["headers"].get("expect"), "100-continue")
        for request in self.be.requests:
            table = table_from_path(request["path"])
            headers = request["headers"]
            self.assertEqual(headers.get("expect"), "100-continue")
            self.assertEqual(
                headers.get("authorization"), "Basic cm9vdDpzZWNyZXQ="
            )
            self.assertEqual(
                split_columns(headers.get("columns")), UPSTREAM_COLUMNS[table]
            )
            label = headers.get("label")
            self.assertRegex(label, rf"^tpch-[0-9a-f]{{32}}_{table}_\d+$")
            index = int(label.rsplit("_", 1)[1])
            if table in SHARDED:
                expected_rows = [ROWS[table][index - 1]]
            else:
                expected_rows = ROWS[table]
            expected_body = ("\n".join(expected_rows) + "\n").encode()
            self.assertEqual(request["body"], expected_body)
            self.assertTrue(request["body"].endswith(b"|\n"))

    def test_reordered_orders_and_lineitem_map_by_column_name(self):
        completed = self.run_https_guide()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        captured = {}
        for request in self.be.requests:
            table = table_from_path(request["path"])
            label = request["headers"].get("label", "")
            if table in DDL_ORDER and label.endswith("_1"):
                captured[table] = request

        lineitem = captured["lineitem"]
        columns = split_columns(lineitem["headers"].get("columns"))
        first = lineitem["body"].decode("utf-8").split("\n")[0].split("|")
        mapped = dict(zip(columns, first))
        self.assertEqual(columns[0], "l_orderkey")
        self.assertEqual(mapped["l_orderkey"], "1")
        self.assertEqual(mapped["l_partkey"], "155190")
        self.assertEqual(mapped["l_shipdate"], "1996-03-13")
        self.assertEqual(DDL_ORDER["lineitem"][0], "l_shipdate")
        self.assertNotEqual(DDL_ORDER["lineitem"], columns[:-1])

        orders = captured["orders"]
        columns = split_columns(orders["headers"].get("columns"))
        first = orders["body"].decode("utf-8").split("\n")[0].split("|")
        mapped = dict(zip(columns, first))
        self.assertEqual(mapped["o_orderkey"], "1")
        self.assertEqual(mapped["o_custkey"], "36901")
        self.assertEqual(mapped["o_orderdate"], "1996-01-02")
        self.assertEqual(DDL_ORDER["orders"][1], "o_orderdate")
        self.assertNotEqual(DDL_ORDER["orders"], columns[:-1])

    def test_https_guide_uses_8050_default_port(self):
        block = guide_block("stream_load_https.py")
        self.assertIn("${FE_HTTPS_PORT:-8050}", block)
        self.assertIn("${BE_HTTPS_PORT:-8040}", block)
        self.assertIn('--user "$USER"', block)
        self.assertNotIn("8443", block)
        self.assertIn('--header "columns: ${COLUMNS[$table]}"', block)

    def test_both_count_gates_pass_client_configuration(self):
        for token in ("--load-output", "stream_load_https.py"):
            with self.subTest(block=token):
                block = guide_block(token)
                self.assertIn('--config "$DORIS_CLIENT_CONFIG"', block)

    def test_http_guide_sets_txid_before_upstream_load(self):
        block = guide_block("load-tpch-data.sh")
        lines = block.splitlines()
        txid = [
            index
            for index, line in enumerate(lines)
            if line.startswith("TXID=")
        ]
        load = [
            index
            for index, line in enumerate(lines)
            if "load-tpch-data.sh" in line
        ]
        self.assertEqual(len(txid), 1)
        self.assertEqual(len(load), 1)
        self.assertLess(txid[0], load[0])
        self.assertIn('-x "$TXID"', lines[load[0]])
        self.assertIn('--txid "$TXID"', guide_block("--load-output"))

    def test_documented_txids_are_unique_and_per_block(self):
        blocks = [
            guide_block("load-tpch-data.sh"),
            guide_block("stream_load_https.py"),
        ]
        values = []
        for block in blocks:
            lines = [
                line for line in block.splitlines() if line.startswith("TXID=")
            ]
            self.assertEqual(len(lines), 1)
            self.assertIn("uuid.uuid4().hex", lines[0])
            self.assertIn("python3", lines[0])
            self.assertNotIn("date +", lines[0])
            generated = []
            for _ in range(2):
                completed = subprocess.run(
                    ["bash", "-c", lines[0] + '\nprintf "%s" "$TXID"'],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                generated.append(completed.stdout)
            self.assertNotEqual(generated[0], generated[1])
            self.assertRegex(generated[0], r"^tpch-[0-9a-f]{32}$")
            values.extend(generated)
        self.assertEqual(len(set(values)), len(values))
        self.assertIn('--txid "$TXID"', guide_block("--load-output"))
        self.assertIn('--txid "$TXID"', blocks[1])

    def test_txid_label_grammar_is_exact(self):
        self.assertTrue(verify.belongs_to("run_region", "run"))
        self.assertTrue(verify.belongs_to("run_region_0", "run"))
        self.assertTrue(verify.belongs_to("run_lineitem_12", "run"))
        self.assertFalse(verify.belongs_to("run_other_region", "run"))
        self.assertFalse(verify.belongs_to("runner_region", "run"))
        self.assertFalse(verify.belongs_to("run_region_x", "run"))
        self.assertFalse(verify.belongs_to("run_region_1_extra", "run"))
        self.assertFalse(verify.belongs_to("run", "run"))
        self.assertTrue(verify.belongs_to("run.1_region", "run.1"))
        self.assertFalse(verify.belongs_to("runx1_region", "run.1"))

    def test_prefix_foreign_transaction_cannot_satisfy_gate(self):
        expected = self.root / "expected.json"
        expected.write_text(json.dumps(COUNTS), encoding="utf-8")
        load = self.root / "load.stdout"
        load.write_text(self.upstream_output("run_other"), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.check_gate(load, expected, "run")
        self.assertIn(
            "no Stream Load responses for transaction id",
            str(caught.exception),
        )

        foreign = self.upstream_output("run_other").replace(
            '"Status": "Success"', '"Status": "Fail"'
        )
        load.write_text(
            foreign + self.upstream_output("run"), encoding="utf-8"
        )
        _, stats, errors = self.check_gate(load, expected, "run")
        self.assertEqual(errors, [])
        for table, count in COUNTS.items():
            self.assertEqual(stats[table]["loaded"], count)

    def gate_args(self, load, expected, txid):
        options = [
            "--load-output",
            str(load),
            "--expected-json",
            str(expected),
            "--database",
            "guide_test",
        ]
        if txid is not None:
            options += ["--txid", txid]
        return verify.parser().parse_args(options)

    def upstream_output(self, txid):
        blocks = []
        for table, count in COUNTS.items():
            if table in SHARDED:
                shards = ((1, 1), (2, count - 1))
            else:
                shards = ((None, count),)
            for index, rows in shards:
                label = f"{txid}_{table}" + (
                    "" if index is None else f"_{index}"
                )
                blocks.append(
                    json.dumps(
                        {
                            "Status": "Success",
                            "Label": label,
                            "NumberLoadedRows": rows,
                            "NumberFilteredRows": 0,
                        }
                    )
                    + "\n"
                )
        return "".join(blocks)

    def fake_client(self, argv, **kwargs):
        table = argv[-1].rsplit(" ", 1)[-1]
        return subprocess.CompletedProcess(argv, 0, f"{COUNTS[table]}\n", "")

    def check_gate(self, load, expected, txid):
        with patch.object(
            verify.subprocess, "run", side_effect=self.fake_client
        ):
            return verify.load_output(self.gate_args(load, expected, txid))

    def test_upstream_txid_labels_map_to_tables_and_shards(self):
        load = self.root / "load.stdout"
        load.write_text(
            self.upstream_output("tpch-20260101000000"), encoding="utf-8"
        )
        expected = self.root / "expected.json"
        expected.write_text(json.dumps(COUNTS), encoding="utf-8")
        _, stats, errors = self.check_gate(
            load, expected, "tpch-20260101000000"
        )
        self.assertEqual(errors, [])
        for table, count in COUNTS.items():
            self.assertEqual(stats[table]["loaded"], count)
            self.assertGreaterEqual(stats[table]["responses"], 1)

    def test_txid_ignores_historical_responses(self):
        current = "tpch-20260101000000"
        stale = self.upstream_output("tpch-20251231000000").replace(
            '"Status": "Success"', '"Status": "Fail"'
        )
        load = self.root / "load.stdout"
        load.write_text(
            stale + self.upstream_output(current), encoding="utf-8"
        )
        expected = self.root / "expected.json"
        expected.write_text(json.dumps(COUNTS), encoding="utf-8")
        _, stats, errors = self.check_gate(load, expected, current)
        self.assertEqual(errors, [])
        self.assertEqual(stats["region"]["loaded"], COUNTS["region"])
        _, _, mixed = self.check_gate(load, expected, None)
        self.assertTrue(mixed)
        self.assertTrue(any("Status is Fail" in error for error in mixed))

    def test_unknown_txid_is_rejected(self):
        load = self.root / "load.stdout"
        load.write_text(
            self.upstream_output("tpch-20260101000000"), encoding="utf-8"
        )
        expected = self.root / "expected.json"
        expected.write_text(json.dumps(COUNTS), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.check_gate(load, expected, "tpch-19990101000000")
        self.assertIn(
            "no Stream Load responses for transaction id",
            str(caught.exception),
        )


PREFLIGHT = Path(__file__).resolve().parent / "preflight.py"
RUNNER = Path(__file__).resolve().parent / "run.sh"


class PreflightTests(unittest.TestCase):
    def test_old_python_stops_before_version_dependent_probes(self):
        spec = importlib.util.spec_from_file_location(
            "guide_preflight", PREFLIGHT
        )
        preflight = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preflight)
        with patch.object(preflight.sys, "version_info", (3, 6, 15)):
            with patch.object(
                preflight,
                "bash_missing",
                side_effect=AssertionError("must not probe on old Python"),
            ):
                self.assertEqual(
                    preflight.missing_requirements(), ["python>=3.7"]
                )

    def temp_dir(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        return directory

    def make_bin(self, names):
        directory = self.temp_dir()
        for name in names:
            target = shutil.which(name)
            self.assertIsNotNone(target, name)
            (directory / name).symlink_to(target)
        return directory

    def preflight_env(self, bin_dir, results):
        env = dict(os.environ)
        env["PATH"] = str(bin_dir)
        env["GUIDE_PREFLIGHT_RESULTS"] = str(results)
        return env

    def assert_env_not_satisfied(self, completed, results, needle):
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 3, output)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "env_not_satisfied")
        self.assertFalse(payload["success"])
        self.assertEqual(payload["tests"], 0)
        self.assertTrue(
            any(needle in item for item in payload["missing"]), payload
        )
        on_disk = json.loads((results / "result.json").read_text())
        self.assertEqual(on_disk["status"], "env_not_satisfied")

    def test_missing_openssl_reports_env_not_satisfied(self):
        results = self.temp_dir()
        bin_dir = self.make_bin(["bash", "dirname", "mkdir", "python3"])
        completed = subprocess.run(
            [shutil.which("bash"), str(RUNNER)],
            env=self.preflight_env(bin_dir, results),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assert_env_not_satisfied(completed, results, "openssl")

    def test_missing_bash_reports_env_not_satisfied(self):
        results = self.temp_dir()
        bin_dir = self.make_bin(["openssl"])
        completed = subprocess.run(
            [sys.executable, str(PREFLIGHT)],
            env=self.preflight_env(bin_dir, results),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assert_env_not_satisfied(completed, results, "bash")

    def test_missing_python_entrypoint_returns_exit3(self):
        results = self.temp_dir()
        bin_dir = self.make_bin(["bash", "dirname", "mkdir", "cat"])
        completed = subprocess.run(
            [shutil.which("bash"), str(RUNNER)],
            env=self.preflight_env(bin_dir, results),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assert_env_not_satisfied(completed, results, "python")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    success = (
        result.wasSuccessful() and not result.skipped and result.testsRun > 0
    )
    output = Path(__file__).resolve().parent / "results"
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(
            {
                "success": success,
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "skipped": len(result.skipped),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(0 if success else 1)
