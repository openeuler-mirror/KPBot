#!/usr/bin/env python3
"""Select the architecture-matching CubeSandbox one-click release asset."""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

GITHUB_API_BASE = "https://api.github.com/repos/TencentCloud/CubeSandbox"
GITHUB_DOWNLOAD_PREFIX = (
    "https://github.com/TencentCloud/CubeSandbox/releases/download/"
)
CNB_DOWNLOAD_PREFIX = (
    "https://cnb.cool/CubeSandbox/CubeSandbox/-/releases/download/"
)
CN_LATEST_URL = "https://download.cubesandbox.com/release/latest.json"
ASSET_PREFIX = "cube-sandbox-one-click-"
SUPPORTED_ARCHES = ("amd64", "arm64")
RECORD_SEPARATOR = "\x1e"


class ReleaseError(RuntimeError):
    """A release cannot be discovered or safely selected."""


def normalize_tag(version):
    """Normalize and validate a release tag without contacting the network."""
    tag = version if version.startswith("v") else "v" + version
    if not re.fullmatch(r"v[0-9A-Za-z][0-9A-Za-z._-]*", tag):
        raise ReleaseError("invalid CubeSandbox version: " + version)
    return tag


def release_endpoint(api_base, version):
    """Return the GitHub API endpoint for latest or a normalized release tag."""
    api_base = api_base.rstrip("/")
    if version == "latest":
        return api_base + "/releases/latest"
    return api_base + "/releases/tags/" + urllib.parse.quote(normalize_tag(version), safe="")


def fetch_json(url, timeout, github_auth=False):
    """Fetch JSON using only the standard library and optional user-provided auth."""
    headers = {"User-Agent": "kpbot-cubesandbox-deployment/1.0"}
    if github_auth:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception as exc:
        raise ReleaseError("failed to fetch release metadata from " + url + ": " + str(exc)) from exc


def fetch_release(api_base, version, timeout):
    """Fetch a GitHub release without requiring third-party Python packages."""
    return fetch_json(release_endpoint(api_base, version), timeout, github_auth=True)


def asset_from_tag(tag, arch):
    """Construct an official asset identity; the download step must prove it exists."""
    if arch not in SUPPORTED_ARCHES:
        raise ReleaseError("unsupported release architecture: " + arch)
    tag = normalize_tag(tag)
    asset_name = ASSET_PREFIX + tag + "-" + arch + ".tar.gz"
    github_url = GITHUB_DOWNLOAD_PREFIX + urllib.parse.quote(tag, safe="") + "/" + asset_name
    return {
        "version": tag,
        "asset_name": asset_name,
        "sha256": "",
        "github_url": github_url,
    }


def with_download_urls(selected, mirror):
    """Attach primary and fallback URLs without changing the asset identity."""
    if mirror == "cn":
        selected["download_url"] = CNB_DOWNLOAD_PREFIX + urllib.parse.quote(
            selected["version"], safe=""
        ) + "/" + selected["asset_name"]
        selected["fallback_url"] = selected["github_url"]
    elif mirror == "github":
        selected["download_url"] = selected["github_url"]
        selected["fallback_url"] = ""
    else:
        raise ReleaseError("unsupported download mirror: " + mirror)
    return selected


def select_asset(release, arch):
    """Select exactly one official bundle and require its GitHub SHA-256 digest."""
    if arch not in SUPPORTED_ARCHES:
        raise ReleaseError("unsupported release architecture: " + arch)
    if not isinstance(release, dict):
        raise ReleaseError("release metadata must be a JSON object")

    tag = release.get("tag_name", "")
    if not isinstance(tag, str) or not tag:
        raise ReleaseError("release metadata has no tag_name")

    asset_name = ASSET_PREFIX + tag + "-" + arch + ".tar.gz"
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise ReleaseError("release metadata assets must be a list")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == asset_name]
    if len(matches) != 1:
        available = sorted(
            asset.get("name", "")
            for asset in assets
            if isinstance(asset, dict) and asset.get("name", "").startswith(ASSET_PREFIX)
        )
        suffix = "; available one-click assets: " + ", ".join(available) if available else ""
        raise ReleaseError("expected release asset " + asset_name + " was not found" + suffix)

    asset = matches[0]
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ReleaseError(
            "release asset " + asset_name + " has no valid GitHub SHA-256 digest; "
            "use an explicit download URL plus CUBE_SANDBOX_SHA256 after independent verification"
        )

    expected = asset_from_tag(tag, arch)
    published_url = asset.get("browser_download_url", "")
    if published_url != expected["github_url"]:
        raise ReleaseError(
            "release asset URL does not match the expected official location: " + str(published_url)
        )
    expected["sha256"] = digest.split(":", 1)[1].lower()
    return expected


