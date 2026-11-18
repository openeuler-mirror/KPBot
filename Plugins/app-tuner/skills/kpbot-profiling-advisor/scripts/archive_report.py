#!/usr/bin/env python3
"""Publish a Markdown report and its local references as a flat, relocatable ZIP."""
import argparse
import hashlib
import os
import re
import tempfile
from collections import deque
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile


# Keep example commands and inline code literal. Reference definitions and HTML
# images are used by some of the Markdown documents linked from tuning reports.
TOKENS = re.compile(
    r"(?P<fence>^[ ]{0,3}(?P<marker>`{3,}|~{3,})[^\n]*\n[\s\S]*?^[ ]{0,3}(?P=marker)[ \t]*$)"
    r"|(?P<code>(?P<ticks>`+)[^`\n]*?(?P=ticks))"
    r"|(?P<inline>\]\([ \t]*(?P<target><[^>\n]+>|(?:\\.|[^\s()\\]|\((?:\\.|[^()\\])*\))+))"
    r"|(?P<definition>^[ ]{0,3}\[[^\]\n]+\]:[ \t]*(?P<ref><[^>\n]+>|[^\s]+))"
    r"|(?P<html><(?:img|a|source)\b[^>\n]*?\b(?:src|href)=[\"'](?P<attr>[^\"']+)[\"'])",
    re.MULTILINE | re.IGNORECASE,
)


def evidence_files(value, base, key=""):
    """Find recorded files in report schema fields relative to the input JSON."""
    if isinstance(value, dict):
        for field, item in value.items():
            yield from evidence_files(item, base, field)
    elif isinstance(value, list):
        for item in value:
            yield from evidence_files(item, base, key)
    elif isinstance(value, str) and value and (
        key.endswith(("_path", "_paths", "_manifest", "_evidence"))
        or key in {"logs", "evidence", "evidence_sources", "evidence_refs", "report_attachments"}
    ):
        path = base / value
        if key == "report_attachments":
            yield path
        elif key != "final_report_path" and "\n" not in value and len(value) < 4096:
            try:
                if path.is_file():
                    yield path
            except OSError:
                pass


def archive_report(report, output, attachments=(), *, attachment_number=None):
    report = Path(report).resolve(strict=True)
    output = Path(output).resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("归档输出需要 .zip 后缀")
    names = {}
    used = {"sha256sums.txt"}
    digests = {}
    queue = deque()

    def register(path):
        path = Path(path).resolve(strict=True)
        if not path.is_file() or path == output:
            raise ValueError(f"附件必须是普通文件，且与输出 ZIP 不同：{path}")
        if path not in names:
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", path.name).rstrip(". ")
            if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", name, re.I):
                name = "_" + name
            slug = Path(name)
            number = 1
            while name.casefold() in used:
                number += 1
                name = f"{slug.stem}-{number}{slug.suffix}"
            names[path] = name
            used.add(name.casefold())
            queue.append(path)
        return names[path]

    register(report)
    extra = []
    for attachment in attachments:
        name = register(attachment)
        if name != names[report] and name not in extra:
            extra.append(name)

    def rewrite(match, source):
        field = next((k for k in ("target", "ref", "attr") if match[k]), None)
        if field is None:
            return match[0]
        target = match[field].strip("<>")
        target = re.sub(r"\\([() ])", r"\1", target)
        uri = urlsplit(target)
        if uri.scheme not in ("", "file") or uri.netloc or not uri.path:
            return match[0]
        path = source.parent / unquote(uri.path)
        try:
            name = register(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"{source} 引用的文件无法归档：{target}") from exc
        ref = quote(name)
        if uri.query:
            ref += "?" + uri.query
        if uri.fragment:
            ref += "#" + uri.fragment
        left, right = match.span(field)
        return match[0][:left-match.start()] + ref + match[0][right-match.start():]

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as f:
        temporary = Path(f.name)
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED, compresslevel=5) as archive:
            while queue:
                source = queue.popleft()
                if source.suffix.lower() not in (".md", ".markdown"):
                    digest = hashlib.sha256()
                    with source.open("rb") as inp, archive.open(names[source], "w", force_zip64=True) as dest:
                        for chunk in iter(lambda: inp.read(1024 * 1024), b""):
                            digest.update(chunk)
                            dest.write(chunk)
                    digests[names[source]] = digest.hexdigest()
                    continue
                content = TOKENS.sub(lambda m: rewrite(m, source), source.read_text(encoding="utf-8"))
                if source == report and extra:
                    prefix = f"{attachment_number} " if attachment_number is not None else ""
                    content += f"\n## {prefix}随包附件\n\n" + "".join(
                        f"- [{name}](<{quote(name)}>)\n" for name in extra
                    )
                blob = content.encode("utf-8")
                archive.writestr(names[source], blob)
                digests[names[source]] = hashlib.sha256(blob).hexdigest()
            archive.writestr("SHA256SUMS.txt", "".join(
                f"{digest}  {name}\n" for name, digest in sorted(digests.items())
            ))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(names) + 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--attach", action="append", default=[], type=Path)
    args = parser.parse_args()
    output = args.output or args.report.with_suffix(".zip")
    try:
        count = archive_report(args.report, output, args.attach)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(f"{output} ({count} files)")


if __name__ == "__main__":
    main()
