#!/usr/bin/env python3
"""Prepare a private, CPU-only OpenViking development fixture; never start a server.

All mutable state lives in this checkout's .runtime/openviking directory.
Only the provision subcommand contacts a running loopback server. Keys are
created or recovered in memory and written to mode-0600 files, never printed.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
from urllib import error, request
from urllib.parse import urlsplit

REPO = Path(__file__).absolute().parents[5]
RUNTIME = REPO / ".runtime" / "openviking"
ACCOUNT = "kpbot-dev-smoke"
ADMIN = "fixture-admin"
USERS = ("user-01", "user-02")
PROJECTS = ("project-a", "project-b")
PINS = {
    "openviking": "0.4.22",
    "openviking-sdk": "0.1.9",
    "llama-cpp-python": "0.3.16",
}
MODEL = "bge-small-zh-v1.5-f16"
MODEL_SIZE = 47_886_240
MODEL_SHA256 = "ab9b81d9cd329c712eee379cf0068eabe6a5e2a01d0def61535eba9384085e2c"
MODEL_URL = (
    "https://huggingface.co/CompendiumLabs/bge-small-zh-v1.5-gguf/resolve/main/"
    "bge-small-zh-v1.5-f16.gguf?download=true"
)


class SetupError(RuntimeError):
    """An error whose text contains no credentials or HTTP response bodies."""


class HTTPFailure(SetupError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Loopback HTTP request failed with status {status}")


def checked(path: Path) -> Path:
    """Reject symlinks and any path outside the single approved runtime root."""
    path = path.absolute()
    if REPO.resolve() != REPO or RUNTIME.resolve() != RUNTIME:
        raise SetupError("Refusing a symlinked repository/runtime path")
    if not path.is_relative_to(RUNTIME) or path.resolve() != path:
        raise SetupError("Refusing a path outside the private runtime or a symlink")
    return path


def prepare_directories() -> None:
    os.umask(0o077)
    parent = REPO / ".runtime"
    if parent.resolve() != parent:
        raise SetupError("Refusing a symlinked .runtime directory")
    parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    for relative in ("", "cache", "tmp", "models", "data", "logs", "credentials",
                     "fixtures", "home", "python", "xdg-cache"):
        path = checked(RUNTIME / relative)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)


@contextlib.contextmanager
def runtime_lock():
    path = checked(RUNTIME / ".setup.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SetupError("Another setup/provision operation holds the runtime lock") from exc
        yield
    finally:
        os.close(fd)


def private_json(path: Path) -> dict:
    path = checked(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise SetupError("Private configuration must be a regular file owned by this user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise SetupError("Private configuration must have mode 0600")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            value = json.load(stream)
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise SetupError("Private configuration must contain a JSON object")
    return value


def write_new_json(path: Path, value: dict) -> None:
    """Never overwrite an existing credential, configuration, or fixture."""
    path = checked(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def command_environment() -> dict[str, str]:
    # Do not enumerate, print, or inherit unrelated model/account credentials.
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TZ")
           if name in os.environ}
    env.update({
        "HOME": str(RUNTIME / "home"),
        "UV_CACHE_DIR": str(RUNTIME / "cache"),
        "UV_PYTHON_INSTALL_DIR": str(RUNTIME / "python"),
        "UV_PYTHON_DOWNLOADS": "never",
        "XDG_CACHE_HOME": str(RUNTIME / "xdg-cache"),
        "TMPDIR": str(RUNTIME / "tmp"),
        "CMAKE_BUILD_PARALLEL_LEVEL": "2",
        "CMAKE_ARGS": "-DGGML_CUDA=OFF",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "OPENVIKING_CONFIG_FILE": str(RUNTIME / "ov.conf"),
    })
    return env


def package_versions() -> dict[str, str]:
    python = RUNTIME / "venv" / "bin" / "python"
    if not python.exists():
        raise SetupError("The private virtual environment does not exist")
    code = (
        "import importlib.metadata,json;print(json.dumps({name:"
        "importlib.metadata.version(name) for name in " + repr(tuple(PINS)) + "}))"
    )
    result = subprocess.run([str(python), "-I", "-c", code], check=True,
                            capture_output=True, text=True, env=command_environment())
    versions = json.loads(result.stdout)
    if versions != PINS:
        raise SetupError("Installed package versions do not match the approved pins")
    return versions


def install_dependencies(python: str) -> None:
    uv = shutil.which("uv")
    interpreter = shutil.which(python) if "/" not in python else python
    if not uv or not interpreter or not Path(interpreter).is_file():
        raise SetupError("Existing uv and Python 3.12 are required; no toolchain download is allowed")
    env = command_environment()
    venv = checked(RUNTIME / "venv")
    if not (venv / "pyvenv.cfg").exists():
        if venv.exists():
            raise SetupError("An incomplete venv already exists; refusing to overwrite it")
        subprocess.run([uv, "venv", "--no-python-downloads", "--python", interpreter,
                        str(venv)], check=True, cwd=RUNTIME, env=env)
    if venv.is_symlink():
        raise SetupError("Refusing a symlinked virtual environment")
    os.chmod(venv, 0o700)
    subprocess.run([
        uv, "pip", "install", "--python", str(venv / "bin" / "python"),
        "--only-binary", "openviking,openviking-sdk",
        "openviking[local-embed]==" + PINS["openviking"],
        "openviking-sdk==" + PINS["openviking-sdk"],
        "llama-cpp-python==" + PINS["llama-cpp-python"],
    ], check=True, cwd=RUNTIME, env=env)
    package_versions()
    # Record a fully version-pinned inventory without creating any application DB.
    result = subprocess.run([uv, "pip", "freeze", "--python", str(venv / "bin" / "python")],
                            check=True, capture_output=True, text=True, cwd=RUNTIME, env=env)
    inventory = checked(RUNTIME / "installed-requirements.txt")
    if not inventory.exists():
        fd = os.open(inventory, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(result.stdout)


def check_model() -> Path:
    path = checked(RUNTIME / "models" / (MODEL + ".gguf"))
    if not path.is_file() or path.stat().st_size != MODEL_SIZE:
        raise SetupError("Local embedding model is absent or has the wrong size")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != MODEL_SHA256:
        raise SetupError("Local embedding model SHA256 does not match the approved artifact")
    return path


def download_model() -> Path:
    model = checked(RUNTIME / "models" / (MODEL + ".gguf"))
    if model.exists():
        return check_model()
    temporary = checked(model.parent / (MODEL + "." + secrets.token_hex(8) + ".part"))
    digest = hashlib.sha256()
    total = 0
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            with request.urlopen(request.Request(MODEL_URL, headers={"User-Agent": "KPBot-dev-setup"}),
                                 timeout=120) as response:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(block)
                    if total > MODEL_SIZE:
                        raise SetupError("Model download exceeds the approved size")
                    digest.update(block)
                    stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        if total != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
            raise SetupError("Downloaded model failed size/SHA256 validation")
        # Validate both paths before moving/linking; link never replaces a target.
        checked(temporary)
        checked(model)
        os.link(temporary, model)
    finally:
        if temporary.exists():
            checked(temporary)
            temporary.unlink()
    return check_model()


def server_configuration(port: int, model: Path) -> dict:
    return {
        "server": {"host": "127.0.0.1", "port": port, "workers": 1,
                   "auth_mode": "api_key", "root_api_key": secrets.token_urlsafe(48),
                   "cors_origins": [], "with_bot": False},
        "storage": {"workspace": str(RUNTIME / "data"), "skip_process_lock": False,
                    "agfs": {"backend": "local"}, "vectordb": {"backend": "local"}},
        "embedding": {"dense": {"provider": "local", "model": MODEL, "dimension": 512,
                                 "input": "text", "model_path": str(model)},
                      "text_source": "content_only", "max_input_tokens": 256,
                      "max_concurrent": 1},
        "vlm": {},
        "log": {"level": "WARNING", "output": str(RUNTIME / "logs" / "openviking.log")},
    }


def configuration() -> dict:
    value = private_json(RUNTIME / "ov.conf")
    server = value.get("server", {})
    dense = value.get("embedding", {}).get("dense", {})
    if (server.get("host") != "127.0.0.1" or server.get("auth_mode") != "api_key"
            or server.get("workers") != 1 or server.get("with_bot") is not False
            or not isinstance(server.get("root_api_key"), str)
            or len(server["root_api_key"]) < 32):
        raise SetupError("Existing server configuration is not the approved private fixture")
    if (value.get("storage", {}).get("workspace") != str(RUNTIME / "data")
            or value.get("storage", {}).get("skip_process_lock", False)
            or dense.get("provider") != "local" or dense.get("model") != MODEL
            or dense.get("dimension") != 512
            or dense.get("model_path") != str(RUNTIME / "models" / (MODEL + ".gguf"))
            or value.get("vlm") != {}):
        raise SetupError("Existing storage/model configuration differs from the approved fixture")
    port = server.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SetupError("Invalid configured loopback port")
    return value


def setup(args) -> None:
    if not args.skip_install:
        install_dependencies(args.python)
    else:
        package_versions()
    model = download_model()
    path = RUNTIME / "ov.conf"
    if not path.exists():
        write_new_json(path, server_configuration(args.port, model))
    conf = configuration()
    if conf["server"]["port"] != args.port:
        raise SetupError("Existing configuration uses another port; keys were not overwritten")
    fixture = RUNTIME / "fixture-environment.json"
    metadata = {"environment": "development-test-only", "account_id": ACCOUNT,
                "users": list(USERS), "projects": list(PROJECTS),
                "processing_mode": "vectors_only", "embedding_sha256": MODEL_SHA256}
    if not fixture.exists():
        write_new_json(fixture, metadata)
    print("Private CPU fixture prepared; server was not started and keys were not printed.")
    print("Interpreter:", RUNTIME / "venv" / "bin" / "python")
    print("Server entry:", RUNTIME / "venv" / "bin" / "openviking-server")
    print("Configuration:", RUNTIME / "ov.conf")
    print("No VLM is configured; the doctor's VLM failure is expected.")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError("Refusing a loopback HTTP redirect carrying credentials")


class LoopbackClient:
    def __init__(self, port: int):
        self.endpoint = f"http://127.0.0.1:{port}"
        split = urlsplit(self.endpoint)
        if split.hostname != "127.0.0.1" or split.scheme != "http":
            raise SetupError("Only the approved loopback HTTP endpoint is allowed")
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def call(self, method: str, path: str, key: str, body=None):
        if not path.startswith("/") or path.startswith("//"):
            raise SetupError("Invalid private API path")
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = request.Request(self.endpoint + path, data=data, method=method,
                              headers={"X-API-Key": key, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=120) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise HTTPFailure(status) from None
        if len(raw) > 4 * 1024 * 1024:
            raise SetupError("Private API response exceeds the safety limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise SetupError("Invalid private API response")
        if payload.get("status") not in ("ok", "ready"):
            raise SetupError("Private API did not report success")
        return payload

    def identity(self, key: str, user: str, role: str) -> None:
        payload = self.call("GET", "/health", key)
        if (payload.get("account_id") != ACCOUNT or payload.get("user_id") != user
                or str(payload.get("role", "")).lower() != role):
            raise SetupError("Credential identity does not match the development fixture")


def recover_key(client: LoopbackClient, root_key: str, user: str, role: str) -> str:
    """Recover an already-created key, never rotate it or display API bodies."""
    payload = client.call("GET", f"/api/v1/admin/accounts/{ACCOUNT}/users", root_key)
    result = payload.get("result")
    items = result if isinstance(result, list) else result.get("items", []) if isinstance(result, dict) else []
    for item in items:
        if item.get("user_id") == user and str(item.get("role", "")).lower() == role:
            key = item.get("user_key")
            if isinstance(key, str) and key:
                return key
    raise SetupError("Existing remote user key cannot be recovered; no key was regenerated")


def provision_key(client: LoopbackClient, root_key: str, user: str, role: str,
                  management_key: str) -> str:
    path = RUNTIME / "credentials" / (user + ".json")
    if path.exists():
        existing = private_json(path)
        if (existing.get("account_id") != ACCOUNT or existing.get("user_id") != user
                or existing.get("role") != role or not isinstance(existing.get("api_key"), str)):
            raise SetupError("Existing private credential metadata differs; refusing replacement")
        key = existing["api_key"]
        client.identity(key, user, role)
        return key
    if role == "admin":
        route = "/api/v1/admin/accounts"
        body = {"account_id": ACCOUNT, "admin_user_id": user}
    else:
        route = f"/api/v1/admin/accounts/{ACCOUNT}/users"
        body = {"user_id": user, "role": "user"}
    try:
        result = client.call("POST", route, management_key, body).get("result", {})
        key = result.get("user_key")
        if not isinstance(key, str) or not key:
            raise SetupError("API-key provisioning did not return a usable tenant key")
    except HTTPFailure as exc:
        if exc.status != 409:
            raise
        key = recover_key(client, root_key, user, role)
    client.identity(key, user, role)
    write_new_json(path, {"account_id": ACCOUNT, "user_id": user, "role": role, "api_key": key})
    return key


def provision(_args) -> None:
    conf = configuration()
    client = LoopbackClient(conf["server"]["port"])
    root_key = conf["server"]["root_api_key"]
    admin_key = provision_key(client, root_key, ADMIN, "admin", root_key)
    for user in USERS:
        key = provision_key(client, root_key, user, "user", admin_key)
        value = {"backend": "openviking", "user_id": user,
                 "current_project": PROJECTS[0], "allowed_projects": list(PROJECTS),
                 "options": {"endpoint": client.endpoint, "api_key": key,
                             "account_id": ACCOUNT, "user_id": user,
                             "processing_mode": "vectors_only", "max_input_bytes": 1048576}}
        path = RUNTIME / ("config-" + user + ".json")
        if path.exists():
            if private_json(path) != value:
                raise SetupError("Existing provider configuration differs; refusing to overwrite keys")
        else:
            write_new_json(path, value)
        print("Verified ordinary USER fixture configuration:", path)
    print("ROOT and ADMIN keys are management-only; no key values were printed.")


def verify(_args) -> None:
    versions = package_versions()
    check_model()
    configuration()
    print("Verified pinned packages:", json.dumps(versions, sort_keys=True))
    print("Verified local model SHA256, private configuration, and loopback-only settings.")
    print("No model inference, server startup, or HTTP call was performed.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("setup", help="Install pinned dependencies, verify/download approved GGUF, and create private config")
    install.add_argument("--python", default="python3.12", help="Existing Python 3.12 executable; uv downloads are disabled")
    install.add_argument("--port", type=int, default=1933, help="Loopback fixture port (default: 1933)")
    install.add_argument("--skip-install", action="store_true", help="Validate already-installed exact pins instead of running uv")
    install.set_defaults(action=setup)
    commands.add_parser("provision", help="Create/reuse an account and two ordinary user keys on the running loopback server").set_defaults(action=provision)
    commands.add_parser("verify", help="Check installed pins, GGUF hash, and private config without starting services").set_defaults(action=verify)
    args = parser.parse_args(argv)
    if hasattr(args, "port") and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        prepare_directories()
        with runtime_lock():
            args.action(args)
        return 0
    except SetupError as exc:
        print("Setup/provision refused:", str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Suppress untrusted HTTP bodies and arbitrary exception strings, which
        # could include credentials. The parent can inspect private local logs.
        print("Setup/provision failed (" + type(exc).__name__ + "); credentials were not printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