def resolve_release(release, arch, mirror):
    """Resolve primary and fallback download URLs for cn or github mode."""
    return with_download_urls(select_asset(release, arch), mirror)


def asset_from_cn_latest(metadata, arch):
    """Use the official CN latest pointer when GitHub API discovery is rate-limited."""
    if not isinstance(metadata, dict):
        raise ReleaseError("CN latest metadata must be a JSON object")
    url = metadata.get("url", "")
    if not isinstance(url, str):
        raise ReleaseError("CN latest metadata contains an invalid URL")
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    match = re.search(
        r"/download/(v[0-9A-Za-z][0-9A-Za-z._-]*)/"
        r"(cube-sandbox-one-click-[^/]+\.tar\.gz)$",
        path,
    )
    if not match:
        raise ReleaseError("CN latest metadata has no recognizable one-click release URL")
    tag, published_name = match.groups()
    if published_name != ASSET_PREFIX + tag + "-amd64.tar.gz":
        raise ReleaseError("CN latest pointer is not the expected amd64 one-click asset: " + published_name)
    return asset_from_tag(tag, arch)


def discover(api_base, version, arch, mirror, timeout):
    """Discover with API digests, or fail over only to official release locations."""
    try:
        release = fetch_release(api_base, version, timeout)
    except ReleaseError as api_error:
        if version == "latest":
            try:
                latest = fetch_json(CN_LATEST_URL, timeout)
                selected = asset_from_cn_latest(latest, arch)
            except ReleaseError as fallback_error:
                raise ReleaseError(str(api_error) + "; official CN latest fallback also failed: " + str(fallback_error)) from fallback_error
        else:
            selected = asset_from_tag(normalize_tag(version), arch)
        with_download_urls(selected, mirror)
        print(
            "cubesandbox release discovery: WARNING: GitHub API unavailable; using the official "
            "CNB/GitHub asset URL without an API-provided SHA-256 (set GITHUB_TOKEN or provide "
            "CUBE_SANDBOX_DOWNLOAD_URL plus CUBE_SANDBOX_SHA256 for digest pinning): " + str(api_error),
            file=sys.stderr,
        )
        return selected

    # If metadata was returned, missing assets/digests are authoritative errors;
    # do not mask them by constructing a possibly nonexistent fallback asset.
    return resolve_release(release, arch, mirror)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="latest", help="latest or a release tag such as v0.7.3")
    parser.add_argument("--arch", required=True, choices=SUPPORTED_ARCHES)
    parser.add_argument("--mirror", choices=("cn", "github"), default="github")
    parser.add_argument(
        "--api-base",
        default=os.environ.get("CUBE_GITHUB_API_BASE", GITHUB_API_BASE),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if args.timeout < 1 or args.timeout > 300:
        parser.error("--timeout must be between 1 and 300 seconds")

    try:
        selected = discover(args.api_base, args.version, args.arch, args.mirror, args.timeout)
    except (ReleaseError, ValueError, json.JSONDecodeError) as exc:
        print("cubesandbox release discovery: ERROR: " + str(exc), file=sys.stderr)
        return 1

    fields = (
        selected["version"],
        selected["asset_name"],
        selected["download_url"],
        selected["fallback_url"],
        selected["sha256"],
    )
    # ASCII record separator is non-whitespace: Bash read preserves empty fields,
    # including an empty fallback URL between the download URL and SHA-256.
    print(RECORD_SEPARATOR.join(fields))
    return 0


if __name__ == "__main__":
    sys.exit(main())
