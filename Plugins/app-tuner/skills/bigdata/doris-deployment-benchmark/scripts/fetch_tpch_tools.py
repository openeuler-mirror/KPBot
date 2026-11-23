"""Fetch the public Apache Doris TPC-H tools at one pinned Git commit."""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import urllib.parse
import urllib.request

REPOSITORY = "apache/doris"
PREFIX = "tools/tpch-tools/"


def read_bytes(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "KPBot-TPC-H-tools"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def read_json(url):
    return json.loads(read_bytes(url))


def fetch(ref, output):
    if output.exists():
        raise FileExistsError("output already exists; use a new directory")
    api = f"https://api.github.com/repos/{REPOSITORY}"
    commit = read_json(api + "/commits/" + urllib.parse.quote(ref, safe=""))[
        "sha"
    ]
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("invalid resolved commit")
    files = []

    def visit(path):
        entries = read_json(api + "/contents/" + path + "?ref=" + commit)
        for entry in entries:
            item = PurePosixPath(entry["path"])
            if ".." in item.parts or not entry["path"].startswith(PREFIX):
                raise ValueError("unexpected source path")
            if entry["type"] == "dir":
                visit(entry["path"])
            elif entry["type"] == "file":
                files.append(entry)
            else:
                raise ValueError("unsupported source entry")

    visit(PREFIX.rstrip("/"))
    if not files:
        raise ValueError("empty source directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="kpbot-tpch-", dir=output.parent))
    try:

        def save(entry):
            url = (
                f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/"
                + entry["path"]
            )
            data = read_bytes(url)
            actual = hashlib.sha1(
                b"blob " + str(len(data)).encode() + b"\0" + data
            ).hexdigest()
            if actual != entry["sha"]:
                raise ValueError("source checksum mismatch: " + entry["path"])
            relative = PurePosixPath(entry["path"]).relative_to(
                PREFIX.rstrip("/")
            )
            target = staging.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return {"path": relative.as_posix(), "git_blob_sha1": actual}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            manifest = list(pool.map(save, files))
        provenance = {
            "repository": REPOSITORY,
            "ref": ref,
            "commit": commit,
            "files": manifest,
        }
        (staging / "source.json").write_text(
            json.dumps(provenance, indent=2), encoding="utf-8"
        )
        staging.rename(output)
        return provenance
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = fetch(args.ref, args.output)
    print(
        json.dumps({"commit": result["commit"], "files": len(result["files"])})
    )
