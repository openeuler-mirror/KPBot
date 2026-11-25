#!/usr/bin/env python3
"""Prepare replay configs and a private snapshot of the selected cases."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

import yaml

from plan_oversub import add_memory_options, memory_plan, positive_int


def endpoint(value):
    url = urlsplit(value)
    if (
        url.scheme not in ("https", "http")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.port == 0
    ):
        raise ValueError(
            "provide an HTTP(S) endpoint without credentials, "
            "query or fragment"
        )
    return value


def git_output(*args):
    env = {"PATH": os.pathsep.join(os.get_exec_path())}
    return subprocess.check_output(["git", *args], text=True, env=env)


def read_preset(commit, name):
    text = git_output("show", f"{commit}:config/common/{name}.yaml")
    config = yaml.safe_load(text)
    if not isinstance(config, dict) or config.get("workflow_type") != "replay":
        raise ValueError(f"{name} must be a replay preset")
    for section in ("aenv", "test", "sandbox", "replay", "report"):
        if not isinstance(config.get(section), dict):
            raise ValueError(f"{name} needs a {section} mapping")
    env = config["aenv"].get("env")
    if not isinstance(env, dict):
        raise ValueError(f"{name} needs an aenv.env mapping")
    for key in ("E2B_API_KEY", "E2B_ACCESS_TOKEN"):
        env.pop(key, None)
    return config


def select_cases(args, commit, traces):
    manifest = args.template_manifest or Path(
        "artifacts/traces/image-map.json"
    )
    try:
        mapping = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{manifest}: invalid JSON") from exc
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(
            f"{manifest}: expected a nonempty filename-to-template mapping"
        )

    if args.smoke_trace is not None:
        names = [args.smoke_trace]
    elif args.case_list:
        names = [
            line.strip()
            for line in args.case_list.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    elif args.trajectory_dir:
        names = sorted(p.name for p in traces.glob("*.replay.json"))
    else:
        # Use the committed filenames even in a shared, dirty checkout.
        tree = f"{commit}:artifacts/traces/replays"
        names = [
            name
            for name in git_output("ls-tree", "-z", "--name-only", tree).split(
                "\0"
            )
            if name.endswith(".replay.json")
        ]
        if len(names) != 55:
            raise ValueError(
                "expected 55 tracked replay files in the default "
                "dataset; use --case-list or --trajectory-dir "
                "for a different set"
            )
    if not names or len(names) != len(set(names)):
        raise ValueError(
            "case selection must be nonempty and contain unique filenames"
        )
    selected = {}
    root = traces.resolve()
    for name in names:
        if (
            Path(name).name != name
            or not name.endswith(".replay.json")
            or (traces / name).resolve().parent != root
            or not (traces / name).is_file()
        ):
            raise ValueError(f"missing or invalid replay filename: {name!r}")
        alias = mapping.get(name)
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or alias != alias.strip()
        ):
            raise ValueError(
                f"{manifest}: missing or invalid template for {name}"
            )
        selected[name] = alias
    return selected


def snapshot_case(source, destination):
    # Validate the actual copy that bench-core will open.
    shutil.copyfile(source, destination)
    try:
        data = json.loads(destination.read_bytes())
        steps = data.get("trajectory") if isinstance(data, dict) else None
        if not isinstance(steps, list) or not steps:
            raise ValueError
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError
            action = step.get("action") or ""
            if not isinstance(action, str):
                raise ValueError
            tokens = action.split(None, 1)
            if tokens and tokens[0] in {"submit", "finish", "done"}:
                if index == 0:
                    raise ValueError
                break
            float(step.get("delay_time") or 0)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid or empty replay: {source.name}") from exc


def prepare(args):
    api = endpoint(args.api_url)
    data = endpoint(args.sandbox_url or api)
    domain = args.domain or urlsplit(data).hostname
    traces = args.trajectory_dir or Path("artifacts/traces/replays")
    commit = git_output("rev-parse", "HEAD").strip()
    selected = select_cases(args, commit, traces)
    ephemeral = read_preset(commit, "replay-ephemeral")
    ephemeral["test"]["benchmark_mode"] = "fixed"
    ephemeral["replay"].update(mode="exec_ephemeral", multiplier=1)
    if args.smoke_trace is not None:
        if args.ratio != 1:
            raise ValueError(
                "smoke uses one sandbox; select a regular replay for 1:n"
            )
        plan = {"mode": "smoke", "sandbox_count": 1}
        count, duration, delay = 1, 240, 0.02
        ephemeral["aenv"]["create_timeout"] = 120
        configs = [("smoke", ephemeral)]
    else:
        plan = memory_plan(args, [args.ratio])
        count = plan["trials"][0]["sandbox_count"]
        if plan["baseline_count"] < 1:
            raise ValueError("the 1:1 baseline needs at least 4 GiB")
        duration, delay = 3600, 1.0
        lifecycle = read_preset(commit, "replay")
        lifecycle["replay"]["mode"] = "lifecycle"
        configs = [("full", ephemeral), ("lifecycle-base", lifecycle)]

    plan.update(
        case_count=len(selected), template_count=len(set(selected.values()))
    )
    run = Path(
        tempfile.mkdtemp(
            prefix="agent-benchmark.", dir=args.run_parent.resolve()
        )
    )
    try:
        pool = run / "replays"
        pool.mkdir()
        for name in selected:
            snapshot_case(traces / name, pool / name)
        manifest = run / "image-map.json"
        manifest.write_text(
            json.dumps(selected, indent=2) + "\n", encoding="utf-8"
        )
        for label, config in configs:
            config["aenv"]["sandbox_ids_file"] = str(
                run / f"{label}-sandbox-ids.txt"
            )
            config["aenv"]["env"].update(
                E2B_API_URL=api, E2B_SANDBOX_URL=data, E2B_DOMAIN=domain
            )
            config["sandbox"].update(
                total_count=count, detect_existing=False, create_only=False
            )
            replay = config["replay"]
            replay.update(
                trajectory_dir=str(pool),
                trajectory_glob="*.replay.json",
                template_manifest=str(manifest),
                running_concurrency=(
                    plan["baseline_count"] if label == "full" else count
                ),
                delay_scale=delay,
            )
            replay.pop("trajectory_limit", None)
            config["test"].update(
                round_size=count, round_count=1, duration=duration
            )
            config["report"].update(
                output_dir=str(run / f"{label}-results"), filename_prefix=label
            )
            (run / f"{label}.yaml").write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )
        (run / "benchmark-commit.txt").write_text(
            commit + "\n", encoding="utf-8"
        )
        (run / "plan.json").write_text(
            json.dumps(plan, indent=2) + "\n", encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(run)
        raise
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--sandbox-url", help="data plane (default: API URL)")
    parser.add_argument(
        "--domain", help="routing domain (default: data plane hostname)"
    )
    parser.add_argument(
        "--run-parent",
        type=Path,
        default=Path.cwd(),
        help="existing parent for new runs",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--smoke-trace", help="prepare one short diagnostic replay"
    )
    group.add_argument(
        "--case-list",
        type=Path,
        help="UTF-8 text file, one replay filename per line",
    )
    parser.add_argument(
        "--trajectory-dir",
        type=Path,
        help="directory of custom *.replay.json files",
    )
    parser.add_argument(
        "--template-manifest",
        type=Path,
        help="JSON mapping replay filenames to template aliases",
    )
    parser.add_argument(
        "--ratio",
        type=positive_int,
        default=1,
        help="1:n denominator for this run (default: 1)",
    )
    add_memory_options(parser)
    args = parser.parse_args()
    try:
        print(prepare(args))
    except (
        OSError,
        ValueError,
        argparse.ArgumentTypeError,
        subprocess.CalledProcessError,
    ) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")
    except yaml.YAMLError:
        parser.exit(2, f"{parser.prog}: invalid YAML in the selected preset\n")


if __name__ == "__main__":
    main()
