#!/usr/bin/env python3
"""Materialize an exact base and ordered patches in a new detached worktree."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def git(repo, *args, data=None, env=None):
    # All inputs are local. Do not forward cloud tokens or the caller's Git index.
    keys = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP", "SystemRoot")
    child_env = {key: os.environ[key] for key in keys if key in os.environ}
    child_env.update(GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0")
    if env:
        child_env.update(env)
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", f"core.hooksPath={os.devnull}",
         "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false", *args],
        input=data, capture_output=True, env=child_env
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise ValueError(f"git {args[0]} failed: {detail}")
    return os.fsdecode(result.stdout).rstrip("\n")


def apply_patch(repo, data, index_env, scratch):
    headers = re.split(rb"\r?\n\r?\n", data[:65536], maxsplit=1)[0]
    is_mail = data.startswith(b"From ") or (
        re.search(rb"^From:", headers, re.I | re.M) and
        re.search(rb"^Subject:", headers, re.I | re.M)
    )
    if not is_mail:
        git(repo, "apply", "--cached", "--whitespace=nowarn", data=data, env=index_env)
        return 1

    with tempfile.TemporaryDirectory(dir=scratch) as mail:
        mail_dir = Path(mail)
        # lore exports mboxrd, format-patch emits mbox, and a single raw email
        # can start directly with RFC headers. Let Git decode each body.
        options = ["--mboxrd"] if data.startswith(b"From mboxrd@") else []
        git(repo, "mailsplit", "-b", *options, f"-o{mail_dir}", data=data)
        messages = sorted(mail_dir.iterdir())
        applied = 0
        for message in messages:
            diff = mail_dir / "diff"
            info = git(repo, "mailinfo", "-k", str(mail_dir / "message"), str(diff),
                       data=message.read_bytes())
            body = diff.read_bytes()
            subject = next((s for s in info.splitlines() if s.startswith("Subject:")), "")
            cover = re.search(r"\[[^\]\n]*\bPATCH\b[^\]\n]*\b0+/[1-9]\d*\b[^\]\n]*\]",
                              subject, re.I)
            if cover and not re.search(rb"(?m)^(?:diff --git |--- [^\n]+\n\+\+\+ )", body):
                continue
            git(repo, "apply", "--cached", "--whitespace=nowarn", data=body, env=index_env)
            applied += 1
        return applied


def prepare(args):
    expected = args.patch_sha256
    if expected and (len(expected) != len(args.patch) or
                     any(not re.fullmatch(r"[0-9a-fA-F]{64}", value) for value in expected)):
        raise ValueError("supply one 64-digit --patch-sha256 per --patch, in the same order")
    repo = args.repo.resolve(strict=True)
    if git(repo, "rev-parse", "--is-bare-repository") != "true":
        repo = Path(git(repo, "rev-parse", "--show-toplevel"))
    base = git(repo, "rev-parse", "--verify", "--end-of-options", args.base + "^{commit}")
    run = args.run.absolute()
    run.mkdir(mode=0o700)  # Exclusive ownership; a retry uses a fresh run.
    record = {"status": "preparing", "base_commit": base, "patches": []}

    def save():
        pending = run / "source.json.tmp"
        pending.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        pending.replace(run / "source.json")

    save()
    try:
        with tempfile.TemporaryDirectory(dir=run) as scratch:
            env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
            git(repo, "read-tree", base, env=env)
            for number, path in enumerate(args.patch):
                record["current_patch"] = path.name
                data = path.read_bytes()
                sha256 = hashlib.sha256(data).hexdigest()
                if expected and sha256 != expected[number].lower():
                    record["expected_sha256"] = expected[number].lower()
                    record["actual_sha256"] = sha256
                    raise ValueError(f"patch SHA-256 mismatch: {path.name}")
                count = apply_patch(repo, data, env, scratch)
                record["patches"].append({
                    "file": path.name, "sha256": sha256,
                    "parts": count,
                })
            if args.patch and not any(p["parts"] for p in record["patches"]):
                raise ValueError("the supplied series contains no patches")
            tree = git(repo, "write-tree", env=env)

        record.pop("current_patch", None)
        source = run / "source"
        git(repo, "worktree", "add", "--no-checkout", "--detach", str(source), base)
        # Only this freshly created checkout receives the preflighted tree.
        git(source, "read-tree", "--reset", "-u", tree)
        record.update(status="prepared", patched_tree=tree)
    except (OSError, ValueError) as error:
        record.update(status="failed", error=str(error))
        save()
        raise
    save()
    print(run / "source.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="local Git repository")
    parser.add_argument("--base", required=True, help="commit, tag or ref to resolve once")
    parser.add_argument("--run", type=Path, required=True, help="new directory under an existing parent")
    parser.add_argument("--patch", type=Path, action="append", default=[],
                        help="unified diff, patch email or mailbox, in application order")
    parser.add_argument("--patch-sha256", action="append", default=[], metavar="HEX",
                        help="trusted expected digest for each patch file, in the same order")
    args = parser.parse_args()
    try:
        prepare(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
