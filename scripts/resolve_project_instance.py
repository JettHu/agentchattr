"""Resolve a project instance: assign ports, create directories, update registry.

Outputs KEY=VAL lines to stdout for shell/bat eval. All errors go to stderr.

Concurrency: read-modify-write of the registry runs under a file lock, so two
launchers will not hand out the same port pair. There is a residual TOCTOU
window after the lock is released and before the spawned server actually
binds the port: an unrelated process could grab the port in between. If the
server fails to bind on startup, retry the launcher to get a fresh port.

Path quoting: project paths must not contain newline (\\n), carriage return
(\\r), or null bytes — those break the KEY=VAL stdout protocol. Single
quotes (') in paths are also unsupported because launcher shell glue may
re-embed paths into single-quoted strings.
"""

import argparse
import errno
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "data" / "project_instances.json"
LOCK_PATH = REGISTRY_PATH.with_suffix(".json.lock")

WEB_PORT_RANGE = range(8300, 8400)
MCP_HTTP_PORT_START = 8200
MCP_SSE_PORT_START = 8201
MCP_PORT_STEP = 2
MCP_PORT_COUNT = 100

DEFAULT_WEB_PORT = 8300
DEFAULT_MCP_HTTP_PORT = 8200
DEFAULT_MCP_SSE_PORT = 8201


def _port_is_free(port: int) -> bool:
    # Probe via bind-then-close. Intentionally no SO_REUSEADDR: we want bind()
    # to fail when another process is currently bound, so we don't hand out a
    # port that is already in use. There's a residual TOCTOU window between
    # the lock release here and the server's eventual bind() — see resolve()
    # docstring.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _port_in_use(port: int, timeout: float = 0.5) -> bool:
    # Inverse of _port_is_free for `list`: returns True if some process is
    # currently listening on 127.0.0.1:port. Uses connect() because bind()
    # tells us nothing about external listeners.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _fetch_instance_info(port: int, timeout: float = 1.5):
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/instance",
            timeout=timeout,
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, socket.timeout):
        return None


def _read_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"projects": {}}
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"projects": {}}


