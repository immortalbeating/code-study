#!/usr/bin/env python3
"""Capture and check explicitly selected source files; never execute project code."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
import tempfile


class SnapshotError(Exception):
    pass


def emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def root_path(value):
    root = Path(value).resolve(strict=True)
    if not root.is_dir():
        raise SnapshotError("project root must be a directory")
    return root


def source_path(root, value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SnapshotError("file path must be a nonempty relative string")
    normalized = value.replace("\\", "/")
    if PurePosixPath(normalized).is_absolute() or PureWindowsPath(value).drive or PureWindowsPath(value).is_absolute():
        raise SnapshotError("absolute file paths are not allowed")
    # Keep the registered route, so retargeting a symlink/junction is observable.
    registered = Path(os.path.abspath(root / normalized))
    candidate = registered.resolve()
    try:
        relative = registered.relative_to(root).as_posix()
        candidate.relative_to(root)
    except ValueError:
        raise SnapshotError("file path escapes project root") from None
    if relative == ".":
        raise SnapshotError("file path must identify a file")
    return candidate, relative


def digest(path):
    if not path.is_file():
        raise SnapshotError("source must be a regular file")
    total = 0
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(block)
            sha.update(block)
    return sha.hexdigest(), total


def git_context(root):
    def query(*args):
        try:
            result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=5, check=False)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return None
    repository = query("rev-parse", "--show-toplevel")
    return {"repository_root": str(Path(repository).resolve()) if repository else None,
            "head": query("rev-parse", "--verify", "HEAD") if repository else None}


def capture(args):
    root = root_path(args.root)
    output = Path(args.output).resolve()
    records = []
    seen = set()
    for value in args.file:
        path, relative = source_path(root, value)
        if path == output or (output.exists() and path.exists() and os.path.samefile(path, output)):
            raise SnapshotError("snapshot output cannot be a source file")
        if relative in seen:
            continue
        sha, size = digest(path)
        records.append({"path": relative, "sha256": sha, "size_bytes": size})
        seen.add(relative)
    if output.exists() and not args.force:
        raise SnapshotError("snapshot output exists; use --force to replace it")
    snapshot = {"schema_version": 1, "project_root": str(root),
                "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "git": git_context(root), "files": records}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if args.force:
            os.replace(temporary, output)
        else:
            # A hard link publishes the complete file atomically without clobbering a racing writer.
            os.link(temporary, output)
        emit({"status": "captured", "snapshot": str(output), "file_count": len(records)})
        return 0
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate(snapshot):
    if not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise SnapshotError("unsupported or invalid snapshot schema")
    for field in ("project_root", "captured_at_utc"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field]:
            raise SnapshotError("invalid snapshot " + field)
    if not Path(snapshot["project_root"]).is_absolute():
        raise SnapshotError("snapshot project_root must be absolute")
    try:
        timestamp = datetime.datetime.fromisoformat(snapshot["captured_at_utc"].replace("Z", "+00:00"))
        if timestamp.utcoffset() != datetime.timedelta(0):
            raise ValueError()
    except ValueError:
        raise SnapshotError("invalid UTC capture timestamp") from None
    git = snapshot.get("git")
    if not isinstance(git, dict) or any(key not in git or (git[key] is not None and not isinstance(git[key], str)) for key in ("repository_root", "head")):
        raise SnapshotError("invalid snapshot git fields")
    files = snapshot.get("files")
    if not isinstance(files, list) or not files:
        raise SnapshotError("snapshot files must be a nonempty list")
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str) or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", record["sha256"]) or type(record.get("size_bytes")) is not int or record["size_bytes"] < 0:
            raise SnapshotError("invalid snapshot file record")


def check(args):
    snapshot_path = Path(args.snapshot).resolve()
    with snapshot_path.open(encoding="utf-8") as stream:
        snapshot = json.load(stream)
    validate(snapshot)
    root = root_path(args.root or snapshot["project_root"])
    result = {"status": "current", "project_root_changed": str(root) != snapshot["project_root"],
              "git_head_changed": git_context(root)["head"] != snapshot["git"]["head"],
              "unchanged": [], "changed": [], "missing": [], "unreadable": []}
    seen = set()
    for record in snapshot["files"]:
        path, relative = source_path(root, record["path"])
        if path == snapshot_path or (path.exists() and os.path.samefile(path, snapshot_path)) or relative in seen:
            raise SnapshotError("snapshot contains itself or duplicate file paths")
        seen.add(relative)
        try:
            sha, size = digest(path)
        except (OSError, SnapshotError):
            result["unreadable" if path.exists() else "missing"].append(relative)
        else:
            result["unchanged" if sha.lower() == record["sha256"].lower() and size == record["size_bytes"] else "changed"].append(relative)
    if result["unreadable"]:
        result["status"] = "unavailable"
    elif result["project_root_changed"] or result["changed"] or result["missing"]:
        result["status"] = "changed"
    emit(result)
    return {"current": 0, "changed": 1, "unavailable": 2}[result["status"]]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SnapshotError(message)


def main():
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    create = commands.add_parser("capture")
    create.add_argument("--root", required=True)
    create.add_argument("--file", action="append", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--force", action="store_true")
    compare = commands.add_parser("check")
    compare.add_argument("--snapshot", required=True)
    compare.add_argument("--root")
    try:
        args = parser.parse_args()
        return capture(args) if args.command == "capture" else check(args)
    except (SnapshotError, OSError, ValueError, RuntimeError) as error:
        emit({"status": "unavailable", "error": str(error)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
