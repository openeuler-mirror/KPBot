#!/usr/bin/env python3
"""Run a bounded synthetic producer benchmark against an existing Kafka."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import sys
import uuid

import kafka_support as support


REQUIRED_TOOLS = (
    "kafka-console-consumer.sh",
    "kafka-console-producer.sh",
    "kafka-consumer-groups.sh",
    "kafka-get-offsets.sh",
    "kafka-producer-perf-test.sh",
    "kafka-topics.sh",
)
TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,249}$")
PERF_PATTERN = re.compile(
    r"(?P<records>\d+) records sent,\s*"
    r"(?P<rps>\d+(?:\.\d+)?) records/sec\s*"
    r"\((?P<mbps>\d+(?:\.\d+)?) MB/sec\),\s*"
    r"(?P<average>\d+(?:\.\d+)?) ms avg latency,\s*"
    r"(?P<maximum>\d+(?:\.\d+)?) ms max latency,\s*"
    r"(?P<p50>\d+) ms 50th,\s*(?P<p95>\d+) ms 95th,\s*"
    r"(?P<p99>\d+) ms 99th,\s*(?P<p999>\d+) ms 99\.9th\."
)


class WorkloadError(RuntimeError):
    """A live workload or exact-validation step failed."""

    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


def bounded_int(minimum, maximum):
    def convert(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum}"
            )
        return number

    return convert


def bounded_seconds(value):
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(seconds) or not 0.1 <= seconds <= 3600:
        raise argparse.ArgumentTypeError(
            "must be finite and between 0.1 and 3600 seconds"
        )
    return seconds


def bounded_throughput(value):
    try:
        throughput = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(throughput) or not (
        0.01 <= throughput <= 1_000_000_000
    ):
        raise argparse.ArgumentTypeError(
            "must be finite and between 0.01 and 1000000000 records/s"
        )
    return throughput


def bootstrap_servers(value):
    if not value or any(char.isspace() for char in value) or "@" in value:
        raise argparse.ArgumentTypeError(
            "must be comma-separated host:port endpoints without user info"
        )
    for endpoint in value.split(","):
        host, separator, port = endpoint.rpartition(":")
        if not separator or not host or not port.isdigit():
            raise argparse.ArgumentTypeError(
                "each bootstrap endpoint must use host:port syntax"
            )
        if not 1 <= int(port) <= 65535:
            raise argparse.ArgumentTypeError("bootstrap port is out of range")
    return value


def topic_name(value):
    if not TOPIC_PATTERN.fullmatch(value) or value in (".", ".."):
        raise argparse.ArgumentTypeError("invalid Kafka topic name")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kafka-dir", type=Path, required=True)
    parser.add_argument(
        "--bootstrap",
        type=bootstrap_servers,
        default="localhost:9092",
    )
    parser.add_argument("--client-config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--topic",
        type=topic_name,
        help="empty dedicated existing topic; it is never deleted",
    )
    target.add_argument(
        "--allow-create-topic",
        action="store_true",
        help="authorize creation of one uniquely named owned test topic",
    )
    parser.add_argument(
        "--partitions",
        type=bounded_int(1, 128),
        default=1,
    )
    parser.add_argument(
        "--replication-factor",
        type=bounded_int(1, 16),
    )
    parser.add_argument(
        "--records",
        type=bounded_int(1, 1_000_000),
        default=100_000,
    )
    parser.add_argument(
        "--warmup-records",
        type=bounded_int(1, 1_000_000),
        default=10_000,
    )
    parser.add_argument(
        "--rounds",
        type=bounded_int(3, 10),
        default=3,
    )
    parser.add_argument(
        "--throughput",
        type=bounded_throughput,
        default=10_000.0,
        help="positive approximate producer limit in records/s",
    )
    parser.add_argument(
        "--compression",
        choices=("none", "gzip", "snappy", "lz4", "zstd"),
        default="none",
    )
    parser.add_argument(
        "--batch-size",
        type=bounded_int(1, 1_048_576),
        default=16_384,
    )
    parser.add_argument(
        "--linger-ms",
        type=bounded_int(0, 60_000),
        default=0,
    )
    parser.add_argument(
        "--timeout",
        type=bounded_seconds,
        default=120.0,
        help="hard timeout for every individual subprocess",
    )
    parser.add_argument(
        "--keep-topic",
        action="store_true",
        help="retain a successfully created owned topic for inspection",
    )
    return parser


def parse_perf_summary(text):
    """Parse the final native ProducerPerformance summary, not windows."""
    matches = list(PERF_PATTERN.finditer(text))
    if not matches:
        raise WorkloadError(
            "correctness",
            "native final producer summary is missing or unsupported",
        )
    values = matches[-1].groupdict()
    return {
        "records_sent": int(values["records"]),
        "records_per_second": float(values["rps"]),
        "megabytes_per_second": float(values["mbps"]),
        "average_latency_ms": float(values["average"]),
        "max_latency_ms": float(values["maximum"]),
        "p50_latency_ms": int(values["p50"]),
        "p95_latency_ms": int(values["p95"]),
        "p99_latency_ms": int(values["p99"]),
        "p99_9_latency_ms": int(values["p999"]),
        "throughput_unit_note": (
            "Kafka calls this MB/sec; its implementation divides by 1024^2, "
            "so the numeric unit is MiB/s"
        ),
    }


def parse_offsets(text, expected_topic):
    offsets = {}
    for line in text.splitlines():
        match = re.fullmatch(
            re.escape(expected_topic) + r":(\d+):(\d+)", line.strip()
        )
        if match:
            offsets[int(match.group(1))] = int(match.group(2))
    if not offsets:
        raise WorkloadError(
            "correctness",
            "no parseable latest offsets were returned for the owned topic",
        )
    return offsets


def monotonic_payload_bytes(records):
    """Return exact UTF-8 payload bytes for identities 0..records-1."""
    return sum(len(str(index)) for index in range(records))


def compare_identities(actual_lines, expected):
    """Compare complete produced/consumed identities, including duplicates."""
    actual = Counter(actual_lines)
    missing = expected - actual
    unexpected = actual - expected
    return {
        "exact": actual == expected,
        "expected_records": sum(expected.values()),
        "actual_records": sum(actual.values()),
        "expected_unique_identities": len(expected),
        "actual_unique_identities": len(actual),
        "missing_records": sum(missing.values()),
        "unexpected_records": sum(unexpected.values()),
        "missing_examples": list(missing.items())[:20],
        "unexpected_examples": list(unexpected.items())[:20],
        "comparison": (
            "exact cumulative multiset: every correctness/perf payload, "
            "including duplicate identities across distinct perf phases"
        ),
    }


def median(values):
    return statistics.median(values) if values else None


def aggregate_rounds(rounds):
    """Aggregate timed rounds only; warmup is intentionally absent."""
    native = [item["native"] for item in rounds]
    return {
        "timed_round_count": len(rounds),
        "timed_records_sent": sum(item["records_sent"] for item in native),
        "timed_payload_bytes": sum(
            monotonic_payload_bytes(item["requested_records"])
            for item in rounds
        ),
        "records_per_second_median": median(
            [item["records_per_second"] for item in native]
        ),
        "mebibytes_per_second_median": median(
            [item["megabytes_per_second"] for item in native]
        ),
        "average_latency_ms_median_of_round_averages": median(
            [item["average_latency_ms"] for item in native]
        ),
        "p95_latency_ms_median_of_round_percentiles": median(
            [item["p95_latency_ms"] for item in native]
        ),
        "p99_latency_ms_median_of_round_percentiles": median(
            [item["p99_latency_ms"] for item in native]
        ),
        "process_wall_duration_ms_median": median(
            [item["process_wall_duration_ms"] for item in rounds]
        ),
        "aggregation_scope": (
            "Medians are across independent native timed rounds. Percentiles "
            "are not pooled and warmup/correctness records are excluded."
        ),
    }


def initial_result(args):
    run_id = uuid.uuid4().hex
    topic = args.topic or "kpbot-bench-" + run_id
    group = "kpbot-bench-group-" + uuid.uuid4().hex
    return {
        "schema_version": "kafka-producer-benchmark/v1",
        "status": "running",
        "workload_status": "not_started",
        "run_id": run_id,
        "scope": {
            "workload": "synthetic producer-only throughput/latency",
            "correctness": (
                "exact three-message console round trip, native counts and "
                "latest-offset deltas per phase, plus exact cumulative "
                "consumption of every produced payload identity"
            ),
            "not_measured": [
                "consumer throughput",
                "production health",
                "end-to-end application latency",
            ],
        },
        "target": {
            "bootstrap": args.bootstrap,
            "topic": topic,
            "topic_mode": "create_unique"
            if args.allow_create_topic
            else "existing_dedicated",
        },
        "workload": {
            "payload": (
                "official ProducerPerformance --payload-monotonic decimal "
                "identities starting at zero independently in each phase"
            ),
            "records_per_timed_round": args.records,
            "warmup_records": args.warmup_records,
            "timed_rounds": args.rounds,
            "throughput_limit_records_per_second": args.throughput,
            "acks": "all",
            "compression": args.compression,
            "batch_size_bytes": args.batch_size,
            "linger_ms": args.linger_ms,
            "producer_count_per_round": 1,
            "per_process_timeout_seconds": args.timeout,
        },
        "exact_versions": {"kafka_cli": None, "java": None},
        "preflight": [],
        "commands": [],
        "ownership": {
            "topic": topic,
            "topic_created_by_this_run": False,
            "topic_create_state": "not_requested"
            if args.allow_create_topic
            else "not_applicable",
            "consumer_group": group,
            "consumer_groups": [group],
            "consumer_groups_started": [],
            "consumer_started": False,
        },
        "correctness": {
            "exact_message_round_trip": False,
            "expected_messages": 3,
            "actual_messages": None,
        },
        "warmup": None,
        "rounds": [],
        "aggregates": None,
        "cleanup": {
            "status": "not_requested",
            "topic": "not_applicable" if args.topic else "not_requested",
            "consumer_groups": {},
            "issues": [],
        },
        "limitations": [
            "Rate-limited native throughput is not the broker capacity limit.",
            "Every produced payload is consumed and compared by identity; "
            "correctness checks are outside native producer timing windows.",
            "An existing topic remains in place with all benchmark records.",
            "Native and process wall timings have different scopes; both are "
            "retained per phase instead of being interchanged.",
        ],
    }


class BenchmarkRun:
    def __init__(self, args, client_config):
        self.args = args
        self.config = client_config
        self.result = initial_result(args)
        self.result["target"]["client_config"] = client_config.evidence()
        self.expected_identities = Counter()

    def write(self):
        (self.args.output / "result.json").write_text(
            json.dumps(self.result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def record_command(self, stem, tool, command):
        evidence = {
            "id": stem,
            "tool": tool,
            **support.public_command(command),
            "error_category": support.error_category(command),
            **support.write_command_logs(self.args.output, stem, command),
        }
        self.result["commands"].append(evidence)
        self.write()

    def execute(
        self,
        stem,
        tool,
        parameters,
        config_flag=None,
        input_text=None,
    ):
        try:
            command = support.run_cli(
                self.args.kafka_dir,
                tool,
                parameters,
                self.args.timeout,
                self.config,
                config_flag,
                input_text,
            )
        except KeyboardInterrupt as exc:
            command = getattr(exc, "process_result", None)
            if command is not None:
                self.record_command(stem, tool, command)
            raise
        self.record_command(stem, tool, command)
        return command

    def require_success(self, command, phase):
        category = support.error_category(command)
        if category:
            raise WorkloadError(
                category,
                f"{phase} failed with category {category}; native logs kept",
            )

    def read_offsets(self, stem):
        command = self.execute(
            stem,
            "kafka-get-offsets.sh",
            [
                "--bootstrap-server",
                self.args.bootstrap,
                "--topic",
                self.result["target"]["topic"],
                "--time",
                "latest",
            ],
            "--command-config",
        )
        self.require_success(command, stem)
        return parse_offsets(command["stdout"], self.result["target"]["topic"])

    def prepare_existing_topic(self):
        command = self.execute(
            "preflight-topic-describe",
            "kafka-topics.sh",
            [
                "--bootstrap-server",
                self.args.bootstrap,
                "--describe",
                "--topic",
                self.args.topic,
            ],
            "--command-config",
        )
        category = support.error_category(command)
        if category:
            return support.probe(
                "existing_topic",
                True,
                "blocked",
                category,
                {
                    "command_id": "preflight-topic-describe",
                    "diagnostic": command["stderr"][-1000:],
                },
            )
        offsets_command = self.execute(
            "preflight-topic-empty",
            "kafka-get-offsets.sh",
            [
                "--bootstrap-server",
                self.args.bootstrap,
                "--topic",
                self.args.topic,
                "--time",
                "latest",
            ],
            "--command-config",
        )
        category = support.error_category(offsets_command)
        if category:
            return support.probe(
                "existing_topic_empty",
                True,
                "blocked",
                category,
                {"command_id": "preflight-topic-empty"},
            )
        try:
            offsets = parse_offsets(offsets_command["stdout"], self.args.topic)
        except WorkloadError as exc:
            return support.probe(
                "existing_topic_empty",
                True,
                "blocked",
                exc.category,
                {"error": str(exc)},
            )
        total = sum(offsets.values())
        return support.probe(
            "existing_topic_empty",
            True,
            "blocked" if total else "ready",
            "topic_not_empty" if total else "ok",
            {
                "command_id": "preflight-topic-empty",
                "latest_offsets": offsets,
                "latest_offset_sum": total,
            },
            "An empty dedicated topic is required for exact console "
            "round-trip validation; no existing records are deleted.",
        )

    def preflight(self):
        self.result["preflight"].append(
            support.probe(
                "secure_client_config",
                True,
                "ready",
                "ok",
                self.config.evidence(),
            )
        )
        self.result["preflight"].extend(
            support.base_probes(
                self.args.kafka_dir,
                self.args.bootstrap,
                self.args.timeout,
                self.config,
                REQUIRED_TOOLS,
            )
        )
        for item in self.result["preflight"]:
            if item["name"] == "cli_version":
                self.result["exact_versions"]["kafka_cli"] = item["evidence"][
                    "version"
                ]
            if item["name"] == "java_runtime":
                self.result["exact_versions"]["java"] = item["evidence"][
                    "version"
                ]
        phase_records = [self.args.warmup_records] + [
            self.args.records
        ] * self.args.rounds
        planned_bytes = sum(
            monotonic_payload_bytes(records) for records in phase_records
        )
        identity_records = sum(phase_records)
        cumulative_bytes = 0
        verification_bytes = 0
        for records in phase_records:
            cumulative_bytes += monotonic_payload_bytes(records)
            verification_bytes += cumulative_bytes
        self.required_result_bytes = verification_bytes + 16 * 1024**2
        longest_plan = max(
            self.args.records / self.args.throughput,
            self.args.warmup_records / self.args.throughput,
        )
        budget_category = "ok"
        if identity_records > 1_000_000 or planned_bytes > 100 * 1024**3:
            budget_category = "resource"
        elif longest_plan > self.args.timeout:
            budget_category = "invalid_config"
        self.result["preflight"].append(
            support.probe(
                "bounded_workload",
                True,
                "blocked" if budget_category != "ok" else "ready",
                budget_category,
                {
                    "planned_payload_bytes": planned_bytes,
                    "maximum_planned_payload_bytes": 100 * 1024**3,
                    "planned_perf_identities": identity_records,
                    "maximum_perf_identities": 1_000_000,
                    "cumulative_verification_payload_bytes": (
                        verification_bytes
                    ),
                    "longest_throttled_phase_seconds": longest_plan,
                    "per_process_timeout_seconds": self.args.timeout,
                },
                "Payload bytes are exact decimal identity bytes. Cumulative "
                "verification rereads all prior phases, so its larger log "
                "budget is explicit. Broker capacity needs separate approval.",
            )
        )
        required_free = max(16 * 1024**2, self.required_result_bytes)
        try:
            free = shutil.disk_usage(self.args.output).free
            category = "resource" if free < required_free else "ok"
            storage = support.probe(
                "output_storage",
                True,
                "blocked" if category != "ok" else "ready",
                category,
                {
                    "available_bytes": free,
                    "minimum_available_bytes": required_free,
                },
            )
        except OSError as exc:
            storage = support.probe(
                "output_storage",
                True,
                "blocked",
                "error",
                {"error": str(exc)},
            )
        self.result["preflight"].append(storage)
        self.write()
        if support.overall_status(self.result["preflight"]) != "ready":
            return False
        if self.args.topic:
            self.result["preflight"].append(self.prepare_existing_topic())
        self.write()
        return support.overall_status(self.result["preflight"]) == "ready"

    def create_topic(self):
        ownership = self.result["ownership"]
        ownership["topic_create_state"] = "rpc_outcome_unknown"
        self.write()
        parameters = [
            "--bootstrap-server",
            self.args.bootstrap,
            "--create",
            "--topic",
            self.result["target"]["topic"],
            "--partitions",
            str(self.args.partitions),
        ]
        if self.args.replication_factor is not None:
            parameters.extend(
                ("--replication-factor", str(self.args.replication_factor))
            )
        command = self.execute(
            "topic-create",
            "kafka-topics.sh",
            parameters,
            "--command-config",
        )
        category = support.error_category(command)
        if category:
            ownership["topic_create_state"] = "ambiguous"
            verification = self.execute(
                "topic-create-ambiguous-describe",
                "kafka-topics.sh",
                [
                    "--bootstrap-server",
                    self.args.bootstrap,
                    "--describe",
                    "--topic",
                    self.result["target"]["topic"],
                ],
                "--command-config",
            )
            ownership["ambiguous_topic_present"] = (
                True if support.error_category(verification) is None else None
            )
            self.result["cleanup"]["topic"] = (
                "retained_rpc_ambiguous_not_owned_for_certain"
            )
            raise WorkloadError(
                category,
                "topic creation outcome is ambiguous; exact name and "
                "read-only verification are preserved and no delete is sent",
            )
        ownership["topic_create_state"] = "created"
        ownership["topic_created_by_this_run"] = True
        self.write()

    def exact_round_trip(self):
        run_id = self.result["run_id"]
        messages = [
            f"kpbot-correctness-{run_id}-{index}" for index in range(3)
        ]
        producer = self.execute(
            "correctness-produce",
            "kafka-console-producer.sh",
            [
                "--bootstrap-server",
                self.args.bootstrap,
                "--topic",
                self.result["target"]["topic"],
                "--producer-property",
                "acks=all",
                "--producer-property",
                "max.block.ms=" + str(max(1, int(self.args.timeout * 1000))),
            ],
            "--producer.config",
            "\n".join(messages) + "\n",
        )
        self.require_success(producer, "exact correctness produce")
        ownership = self.result["ownership"]
        ownership["consumer_started"] = True
        ownership["consumer_groups_started"].append(
            ownership["consumer_group"]
        )
        self.write()
        consumer = self.execute(
            "correctness-consume",
            "kafka-console-consumer.sh",
            [
                "--bootstrap-server",
                self.args.bootstrap,
                "--topic",
                self.result["target"]["topic"],
                "--group",
                self.result["ownership"]["consumer_group"],
                "--from-beginning",
                "--max-messages",
                "3",
                "--timeout-ms",
                str(max(1, int(self.args.timeout * 1000))),
                "--consumer-property",
                "enable.auto.commit=true",
            ],
            "--consumer.config",
        )
        self.require_success(consumer, "exact correctness consume")
        actual = support.consumed_payload_lines(consumer["stdout"])
        exact = len(actual) == 3 and sorted(actual) == sorted(messages)
        self.result["correctness"].update(
            {
                "exact_message_round_trip": exact,
                "actual_messages": len(actual),
                "comparison": "exact count and order-independent payload set",
                "consumer_command_id": "correctness-consume",
            }
        )
        self.write()
        if not exact:
            raise WorkloadError(
                "correctness",
                "consumed payload count or content differs exactly",
            )
        self.expected_identities.update(messages)

    def register_consumer_group(self):
        group = "kpbot-bench-group-" + uuid.uuid4().hex
        ownership = self.result["ownership"]
        ownership["consumer_groups"].append(group)
        ownership["consumer_groups_started"].append(group)
        ownership["consumer_started"] = True
        self.write()
        return group

    def verify_all_identities(self, stem, records):
        self.expected_identities.update(str(index) for index in range(records))
        group = self.register_consumer_group()
        command = self.execute(
            stem + "-consume-identities",
            "kafka-console-consumer.sh",
            [
                "--bootstrap-server",
                self.args.bootstrap,
                "--topic",
                self.result["target"]["topic"],
                "--group",
                group,
                "--from-beginning",
                "--max-messages",
                str(sum(self.expected_identities.values())),
                "--timeout-ms",
                str(max(1, int(self.args.timeout * 1000))),
                "--consumer-property",
                "enable.auto.commit=true",
            ],
            "--consumer.config",
        )
        self.require_success(command, stem + " identity consumption")
        validation = compare_identities(
            support.consumed_payload_lines(command["stdout"]),
            self.expected_identities,
        )
        validation["consumer_group"] = group
        validation["consumer_command_id"] = stem + "-consume-identities"
        self.write()
        return validation

    def producer_phase(self, stem, records, phase):
        item = {
            "phase": phase,
            "status": "running",
            "requested_records": records,
            "exit_code": None,
            "timed_out": False,
            "error_category": None,
            "native": None,
            "process_wall_duration_ms": None,
            "offset_delta": None,
        }
        if phase == "warmup":
            self.result["warmup"] = item
        else:
            self.result["rounds"].append(item)
        self.write()
        before = self.read_offsets(stem + "-offsets-before")
        producer_properties = [
            "bootstrap.servers=" + self.args.bootstrap,
            "acks=all",
            "compression.type=" + self.args.compression,
            "batch.size=" + str(self.args.batch_size),
            "linger.ms=" + str(self.args.linger_ms),
            "max.block.ms=" + str(max(1, int(self.args.timeout * 1000))),
            "client.id=kpbot-" + self.result["run_id"] + "-" + stem,
        ]
        command = self.execute(
            stem,
            "kafka-producer-perf-test.sh",
            [
                "--topic",
                self.result["target"]["topic"],
                "--num-records",
                str(records),
                "--payload-monotonic",
                "--throughput",
                str(self.args.throughput),
                "--producer-props",
                *producer_properties,
            ],
            "--producer.config",
        )
        item["exit_code"] = command["returncode"]
        item["timed_out"] = command["timed_out"]
        item["error_category"] = support.error_category(command)
        item["process_wall_duration_ms"] = command["duration_ms"]
        item["producer_command_id"] = stem
        self.write()
        self.require_success(command, stem)
        item["native"] = parse_perf_summary(command["stdout"])
        after = self.read_offsets(stem + "-offsets-after")
        before_sum = sum(before.values())
        after_sum = sum(after.values())
        item["offsets_before"] = before
        item["offsets_after"] = after
        item["offset_delta"] = after_sum - before_sum
        exact_counts = (
            item["native"]["records_sent"] == records
            and item["offset_delta"] == records
        )
        item["exact_counts"] = exact_counts
        self.write()
        if not exact_counts:
            item["status"] = "failed"
            self.write()
            raise WorkloadError(
                "correctness",
                f"{stem} native count or offset delta differs from {records}",
            )
        item["identity_validation"] = self.verify_all_identities(stem, records)
        item["status"] = (
            "passed" if item["identity_validation"]["exact"] else "failed"
        )
        self.write()
        if not item["identity_validation"]["exact"]:
            raise WorkloadError(
                "correctness",
                f"{stem} consumed identities differ from produced payloads",
            )
        return item

    def final_offset_check(self):
        final = self.read_offsets("final-offsets")
        expected = (
            3 + self.args.warmup_records + self.args.records * self.args.rounds
        )
        actual = sum(final.values())
        self.result["final_offset_validation"] = {
            "expected_latest_offset_sum": expected,
            "actual_latest_offset_sum": actual,
            "latest_offsets": final,
            "exact": actual == expected,
        }
        self.write()
        if actual != expected:
            raise WorkloadError(
                "correctness",
                "final latest-offset sum differs from the exact workload "
                "total",
            )

    def cleanup(self):
        ownership = self.result["ownership"]
        self.result["cleanup"]["status"] = "running"
        for index, group in enumerate(ownership["consumer_groups_started"]):
            command = self.execute(
                f"cleanup-consumer-group-{index:03d}",
                "kafka-consumer-groups.sh",
                [
                    "--bootstrap-server",
                    self.args.bootstrap,
                    "--delete",
                    "--group",
                    group,
                ],
                "--command-config",
            )
            category = support.error_category(command)
            output = (command["stdout"] + command["stderr"]).lower()
            absent = "does not exist" in output or "not found" in output
            if category and not absent:
                message = (
                    "owned consumer group "
                    + group
                    + " cleanup failed: "
                    + category
                )
                self.result["cleanup"]["issues"].append(message)
                self.result["cleanup"]["consumer_groups"][group] = "failed"
            else:
                self.result["cleanup"]["consumer_groups"][group] = (
                    "already_absent" if absent else "deleted_exact_owned_group"
                )
        if (
            ownership["topic_created_by_this_run"]
            and ownership["topic_create_state"] == "created"
        ):
            if self.args.keep_topic:
                self.result["cleanup"]["topic"] = "retained_by_request"
            else:
                command = self.execute(
                    "cleanup-topic",
                    "kafka-topics.sh",
                    [
                        "--bootstrap-server",
                        self.args.bootstrap,
                        "--delete",
                        "--topic",
                        ownership["topic"],
                    ],
                    "--command-config",
                )
                category = support.error_category(command)
                if category:
                    self.result["cleanup"]["issues"].append(
                        "owned topic cleanup failed: " + category
                    )
                    self.result["cleanup"]["topic"] = "failed"
                else:
                    self.result["cleanup"]["topic"] = (
                        "deleted_exact_owned_topic"
                    )
        self.result["cleanup"]["status"] = (
            "failed" if self.result["cleanup"]["issues"] else "passed"
        )
        self.write()

    def start(self):
        if not self.preflight():
            self.result["status"] = "blocked"
            self.write()
            return 2
        exit_code = 0
        self.result["workload_status"] = "running"
        self.write()
        try:
            if self.args.allow_create_topic:
                self.create_topic()
            self.exact_round_trip()
            self.producer_phase("warmup-1", self.args.warmup_records, "warmup")
            for index in range(1, self.args.rounds + 1):
                self.producer_phase(
                    f"round-{index}", self.args.records, "timed"
                )
            self.final_offset_check()
            self.result["aggregates"] = aggregate_rounds(self.result["rounds"])
            self.result["workload_status"] = "passed"
            self.result["status"] = "passed"
        except WorkloadError as exc:
            self.result["status"] = "failed"
            self.result["workload_status"] = "failed"
            self.result["failure"] = {
                "category": exc.category,
                "message": str(exc),
            }
            exit_code = 1
        except KeyboardInterrupt:
            self.result["status"] = "failed"
            self.result["workload_status"] = "failed"
            self.result["failure"] = {
                "category": "interrupted",
                "message": "interrupted; bounded cleanup was attempted",
            }
            exit_code = 130
        except (OSError, ValueError, RuntimeError) as exc:
            self.result["status"] = "failed"
            self.result["workload_status"] = "failed"
            self.result["failure"] = {
                "category": "error",
                "message": support.redact(str(exc), self.config.redactions),
            }
            exit_code = 1
        finally:
            phases = [self.result["warmup"], *self.result["rounds"]]
            for item in phases:
                if item is not None and item["status"] == "running":
                    item["status"] = "failed"
            self.cleanup()
        if self.result["cleanup"]["issues"]:
            self.result["status"] = "failed"
            if exit_code == 0:
                exit_code = 1
        self.write()
        return exit_code


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "category": "output_collision"
                    if args.output.exists()
                    else "error",
                    "error": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 2
    try:
        config = support.load_client_config(args.client_config)
    except support.ConfigError as exc:
        result = initial_result(args)
        result["status"] = "blocked"
        result["preflight"] = [
            support.probe(
                "secure_client_config",
                True,
                "blocked",
                exc.category,
                {"error": str(exc), "property_values": "not_collected"},
            )
        ]
        (args.output / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, sort_keys=True))
        return 2
    benchmark = BenchmarkRun(args, config)
    benchmark.write()
    exit_code = benchmark.start()
    print(json.dumps(benchmark.result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
