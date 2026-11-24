#!/usr/bin/env python3
"""Read-only preflight for the bounded Kafka producer benchmark."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys

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


def monotonic_payload_bytes(records):
    return sum(len(str(index)) for index in range(records))


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
    target.add_argument("--topic", type=topic_name)
    target.add_argument("--allow-create-topic", action="store_true")
    parser.add_argument(
        "--operations-authorized",
        action="store_true",
        help=(
            "declare authorization for the planned write benchmark; this "
            "does not grant or verify broker ACLs"
        ),
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
    )
    parser.add_argument(
        "--timeout",
        type=bounded_seconds,
        default=120.0,
    )
    return parser


def write_result(output, result):
    (output / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def config_error_result(args, exc):
    result = {
        "schema_version": "kafka-environment-check/v1",
        "status": "blocked",
        "read_only": True,
        "probes": [
            support.probe(
                "secure_client_config",
                True,
                "blocked",
                exc.category,
                {"error": str(exc), "property_values": "not_collected"},
            )
        ],
        "limitations": [
            "No network probe was attempted after config validation failed."
        ],
    }
    write_result(args.output, result)
    return result


def topic_probe(args, client_config):
    if args.allow_create_topic:
        return support.probe(
            "topic_plan",
            True,
            "ready",
            "ok",
            {
                "mode": "create_unique_at_benchmark_time",
                "name_pattern": "kpbot-bench-<random-uuid>",
                "partitions": args.partitions,
                "replication_factor": args.replication_factor,
                "create_or_delete_executed": False,
            },
            (
                "Creation and deletion ACLs cannot be proven by a "
                "read-only probe."
            ),
        )
    result = support.run_cli(
        args.kafka_dir,
        "kafka-topics.sh",
        [
            "--bootstrap-server",
            args.bootstrap,
            "--describe",
            "--topic",
            args.topic,
        ],
        args.timeout,
        client_config,
        "--command-config",
    )
    category = support.error_category(result)
    partition_match = re.search(r"PartitionCount:\s*(\d+)", result["stdout"])
    replication_match = re.search(
        r"ReplicationFactor:\s*(\d+)", result["stdout"]
    )
    evidence = {
        "command": support.public_command(result),
        "topic": args.topic,
        "partition_count": int(partition_match.group(1))
        if partition_match
        else None,
        "replication_factor": int(replication_match.group(1))
        if replication_match
        else None,
        "diagnostic": result["stderr"][-1000:],
        "latest_offset_sum": None,
    }
    if category is None:
        offsets = support.run_cli(
            args.kafka_dir,
            "kafka-get-offsets.sh",
            [
                "--bootstrap-server",
                args.bootstrap,
                "--topic",
                args.topic,
                "--time",
                "latest",
            ],
            args.timeout,
            client_config,
            "--command-config",
        )
        category = support.error_category(offsets)
        evidence["offsets_command"] = support.public_command(offsets)
        evidence["offsets_diagnostic"] = offsets["stderr"][-1000:]
        latest = 0
        lines = 0
        if category is None:
            for line in offsets["stdout"].splitlines():
                match = re.fullmatch(
                    re.escape(args.topic) + r":(\d+):(\d+)", line.strip()
                )
                if match:
                    latest += int(match.group(2))
                    lines += 1
            if not lines:
                category = "error"
            elif latest:
                category = "topic_not_empty"
            evidence["latest_offset_sum"] = latest
    return support.probe(
        "topic_metadata",
        True,
        "blocked" if category else "ready",
        category or "ok",
        evidence,
        (
            "Describe access does not prove produce/consume ACLs. An "
            "existing topic must have a zero latest-offset sum so exact "
            "correctness messages cannot be mixed with business data."
        ),
    )


def resource_probes(args):
    phase_records = [args.warmup_records] + [args.records] * args.rounds
    payload_bytes = sum(
        monotonic_payload_bytes(records) for records in phase_records
    )
    identity_records = sum(phase_records)
    cumulative_bytes = 0
    verification_bytes = 0
    for records in phase_records:
        cumulative_bytes += monotonic_payload_bytes(records)
        verification_bytes += cumulative_bytes
    maximum_bytes = 100 * 1024**3
    longest_plan = max(
        args.records / args.throughput,
        args.warmup_records / args.throughput,
    )
    budget_category = "ok"
    if identity_records > 1_000_000 or payload_bytes > maximum_bytes:
        budget_category = "resource"
    elif longest_plan > args.timeout:
        budget_category = "invalid_config"
    budget = support.probe(
        "workload_budget",
        True,
        "blocked" if budget_category != "ok" else "ready",
        budget_category,
        {
            "timed_rounds": args.rounds,
            "records_per_timed_round": args.records,
            "warmup_records": args.warmup_records,
            "payload": "decimal monotonic identities per phase",
            "planned_payload_bytes": payload_bytes,
            "maximum_planned_payload_bytes": maximum_bytes,
            "planned_perf_identities": identity_records,
            "maximum_perf_identities": 1_000_000,
            "cumulative_verification_payload_bytes": verification_bytes,
            "throughput_limit_records_per_second": args.throughput,
            "per_process_timeout_seconds": args.timeout,
            "estimated_throttled_seconds_per_round": (
                args.records / args.throughput
            ),
        },
        (
            "Payload bytes exclude replication and retention; cumulative "
            "verification rereads prior phases and needs separate log space."
        ),
    )
    required_free = max(16 * 1024**2, verification_bytes + 16 * 1024**2)
    try:
        free = shutil.disk_usage(args.output).free
        category = "resource" if free < required_free else "ok"
        storage = support.probe(
            "output_storage",
            True,
            "blocked" if category != "ok" else "ready",
            category,
            {
                "available_bytes": free,
                "minimum_available_bytes": required_free,
                "scope": "local result logs only",
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

    memory = None
    try:
        for line in (
            Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        ):
            if line.startswith("MemAvailable:"):
                memory = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        memory = None
    host = support.probe(
        "host_resource_evidence",
        False,
        "ready" if memory is not None else "unknown",
        "ok" if memory is not None else "unknown",
        {
            "logical_cpu_count": os.cpu_count(),
            "memory_available_bytes": memory,
        },
        "Informational host evidence is not a capacity guarantee.",
    )
    return [budget, storage, host]


def run(args):
    result = {
        "schema_version": "kafka-environment-check/v1",
        "status": "unknown",
        "read_only": True,
        "target": {"bootstrap": args.bootstrap},
        "workload": {
            "scope": "synthetic Kafka producer benchmark",
            "topic_mode": "create_unique"
            if args.allow_create_topic
            else "existing_dedicated",
            "rounds": args.rounds,
            "records_per_round": args.records,
            "warmup_records": args.warmup_records,
            "payload": "decimal monotonic identities per phase",
            "throughput_records_per_second": args.throughput,
        },
        "probes": [],
        "limitations": [
            "This is a bounded benchmark preflight, not production health "
            "certification, a capacity test, or an ACL proof.",
            "No topic, group, message, broker configuration, service, or OS "
            "setting is created, changed, started, stopped, or deleted.",
        ],
    }
    try:
        client_config = support.load_client_config(args.client_config)
    except support.ConfigError as exc:
        return config_error_result(args, exc), 1
    result["target"]["client_config"] = client_config.evidence()
    result["probes"].append(
        support.probe(
            "secure_client_config",
            True,
            "ready",
            "ok",
            client_config.evidence(),
            (
                "Only transport/auth properties are accepted; values are "
                "redacted."
            ),
        )
    )
    result["probes"].extend(
        support.base_probes(
            args.kafka_dir,
            args.bootstrap,
            args.timeout,
            client_config,
            REQUIRED_TOOLS,
        )
    )
    result["probes"].append(topic_probe(args, client_config))
    result["probes"].append(
        support.probe(
            "operation_authorization",
            True,
            "ready" if args.operations_authorized else "unknown",
            "user_declared" if args.operations_authorized else "unknown",
            {
                "source": "user_declaration"
                if args.operations_authorized
                else None,
                "planned_operations": [
                    "produce",
                    "consume_with_unique_group",
                ]
                + (
                    ["create_unique_topic", "delete_exact_owned_topic"]
                    if args.allow_create_topic
                    else []
                ),
                "broker_acl_verified": False,
            },
            "Pass --operations-authorized only after the owner approves the "
            "planned workload; otherwise the aggregate remains unknown.",
        )
    )
    result["probes"].extend(resource_probes(args))
    result["status"] = support.overall_status(result["probes"])
    write_result(args.output, result)
    return result, {"ready": 0, "blocked": 1, "unknown": 3}[result["status"]]


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
        result, exit_code = run(args)
    except (OSError, ValueError) as exc:
        failure = {
            "schema_version": "kafka-environment-check/v1",
            "status": "blocked",
            "read_only": True,
            "probes": [
                support.probe(
                    "local_runtime",
                    True,
                    "blocked",
                    "error",
                    {"error": str(exc)},
                )
            ],
        }
        write_result(args.output, failure)
        print(json.dumps(failure, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
