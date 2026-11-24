"""Stream Load into Doris over certificate-validated HTTPS.

The FE redirect is inspected first and its host must be explicitly allowed.
Doris 3.0.8's plaintext redirect is upgraded to an explicitly configured TLS
port without following HTTP or retaining URL credentials. Data and the Basic
Authorization header are sent only after certificate validation. The password
is read from the environment, never from argv.
"""

import argparse
import base64
import http.client
import json
import math
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import urljoin, urlsplit, urlunsplit


class LoadError(Exception):
    pass


def positive_timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 3600:
        raise argparse.ArgumentTypeError(
            "timeout must be between 0 and 3600 seconds"
        )
    return seconds


def parse_https_url(value):
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise LoadError("URL must use https")
    if not parts.hostname:
        raise LoadError("URL has no host")
    if parts.username or parts.password:
        raise LoadError("URL must not embed credentials")
    if parts.fragment:
        raise LoadError("URL must not contain a fragment")
    return parts


def header_value(value):
    if ":" not in value:
        raise argparse.ArgumentTypeError("header must be 'name: value'")
    name, text = value.split(":", 1)
    if not name.strip():
        raise argparse.ArgumentTypeError("header name is empty")
    return name.strip(), text.strip()


def send(url, body, headers, auth, context, timeout):
    parts = parse_https_url(url)
    connection = http.client.HTTPSConnection(
        parts.hostname,
        parts.port or 443,
        timeout=timeout,
        context=context,
    )
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.putrequest("PUT", target, skip_accept_encoding=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Authorization", auth)
        connection.putheader("Content-Length", str(len(body or b"")))
        connection.endheaders()
        if body:
            connection.send(body)
        response = connection.getresponse()
        payload = response.read()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            payload,
        )
    finally:
        connection.close()


def redirect_target(current, headers, allowed, be_https_port=None):
    location = headers.get("location")
    if not location:
        raise LoadError("redirect response has no Location header")
    target = urljoin(current, location)
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https"):
        raise LoadError("redirect URL must use http or https")
    if not parts.hostname:
        raise LoadError("redirect URL has no host")
    if parts.fragment:
        raise LoadError("redirect URL must not contain a fragment")
    hostname = parts.hostname.lower()
    if hostname not in allowed:
        raise LoadError("redirect host is not in --allow-host: " + hostname)
    try:
        port = parts.port
    except ValueError as exc:
        raise LoadError("redirect URL has an invalid port") from exc
    if parts.scheme == "http":
        if be_https_port is None:
            raise LoadError(
                "plaintext redirect requires --be-https-port; "
                "refusing to send data over HTTP"
            )
        port = be_https_port
    # Doris 3.0.8 embeds Basic credentials in this Location. Never reuse or
    # persist them: reconstruct a credential-free URL and keep TLS validation.
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit(("https", netloc, parts.path, parts.query, ""))


def discover(
    url,
    headers,
    auth,
    context,
    timeout,
    allowed,
    max_redirects,
    be_https_port=None,
):
    hops = 0
    while True:
        status, response_headers, _ = send(
            url, None, headers, auth, context, timeout
        )
        if 300 <= status < 400:
            target = redirect_target(
                url, response_headers, allowed, be_https_port
            )
            hops += 1
            if hops > max_redirects:
                raise LoadError("too many redirects")
            return status, target
        return status, url


def follow(
    url,
    body,
    headers,
    auth,
    context,
    timeout,
    allowed,
    max_redirects,
    be_https_port=None,
):
    hops = 0
    while True:
        status, response_headers, payload = send(
            url, body, headers, auth, context, timeout
        )
        if 300 <= status < 400:
            url = redirect_target(
                url, response_headers, allowed, be_https_port
            )
            hops += 1
            if hops > max_redirects:
                raise LoadError("too many redirects")
            continue
        return status, url, payload


