"""Verify the pinned TPC-H dbgen archive, then extract and build it."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "references/tpch-dbgen-provenance.json"
)
EXTRACTED = "TPC-H_Tools_v3.0.0"
MARKER = ".kpbot-tpch-dbgen-verified.json"


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root):
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def load_manifest(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for field in ("url", "sha256", "size_bytes", "source_apache_commit"):
        if field not in data:
            raise ValueError(f"manifest is missing {field}")
    if not data["url"].startswith("https://"):
        raise ValueError("manifest url must use https")
    if len(data["sha256"]) != 64:
        raise ValueError("manifest sha256 must be a full SHA-256 digest")
    return data


def download(url, destination, timeout):
    request = urllib.request.Request(
        url, headers={"User-Agent": "KPBot-TPC-H-dbgen"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("archive download left verified HTTPS")
        with destination.open("wb") as target:
            shutil.copyfileobj(response, target)


def verify_archive(archive, manifest):
    actual_size = archive.stat().st_size
    actual_sha256 = sha256_file(archive)
    if actual_size != manifest["size_bytes"]:
        raise ValueError(
            "archive size mismatch: "
            f"expected {manifest['size_bytes']}, got {actual_size}"
        )
    if actual_sha256 != manifest["sha256"]:
        raise ValueError(
            "archive SHA-256 mismatch: "
            f"expected {manifest['sha256']}, got {actual_sha256}"
        )
    return {
        "sha256": actual_sha256,
        "size_bytes": actual_size,
        "url": manifest["url"],
        "source_apache_commit": manifest["source_apache_commit"],
    }


def safe_members(archive):
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(
                    "archive contains an unsafe path: " + info.filename
                )
            if not name.parts or name.parts[0] != EXTRACTED:
                raise ValueError("unexpected archive root: " + info.filename)
        return bundle.namelist()


def prepare(archive, manifest, bin_dir, replace_existing):
    target = bin_dir / EXTRACTED
    marker = bin_dir / MARKER
    verified = verify_archive(archive, manifest)
    if target.exists():
        current = None
        if marker.is_file():
            try:
                current = json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = None
        if not (
            current
            and current.get("sha256") == verified["sha256"]
            and current.get("size_bytes") == verified["size_bytes"]
            and current.get("tree_sha256") == tree_sha256(target)
        ):
            if not replace_existing:
                raise ValueError(
                    f"{target} exists without a matching verified marker; "
                    "rerun with --replace-existing to remove only that "
                    "directory and extract the verified archive"
                )
            resolved = target.resolve()
            if (
                resolved.parent != bin_dir.resolve()
                or resolved.name != EXTRACTED
            ):
                raise ValueError("refusing to remove an unexpected directory")
            shutil.rmtree(resolved)
    if not target.exists():
        staging = Path(tempfile.mkdtemp(prefix=".tpch-dbgen-", dir=bin_dir))
        try:
            safe_members(archive)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(staging)
            extracted = staging / EXTRACTED
            if not extracted.is_dir():
                raise ValueError("archive did not contain " + EXTRACTED)
            extracted.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    marker.write_text(
        json.dumps(
            {
                **verified,
                "artifact": manifest["artifact"],
                "digest_kind": manifest["digest_kind"],
                "tree_sha256": tree_sha256(target),
                "verified_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return target, verified


def build(tools_dir):
    script = tools_dir / "bin/build-tpch-dbgen.sh"
    if not script.is_file():
        raise ValueError(f"missing upstream build script: {script}")
    subprocess.run(
        ["bash", str(script)],
        cwd=str(tools_dir / "bin"),
        check=True,
    )
    dbgen = tools_dir / "bin" / EXTRACTED / "dbgen/dbgen"
    if not dbgen.is_file():
        raise ValueError(f"upstream build did not produce {dbgen}")
    return dbgen


def run(
    tools_dir, manifest_path, archive=None, replace_existing=False, timeout=120
):
    tools_dir = tools_dir.resolve()
    if not tools_dir.is_dir():
        raise ValueError(f"tools directory does not exist: {tools_dir}")
    manifest = load_manifest(manifest_path)
    with tempfile.TemporaryDirectory(prefix="kpbot-tpch-dbgen-") as temp:
        if archive is None:
            archive = Path(temp) / manifest["artifact"]
            download(manifest["url"], archive, timeout)
        else:
            archive = archive.resolve()
            if not archive.is_file():
                raise ValueError(f"archive does not exist: {archive}")
        target, verified = prepare(
            archive,
            manifest,
            tools_dir / "bin",
            replace_existing,
        )
        dbgen = build(tools_dir)
    return {
        "status": "passed",
        "archive": manifest["artifact"],
        "sha256": verified["sha256"],
        "size_bytes": verified["size_bytes"],
        "source_apache_commit": verified["source_apache_commit"],
        "extracted": str(target),
        "dbgen": str(dbgen),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument(
        "--archive",
        type=Path,
        help="verify this local archive instead of downloading the pinned URL",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="remove only the extracted dbgen tree after the archive verifies",
    )
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not 0 < args.timeout <= 3600:
        parser.error("timeout must be between 0 and 3600 seconds")
    try:
        result = run(
            args.tools_dir,
            args.manifest,
            archive=args.archive,
            replace_existing=args.replace_existing,
            timeout=args.timeout,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
