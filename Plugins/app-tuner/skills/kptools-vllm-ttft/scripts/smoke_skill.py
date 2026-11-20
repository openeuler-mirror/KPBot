"""Offline smoke test for the v0.27 TTFT skill core.

Creates a fake target environment (site layout with vllm/vllm_ascend roots at
the pristine 0.27 baselines), then runs inspect -> plan -> apply -> report ->
rollback through the real CLI, verifying:

  - pristine baseline files are instrumented exactly to the payload 'after'
  - a file with an unrelated local edit at a location far from any hunk still
    applies, and the local edit survives
  - a file whose edited region overlaps a hunk refuses to apply
  - already-applied trees are reported as no-op
  - rollback restores byte-identical originals (and removes created files)
"""

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

SKILL = Path(__file__).resolve().parent
PY = sys.executable
MANIFEST = json.loads(
    (SKILL.parent / "payload" / "vllm-ascend-timing.json").read_text(encoding="utf-8")
)
PROFILES = {
    k: next(p for p in MANIFEST["profiles"] if k in p["repos"])
    for k in ("vllm", "ascend")
}


def run(env_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(env_dir))
    return subprocess.run(
        [PY, str(SKILL / "ttft.py"), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=env_dir,
    )


def make_env(root: Path, repo: str, mutate=None) -> Path:
    env = root / "site"
    env.mkdir(parents=True, exist_ok=True)
    pkg = env / ("vllm_ascend" if repo == "ascend" else repo)
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for entry in PROFILES[repo]["repos"][repo]:
        if entry["before"] is None:
            continue  # new files are created by apply; baseline has no copy
        target = env / entry["path"]
        text = entry["before"]
        if mutate and entry["path"] == mutate[0]:
            text = mutate[1](text)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    return env


def check_python_payload() -> list[str]:
    """Audit imports and run real diagnostic context code, without serving."""
    for profile in MANIFEST["profiles"]:
        for entries in profile["repos"].values():
            for entry in entries:
                before_imports = {
                    ast.unparse(node)
                    for node in ast.walk(ast.parse(entry["before"] or ""))
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    and "mooncake" in ast.unparse(node)
                }
                after_imports = {
                    ast.unparse(node)
                    for node in ast.walk(ast.parse(entry["after"]))
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    and "mooncake" in ast.unparse(node)
                }
                assert before_imports == after_imports, entry["path"]
                for obsolete in (
                    "supports_batch_put_trace",
                    "batch_put_from_multi_buffers_with_trace",
                    "_native_trace_supported",
                    "BatchPutTiming",
                ):
                    assert obsolete not in entry["after"], (entry["path"], obsolete)
    ascend = PROFILES["ascend"]["repos"]["ascend"]
    backend = next(
        e for e in ascend if e["path"].endswith("/backend/mooncake_backend.py")
    )
    assert (
        "self.store.batch_put_from_multi_buffers(keys, addrs, sizes, config)"
        in backend["after"]
    )
    diag_source = next(
        e["after"]
        for e in PROFILES["vllm"]["repos"]["vllm"]
        if e["path"].endswith("/ttft_diagnostics.py")
    )
    store_source = next(
        e["after"] for e in ascend if e["path"].endswith("/ttft_diagnostics.py")
    )
    import types

    diag = types.ModuleType("_offline_diag")
    sys.modules[diag.__name__] = diag
    # Execute only trusted, shipped payload source to exercise its real context.
    exec(compile(diag_source, "<payload host diagnostics>", "exec"), diag.__dict__)  # noqa: S102
    store = types.ModuleType("_offline_store_diag")
    sys.modules[store.__name__] = store
    # Bind the real diagnostic implementation without importing serving packages.
    tree = ast.parse(store_source)
    tree.body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.ImportFrom) and node.module == "vllm.v1.metrics")
    ]
    store.diag = diag
    exec(compile(tree, "<payload queue diagnostics>", "exec"), store.__dict__)  # noqa: S102

    class Request:
        req_id = "synthetic-request"

    class Thread:
        tp_rank = 0
        diagnostic_direction = "save"

        def _handle_request(self, payload):
            assert payload is request
            assert diag.current()["execution_id"] == "synthetic-execution"
            assert diag.current()["task_id"] == dict(queued.fields)["task_id"]

    request, thread = Request(), Thread()
    with diag.scope(execution_id="synthetic-execution"):
        queued = store.capture(thread, request)
    assert diag.current() == {}
    store.execute(thread, queued)
    assert diag.current() == {}
    print("PASS unchanged business imports/API; real host context crosses queue")

    # Execute the actual outer-wait method with isolated event/queue dependencies,
    # then drive it through the shipped immediate/deferred connector lifecycle.
    pool_source = next(
        e["after"] for e in ascend if e["path"].endswith("/pool_worker.py")
    )
    pool_tree = ast.parse(pool_source)
    wait_method = next(
        node for node in ast.walk(pool_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "wait_for_save"
    )
    messages, queued_requests, actions = [], [], []

    class Clock:
        value = 0.0

        def perf_counter(self):
            self.value += 0.01
            return self.value

    clock = Clock()

    class Logger:
        def info(self, message, *args):
            messages.append(message % args)

    class Event:
        def record(self):
            actions.append("event")

    class SendThread:
        diagnostic_direction = "save"

        def __init__(self, rank):
            self.tp_rank = rank
            self.request_queue = self

        def add_stored_request(self, req_id):
            actions.append(("stored", req_id))

        def add_request(self, request):
            actions.append(("queued", request.req_id))
            queued_requests.append(store.capture(self, request))

        def join(self):
            actions.append("join")
            clock.value += 0.2 * (self.tp_rank + 1)

    namespace = {
        "diag": diag, "store_diag": store, "os": os, "time": clock,
        "logger": Logger(), "torch": types.SimpleNamespace(
            npu=types.SimpleNamespace(Event=Event)
        ),
    }
    isolated = ast.parse("from __future__ import annotations")
    isolated.body.append(wait_method)
    exec(compile(isolated, "<payload outer wait>", "exec"), namespace)  # noqa: S102

    class Connector:
        def bind_connector_metadata(self, metadata):
            self.metadata = metadata

        def start_load_kv(self, context):
            pass

        def wait_for_save(self):
            namespace["wait_for_save"](self.worker, self.metadata)

        def clear_connector_metadata(self):
            self.metadata = None

        def get_finished(self, finished):
            return set(), set()

        def get_block_ids_with_load_errors(self):
            return set()

        def get_kv_connector_stats(self):
            return None

        def get_kv_connector_kv_cache_events(self):
            return None

        def build_connector_worker_meta(self):
            return None

    connector = Connector()
    mixin_source = next(
        e["after"] for e in PROFILES["vllm"]["repos"]["vllm"]
        if e["path"].endswith("/kv_connector_model_runner_mixin.py")
    )
    mixin_tree = ast.parse(mixin_source)
    mixin_class = next(
        node for node in mixin_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "KVConnectorModelRunnerMixin"
    )
    mixin_class.body = [
        node for node in mixin_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("_get_kv_connector_output", "finalize_kv_connector")
    ]
    lifecycle = ast.parse("from __future__ import annotations")
    lifecycle.body.append(mixin_class)
    lifecycle_globals = {
        "ttft_diag": diag, "time": clock, "logger": Logger(),
        "contextmanager": contextmanager, "KVConnectorOutput": types.SimpleNamespace,
        "KVConnectorBase": Connector, "get_kv_transfer_group": lambda: connector,
        "has_kv_transfer_group": lambda: True, "get_forward_context": lambda: None,
        "_pending_deferred_kv_output": None, "_pending_deferred_kv_fields": None,
    }
    exec(compile(lifecycle, "<payload connector lifecycle>", "exec"), lifecycle_globals)  # noqa: S102
    mixin = lifecycle_globals["KVConnectorModelRunnerMixin"]
    for rank in (0, 1):
        connector.worker = types.SimpleNamespace(
            tp_rank=rank, kv_send_thread=SendThread(rank),
            group_uses_align_state=[False],
        )
        request = types.SimpleNamespace(req_id="synthetic", can_save=True)
        scheduler = types.SimpleNamespace(
            ttft_execution_id="e0", num_scheduled_tokens={"synthetic": 32},
            kv_connector_metadata=types.SimpleNamespace(requests=[request]),
            finished_req_ids=set(),
        )
        actions.clear()
        before = len(queued_requests)
        # Deliberately wrong ambient identity: scheduler scope must win.
        with diag.scope(execution_id="not-the-origin"):
            with mixin._get_kv_connector_output(
                scheduler, defer_finalize=bool(rank)
            ) as output:
                pass
        assert diag.current() == {}
        if rank:
            assert len(queued_requests) == before
            assert output.worker_save_kv_time == -1
            with diag.scope(execution_id="later-execution"):
                mixin.finalize_kv_connector()
            assert output.worker_save_kv_time >= 0
            assert lifecycle_globals["_pending_deferred_kv_fields"] is None
        assert actions == [
            "event", ("stored", "synthetic"), ("queued", "synthetic"), "join"
        ]
        fields = dict(queued_requests[-1].fields)
        assert fields["execution_id"] == "e0" and fields["tp_rank"] == rank
        assert queued_requests[-1].payload is request
        assert request.skip_null_blocks_by_group == [False]
        assert isinstance(request.current_event, Event)
        assert diag.current() == {}
    # Empty/disabled requests do not introduce an event or a business join.
    actions.clear()
    with diag.scope(execution_id="e1"):
        namespace["wait_for_save"](
            connector.worker,
            types.SimpleNamespace(requests=[
                types.SimpleNamespace(req_id="synthetic", can_save=False)
            ]),
        )
    assert not actions
    boundaries = [m for m in messages if m.startswith("[TTFT_DEBUG][AscendStoreSave]")]
    assert len(boundaries) == 3
    print("PASS real outer wait formatting and immediate/deferred originating context")
    return boundaries


def check_import_isolation(tmp: Path) -> None:
    """Namespace fixtures test resolution only, not a real binary installation."""
    site = tmp / "namespace-site"
    package = site / "mooncake"
    package.mkdir(parents=True)
    (package / "engine.py").write_text("class TransferEngine: pass\n", encoding="utf-8")
    (package / "store.py").write_text(
        "class ReplicateConfig: pass\nclass MooncakeDistributedStore: pass\n",
        encoding="utf-8",
    )
    assert not (package / "__init__.py").exists()
    shadow = tmp / "legacy-scripts"
    shadow.mkdir()
    (shadow / "mooncake.py").write_text(
        "# Simulate the removed top-level skill module name, not its behavior.\n",
        encoding="utf-8",
    )
    hybrid = next(
        e
        for e in PROFILES["ascend"]["repos"]["ascend"]
        if e["path"].endswith("/mooncake_hybrid_connector.py")
    )
    statement = next(
        ast.unparse(node)
        for node in ast.walk(ast.parse(hybrid["after"]))
        if isinstance(node, ast.ImportFrom) and node.module == "mooncake.engine"
    )
    code = (
        "import runpy,sys\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "cli=runpy.run_path(sys.argv[2],run_name='offline_cli_import')\n"
        "try:\n"
        "    cli['main'](['report','--help'])\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0, exc.code\n"
        + statement
        + "\nfrom mooncake.store import ReplicateConfig,MooncakeDistributedStore\n"
        "import mooncake\nprint(mooncake.__path__[0])\n"
    )
    env = dict(os.environ, PYTHONPATH=str(site))
    command = [PY, "-S", "-c", code, str(shadow), str(SKILL / "ttft.py")]
    failed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp,
    )
    assert failed.returncode != 0, failed.stdout
    assert "'mooncake' is not a package" in failed.stderr, failed.stderr
    command[-2] = str(SKILL)
    passed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp,
    )
    assert passed.returncode == 0, passed.stderr
    assert str(package) in passed.stdout, passed.stdout
    assert "usage: ttft report" in passed.stdout, passed.stdout

    # A regular installed package must work as well as the namespace layout.
    (package / "__init__.py").write_text("", encoding="utf-8")
    regular = subprocess.run(
        command, check=False, capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=tmp,
    )
    assert regular.returncode == 0, regular.stderr
    assert str(package) in regular.stdout, regular.stdout

    # Refuse polluted releases before importing any sibling helper or collision.
    for location in ("scripts", "package"):
        for name in ("mooncake.py", "mooncake.pyc", "mooncake"):
            release = tmp / ("contaminated-" + location + "-" + name)
            scripts = release / "scripts"
            scripts.mkdir(parents=True)
            entry = scripts / "ttft.py"
            entry.write_bytes((SKILL / "ttft.py").read_bytes())
            collision = (scripts if location == "scripts" else release) / name
            if name == "mooncake":
                collision.mkdir()
            else:
                collision.write_bytes(b"raise AssertionError('collision must not execute')\n")
            refused = subprocess.run(
                [PY, "-B", str(entry), "--help"], check=False,
                capture_output=True, text=True, encoding="utf-8", env=env, cwd=tmp,
            )
            assert refused.returncode != 0, refused.stdout
            assert "skill release contains Mooncake import collision(s)" in refused.stderr
            assert str(collision) in refused.stderr, refused.stderr
            assert "NEW directory" in refused.stderr, refused.stderr
            assert "Traceback" not in refused.stderr, refused.stderr
            assert collision.exists()
            if collision.is_file():
                assert collision.read_bytes() == b"raise AssertionError('collision must not execute')\n"

    # Diagnose from the original launch cwd: preserve exact origin/candidates
    # and do not import serving packages unless explicitly requested.
    diagnose = [PY, "-B", str(SKILL / "ttft.py"), "diagnose", "--python", PY]
    evidence = subprocess.run(
        diagnose, check=False, capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=shadow,
    )
    assert evidence.returncode == 0, evidence.stderr
    probe = json.loads(evidence.stdout)["probe"]
    assert Path(probe["resolved_without_import"]["mooncake"]["origin"]) == shadow / "mooncake.py"
    assert probe["resolved_without_import"]["mooncake.engine"] is None
    assert probe["imports"] == []
    assert "mooncake" not in probe["loaded_modules"]
    assert any(Path(item["path"]) == shadow / "mooncake.py"
               for item in probe["path_candidates"]["mooncake"])
    assert probe["import_safety"]["resolved_mooncake_is_package"] is False
    assert any("operator" in item for item in probe["import_safety"]["guidance"])
    attempted = subprocess.run(
        [*diagnose, "--probe-import", "mooncake.engine"], check=False,
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=shadow,
    )
    assert attempted.returncode == 1, attempted.stderr
    imports = json.loads(attempted.stdout)["probe"]["imports"]
    assert "'mooncake' is not a package" in imports[0]["traceback"], imports
    assert (shadow / "mooncake.py").read_text(encoding="utf-8").startswith(
        "# Simulate the removed top-level skill module name"
    )
    clean = subprocess.run(
        [*diagnose, "--probe-import", "mooncake.engine", "--probe-import", "mooncake.store"],
        check=False, capture_output=True, text=True, encoding="utf-8", env=env, cwd=tmp,
    )
    assert clean.returncode == 0, (clean.stdout, clean.stderr)
    clean_probe = json.loads(clean.stdout)["probe"]
    assert clean_probe["import_safety"]["resolved_mooncake_is_package"] is True
    assert all(item["status"] == "imported" for item in clean_probe["imports"])
    assert Path(clean_probe["resolved_without_import"]["mooncake"]["origin"]) == package / "__init__.py"
    assert not (SKILL / "mooncake.py").exists()
    assert not (SKILL.parent / "mooncake.py").exists()
    print(
        "PASS namespace/package CLI imports; polluted release refuses; "
        "launch-directory shadow diagnosed read-only"
    )