def basic_auth(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return "Basic " + token


URL_USERINFO = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
URL_SECRET = re.compile(
    r"([?&](?:password|passwd|token|secret)=)[^&#\s]*", re.IGNORECASE
)
SENSITIVE_FIELDS = ("authorization", "password", "passwd", "secret", "token")


def sanitize_text(value):
    value = URL_USERINFO.sub(r"\1[redacted]@", value)
    return URL_SECRET.sub(r"\1[redacted]", value)


def sanitize_response(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(field in normalized for field in SENSITIVE_FIELDS):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = sanitize_response(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_response(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def load(args, save_response=None):
    if not 0 < args.expected_rows:
        raise LoadError("expected rows must be positive")
    if not args.allow_host:
        raise LoadError("at least one --allow-host is required")
    password = os.environ.get(args.password_env)
    if password is None:
        raise LoadError(f"environment variable {args.password_env} is not set")
    user = args.user or os.environ.get("DORIS_USER", "root")
    allowed = {host.lower() for host in args.allow_host}
    data = args.data_file.read_bytes()
    context = ssl.create_default_context(
        cafile=str(args.cafile) if args.cafile else None
    )
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    if args.client_cert:
        context.load_cert_chain(
            certfile=str(args.client_cert),
            keyfile=str(args.client_key) if args.client_key else None,
        )
    headers = {
        "Content-Type": "application/octet-stream",
        "Accept": "application/json",
        "Expect": "100-continue",
    }
    for name, value in args.header:
        headers[name] = value
    auth = basic_auth(user, password)

    status, be_url = discover(
        args.fe_url,
        headers,
        auth,
        context,
        args.timeout,
        allowed,
        args.max_redirects,
        args.be_https_port,
    )
    if 200 <= status < 300:
        raise LoadError(
            "FE returned success without a BE redirect; refusing to send "
            "data without a validated target"
        )
    if status != 307:
        raise LoadError(f"FE redirect discovery returned HTTP {status}")
    status, be_url, payload = follow(
        be_url,
        data,
        headers,
        auth,
        context,
        args.timeout,
        allowed,
        args.max_redirects,
        args.be_https_port,
    )
    def persist_response(value):
        sanitized = sanitize_response(value)
        if save_response is not None:
            save_response(sanitized)
        return sanitized

    try:
        response = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        persist_response(
            {"RawResponse": payload.decode("utf-8", errors="replace")}
        )
        raise LoadError("BE returned invalid JSON") from exc
    if not isinstance(response, dict):
        persist_response(response)
        raise LoadError("BE response is not a JSON object")
    response = persist_response(response)
    if status < 200 or status >= 300:
        raise LoadError(f"BE Stream Load returned HTTP {status}")
    if response.get("Status") != "Success":
        raise LoadError(
            "Stream Load Status is not Success: " + str(response.get("Status"))
        )
    try:
        filtered = int(response["NumberFilteredRows"])
        loaded = int(response["NumberLoadedRows"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LoadError("Stream Load response lacks row counts") from exc
    if filtered != 0:
        raise LoadError(f"Stream Load filtered {filtered} rows")
    if loaded != args.expected_rows:
        raise LoadError(
            f"Stream Load imported {loaded} rows, "
            f"expected {args.expected_rows}"
        )
    return {
        "status": "passed",
        "fe_url": args.fe_url,
        "be_url": be_url,
        "tls_verified": True,
        "redirect_validated": True,
        "expected_rows": args.expected_rows,
        "number_loaded_rows": loaded,
        "number_filtered_rows": filtered,
        "load_status": response["Status"],
        "response": response,
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--fe-url", required=True)
    result.add_argument("--data-file", type=Path, required=True)
    result.add_argument("--allow-host", action="append", default=[])
    result.add_argument(
        "--be-https-port",
        type=int,
        help="TLS port used to upgrade Doris 3.0.8's plaintext FE redirect",
    )
    result.add_argument("--cafile", type=Path)
    result.add_argument("--client-cert", type=Path)
    result.add_argument("--client-key", type=Path)
    result.add_argument("--user")
    result.add_argument("--password-env", default="DORIS_PASSWORD")
    result.add_argument(
        "--header", type=header_value, action="append", default=[]
    )
    result.add_argument("--expected-rows", type=int, required=True)
    result.add_argument("--timeout", type=positive_timeout, default=30)
    result.add_argument("--max-redirects", type=int, default=2)
    result.add_argument("--output", type=Path)
    return result


def main():
    args = parser().parse_args()
    if not 0 <= args.max_redirects <= 8:
        parser().error("max redirects must be between 0 and 8")
    if args.be_https_port is not None and not 0 < args.be_https_port <= 65535:
        parser().error("BE HTTPS port must be between 1 and 65535")
    if args.client_cert and not args.client_key:
        parser().error("--client-cert requires --client-key")
    if args.cafile and not args.cafile.is_file():
        parser().error("cafile does not exist")
    if not args.data_file.is_file():
        parser().error("data file does not exist")
    output = args.output
    response_writer = None
    if output is not None:
        try:
            output.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            print("output directory already exists", file=sys.stderr)
            return 2
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        def response_writer(response):
            (output / "stream-load-response.json").write_text(
                json.dumps(response, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    try:
        result = load(args, save_response=response_writer)
    except (
        LoadError,
        OSError,
        ssl.SSLError,
        http.client.HTTPException,
    ) as exc:
        result = {"status": "failed", "error": str(exc), "tls_verified": None}
        print(json.dumps(result, ensure_ascii=False))
        if output is not None:
            (output / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 1
    if output is not None:
        result.pop("response")
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
