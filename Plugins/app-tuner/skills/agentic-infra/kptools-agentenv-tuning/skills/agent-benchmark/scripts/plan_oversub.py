#!/usr/bin/env python3
"""Plan memory-based sandbox counts and write optional trial YAML."""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

KIB_PER_GIB = 1024 * 1024
SANDBOX_KIB = 4 * KIB_PER_GIB
DMI_UNIT_KIB = {
    "kb": 1,
    "mb": 1024,
    "gb": KIB_PER_GIB,
    "tb": 1024 * KIB_PER_GIB,
}
REPLAY_MODES = ("lifecycle", "exec_only", "exec_ephemeral")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def installed_memory_kib():
    """Read the installed capacity from SMBIOS Memory Device records."""
    try:
        result = subprocess.run(
            ["dmidecode", "--type", "17"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": os.pathsep.join(os.get_exec_path()), "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None

    total = 0
    for line in result.stdout.splitlines():
        key, _, value = line.strip().partition(":")
        if key != "Size" or value.strip() == "No Module Installed":
            continue
        size = re.fullmatch(
            r"\s*(\d+)\s+(kB|MB|GB|TB)\s*", value, re.IGNORECASE
        )
        if size is None:
            return None
        count, unit = size.groups()
        total += int(count) * DMI_UNIT_KIB[unit.lower()]
    return total or None


def memtotal_kib():
    for line in Path("/proc/meminfo").read_text().splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "MemTotal:" and fields[2] == "kB":
            return positive_int(fields[1])
    raise ValueError("could not find MemTotal in /proc/meminfo")


def capacity(args):
    if args.mem_total_gib is not None:
        return args.mem_total_gib * KIB_PER_GIB, "--mem-total-gib"
    if args.mem_total_kib is not None:
        return args.mem_total_kib, "--mem-total-kib"
    if args.memory_source == "installed":
        total = installed_memory_kib()
        if total is not None:
            return total, "dmidecode:type17"
        print(
            "DIMM capacity unavailable. Using /proc/meminfo MemTotal.",
            file=sys.stderr,
        )
    return memtotal_kib(), "/proc/meminfo:MemTotal"


def write_trials(base_file, output_dir, plan):
    try:
        import yaml
    except ImportError as exc:
        raise ValueError(
            "use the repository virtualenv Python to generate YAML"
        ) from exc

    try:
        base = yaml.safe_load(base_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid replay YAML: {base_file}") from exc
    if not isinstance(base, dict) or base.get("workflow_type") != "replay":
        raise ValueError("base config must use workflow_type: replay")
    for section in ("aenv", "sandbox", "test", "replay", "report"):
        if not isinstance(base.setdefault(section, {}), dict):
            raise ValueError(
                f"base config section {section!r} must be a mapping"
            )

    provider_env = base["aenv"].get("env", {})
    if not isinstance(provider_env, dict):
        raise ValueError("aenv.env must be a mapping")
    if any(
        provider_env.get(key) for key in ("E2B_API_KEY", "E2B_ACCESS_TOKEN")
    ):
        raise ValueError(
            "use E2B_CONFIG for AgentENV credentials"
        )

    mode = base["replay"].get("mode", "lifecycle")
    if mode not in REPLAY_MODES:
        raise ValueError(f"unsupported replay mode: {mode!r}")
    baseline = plan["baseline_count"]
    if baseline == 0:
        raise ValueError(
            "capacity is below 4 GiB, so a 1:1 trial has zero sandboxes"
        )

    # bench-core interprets dataset paths relative to its working directory.
    for key in ("trajectory_dir", "template_manifest"):
        value = base["replay"].get(key)
        if value:
            base["replay"][key] = str(Path(value).resolve())

    # Reserve the output name before resolving any dangling symlink.
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = output_dir.resolve()
    plan["mode"] = mode
    for trial in plan["trials"]:
        label = f'1-to-{trial["ratio"]}'
        trial_dir = root / label
        trial_dir.mkdir(mode=0o700)
        count = trial["sandbox_count"]

        config = copy.deepcopy(base)
        config["aenv"]["sandbox_ids_file"] = str(trial_dir / "sandbox-ids.txt")
        config["sandbox"].update(
            total_count=count, detect_existing=False, create_only=False
        )
        config["test"]["round_size"] = count
        config["replay"].update(
            mode=mode,
            running_concurrency=count if mode == "lifecycle" else baseline,
        )
        if not config["replay"].get("template_manifest"):
            config["replay"].setdefault("trajectory_limit", baseline)
        result_dir = trial_dir / "results"
        config["report"].update(
            output_dir=str(result_dir), filename_prefix=f"oversub_{label}"
        )

        config_path = trial_dir / "trial.yaml"
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        trial.update(config=str(config_path), results_dir=str(result_dir))

    (root / "plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )


def add_memory_options(parser):
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--memory-source",
        choices=("installed", "memtotal"),
        default="installed",
        help="DIMM capacity (with MemTotal fallback), or Linux MemTotal",
    )
    source.add_argument(
        "--mem-total-gib", type=positive_int, help="explicit capacity in GiB"
    )
    source.add_argument(
        "--mem-total-kib", type=positive_int, help="explicit capacity in KiB"
    )


def memory_plan(args, ratios):
    total, source_name = capacity(args)
    baseline = total // SANDBOX_KIB
    return {
        "memory_source": source_name,
        "memory_kib": total,
        "memory_gib": total / KIB_PER_GIB,
        "sandbox_memory_mib": 4096,
        "sandbox_vcpu": 2,
        "baseline_count": baseline,
        "trials": [
            {
                "ratio": ratio,
                "sandbox_count": total * ratio // SANDBOX_KIB,
                "native_count": baseline * ratio,
            }
            for ratio in ratios
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "ratios",
        metavar="n",
        type=positive_int,
        nargs="+",
        help="1:n denominators",
    )
    add_memory_options(parser)
    parser.add_argument(
        "--base-config",
        type=Path,
        help="prepared replay YAML, read from the checkout root",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="new directory for trial YAML and plan.json",
    )
    args = parser.parse_args()

    if (args.base_config is None) != (args.output_dir is None):
        parser.error(
            "--base-config and --output-dir must be supplied together"
        )
    if len(set(args.ratios)) != len(args.ratios):
        parser.error("ratios must be unique")

    try:
        plan = memory_plan(args, args.ratios)
        if args.base_config is not None:
            write_trials(args.base_config, args.output_dir, plan)
    except (OSError, TypeError, ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(str(exc))

    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
