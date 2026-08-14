#!/usr/bin/env python3
"""
Llama.cpp Prompt-Cache Session Manager
=======================================

A small, persistent interactive terminal program that manages llama.cpp
prompt-cache files through llama.cpp's HTTP /slots/... API.

The program is OS-agnostic by design: all logic lives in this single
Python 3 standard-library script. Thin, OS-specific launchers (a macOS
.command file, a Windows .bat file, etc.) simply invoke this script, so
the user-visible behavior is identical across operating systems.

This file is organized to mirror the suggested architecture:

    Input / command parser
            |
            +--> Session menu handling
            |
            +--> Session name validation
            |
            +--> Filesystem / session discovery
            |
            +--> llama.cpp HTTP API client
            |
            +--> Status / error reporting
            |
            +--> Home-screen renderer

No third-party dependencies are required (only the Python standard
library), so nothing extra needs to be installed on a stock macOS
install that has Python 3.
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

CACHE_EXT = ".bin"                     # extension used for managed session files
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SERVER_URL = "http://127.0.0.1:8080"
DEFAULT_SLOT_ID = 0
DEFAULT_CACHE_DIR = SCRIPT_DIR / "llama_sessions"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"
HTTP_TIMEOUT_SECONDS = 20
HEALTH_CHECK_TIMEOUT_SECONDS = 2
MAX_SESSION_NAME_LENGTH = 128

COMMANDS_HELP = [
    ("-save [name]", "Save slot 0 to a session"),
    ("-load [name]", "Load a session into slot 0"),
    ("-delete [name]", "Delete a session"),
    ("-list", "List all sessions"),
    ("-exit", "Exit"),
]


# ---------------------------------------------------------------------------
# Small utility exceptions
# ---------------------------------------------------------------------------

class OperationCancelled(Exception):
    """Raised internally when the user cancels a multi-step prompt (Ctrl-C/Ctrl-D)."""


class ApiError(Exception):
    """Raised when the llama.cpp HTTP API cannot be reached or returns an error."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    server_url: str
    slot_id: int
    cache_dir: Path


def parse_cli_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Llama.cpp Prompt-Cache Session Manager"
    )
    parser.add_argument("--server", help="llama.cpp server base URL")
    parser.add_argument("--slot", type=int, help="target slot id")
    parser.add_argument("--cache-dir", help="path to llama.cpp's --slot-save-path directory")
    parser.add_argument("--config", help="path to a config.json file", default=None)
    return parser.parse_args(argv)


def load_config(argv: List[str]) -> Config:
    """
    Resolve configuration with this precedence (highest wins):

        command-line arguments  >  environment variables  >  config.json  >  built-in defaults
    """
    args = parse_cli_args(argv)
    config_path = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH

    values = {
        "server_url": DEFAULT_SERVER_URL,
        "slot_id": DEFAULT_SLOT_ID,
        "cache_dir": str(DEFAULT_CACHE_DIR),
    }

    # config.json (created on first run so it's easy to find and edit)
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                file_values = json.load(fh)
            for key in ("server_url", "slot_id", "cache_dir"):
                if key in file_values:
                    values[key] = file_values[key]
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: could not read '{config_path.name}' ({exc}). Using defaults.")
    else:
        try:
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(values, fh, indent=2)
                fh.write("\n")
        except OSError:
            pass  # non-fatal: config file is a convenience, not a requirement

    # Environment variables
    if os.environ.get("LLAMA_CACHE_SERVER"):
        values["server_url"] = os.environ["LLAMA_CACHE_SERVER"]
    if os.environ.get("LLAMA_CACHE_SLOT"):
        try:
            values["slot_id"] = int(os.environ["LLAMA_CACHE_SLOT"])
        except ValueError:
            pass
    if os.environ.get("LLAMA_CACHE_DIR"):
        values["cache_dir"] = os.environ["LLAMA_CACHE_DIR"]

    # Command-line arguments (highest precedence)
    if args.server:
        values["server_url"] = args.server
    if args.slot is not None:
        values["slot_id"] = args.slot
    if args.cache_dir:
        values["cache_dir"] = args.cache_dir

    cache_dir = Path(str(values["cache_dir"])).expanduser()

    return Config(
        server_url=str(values["server_url"]).rstrip("/"),
        slot_id=int(values["slot_id"]),
        cache_dir=cache_dir,
    )