def _write_registry(data: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(REGISTRY_PATH.parent), suffix=".tmp", prefix="registry_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, str(REGISTRY_PATH))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_PATH, "w")
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as exc:
            lock_fd.close()
            print(
                f"Error: registry locked by another process; please retry. ({exc})",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd


def _release_lock(lock_fd) -> None:
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    lock_fd.close()


def _find_free_web_port(registry: dict) -> int:
    used = {
        rec["web_port"]
        for rec in registry.get("projects", {}).values()
        if "web_port" in rec
    }
    for port in WEB_PORT_RANGE:
        if port == DEFAULT_WEB_PORT:
            continue
        if port not in used and _port_is_free(port):
            return port
    print("Error: no free web port in range 8300-8399", file=sys.stderr)
    sys.exit(1)


def _find_free_mcp_ports(registry: dict) -> tuple[int, int]:
    used_http = {
        rec["mcp_http_port"]
        for rec in registry.get("projects", {}).values()
        if "mcp_http_port" in rec
    }
    used_sse = {
        rec["mcp_sse_port"]
        for rec in registry.get("projects", {}).values()
        if "mcp_sse_port" in rec
    }
    for i in range(MCP_PORT_COUNT):
        http_port = MCP_HTTP_PORT_START + i * MCP_PORT_STEP
        sse_port = MCP_SSE_PORT_START + i * MCP_PORT_STEP
        # Skip default instance ports
        if http_port == DEFAULT_MCP_HTTP_PORT:
            continue
        if (
            http_port not in used_http
            and sse_port not in used_sse
            and _port_is_free(http_port)
            and _port_is_free(sse_port)
        ):
            return http_port, sse_port
    print("Error: no free MCP port pair available", file=sys.stderr)
    sys.exit(1)


def _resolve_project_id(basename: str, abs_path: str, registry: dict) -> str:
    for path, rec in registry.get("projects", {}).items():
        if path != abs_path and rec.get("project_id") == basename:
            short_hash = hashlib.sha1(abs_path.encode()).hexdigest()[:8]
            return f"{basename}-{short_hash}"
    return basename


def resolve(args: argparse.Namespace) -> None:
    if args.project is None:
        return

    project_path = Path(args.project).resolve()
    abs_path = str(project_path)

    if "\n" in abs_path or "\r" in abs_path:
        print("Error: project path contains newline characters", file=sys.stderr)
        sys.exit(1)

    basename = project_path.name
    project_name = args.project_name or basename

    agentchattr_dir = project_path / ".agentchattr"
    data_dir = agentchattr_dir / "data"
    upload_dir = agentchattr_dir / "uploads"
    artifact_root = (
        Path(args.artifact_root).resolve()
        if args.artifact_root
        else agentchattr_dir / "artifacts"
    )

    data_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)

    lock_fd = _acquire_lock()
    try:
        registry = _read_registry()
        existing = registry.get("projects", {}).get(abs_path)

        if existing:
            web_port = existing["web_port"]
            mcp_http_port = existing["mcp_http_port"]
            mcp_sse_port = existing["mcp_sse_port"]
            project_id = existing.get("project_id", basename)

            if args.port is not None:
                web_port = args.port
            if args.mcp_http_port is not None:
                mcp_http_port = args.mcp_http_port
            if args.mcp_sse_port is not None:
                mcp_sse_port = args.mcp_sse_port

            # If our own server for this project is already running on the
            # recorded web_port, reuse all ports unconditionally so a second
            # launcher (e.g. running start_codex.sh after start_claude.sh
            # with the same --project) attaches instead of reassigning a
            # fresh port and starting a duplicate server.
            same_project_running = (
                args.port is None
                and args.mcp_http_port is None
                and args.mcp_sse_port is None
                and _classify_record(abs_path, {"web_port": web_port}) == "running"
            )

            if not same_project_running:
                # Verify reused ports are actually free
                for name, port in [
                    ("web", web_port),
                    ("mcp-http", mcp_http_port),
                    ("mcp-sse", mcp_sse_port),
                ]:
                    if not _port_is_free(port):
                        if (name == "web" and args.port is not None) or \
                           (name == "mcp-http" and args.mcp_http_port is not None) or \
                           (name == "mcp-sse" and args.mcp_sse_port is not None):
                            print(
                                f"Error: explicit {name} port {port} is in use",
                                file=sys.stderr,
                            )
                            sys.exit(1)
                        # Occupied by unrelated process — reassign
                        if name == "web":
                            web_port = _find_free_web_port(registry)
                        elif name == "mcp-http":
                            mcp_http_port, mcp_sse_port = _find_free_mcp_ports(registry)
                        elif name == "mcp-sse":
                            mcp_http_port, mcp_sse_port = _find_free_mcp_ports(registry)
        else:
            project_id = _resolve_project_id(basename, abs_path, registry)

            if args.port is not None:
                if not _port_is_free(args.port):
                    print(
                        f"Error: explicit web port {args.port} is in use",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                web_port = args.port
            else:
                web_port = _find_free_web_port(registry)

            if args.mcp_http_port is not None:
                if not _port_is_free(args.mcp_http_port):
                    print(
                        f"Error: explicit mcp-http port {args.mcp_http_port} is in use",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                mcp_http_port = args.mcp_http_port
            else:
                mcp_http_port = None

            if args.mcp_sse_port is not None:
                if not _port_is_free(args.mcp_sse_port):
                    print(
                        f"Error: explicit mcp-sse port {args.mcp_sse_port} is in use",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                mcp_sse_port = args.mcp_sse_port
            else:
                mcp_sse_port = None

            if mcp_http_port is None or mcp_sse_port is None:
                auto_http, auto_sse = _find_free_mcp_ports(registry)
                if mcp_http_port is None:
                    mcp_http_port = auto_http
                if mcp_sse_port is None:
                    mcp_sse_port = auto_sse

        record = {
            "project_id": project_id,
            "web_port": web_port,
            "mcp_http_port": mcp_http_port,
            "mcp_sse_port": mcp_sse_port,
            "data_dir": str(data_dir),
            "upload_dir": str(upload_dir),
            "artifact_root": str(artifact_root),
            "updated_at": int(time.time()),
        }
        registry.setdefault("projects", {})[abs_path] = record
        _write_registry(registry)
    finally:
        _release_lock(lock_fd)

    lines = [
        f"AGENTCHATTR_PROJECT={abs_path}",
        f"AGENTCHATTR_PROJECT_ID={project_id}",
        f"AGENTCHATTR_PROJECT_NAME={project_name}",
        f"AGENTCHATTR_DATA_DIR={data_dir}",
        f"AGENTCHATTR_UPLOAD_DIR={upload_dir}",
        f"AGENTCHATTR_ARTIFACT_ROOT={artifact_root}",
        f"AGENTCHATTR_PORT={web_port}",
        f"AGENTCHATTR_MCP_HTTP_PORT={mcp_http_port}",
        f"AGENTCHATTR_MCP_SSE_PORT={mcp_sse_port}",
    ]
    for line in lines:
        print(line)


def _classify_record(abs_path: str, rec: dict) -> str:
    web_port = rec.get("web_port")
    if not web_port or not _port_in_use(web_port):
        return "stale"
    info = _fetch_instance_info(web_port)
    if info is None:
        return "port-conflict"
    if info.get("project_path") and info["project_path"] != abs_path:
        return "port-conflict"
    return "running"


def list_cmd(args: argparse.Namespace) -> None:
    registry = _read_registry()
    projects = registry.get("projects", {})

    rows = []
    for abs_path, rec in sorted(projects.items()):
        status = _classify_record(abs_path, rec)
        rows.append({
            "status": status,
            "project_path": abs_path,
            "project_id": rec.get("project_id", ""),
            "web_port": rec.get("web_port"),
            "mcp_http_port": rec.get("mcp_http_port"),
            "mcp_sse_port": rec.get("mcp_sse_port"),
            "data_dir": rec.get("data_dir", ""),
            "updated_at": rec.get("updated_at"),
        })

    if args.json:
        print(json.dumps({"projects": rows}, indent=2))
        return

    if not rows:
        print("(no projects in registry)")
        return

    fmt = "{:<13} {:<6} {:<8} {:<7} {:<24} {}"
    print(fmt.format("STATUS", "WEB", "MCP_HTTP", "MCP_SSE", "ID", "PATH"))
    for row in rows:
        ts = row["updated_at"]
        ts_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts else "-"
        print(fmt.format(
            row["status"],
            str(row["web_port"] or "-"),
            str(row["mcp_http_port"] or "-"),
            str(row["mcp_sse_port"] or "-"),
            row["project_id"][:23],
            f"{row['project_path']}  ({ts_s})",
        ))


def forget_cmd(args: argparse.Namespace) -> int:
    if not args.project and not args.all_stale:
        print("Error: forget requires --project <path> or --all-stale", file=sys.stderr)
        return 2

    lock_fd = _acquire_lock()
    try:
        registry = _read_registry()
        projects = registry.get("projects", {})

        targets = []
        if args.all_stale:
            for abs_path, rec in list(projects.items()):
                if _classify_record(abs_path, rec) == "stale":
                    targets.append(abs_path)
        else:
            project_path = Path(args.project).resolve()
            abs_path = str(project_path)
            if abs_path not in projects:
                print(f"Error: {abs_path} not in registry", file=sys.stderr)
                return 1
            targets.append(abs_path)

        if not targets:
            print("No matching records to forget.")
            return 0

        for abs_path in list(targets):
            status = _classify_record(abs_path, projects.get(abs_path, {}))
            if status == "running" and not args.force:
                print(
                    f"Refusing to forget running project {abs_path} (use --force to override)",
                    file=sys.stderr,
                )
                targets.remove(abs_path)

        if not targets:
            return 1

        for abs_path in targets:
            del projects[abs_path]
            print(f"Forgot {abs_path}")

        _write_registry(registry)
    finally:
        _release_lock(lock_fd)
    return 0


def stop_cmd(args: argparse.Namespace) -> int:
    print(
        "stop is not implemented in V1. Stop the agentchattr server process "
        "manually (e.g. close the launcher's terminal window or kill the PID "
        "listening on the project's web port).",
        file=sys.stderr,
    )
    return 2


def _add_resolve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=str, default=None)
    parser.add_argument("--project-name", type=str, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--mcp-http-port", type=int, default=None)
    parser.add_argument("--mcp-sse-port", type=int, default=None)
    parser.add_argument("--artifact-root", type=str, default=None)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve / list / forget agentchattr project instances."
    )
    sub = parser.add_subparsers(dest="command")

    resolve_parser = sub.add_parser("resolve", help="Resolve a project instance (default).")
    _add_resolve_args(resolve_parser)

    list_parser = sub.add_parser("list", help="List registered projects with running/stale status.")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    forget_parser = sub.add_parser("forget", help="Remove a project from the registry (data files preserved).")
    forget_parser.add_argument("--project", type=str, default=None)
    forget_parser.add_argument("--all-stale", action="store_true",
                               help="Forget every record currently classified as stale.")
    forget_parser.add_argument("--force", action="store_true",
                               help="Forget even if the project is currently running.")

    sub.add_parser("stop", help="(not implemented in V1)")

    _add_resolve_args(parser)

    args = parser.parse_args()
    cmd = args.command or "resolve"
    if cmd == "list":
        list_cmd(args)
    elif cmd == "forget":
        sys.exit(forget_cmd(args))
    elif cmd == "stop":
        sys.exit(stop_cmd(args))
    else:
        resolve(args)


if __name__ == "__main__":
    main()