def check_python_report(tmp: Path) -> None:
    log = tmp / "synthetic-python-only.log"
    out = tmp / "synthetic-python-only.html"
    log.write_text(
        "# SYNTHETIC OFFLINE FIXTURE: not an NPU measurement\n"
        "[TTFT][QUEUE] req=synthetic initial_queue_valid=true reason=available "
        "preemptions=0 queued_events=1 scheduled_events=1 queue_wait_s=0.1\n"
        "[TTFT_DEBUG][EngineCore] execution_id=e0 request_ids=synthetic "
        "prefill_request_ids=synthetic num_total=1 num_tokens=32\n"
        "[TTFT_DEBUG][AscendStoreSave] req_id=synthetic execution_id=e0 task_id=t0 "
        "tp_rank=0 pid=123 keys=2 bytes=1048576 queue_wait=0.01 build_key=0.02 "
        "lookup=0.03 prepare=0.04 sync=0.05 put=0.125\n"
        "[TTFT_DEBUG][EngineCore] execution_id=e1 request_ids=synthetic "
        "prefill_request_ids=synthetic num_total=1 num_tokens=32\n"
        "[TTFT_DEBUG][AscendStoreSave] req_id=synthetic execution_id=e1 task_id=t1 "
        "tp_rank=0 pid=123 keys=1\n"
        "[TTFT][BREAKDOWN] req=synthetic steps=2 ttft=1.0 hash=0.01 ext_lookup=0.02 "
        "alloc=0.03 overhead=0.04 load_kv=0.1 forward=0.2 save_kv=0.3 "
        "update=0.05 residual=0.25\n",
        encoding="utf-8",
    )
    assert "BatchPutTiming" not in log.read_text(encoding="utf-8")
    for args in (("check", str(log)), ("report", str(log), "-o", str(out))):
        result = run(tmp, *args)
        assert result.returncode == 0, (result.stdout, result.stderr)
    print(result.stdout.strip())
    for removed in (("mooncake", "--help"), ("report", str(log), "--hide-native-put")):
        refused = run(tmp, *removed)
        assert refused.returncode == 2, (refused.stdout, refused.stderr)
    text = out.read_text(encoding="utf-8")
    for obsolete in (
        ">start</th>",
        ">submit</th>",
        ">wait</th>",
        ">finalize</th>",
        "BatchPutTiming",
        "hide-native-put",
        "TTFT_THREAD",
    ):
        assert obsolete not in text, obsolete
    assert '<td class="put" data-field="put">0.125000</td>' in text
    assert '<td class="put" data-field="put">—</td>' in text
    assert '<td data-field="keys">2</td>' in text
    assert '<td data-field="mib">1.0</td>' in text
    assert '<td data-field="mib">—</td>' in text
    assert '<td class="put" data-field="put">0.000000</td>' not in text
    # Inspect the actual table model as well as rendered HTML.
    generator = report_generator()
    data = generator.parse(log)
    request, breakdown = generator.choose_request(data, None)
    steps, end = generator.select_steps(data, request, breakdown, "prefill", [])
    rows, _, _ = generator.save_rows(data, request, end, steps, None, [])
    assert rows[0]["ranks"][0]["put"] == 0.125
    assert rows[1]["ranks"][0]["put"] is None
    assert rows[1]["ranks"][0]["volume"][1] is None
    print(
        "PASS real CLI Python-only synthetic report: put/volume retained; missing is not zero"
    )