# ---------------------------------------------------------------------------
# llama.cpp HTTP API client
# ---------------------------------------------------------------------------

class LlamaClient:
    """Thin wrapper around llama.cpp's /slots/{id}?action=... HTTP API."""

    def __init__(self, server_url: str, timeout: int = HTTP_TIMEOUT_SECONDS):
        self.server_url = server_url
        self.timeout = timeout

    def _post_json(self, path: str, payload: dict) -> dict:
        url = f"{self.server_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.getcode()
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise ApiError(
                f"Could not reach llama.cpp server at {self.server_url} ({reason})."
            ) from exc
        except socket.timeout as exc:
            raise ApiError(f"Request to {self.server_url} timed out.") from exc
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected as ApiError
            raise ApiError(f"Unexpected network error: {exc}") from exc

        parsed: dict = {}
        if body:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ApiError(
                    f"Server returned a malformed response (HTTP {status})."
                ) from exc

        if status < 200 or status >= 300:
            message = None
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(err, dict):
                message = err.get("message")
            elif isinstance(err, str):
                message = err
            raise ApiError(message or f"Server returned HTTP {status}.")

        if not isinstance(parsed, dict):
            raise ApiError("Server returned an unexpected response format.")

        return parsed

    def save_slot(self, slot_id: int, filename: str) -> dict:
        return self._post_json(f"/slots/{slot_id}?action=save", {"filename": filename})

    def restore_slot(self, slot_id: int, filename: str) -> dict:
        return self._post_json(f"/slots/{slot_id}?action=restore", {"filename": filename})

    def is_reachable(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.server_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=HEALTH_CHECK_TIMEOUT_SECONDS):
                return True
        except Exception:
            # Some llama.cpp builds may not expose /health; fall back to a
            # bare connection attempt to the base URL before giving up.
            try:
                req = urllib.request.Request(self.server_url, method="GET")
                with urllib.request.urlopen(req, timeout=HEALTH_CHECK_TIMEOUT_SECONDS):
                    return True
            except Exception:
                return False


# ---------------------------------------------------------------------------
# Session discovery / filesystem helpers
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    name: str
    path: Path
    size: int
    created_epoch: float


class SessionDiscoveryError(Exception):
    """Raised when the cache directory cannot be listed."""


def discover_sessions(cache_dir: Path) -> List[SessionInfo]:
    """
    Return every managed session in `cache_dir`, sorted alphabetically
    (case-insensitive) by session name for a stable, predictable ordering.
    """
    if not cache_dir.exists():
        return []

    try:
        entries = os.listdir(cache_dir)
    except PermissionError as exc:
        raise SessionDiscoveryError(
            f"Cache directory is not readable: {cache_dir}"
        ) from exc
    except OSError as exc:
        raise SessionDiscoveryError(f"Could not read cache directory: {exc}") from exc

    sessions: List[SessionInfo] = []
    for entry in entries:
        full_path = cache_dir / entry
        if entry.startswith("."):
            continue
        if full_path.suffix.lower() != CACHE_EXT:
            continue
        if not full_path.is_file():
            continue
        try:
            stat_result = full_path.stat()
        except OSError:
            continue  # skip files we can't stat (permissions, race conditions, etc.)

        # True creation time where the OS provides one:
        #   - macOS/BSD expose it directly as st_birthtime.
        #   - Windows has no st_birthtime, but st_ctime *is* creation time there
        #     (unlike Linux, where st_ctime is metadata-change time - the best
        #     available fallback on that platform).
        created_epoch = getattr(stat_result, "st_birthtime", None)
        if created_epoch is None:
            created_epoch = stat_result.st_ctime

        sessions.append(
            SessionInfo(
                name=full_path.stem,
                path=full_path,
                size=stat_result.st_size,
                created_epoch=created_epoch,
            )
        )

    sessions.sort(key=lambda s: s.name.lower())
    return sessions


def validate_session_name(name: Optional[str]) -> Optional[str]:
    """Return an error message if `name` is unsafe/invalid, else None."""
    if name is None:
        return "Session name cannot be empty."
    name = name.strip()
    if not name:
        return "Session name cannot be empty."
    if len(name) > MAX_SESSION_NAME_LENGTH:
        return f"Session name is too long (max {MAX_SESSION_NAME_LENGTH} characters)."
    if "/" in name or "\\" in name:
        return "Session name cannot contain path separators ('/' or '\\')."
    if ".." in name:
        return "Session name cannot contain '..'."
    if name in (".", ".."):
        return "Invalid session name."
    if name.startswith("."):
        return "Session name cannot start with a period."
    if any(ord(ch) < 32 for ch in name):
        return "Session name contains invalid control characters."
    if name.lower() == DEFAULT_CONFIG_PATH.stem.lower():
        return "That name is reserved for the program's configuration file."
    return None


def resolve_session_path(cache_dir: Path, name: str) -> Path:
    """
    Build the on-disk path for `name` and defensively verify it still
    resolves to a direct child of `cache_dir` (protects against path
    traversal even if validate_session_name() is ever bypassed).
    """
    cache_dir_resolved = cache_dir.resolve()
    candidate = (cache_dir_resolved / f"{name}{CACHE_EXT}").resolve()
    if candidate.parent != cache_dir_resolved:
        raise ValueError("Resolved session path escapes the cache directory.")
    return candidate


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    return f"{value:.0f} PB"  # pragma: no cover - effectively unreachable


def format_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def total_size(sessions: List[SessionInfo]) -> int:
    return sum(s.size for s in sessions)


# ---------------------------------------------------------------------------
# Input helpers (Ctrl-C / Ctrl-D safe)
# ---------------------------------------------------------------------------