class ReportPage(HTMLParser):
    """Read consumer values and table geometry, not incidental markup/classes."""

    def __init__(self, text, table_id="main-table"):
        super().__init__()
        self.table_id = table_id
        self.active = False
        self.rows = []
        self.cell = None
        self.named = {}
        self.options = []
        self.stack = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        captured = None
        if attrs.get("id") or tag == "option":
            captured = dict(attrs, text="")
            if attrs.get("id"):
                self.named[attrs["id"]] = captured
            if tag == "option":
                self.options.append(captured)
        if tag not in ("br", "hr", "meta", "link", "input"):
            self.stack.append((tag, captured))
        if tag == "table" and attrs.get("id") == self.table_id:
            self.active = True
        if self.active:
            if tag == "tr":
                self.rows.append([])
            elif tag in ("td", "th"):
                self.cell = dict(attrs, text="")
                self.rows[-1].append(self.cell)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data
        for _, captured in self.stack:
            if captured is not None:
                captured["text"] += data

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.cell = None
        elif tag == "table":
            self.active = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def check_geometry(self):
        occupied = set()
        for row_index, cells in enumerate(self.rows):
            column = 0
            for cell in cells:
                while (row_index, column) in occupied:
                    column += 1
                width = int(cell.get("colspan", 1))
                height = int(cell.get("rowspan", 1))
                for row in range(row_index, row_index + height):
                    for col in range(column, column + width):
                        assert (row, col) not in occupied, (row, col)
                        occupied.add((row, col))
                column += width
        widths = [
            sum(row == row_index for row, _ in occupied)
            for row_index in range(len(self.rows))
        ]
        assert widths and len(set(widths)) == 1, widths


def report_generator():
    spec = importlib.util.spec_from_file_location(
        "offline_report", SKILL / "gen_ttft_report.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    return generator


def check_eight_tp_ordering(tmp: Path) -> None:
    """Worker prefixes remain authoritative when eight TPs arrive out of order."""
    generator = report_generator()
    log = tmp / "eight-tp.log"
    order = (6, 7, 4, 5, 3, 1, 2, 0)
    lines = [
        "[TTFT_DEBUG][EngineCore] execution_id=e0 request_ids=synthetic "
        "prefill_request_ids=synthetic num_total=1 num_tokens=32 "
        "load=0.05 forward=0.125"
    ]
    pids = {rank: 500 + rank * 10 for rank in range(8)}
    for rank in order:
        pid = pids[rank]
        lines.append(
            f"(Worker_DP0_TP{rank}_EP{rank} pid={pid}) "
            "[TTFT_DEBUG][AscendStoreSave] "
            f"req_id=synthetic execution_id=e0 tp_rank={rank} pid={pid} "
            f"wait_for_save={rank + 1}.001 enqueue=0.001 join={rank + 1}.001"
        )
    lines.append(
        "[TTFT][BREAKDOWN] req=synthetic steps=1 ttft=1 forward=0.2 "
        "save_kv=0.3 residual=0.5"
    )
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    data = generator.parse(log)
    request, breakdown = generator.choose_request(data, None)
    warnings = []
    steps, end = generator.select_steps(data, request, breakdown, "prefill", warnings)
    rows, workers, _ = generator.save_rows(
        data, request, end, steps, None, warnings
    )
    assert workers == [(rank, pids[rank]) for rank in range(8)]
    assert [row["worker"][0] for row in rows[0]["ranks"]] == list(range(8))
    assert [row["worker"][1] for row in rows[0]["ranks"]] == [
        pids[rank] for rank in range(8)
    ]
    assert [round(row["wait_for_save"], 3) for row in rows[0]["ranks"]] == [
        round(rank + 1.001, 3) for rank in range(8)
    ]


def check_save_waits(tmp: Path, produced: list[str]) -> None:
    """Real producer output stays per execution/TP, separate from task elapsed."""
    generator = report_generator()
    log, out = tmp / "outer-waits.log", tmp / "outer-waits.html"

    task = (
        "[TTFT_DEBUG][AscendStoreSave] req_id=synthetic execution_id=e0 "
        "task_id=t{rank} tp_rank={rank} pid={pid} keys=2 bytes=1048576 "
        "queue_wait=0.01 build_key=0.02 lookup=0.03 prepare=0.04 sync=0.05 "
        "put={put}\n"
    )
    engine = (
        "[TTFT_DEBUG][EngineCore] execution_id={execution} request_ids=synthetic "
        "prefill_request_ids=synthetic num_total=1 num_tokens=32 "
        "load=0.05 forward=0.125\n"
    )
    breakdown = (
        "[TTFT][BREAKDOWN] req=synthetic steps=2 ttft=1 forward=0.2 "
        "save_kv=0.3 residual=0.5\n"
    )
    text = (
        engine.format(execution="e0")
        + "\n".join(produced[:2]) + "\n"
        + task.format(rank=0, pid=os.getpid(), put=0.125)
        + task.format(rank=1, pid=os.getpid(), put=0.25)
        + engine.format(execution="e1") + produced[2] + "\n" + breakdown
    )

    def model(content):
        log.write_text(content, encoding="utf-8")
        data = generator.parse(log)
        request, bd = generator.choose_request(data, None)
        warnings = []
        steps, end = generator.select_steps(data, request, bd, "prefill", warnings)
        result = generator.save_rows(data, request, end, steps, None, warnings)
        assert len(result) == 3
        return data, request, bd, steps, result, warnings

    data, request, bd, steps, result, _ = model(text)
    rows, workers, _ = result
    assert workers == [(0, os.getpid()), (1, os.getpid())]
    for rank, row in enumerate(rows[0]["ranks"]):
        boundary = data["wfs"][rank]
        assert generator.ids(boundary) == {request}
        assert generator.execution(boundary) == "e0"
        assert boundary["_worker"] == workers[rank]
        for field in ("wait_for_save", "enqueue", "join"):
            assert row[field] == float(boundary[field])
        assert row["boundary_sources"] == [boundary]
        assert row["put"] == (0.125, 0.25)[rank]
        assert row["values"] == [0.01, 0.02, 0.03, 0.04, 0.05]
    assert rows[0]["ranks"][0]["wait_for_save"] != rows[0]["ranks"][1]["wait_for_save"]
    assert rows[1]["ranks"][0]["join"] is None
    assert rows[1]["ranks"][1]["join"] == 0
    assert rows[1]["ranks"][1]["empty"]
    generator.render(data, out)
    page = ReportPage(out.read_text(encoding="utf-8"))
    page.check_geometry()
    for field in ("wait_for_save", "enqueue", "join"):
        cells = [
            cell["text"].strip() for row in page.rows for cell in row
            if cell.get("data-field") == field
        ]
        assert cells == [
            generator.fmt(rank[field]) for row in rows for rank in row["ranks"]
        ]
    totals = page.rows[1]
    generator.render(data, out, tp_rank=1)
    filtered = ReportPage(out.read_text(encoding="utf-8"))
    filtered.check_geometry()
    assert filtered.rows[1] == totals

    # Exact zero is retained; absence and duplicate/conflicting boundaries are
    # missing, never summed or copied from another TP, request or execution.
    primary = produced[0]
    for extra in (
        primary,
        primary.replace("join=", "join=9 ignored_join="),
        primary.replace("req_ids=['synthetic']", "req_ids=['synthetic', 'other']"),
    ):
        _, _, _, _, changed, warnings = model(text.replace(breakdown, extra + "\n" + breakdown))
        assert changed[0][0]["ranks"][0]["wait_for_save"] is None
        assert not changed[0][0]["ranks"][0]["boundary_sources"]
        assert changed[0][0]["ranks"][0]["boundary_reason"] and warnings
        assert changed[0][0]["ranks"][1]["wait_for_save"] == rows[0]["ranks"][1]["wait_for_save"]
    for extra in (
        primary.replace("req_ids=['synthetic']", "req_ids=['other']"),
        primary.replace("execution_id=e0", "execution_id=foreign"),
    ):
        _, _, _, _, changed, _ = model(text.replace(breakdown, extra + "\n" + breakdown))
        assert changed[0] == rows
    missing_join = primary.replace(f" join={data['wfs'][0]['join']}", "")
    _, _, _, _, changed, _ = model(text.replace(primary, missing_join))
    assert changed[0][0]["ranks"][0]["join"] is None
    assert changed[0][0]["ranks"][0]["wait_for_save"] == rows[0]["ranks"][0]["wait_for_save"]
    assert changed[0][0]["ranks"][0]["boundary_sources"]
    explicit = "(Worker pid=999 rank_DP0_TP7) " + primary
    explicit_data, _, _, _, changed, _ = model(text.replace(primary, explicit))
    assert explicit_data["wfs"][0]["_worker"] == workers[0]
    assert changed[0][0]["ranks"][0]["wait_for_save"] == rows[0]["ranks"][0]["wait_for_save"]
    zero = primary
    for field in ("wait_for_save", "enqueue", "join"):
        zero = zero.replace(
            f"{field}={data['wfs'][0][field]}", f"{field}=0.000000"
        )
    _, _, _, _, changed, _ = model(text.replace(primary, zero))
    assert all(changed[0][0]["ranks"][0][field] == 0 for field in ("wait_for_save", "enqueue", "join"))
    legacy = primary.replace(" execution_id=e0", "")
    legacy = f"(Worker pid={os.getpid()} rank_DP0_TP0) " + legacy.replace(
        f" tp_rank=0 pid={os.getpid()}", ""
    )
    legacy_data, _, _, _, changed, _ = model(text.replace(primary, legacy))
    assert changed[0][0]["ranks"][0]["wait_for_save"] is None
    assert legacy_data["wfs"][0]["_worker"] == workers[0]
    generator.render(legacy_data, out)
    unbound = ReportPage(out.read_text(encoding="utf-8"), table_id="unbound-waits")
    assert len(unbound.rows) == 2
    assert [
        cell["text"].strip() for cell in unbound.rows[1]
        if cell.get("data-field") in ("wait_for_save", "enqueue", "join")
    ] == [data["wfs"][0][field] for field in ("wait_for_save", "enqueue", "join")]


def check_tokenizer_selection(tmp: Path) -> None:
    generator = report_generator()
    log, out = tmp / "tokenizer.log", tmp / "tokenizer.html"
    request, alias = "synthetic-1234abcd", "synthetic"
    base = (tmp / "synthetic-python-only.log").read_text(encoding="utf-8").replace(
        "synthetic", request
    )
    summary = (
        f"[TTFT_DEBUG][Tokenizer] request_id={alias} purpose=business "
        "n=1 render=0.02 tokenize=0.03\n"
    )
    spans = [
        f'[TTFT_DIAG] event={name} request_id="{alias}" purpose="business" n=1 '
        f"start_ns=100000000 end_ns={100000000 + duration} duration_ns={duration}\n"
        for name, duration in (("render", 20000000), ("tokenize", 30000000))
    ]
    renderer_summary = (
        f"[TTFT_DEBUG][Tokenizer] render=0.02 tokenize=0.03 n=1 "
        f"request_id=\"{alias}\" purpose=\"business\" "
        "render_start_ns=100000000 render_end_ns=120000000 "
        "tokenize_start_ns=120000000 tokenize_end_ns=150000000\n"
    )

    def select(records):
        log.write_text("".join(records) + base, encoding="utf-8")
        data = generator.parse(log)
        end = data["bd"][0]["_line"]
        warnings = []
        tok = generator.tokenizer_for_request(data, request, end, warnings)
        return data, tok, warnings

    for records in ([summary], spans, [summary, *spans]):
        data, tok, _ = select(records)
        generator.render(data, out)
        page = ReportPage(out.read_text(encoding="utf-8"))
        selected = [option for option in page.options if "selected" in option]
        assert len(selected) == 1 and selected[0]["value"] == "auto"
        assert selected[0]["data-manual"] == "false"
        assert selected[0]["data-render"] == "0.020000"
        assert selected[0]["data-tokenize"] == "0.030000"
        assert selected[0]["data-status"] == page.named["tokenizer-status"]["text"]
        assert page.named["render-value"]["text"] == "0.020000"
        assert page.named["tokenizer-value"]["text"] == "0.030000"
        assert page.named["remaining-value"]["text"].strip() == "0.100000 s"
        assert selected[0]["data-remaining"] == "0.100000"
    mutations = (
        ("n=1", "n=-1"), ("n=1", "n=0"), ("n=1", "n=1.5"),
        ("n=1", "n=true"), ("n=1", ""),
        ("purpose=business", "purpose=warmup"),
        ("purpose=business", "purpose=unscoped"),
        ("purpose=business", ""),
        (f'request_id="{alias}"', 'request_id="other"'),
        (f'request_id="{alias}"', f'request_ids="{alias},other"'),
        (f'request_id="{alias}"', ""),
    )
    for old, new in mutations:
        for records in ([summary], spans):
            mutation_old, mutation_new = old, new
            if records is spans and old == "purpose=business":
                mutation_old, mutation_new = 'purpose="business"', 'purpose="warmup"'
            if old.startswith('request_id="'):
                if records == [summary]:
                    mutation_old = old.replace('request_id="', "request_id=").replace('"', "")
                    mutation_new = new.replace('request_id="', "request_id=").replace('"', "")
                else:
                    mutation_old, mutation_new = old, new
            data, tok, warnings = select([r.replace(mutation_old, mutation_new) for r in records])
            assert not tok and warnings, (old, new, records)
            candidates = data["tok"] or data["diag"]
            assert all(
                generator.tokenizer_reasons(
                    r, request, data["bd"][0]["_line"], span=not data["tok"]
                ) for r in candidates
            )
    for invalid in (
        summary.replace("render=0.02", "render=-0.02"),
        summary.replace("tokenize=0.03", "tokenize=nan"),
        summary.replace("tokenize=0.03", "tokenize=inf"),
        spans[0].replace("end_ns=120000000", "end_ns=90000000"),
        spans[0].replace("duration_ns=20000000", "duration_ns=1"),
        spans[0].replace("start_ns=100000000", "start_ns=-1"),
    ):
        _, tok, warnings = select([invalid])
        assert not tok and warnings
    _, tok, warnings = select([summary, summary])
    assert not tok and warnings
    _, tok, warnings = select([*spans, *spans])
    assert not tok and warnings
    data, tok, warnings = select([*spans, spans[0]])
    assert "render" not in tok and tok["tokenize"] == 0.03 and warnings
    generator.render(data, out)
    partial = ReportPage(out.read_text(encoding="utf-8"))
    assert partial.named["render-value"]["text"] == "—"
    assert partial.named["remaining-value"]["text"].strip() == "— s"
    legacy = summary.replace(f"request_id={alias} purpose=business ", "")
    data, tok, _ = select([legacy])
    assert not tok
    generator.render(data, out, tokenizer_line=data["tok"][0]["_line"])
    manual = ReportPage(out.read_text(encoding="utf-8"))
    selected = next(option for option in manual.options if "selected" in option)
    assert selected["data-manual"] == "true"
    assert manual.named["render-value"]["text"] == "0.020000"
    assert manual.named["tokenizer-value"]["text"] == "0.030000"
    assert manual.named["remaining-value"]["text"].strip() == "— s"
    assert selected["data-remaining"] == "—"
    print("PASS tokenizer strict eligibility, suffix alias, static auto/partial/manual values and residual")


def check_forward_identity_and_missing(tmp: Path) -> None:
    """Legacy diagnostics cannot invent TP rows or overwrite measured totals."""

    log = tmp / "forward.log"
    out = tmp / "forward.html"
    base = (tmp / "synthetic-python-only.log").read_text(encoding="utf-8")
    base = base.replace("num_tokens=32", "num_tokens=32 forward=0.125", 1)
    legacy = (
        "[TTFT_DIAG] event=forward_breakdown execution_id=e0 "
        "request_ids=synthetic tp_rank=7 pid=777 forward=999 model_run=999\n"
        "[TTFT_DIAG] event=forward_breakdown execution_id=e0 "
        "request_ids=synthetic tp_rank=0 pid=999 forward=999 model_call=999\n"
    )

    def report(text, *args):
        log.write_text(text, encoding="utf-8")
        result = run(tmp, "report", str(log), "-o", str(out), *args)
        assert result.returncode == 0, (result.stdout, result.stderr)
        page = ReportPage(out.read_text(encoding="utf-8"))
        page.check_geometry()
        return page.rows

    def request_forward(rows):
        return next(
            cell["text"].split()[0]
            for cell in rows[1] if "BREAKDOWN.forward" in cell["text"]
        )

    expected = report(base)
    forward_index = next(
        i for i, cell in enumerate(expected[2])
        if cell["text"].startswith("forward")
    )
    assert request_forward(expected) == "0.200000"
    assert expected[3][forward_index]["text"] == "0.125000"
    assert expected[4][forward_index]["text"] == "—"
    assert sum(cell["text"].startswith("forward") for cell in expected[2]) == 1
    contaminated = base.replace("[TTFT][BREAKDOWN]", legacy * 2 + "[TTFT][BREAKDOWN]")
    assert report(contaminated) == expected
    assert report(contaminated, "--tp-rank", "0") == expected
    refused = run(tmp, "report", str(log), "-o", str(out), "--tp-rank", "7")
    assert refused.returncode != 0, (refused.stdout, refused.stderr)
    zero = report(base.replace("forward=0.125", "forward=0"))
    assert zero[3][forward_index]["text"] == "0.000000"
    missing = report(base.replace("forward=0.125", "").replace("forward=0.2", ""))
    assert missing[3][forward_index]["text"] == "—"
    assert request_forward(missing) == "—"
    zero_total = report(base.replace("forward=0.2", "forward=0"))
    assert request_forward(zero_total) == "0.000000"
    print("PASS forward totals/missing/zero; legacy diagnostics cannot invent TP rows")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ttft-skill-smoke-") as tmp:
        tmp = Path(tmp)
        make_env(tmp / "a", "vllm")
        env_b = make_env(
            tmp / "b",
            "vllm",
            (
                "vllm/v1/metrics/stats.py",
                lambda t: t.replace(
                    "class RequestStateStats:",
                    "class RequestStateStats:\n    # local unrelated edit\n",
                    1,
                ),
            ),
        )
        make_env(
            tmp / "c",
            "vllm",
            (
                "vllm/v1/metrics/stats.py",
                lambda t: t.replace(
                    "from vllm.logger import logger",
                    "from vllm.logger import logger  # moved",
                    1,
                ),
            ),
        )
        make_env(tmp / "d", "vllm")
        # Existing state must survive refusal without any source writes.
        occupied = tmp / "occupied"
        occupied.mkdir()
        state_file = occupied / "state.json"
        state_file.write_bytes(b'{"existing": "rollback evidence"}')
        for repo in ("vllm", "ascend"):
            env = make_env(tmp / f"guard-{repo}", repo)
            before = {
                p.relative_to(env): p.read_bytes()
                for p in env.rglob("*")
                if p.is_file()
            }
            inspected = run(env, "inspect", "--python", PY)
            assert inspected.returncode == 0, inspected.stderr
            planned = run(env, "plan", "--python", PY, "--repo", repo)
            assert planned.returncode == 0, planned.stderr
            r = run(
                env,
                "apply",
                "--python",
                PY,
                "--repo",
                repo,
                "--state-dir",
                str(occupied),
            )
            assert r.returncode == 1 and "回滚状态已存在" in r.stderr, (
                r.stdout,
                r.stderr,
            )
            assert state_file.read_bytes() == b'{"existing": "rollback evidence"}'
            assert before == {
                p.relative_to(env): p.read_bytes()
                for p in env.rglob("*")
                if p.is_file()
            }
            fresh = tmp / f"fresh-{repo}"
            r = run(
                env, "apply", "--python", PY, "--repo", repo, "--state-dir", str(fresh)
            )
            assert r.returncode == 0, r.stderr
            for entry in PROFILES[repo]["repos"][repo]:
                assert (env / entry["path"]).read_bytes() == entry["after"].encode(
                    "utf-8"
                )
            r = run(env, "rollback", "--state-file", str(fresh / "state.json"))
            assert r.returncode == 0, r.stderr
            assert before == {
                p.relative_to(env): p.read_bytes()
                for p in env.rglob("*")
                if p.is_file()
            }
        print(
            "PASS existing state refused unchanged; independent core states apply/rollback"
        )

        # b: far-away local edit must survive instrumentation.
        r = run(
            env_b,
            "apply",
            "--python",
            PY,
            "--repo",
            "vllm",
            "--state-dir",
            str(tmp / "state-b"),
        )
        stats_b = (env_b / "vllm/v1/metrics/stats.py").read_text(encoding="utf-8")
        assert "# local unrelated edit" in stats_b
        assert "[TTFT][BREAKDOWN]" in stats_b
        assert "from vllm.logger import logger" in stats_b

        r = run(env_b, "plan", "--python", PY, "--repo", "vllm")
        # d: pristine tree also applies; a: report idempotence on b; c: refuse.
        r = run(
            tmp / "d" / "site",
            "apply",
            "--python",
            PY,
            "--repo",
            "vllm",
            "--state-dir",
            str(tmp / "state-d"),
        )
        assert r.returncode == 0, r.stderr
        r = run(
            env_b,
            "apply",
            "--python",
            PY,
            "--repo",
            "vllm",
            "--state-dir",
            str(tmp / "state-b2"),
        )
        assert r.returncode == 0 and "已包含打点" in r.stdout, (r.stdout, r.stderr)
        # new-file entries (ttft_diagnostics.py) created on b, removed on rollback
        r = run(env_b, "rollback", "--state-file", str(tmp / "state-b" / "state.json"))
        assert r.returncode == 0, r.stderr
        assert not (env_b / "vllm/v1/metrics/ttft_diagnostics.py").exists()
        restored = (env_b / "vllm/v1/metrics/stats.py").read_text(encoding="utf-8")
        assert "# local unrelated edit" in restored
        # Ascend side: same engine against the ascend profile.
        env_e = make_env(tmp / "e", "ascend")
        original_e = {
            p.relative_to(env_e): p.read_bytes()
            for p in env_e.rglob("*")
            if p.is_file()
        }
        r = run(env_e, "plan", "--python", PY, "--repo", "ascend")
        assert r.returncode == 0, r.stderr
        assert "文件 7 个，其中 0 个已包含打点" in r.stdout, r.stdout
        assert "不以 Git HEAD 为门禁" in r.stdout, r.stdout
        assert "不是版本/HEAD 认证或真实服务验证" in r.stdout, r.stdout
        assert original_e == {
            p.relative_to(env_e): p.read_bytes()
            for p in env_e.rglob("*")
            if p.is_file()
        }
        r = run(
            env_e,
            "apply",
            "--python",
            PY,
            "--repo",
            "ascend",
            "--state-dir",
            str(tmp / "state-e"),
        )
        assert r.returncode == 0, r.stderr
        r = run(
            env_e,
            "apply",
            "--python",
            PY,
            "--repo",
            "ascend",
            "--state-dir",
            str(tmp / "state-e-noop"),
        )
        assert r.returncode == 0 and "无需修改" in r.stdout, (r.stdout, r.stderr)
        assert not (tmp / "state-e-noop" / "state.json").exists()
        r = run(env_e, "rollback", "--state-file", str(tmp / "state-e" / "state.json"))
        assert r.returncode == 0, r.stderr
        kv = (
            env_e / "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/"
            "kv_transfer.py"
        ).read_text(encoding="utf-8")
        assert "[TTFT_DEBUG][AscendStoreSave]" not in kv
        print("PASS ascend apply/rollback")
        assert original_e == {
            p.relative_to(env_e): p.read_bytes()
            for p in env_e.rglob("*")
            if p.is_file()
        }

        # A successful plan is not a waiver: apply checks content again.
        # These payload fixtures have no Git metadata and do not certify any
        # alternate server HEAD, including 3b3188623.
        env_conflict = make_env(tmp / "ascend-conflict", "ascend")
        r = run(env_conflict, "plan", "--python", PY, "--repo", "ascend")
        assert r.returncode == 0, r.stderr
        ts = __import__("ttft_skill")
        entry = next(
            e
            for e in PROFILES["ascend"]["repos"]["ascend"]
            if e["path"].endswith("/kv_transfer.py")
        )
        hunk = next(
            h
            for h in ts.diff_hunks(entry["before"], entry["after"])
            if h["context"] or h["old"]
        )
        target = env_conflict / entry["path"]
        changed = entry["before"].replace(
            hunk["context"] + hunk["old"], "# conflicting local edit\n", 1
        )
        assert changed != entry["before"]
        target.write_text(changed, encoding="utf-8", newline="\n")
        conflict_bytes = {
            p.relative_to(env_conflict): p.read_bytes()
            for p in env_conflict.rglob("*")
            if p.is_file()
        }
        conflict_state = tmp / "state-ascend-conflict"
        for command in ("plan", "apply"):
            extra = ("--state-dir", str(conflict_state)) if command == "apply" else ()
            r = run(env_conflict, command, "--python", PY, "--repo", "ascend", *extra)
            assert r.returncode == 1 and "拒绝执行" in r.stderr, (r.stdout, r.stderr)
            assert conflict_bytes == {
                p.relative_to(env_conflict): p.read_bytes()
                for p in env_conflict.rglob("*")
                if p.is_file()
            }
            assert not (conflict_state / "state.json").exists()
        print(
            "PASS ascend content-only plan, recheck refusal, idempotence and exact rollback"
        )

        # Byte-identical guarantee for pristine trees: the payload itself is
        # the oracle (portable — no git repo needed on the host).
        import json as _json

        ts = __import__("ttft_skill")
        manifest = _json.loads(
            (SKILL.parent / "payload" / "vllm-ascend-timing.json").read_text(
                encoding="utf-8"
            )
        )
        for repo in ("vllm", "ascend"):
            profile = next(p for p in manifest["profiles"] if repo in p["repos"])
            for entry in profile["repos"][repo]:
                if entry["before"] is None:
                    continue
                before, after = entry["before"], entry["after"]
                assert ts.instrument_exact(before, after) == after, entry["path"]
                starts = [h["before_start"] for h in ts.diff_hunks(before, after)]
                nlines = before.count("\n")
                for pos in (5, nlines // 2, nlines - 5):
                    if pos <= 0 or any(abs(pos - s) <= 10 for s in starts):
                        continue
                    cur = before.splitlines(keepends=True)
                    cur.insert(pos, "# local edit\n")
                    result = ts.apply_mapped("".join(cur), before, after)
                    stripped = "".join(
                        l
                        for l in result.splitlines(keepends=True)
                        if l != "# local edit\n"
                    )
                    assert stripped == after and result.count("# local edit\n") == 1, (
                        entry["path"],
                        pos,
                    )
                for h in ts.diff_hunks(before, after):
                    if not h["old"]:
                        continue
                    cur = before.splitlines(keepends=True)
                    cur.insert(h["before_start"], "# local edit\n")
                    try:
                        ts.apply_mapped("".join(cur), before, after)
                    except ts.SkillError:
                        break
                    raise AssertionError(
                        f"{entry['path']}: hunk-region edit not refused"
                    )
        print("PASS pristine reconstruction byte-identical (21 files)")
        print("PASS drifted mapping (edits preserved; hunk-region edits refused)")

        produced_waits = check_python_payload()
        check_import_isolation(tmp)
        check_python_report(tmp)
        check_forward_identity_and_missing(tmp)
        check_save_waits(tmp, produced_waits)
        check_eight_tp_ordering(tmp)
        check_tokenizer_selection(tmp)
        print(
            "PASS apply/idempotence/refusal/rollback; local edit preserved; new file removed"
        )
        print(
            "LIMIT: offline fixture trees at pristine 0.27 baselines; no NPU/serving run"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