def read_line(prompt: str) -> str:
    """input() wrapper that turns Ctrl-C/Ctrl-D into a clean cancellation."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise OperationCancelled()


def confirm(prompt: str) -> bool:
    answer = read_line(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Screen rendering
# ---------------------------------------------------------------------------

def render_commands_block() -> str:
    lines = ["Commands:"]
    width = max(len(c) for c, _ in COMMANDS_HELP)
    for cmd, desc in COMMANDS_HELP:
        lines.append(f"  {cmd:<{width}}   {desc}")
    return "\n".join(lines)


def print_startup_banner(config: Config, client: LlamaClient) -> None:
    try:
        sessions = discover_sessions(config.cache_dir)
        count_text = str(len(sessions))
        size_text = human_size(total_size(sessions))
    except SessionDiscoveryError as exc:
        count_text = "?"
        size_text = "?"
        print(f"Warning: {exc}")

    reachable = client.is_reachable()
    server_status = "reachable" if reachable else "NOT reachable"

    print("=" * 44)
    print("Llama.cpp Cache Session Manager")
    print("=" * 44)
    print()
    print(f"Cache directory : {config.cache_dir}")
    print(f"Server          : {config.server_url} ({server_status})")
    print(f"Target slot     : {config.slot_id}")
    print(f"Sessions        : {count_text}")
    print(f"Total size      : {size_text}")
    print()
    if not reachable:
        print(
            "Note: the llama.cpp server is not reachable right now. '-list' will\n"
            "still work, but '-save' and '-load' require a running server started\n"
            f"with: --slot-save-path \"{config.cache_dir}\"\n"
        )
    print(render_commands_block())
    print()


def print_home(config: Config) -> None:
    """Reprinted after every completed operation, per spec section 12/19."""
    try:
        sessions = discover_sessions(config.cache_dir)
        count_text = str(len(sessions))
        size_text = human_size(total_size(sessions))
    except SessionDiscoveryError as exc:
        count_text = "?"
        size_text = "?"
        print(f"Error: {exc}")

    print("-" * 44)
    print(f"Sessions   : {count_text}")
    print(f"Total size : {size_text}")
    print("-" * 44)
    print(render_commands_block())
    print()


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def print_error(message: str) -> None:
    print(f"Error: {message}")


def list_sessions_table(sessions: List[SessionInfo]) -> None:
    print("Stored sessions:")
    print()
    if not sessions:
        print("No stored sessions.")
        return
    name_width = max((len(s.name) for s in sessions), default=4)
    name_width = max(name_width, 4)
    for idx, session in enumerate(sessions, start=1):
        size_text = human_size(session.size)
        date_text = format_timestamp(session.created_epoch)
        print(f"{idx:02d} - {session.name:<{name_width}}  {size_text:>8}  {date_text}")


def cmd_list(config: Config) -> None:
    try:
        sessions = discover_sessions(config.cache_dir)
    except SessionDiscoveryError as exc:
        print_error(str(exc))
        return
    list_sessions_table(sessions)


def _do_save(config: Config, client: LlamaClient, name: str) -> None:
    """Perform the actual save-to-slot HTTP call + report the result."""
    path = resolve_session_path(config.cache_dir, name)
    try:
        config.cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print_error(f"Cache directory could not be created/written: {exc}")
        return

    print(f"Saving slot {config.slot_id} to '{name}'...")
    try:
        client.save_slot(config.slot_id, path.name)
    except ApiError as exc:
        print_error(str(exc))
        return

    if not path.exists():
        print_error(
            "llama.cpp reported success but no cache file was found. "
            "Check that the manager's cache directory matches the server's "
            "--slot-save-path."
        )
        return

    size_text = human_size(path.stat().st_size)
    print(f"Slot {config.slot_id} saved to {name} ({size_text}).")


def cmd_save(config: Config, client: LlamaClient, arg: Optional[str]) -> None:
    try:
        sessions = discover_sessions(config.cache_dir)
    except SessionDiscoveryError as exc:
        print_error(str(exc))
        return

    try:
        if arg:
            name = arg.strip()
            error = validate_session_name(name)
            if error:
                print_error(error)
                return
            path = resolve_session_path(config.cache_dir, name)
            if path.exists():
                if not confirm(
                    f"Session '{name}' already exists ({human_size(path.stat().st_size)}). Overwrite?"
                ):
                    print("Save cancelled.")
                    return
            _do_save(config, client, name)
            return

        # No name given -> numbered selection menu
        print("Stored sessions:")
        print()
        print("00 - Create new session")
        for idx, session in enumerate(sessions, start=1):
            print(f"{idx:02d} - {session.name}")
        print()
        selection = read_line("Select session: ").strip()

        if not selection.isdigit():
            print_error("Invalid selection.")
            return
        choice = int(selection)

        if choice == 0:
            new_name = read_line("New session name: ").strip()
            error = validate_session_name(new_name)
            if error:
                print_error(error)
                return
            path = resolve_session_path(config.cache_dir, new_name)
            if path.exists():
                print_error(f"session '{new_name}' already exists.")
                return
            _do_save(config, client, new_name)
            return

        if 1 <= choice <= len(sessions):
            target = sessions[choice - 1]
            if not confirm(
                f"This will overwrite '{target.name}' ({human_size(target.size)}). Continue?"
            ):
                print("Save cancelled.")
                return
            _do_save(config, client, target.name)
            return

        print_error("Invalid selection.")
    except OperationCancelled:
        print("Save cancelled.")


def _do_load(config: Config, client: LlamaClient, session: SessionInfo) -> None:
    print(f"Loading '{session.name}' into slot {config.slot_id}...")
    try:
        client.restore_slot(config.slot_id, session.path.name)
    except ApiError as exc:
        print_error(str(exc))
        return

    try:
        size_text = human_size(session.path.stat().st_size)
    except OSError:
        size_text = human_size(session.size)

    print(f"{session.name} loaded to Slot {config.slot_id} ({size_text}).")


def cmd_load(config: Config, client: LlamaClient, arg: Optional[str]) -> None:
    try:
        sessions = discover_sessions(config.cache_dir)
    except SessionDiscoveryError as exc:
        print_error(str(exc))
        return

    try:
        if arg:
            name = arg.strip()
            error = validate_session_name(name)
            if error:
                print_error(error)
                return
            match = next((s for s in sessions if s.name == name), None)
            if match is None:
                print_error(f"session '{name}' was not found.")
                return
            _do_load(config, client, match)
            return

        # No name given -> numbered selection menu (no "00" option here)
        if not sessions:
            print("No stored sessions to load.")
            return

        print("Stored sessions:")
        print()
        for idx, session in enumerate(sessions, start=1):
            print(f"{idx:02d} - {session.name}")
        print()
        selection = read_line("Select session: ").strip()

        if not selection.isdigit():
            print_error("Invalid selection.")
            return
        choice = int(selection)
        if choice < 1 or choice > len(sessions):
            print_error("Invalid selection.")
            return

        _do_load(config, client, sessions[choice - 1])
    except OperationCancelled:
        print("Load cancelled.")


def _do_delete(session: SessionInfo) -> None:
    size_text = human_size(session.size)  # capture size before deleting
    try:
        os.remove(session.path)
    except OSError as exc:
        print_error(f"could not delete '{session.name}': {exc}")
        return
    print(f"{session.name} was deleted ({size_text}).")


def cmd_delete(config: Config, arg: Optional[str]) -> None:
    try:
        sessions = discover_sessions(config.cache_dir)
    except SessionDiscoveryError as exc:
        print_error(str(exc))
        return

    try:
        if arg:
            name = arg.strip()
            error = validate_session_name(name)
            if error:
                print_error(error)
                return
            match = next((s for s in sessions if s.name == name), None)
            if match is None:
                print_error(f"session '{name}' was not found.")
                return
            if not confirm(f"Delete '{match.name}' ({human_size(match.size)})? This cannot be undone."):
                print("Delete cancelled.")
                return
            _do_delete(match)
            return

        # No name given -> numbered selection menu (no "00" option here)
        if not sessions:
            print("No stored sessions to delete.")
            return

        print("Stored sessions:")
        print()
        for idx, session in enumerate(sessions, start=1):
            print(f"{idx:02d} - {session.name}")
        print()
        selection = read_line("Select session: ").strip()

        if not selection.isdigit():
            print_error("Invalid selection.")
            return
        choice = int(selection)
        if choice < 1 or choice > len(sessions):
            print_error("Invalid selection.")
            return

        target = sessions[choice - 1]
        if not confirm(f"Delete '{target.name}' ({human_size(target.size)})? This cannot be undone."):
            print("Delete cancelled.")
            return
        _do_delete(target)
    except OperationCancelled:
        print("Delete cancelled.")


# ---------------------------------------------------------------------------
# Main interactive loop
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config = load_config(argv)
    client = LlamaClient(config.server_url)

    print_startup_banner(config, client)

    while True:
        try:
            raw = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            print("Exiting Llama Cache Manager. Goodbye!")
            return 0

        line = raw.strip()
        if not line:
            continue

        parts = line.split(None, 1)
        command = parts[0].strip().lower()
        arg = parts[1].strip() if len(parts) > 1 else None
        if arg == "":
            arg = None

        try:
            if command == "-exit":
                print("Exiting Llama Cache Manager. Goodbye!")
                return 0
            elif command == "-list":
                cmd_list(config)
                print_home(config)
            elif command == "-save":
                cmd_save(config, client, arg)
                print_home(config)
            elif command == "-load":
                cmd_load(config, client, arg)
                print_home(config)
            elif command == "-delete":
                cmd_delete(config, arg)
                print_home(config)
            else:
                print_error(f"unknown command '{command}'.")
                print_home(config)
        except Exception as exc:  # noqa: BLE001 - never let the shell crash
            print_error(f"unexpected problem: {exc}")
            print_home(config)


if __name__ == "__main__":
    sys.exit(main())
