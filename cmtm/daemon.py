"""TUIOS is the process authority; this module only owns bounded clients.

The endpoint follows TUIOS internal/sockpath and internal/serverscope: named
servers never search another socket directory. Remote links currently have no
proven named-server selector and are deliberately not usable here. Test names
use cmtm-t- plus six hexadecimal digits to fit TUIOS's 14-character limit.
"""
from __future__ import annotations

import copy
from collections import deque
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import tomllib
import unicodedata
from urllib.parse import quote


class DaemonError(RuntimeError):
    """Visible failure, optionally retaining a pane after a partial operation."""

    def __init__(self, message, *, code="failed", target=None, outcome="failed", detail=None):
        super().__init__(message)
        self.code = code
        self.target = target
        self.outcome = outcome
        self.detail = detail or {}


def _validate_server(server):
    if server != "clean-mtm" and not re.fullmatch(r"cmtm-t-[a-f0-9]{6}", server or ""):
        raise DaemonError("Server must be clean-mtm or cmtm-t-<six lowercase hex digits>; default servers are forbidden.", code="isolation")
    return server


def isolated_environment(server, base=None):
    """Discard inherited pane/daemon identity, including test socket overrides."""
    _validate_server(server)
    result = {k: v for k, v in (os.environ if base is None else base).items()
              if not k.startswith(("TUIOS_", "HERDR_")) and k not in {"TMUX", "TMUX_PANE"}}
    result.update(TUIOS_SERVER=server, CMTM_SERVER=server)
    return result


def _plain(text):
    # Plain captures normally have no escapes. Still strip whole string-control
    # and CSI sequences before removing controls; never execute pane output.
    text = re.sub(r"(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[P^_].*?\x1b\\)", "", str(text), flags=re.S)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]", "", text)
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"})


class _Connection:
    """One serialized JSON connection, with a hard deadline per whole reply."""

    LIMIT = 16 * 1024 * 1024

    def __init__(self, path, timeout=5):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.buffer = bytearray()
        self.serial = 0
        try:
            self.socket.settimeout(timeout)
            self.socket.connect(str(path))
        except OSError:
            self.socket.close()
            raise

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()

    def line(self, deadline):
        while True:
            end = self.buffer.find(b"\n")
            if end >= 0:
                line = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                return line
            if len(self.buffer) > self.LIMIT:
                raise DaemonError("TUIOS response exceeds 16 MiB.", code="protocol")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TUIOS reply deadline exceeded")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(min(65536, self.LIMIT + 1 - len(self.buffer)))
            if not chunk:
                raise ConnectionError("TUIOS closed its connection")
            self.buffer.extend(chunk)

    def request(self, verb, params=None, timeout=6):
        self.serial += 1
        wire = json.dumps({"id": self.serial, "verb": verb, "params": params or {}}, ensure_ascii=False).encode() + b"\n"
        if len(wire) > self.LIMIT:
            raise DaemonError("TUIOS request exceeds 16 MiB.", code="invalid_params")
        deadline = time.monotonic() + timeout
        self.socket.settimeout(timeout)
        self.socket.sendall(wire)
        response = json.loads(self.line(deadline))
        if not isinstance(response, dict) or response.get("id") != self.serial:
            raise DaemonError("TUIOS returned a mismatched response.", code="protocol")
        if response.get("error"):
            error = response["error"]
            hint = error.get("hint", {})
            message = error.get("message", "TUIOS refused the operation")
            if hint.get("detail"):
                message += ": " + hint["detail"]
            raise DaemonError(message, code=error.get("code", "failed"), detail=error)
        result = response.get("result")
        if not isinstance(result, dict):
            raise DaemonError("TUIOS returned no result object.", code="protocol")
        return result


class LiveStream:
    """A real stream-pane connection: bounded drain, input and renewable lease.

    poll() returns metadata/invalidation events, never terminal escapes to render.
    A UI can request capture_info on output events without giving capture any
    input or sizing authority. All input goes to this stream's fixed identity.
    """

    def __init__(self, daemon, target, cols, rows):
        self.daemon, self.target = daemon, target
        self.params = daemon._target(target)
        self.cols, self.rows = self._size(cols, rows)
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._events = deque(maxlen=64)
        self._error = None
        self._connection = daemon._connect()
        try:
            self.info = self._connection.request("stream-pane", {
                **self.params, "lease_cols": self.cols, "lease_rows": self.rows,
                "fit": True,
            })
        except BaseException:
            self._connection.close()
            raise
        self._renewed = time.monotonic()
        self._thread = threading.Thread(target=self._drain, name="cmtm-pane-stream", daemon=True)
        self._thread.start()

    @staticmethod
    def _size(cols, rows):
        cols, rows = int(cols), int(rows)
        if not 1 <= cols <= 4096 or not 1 <= rows <= 4096:
            raise DaemonError("Live detail dimensions must be between 1 and 4096 cells.", code="invalid_params")
        return cols, rows

    def _frame(self, kind, payload):
        with self._write_lock:
            if self._stop.is_set():
                raise DaemonError("The live stream is closed.", code="closed", target=self.target)
            try:
                self._connection.socket.settimeout(2)
                self._connection.socket.sendall(kind + struct.pack(">I", len(payload)) + payload)
            except OSError as error:
                raise DaemonError(f"Live stream write failed: {error}", code="disconnected",
                                  target=self.target, outcome="unknown") from error

    def send(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, bytes):
            raise DaemonError("Live input must be bytes or text.", code="invalid_params")
        if len(data) > 65536:
            raise DaemonError("One live input operation is limited to 64 KiB; split a larger paste.", code="invalid_params")
        self._frame(b"I", data)

    def resize(self, cols, rows):
        self.cols, self.rows = self._size(cols, rows)
        self._frame(b"L", struct.pack(">HH", self.cols, self.rows))
        self._renewed = time.monotonic()

    def zoom(self):
        self.daemon.call("focus-window", self.params)
        return self.daemon.call("run-command", {"session": self.params["session"], "command": "ToggleZoom"})

    def _push(self, event):
        with self._event_lock:
            # Output is an invalidation, not a transcript. Coalesce a burst
            # without discarding errors or making a busy pane grow a UI queue.
            if self._events and event["type"] == "output" and self._events[-1]["type"] == "output":
                self._events[-1] = event
            else:
                self._events.append(event)
            if event["type"] in {"error", "closed"}:
                self._error = event

    def poll(self):
        with self._event_lock:
            events = list(self._events)
            self._events.clear()
            if self._error and self._error not in events:
                events.append(self._error)
            self._error = None
            return events

    def _drain(self):
        buffer = self._connection.buffer
        incomplete_since = None
        try:
            while not self._stop.is_set():
                if time.monotonic() - self._renewed >= 10:
                    self.resize(self.cols, self.rows)
                while len(buffer) >= 5:
                    kind, length = buffer[0], struct.unpack(">I", buffer[1:5])[0]
                    if length > _Connection.LIMIT:
                        raise DaemonError("Live frame exceeds 16 MiB.", code="protocol")
                    if len(buffer) < 5 + length:
                        break
                    payload = bytes(buffer[5:5 + length])
                    del buffer[:5 + length]
                    incomplete_since = None
                    if kind == ord("S") and len(payload) >= 12:
                        seq, cols, rows = struct.unpack(">QHH", payload[:12])
                        self._push({"type": "snapshot", "seq": seq, "cols": cols, "rows": rows})
                    elif kind == ord("O") and len(payload) >= 8:
                        self._push({"type": "output", "seq": struct.unpack(">Q", payload[:8])[0]})
                    elif kind == ord("R") and len(payload) >= 12:
                        seq, cols, rows = struct.unpack(">QHH", payload[:12])
                        self._push({"type": "resize", "seq": seq, "cols": cols, "rows": rows})
                    elif kind == ord("E"):
                        self._push({"type": "error", **json.loads(payload)})
                    elif kind == ord("X"):
                        self._push({"type": "closed", "reason": payload.decode(errors="replace")})
                        return
                    # Unknown binary frame types are ignored as the protocol
                    # requires; their bounded payload is still fully drained.
                if buffer:
                    incomplete_since = incomplete_since or time.monotonic()
                    if time.monotonic() - incomplete_since > 10:
                        raise DaemonError("Incomplete live frame exceeded its 10-second deadline.", code="timeout")
                ready, _, _ = select.select([self._connection.socket], [], [], 0.2)
                if ready:
                    chunk = self._connection.socket.recv(65536)
                    if not chunk:
                        raise ConnectionError("TUIOS closed the live stream")
                    buffer.extend(chunk)
        except (OSError, ConnectionError, ValueError, DaemonError) as error:
            if not self._stop.is_set():
                self._push({"type": "error", "message": str(error), "state": "disconnected"})
        finally:
            self._stop.set()
            self._connection.close()

    def close(self):
        with self._write_lock:
            if not self._stop.is_set():
                # Make release and the closed flag atomic with respect to input.
                # A sender already holding this lock finishes before release;
                # every later sender sees closed and sends nothing.
                self._stop.set()
                try:
                    self._connection.socket.settimeout(2)
                    self._connection.socket.sendall(b"L" + struct.pack(">IHH", 4, 0, 0))
                except OSError:
                    # Closing a disconnected socket also releases its lease.
                    pass
            self._connection.close()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        with self.daemon._lock:
            self.daemon._streams.discard(self)


class Daemon:
    """A named-server client. Construction and reads never start a daemon."""

    def __init__(self, server="clean-mtm"):
        self.server = _validate_server(server)
        self.environment = isolated_environment(server)
        runtime = self.environment.get("XDG_RUNTIME_DIR")
        if not runtime and sys.platform != "darwin":
            candidate = Path("/run/user") / str(os.getuid())
            try:
                info = candidate.lstat()
                if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700:
                    runtime = str(candidate)
            except OSError:
                pass
        base = Path(runtime) / "tuios" if runtime else Path(f"/tmp/tuios-{os.getuid()}")
        self.socket_path = base / "servers" / server / "tuios.sock"
        self.state = "disconnected"
        self.last_error = "Not connected; use Start explicitly if the named server is absent."
        self.last_success = None
        self._lock = threading.RLock()
        self._connection = None
        self._closed = threading.Event()
        self._subscriptions = set()
        self._streams = set()
        self._snapshot = []
        self._ownership = {}
        self._sessions_owned = set()
        self._catalog = None
        self._presence = None
        self._live_process = None

    def _connect(self):
        if self._closed.is_set():
            raise DaemonError("This TUIOS client has been closed.", code="closed")
        self.state = "connecting"
        directory = self.socket_path.parent.lstat()
        endpoint = self.socket_path.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) != 0o700
                or not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid()):
            raise DaemonError("The selected named-server socket must be a real socket in a private, user-owned directory; symlink endpoints are refused.", code="isolation")
        connection = _Connection(self.socket_path)
        try:
            connection.request("hello", {"client": "cmtm", "version": "1", "protocol": 1,
                                           "term": self.environment.get("TERM", "xterm-256color"),
                                           "color_term": self.environment.get("COLORTERM", "truecolor")})
        except BaseException:
            connection.close()
            raise
        return connection

    def _disconnect(self, error):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._presence = None
        self._catalog = None
        self.state = "stale" if self._snapshot else "disconnected"
        self.last_error = str(error)
        for record in self._snapshot:
            record["fields"]["freshness"] = "stale"
            record["fields"]["connection_error"] = str(error)

    def _guard(self, verb, params):
        # Host listings are local configuration/state, not remote inventory reads.
        if any(params.get(k) not in (None, "", "local", False) for k in ("host", "from_host", "to_host")):
            raise DaemonError("Remote operation refused: this TUIOS link API does not prove named-server isolation on the remote host.", code="isolation")
        for key in ("session", "to_session", "window"):
            if ":" in str(params.get(key, "")):
                raise DaemonError("Host-qualified targets are forbidden: remote named-server isolation is unavailable.", code="isolation")
        if ("host" in verb and verb != "list-hosts") or params.get("hosts"):
            raise DaemonError("Remote host operations are unavailable until TUIOS supports explicit isolated-server links.", code="isolation")
        if verb in {"open-pane", "open-host-connection", "link-peer", "kill-server"}:
            raise DaemonError(f"{verb} is not exposed by this client.", code="isolation")
        if verb in {"close-window", "send-text", "send-keys", "get-window", "capture-pane", "resize", "focus-window", "move-window", "set-window", "queue-prompt", "ask-agent"}:
            if not params.get("session") or not params.get("window"):
                raise DaemonError(f"{verb} requires an explicit session and stable window identity; focused-pane defaults are forbidden.", code="invalid_params")
        if verb in {"kill-session", "rename-session", "session-info", "list-windows", "new-window", "start-agent", "run-command", "set-option", "set-session-name", "set-session-space", "set-session-accent"} and not params.get("session"):
            raise DaemonError(f"{verb} requires an explicit session.", code="invalid_params")
        if verb == "apply-config":
            self._check_start_config()

    def call(self, verb, params=None):
        """Return the native result object; never retry an ambiguous mutation."""
        params = dict(params or {})
        if verb == "list-attention":
            params.setdefault("host", "local")
        self._guard(verb, params)
        with self._lock:
            try:
                if self._connection is None:
                    self._connection = self._connect()
                result = self._connection.request(verb, params, timeout=10 if verb == "start-agent" else 6)
                self.state, self.last_error, self.last_success = "current", "", time.time()
                return result
            except (OSError, ConnectionError, TimeoutError, ValueError) as error:
                self._disconnect(error)
                raise DaemonError(f"Server {self.server} unavailable at {self.socket_path}: {error}. No fallback was attempted. If a mutation was sent, its outcome is unknown; inspect before retrying.", code="disconnected", outcome="unknown") from error
            except DaemonError as error:
                self.last_error = str(error)
                if error.code == "protocol":
                    self._disconnect(error)
                raise

    def _cli(self, args, timeout=10):
        executable = shutil.which("tuios", path=self.environment.get("PATH"))
        if not executable:
            raise DaemonError("tuios is not installed on PATH.", code="unavailable")
        try:
            result = subprocess.run([executable, "--server", self.server, *args], env=self.environment,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DaemonError(f"Named TUIOS command failed: {error}", outcome="unknown") from error
        if result.returncode:
            raise DaemonError(_plain(result.stderr.decode(errors="replace") or result.stdout.decode(errors="replace")).strip())
        if len(result.stdout) > _Connection.LIMIT:
            raise DaemonError("TUIOS CLI result exceeds 16 MiB.", code="protocol")
        return result.stdout.decode(errors="replace")

    def _check_start_config(self):
        """Read TUIOS's own layer order; refuse active unisolated host links."""
        try:
            report = json.loads(self._cli(["config", "files", "--json"]))
        except (DaemonError, ValueError) as error:
            if "there is no config file at " in str(error) and "tuios uses the defaults" in str(error):
                return
            raise DaemonError(f"Cannot establish safe daemon startup configuration: {error}", code="isolation") from error
        if not isinstance(report, dict) or not isinstance(report.get("files"), list):
            raise DaemonError("TUIOS did not report its configuration layers; startup refused.", code="isolation")
        if report.get("warnings"):
            raise DaemonError("TUIOS configuration has warnings; cannot establish isolated startup: " +
                              "; ".join(map(str, report["warnings"])), code="isolation")
        addresses = {}
        for layer in report["files"]:
            if layer.get("missing"):
                continue
            try:
                with open(layer["path"], "rb") as source:
                    raw = source.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("configuration layer exceeds 4 MiB")
                document = tomllib.loads(raw.decode("utf-8"))
                for name, host in document.get("hosts", {}).items():
                    if not isinstance(host, dict):
                        raise ValueError(f"hosts.{name} is not a table")
                    if "addr" in host:
                        addresses[name] = host["addr"]
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise DaemonError(f"Cannot inspect TUIOS startup config safely: {error}", code="isolation") from error
        active = sorted(name for name, address in addresses.items() if address and name != "*")
        if active:
            raise DaemonError(
                "Startup refused: shared TUIOS config auto-connects host links (" + ", ".join(active) +
                ") without a proven named-server selector. No daemon was started. Start cmtm with an explicitly isolated XDG_CONFIG_HOME and XDG_CONFIG_DIRS containing no active [hosts] addresses; cmtm never changes your existing config.",
                code="isolation")

    def start(self):
        """Explicitly start only this server; no resources are killed on exit."""
        try:
            return self.call("hello", {"client": "cmtm", "protocol": 1})
        except DaemonError as error:
            if error.code != "disconnected":
                raise
        self._check_start_config()
        # `new --detach` is TUIOS's supported bounded startup path. The initial
        # ordinary shell belongs in no-project, never in the UI-only session.
        try:
            self._cli(["new", "no-project", "--detach"], timeout=15)
        except DaemonError as error:
            # Startup may have restored an existing no-project session, or a
            # concurrent explicit starter may have won. Only a successful read
            # of this exact endpoint can establish that startup succeeded.
            try:
                result = self.call("hello", {"client": "cmtm", "protocol": 1})
            except DaemonError:
                raise error
            return {**result, "start_note": str(error)}
        return self.call("hello", {"client": "cmtm", "protocol": 1})

    def close(self):
        self._closed.set()
        with self._lock:
            self._disconnect("Client disconnected; managed processes remain running.")
            connections = list(self._subscriptions)
            streams = list(self._streams)
        for connection in connections:
            connection.close()
        for stream in streams:
            stream.close()
        if self._live_process is not None:
            self._stop_viewer(self._live_process)
        self.state = "disconnected"

    def verbs(self):
        if self._catalog is None:
            self._catalog = self.call("list-verbs")
        return copy.deepcopy(self._catalog)

    def capabilities(self):
        """Recognized integration catalog, binary availability, not account access."""
        try:
            rows = json.loads(self._cli(["integration", "status", "--json"]))
        except ValueError as error:
            raise DaemonError(f"Invalid TUIOS integration catalog: {error}", code="protocol") from error
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DaemonError("Unexpected integration status shape.", code="protocol")
        return {"harnesses": [{**row, "recognized": True, "installed": bool(row.get("binary_path")),
                                "integration_installed": row.get("installed", False), "usable": "unknown",
                                "models": [], "efforts": [], "speeds": []} for row in rows],
                "note": "Catalog covers installed TUIOS integrations. Binary presence does not establish account access. TUIOS exposes no authoritative model/effort/speed catalog; those options are unavailable."}

    def _record(self, kind, identity, label, fields, project_id=None):
        fields = dict(fields)
        fields.update(server=self.server, freshness="current", captured_at=time.time())
        fields.setdefault("host", "local")
        fields.setdefault("session", "")
        fields.setdefault("window", "")
        fields.setdefault("cwd", "")
        fields.setdefault("state", "unknown")
        return {"id": "tuios:" + ":".join(quote(str(x), safe="") for x in (self.server, fields["host"], kind, identity)),
                "kind": kind, "label": label, "project_id": project_id, "fields": fields,
                "starred": False, "pinned": False}

    def _window_record(self, window, session, session_id=""):
        source = dict(window)
        wid = source.get("window_id") or source.get("window")
        if not wid:
            raise DaemonError("TUIOS window result has no stable identity.", code="protocol")
        host = source.get("host") or "local"
        agent_state = source.get("agent_state", source.get("state", "unknown"))
        harness = source.get("harness_id", source.get("agent_harness", source.get("harness", "")))
        is_agent = bool(harness or source.get("identity") or agent_state not in ("none", "unknown", ""))
        ownership = self._ownership.get((host, wid), {})
        fields = {**source, "host": host, "session": session, "session_id": session_id,
                  "window": wid, "pane_kind": "agent" if is_agent else ownership.get("kind", "terminal"),
                  "state": source.get("host_link") or agent_state,
                  "project": ownership.get("project", "no-project" if session == "no-project" else "unknown"),
                  "provenance": "TUIOS list-windows/list-agents; ownership requires explicit application association",
                  "working": agent_state == "working", "blocked": bool(source.get("blocked_by")) or agent_state == "needs_input",
                  "needs_input": agent_state == "needs_input", "completed": agent_state == "done",
                  "activity_age_ms": source.get("evidence_age_ms"), "unread": source.get("finished_unread", source.get("unread")),
                  "queued": source.get("queued"), "subagents": source.get("subagents"),
                  "remote_isolation": "unsupported" if host != "local" else "isolated"}
        fields["kind"] = fields["pane_kind"]
        fields["activity_age"] = source["evidence_age_ms"] / 1000 if isinstance(source.get("evidence_age_ms"), (int, float)) else None
        return self._record("window", wid, source.get("display_name") or source.get("name") or source.get("title") or wid,
                            fields, ownership.get("project_id"))

    def snapshot(self):
        """One consistent replacement only after all required reads succeed."""
        try:
            return self._read_snapshot()
        except DaemonError as error:
            self.state = "stale" if self._snapshot else "disconnected"
            self.last_error = str(error)
            for record in self._snapshot:
                record["fields"].update(freshness="stale", connection_error=str(error))
            raise

    def _read_snapshot(self):
        sessions = self.call("list-sessions").get("sessions", [])
        records = [self._record("host", "local", "local", {"state": "connected", "provenance": str(self.socket_path)})]
        hosts = self.call("list-hosts").get("hosts", [])
        for host in hosts:
            if isinstance(host, dict):
                name = host.get("name", host.get("host", ""))
                if name and name != "local":
                    records.append(self._record("host", name, name, {**host, "host": name,
                        "state": host.get("status", "unknown"), "remote_isolation": "unsupported",
                        "operation_error": "Remote operations refused: isolated named-server targeting is unproven."}))
        agents = {}
        agent_error = ""
        try:
            for agent in self.call("list-agents", {"all_sessions": True}).get("agents", []):
                agents[(agent.get("session"), agent.get("window_id"))] = agent
        except DaemonError as error:
            if error.code not in {"unknown_verb", "agents_disabled"}:
                raise
            agent_error = str(error)
        for session in sessions:
            name = session.get("name", session.get("session"))
            sid = session.get("id", session.get("session_id"))
            if not name or not sid:
                raise DaemonError("TUIOS session lacks a stable id or name.", code="protocol")
            records.append(self._record("session", sid, session.get("display_name") or name,
                {**session, "session": name, "session_id": sid, "state": "attached" if session.get("attached") else "detached"}))
            for window in self.call("list-windows", {"session": name}).get("windows", []):
                merged = {**window, **agents.get((name, window.get("window_id")), {})}
                if agent_error:
                    merged["agent_data_error"] = agent_error
                records.append(self._window_record(merged, name, sid))
        self._snapshot = records
        return copy.deepcopy(records)

    def cached_snapshot(self):
        """Last complete inventory, with stale markers after a transport failure."""
        return copy.deepcopy(self._snapshot)

    def _target(self, target):
        fields = target.get("fields", {}) if isinstance(target, dict) else {}
        if fields.get("server") != self.server:
            raise DaemonError("Target does not belong to the selected isolated server.", code="isolation")
        if fields.get("host", "local") != "local":
            raise DaemonError("Remote pane operation refused: named-server isolation is unproven.", code="isolation")
        session, window = fields.get("session"), fields.get("window")
        if not session or not window:
            raise DaemonError("A stable window and explicit session are required.", code="invalid_params")
        return {"session": session, "window": window}

    def capture_info(self, target, cols=80, rows=24):
        params = self._target(target)
        # Request enough for a full alternate screen; crop in the renderer,
        # never by resizing the source. Recent includes bounded normal history.
        lines = min(512, max(1, int(rows), int(target["fields"].get("height", 0) or 0)) + 100)
        result = self.call("capture-pane", {**params, "source": "recent", "lines": lines, "styled": False})
        content = _plain(result.get("content", ""))
        clipped = len(content.encode()) > 256 * 1024
        if clipped:
            content = content.encode()[-256 * 1024:].decode(errors="replace")
        return {**result, "content": content, "server": self.server, "host": "local", **params,
                "captured_at": time.time(), "state": "current" if content else "empty",
                "truncated": clipped or result.get("history_rows", 0) + int(target["fields"].get("height", 0) or 0) > lines,
                "requested_cols": cols, "requested_rows": rows}

    def capture(self, target, cols=80, rows=24):
        return self.capture_info(target, cols, rows)["content"]

    def inspect(self, target, tab="activity"):
        params = self._target(target)
        tab = tab.lower()
        if tab == "screen":
            return self.capture_info(target)
        if tab in {"activity", "turns"}:
            result = self.call("agent-activity", {**params, "limit": 256, "recap": True})
            if tab == "turns":
                result["entries"] = [entry for entry in result.get("entries", []) if entry.get("kind") == "turn_end"]
                result["note"] = "Recorded completed turns from the bounded activity ring; absence is not proof of no turns."
            return result
        if tab == "transcript":
            with self._lock:
                if not self._presence:
                    self._presence = self.call("attach-presence").get("human_nonce")
                return self.call("agent-transcript", {**params, "human_nonce": self._presence, "limit": 200})
        raise DaemonError(f"Unsupported inspection tab: {tab}", code="unsupported")

    def _placement(self, project, cwd, project_id=None, ui=False):
        session = str(project).strip() if project is not None else "no-project"
        session = session or "no-project"
        if session.casefold() == "no-project":
            session = "no-project"
        if session.casefold() == "mtm-only" and not ui:
            raise DaemonError("mtm-only is reserved for the Matomeru UI, not project work.", code="placement")
        if ":" in session or any(ord(c) < 32 for c in session):
            raise DaemonError("Project/session names cannot contain host separators or controls.", code="placement")
        sessions = self.call("list-sessions").get("sessions", [])
        existing = next((s for s in sessions if s.get("name") == session), None)
        collision = next((s for s in sessions if s.get("name", "").casefold() == session.casefold() and s.get("name") != session), None)
        if collision:
            raise DaemonError(f"Project name conflicts with existing session {collision['name']!r}; no session was changed.", code="placement")
        if existing and session not in {"no-project", "mtm-only"} and not project_id and session not in self._sessions_owned:
            raise DaemonError(f"Session {session!r} already exists without verified project ownership. Select the existing project explicitly instead of merging names.", code="placement")
        if existing is None:
            created = self.call("new-session", {"name": session, "window": False, "cwd": cwd})
            sid = created["session_id"]
            self._sessions_owned.add(session)
            return session, sid, 1
        windows = self.call("list-windows", {"session": session}).get("windows", [])
        occupied = {w.get("workspace", 1) for w in windows}
        for workspace in range(1, 10):
            if workspace not in occupied:
                return session, existing.get("id", existing.get("session_id", "")), workspace
        raise DaemonError("No empty workspace remains in this session. Launch refused rather than rearranging or resizing existing panes.", code="no_space")

    def launch(self, kind, project="no-project", cwd="", name="", harness="", model="", prompt="", host="local", **options):
        if host != "local":
            raise DaemonError("Remote launches require proven named-server isolation, unavailable in this TUIOS API.", code="isolation")
        if kind not in {"terminal", "agent", "vim"}:
            raise DaemonError(f"Unsupported launch kind: {kind}", code="invalid_params")
        allowed = {"project_id", "effort", "speed", "args", "protocol", "command"}
        unknown = set(options) - allowed
        if unknown:
            raise DaemonError("Unsupported launch options: " + ", ".join(sorted(unknown)), code="invalid_params")
        if model or options.get("effort") or options.get("speed"):
            raise DaemonError("This TUIOS API exposes no authoritative model/effort/speed choices; those values cannot be forwarded safely.", code="unsupported")
        cwd = str(Path(cwd).expanduser().resolve()) if cwd else str(Path.home())
        if not Path(cwd).is_dir():
            raise DaemonError(f"Local directory does not exist: {cwd}", code="invalid_params")
        argv = options.get("command")
        if argv is not None and (not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv)):
            raise DaemonError("command must be a nonempty argv list of strings.", code="invalid_params")
        args = options.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args):
            raise DaemonError("Agent args must be an argv list, not a shell command.", code="invalid_params")
        if kind == "agent":
            if not re.fullmatch(r"[A-Za-z0-9_-]+", harness or ""):
                raise DaemonError("Select an explicit recognized harness identifier.", code="invalid_params")
            if argv:
                raise DaemonError("Agent launch takes harness and args, not command.", code="invalid_params")
            rows = self.capabilities()["harnesses"]
            chosen = next((row for row in rows if row.get("harness") == harness), None)
            if chosen is None or not chosen["installed"]:
                raise DaemonError(f"Harness {harness!r} is not recognized and installed in the available integration catalog.", code="unavailable")
        elif kind == "vim" and argv is None:
            editor = shutil.which("nvim", path=self.environment.get("PATH")) or shutil.which("vim", path=self.environment.get("PATH"))
            if not editor:
                raise DaemonError("Neither nvim nor vim is installed on PATH.", code="unavailable")
            argv = [editor, *args]
        if argv and not shutil.which(argv[0], path=self.environment.get("PATH")):
            raise DaemonError(f"Launch program is unavailable: {argv[0]}", code="unavailable")
        session, sid, workspace = self._placement(project, cwd, options.get("project_id"))
        params = {"session": session, "cwd": cwd, "name": name, "workspace": workspace, "focus": False}
        if kind == "agent":
            params.update(agent=harness, args=args, ready_timeout=3000)
            if options.get("protocol"):
                params["protocol"] = options["protocol"]
            # PATH is the only caller environment an agent needs; TUIOS installs
            # its own fresh server/session/pane identities in the child.
            params["env"] = {"PATH": self.environment.get("PATH", os.defpath)}
            result = self.call("start-agent", params)
        else:
            if argv:
                params["command"] = argv
            result = self.call("new-window", params)
        wid = result.get("window_id")
        if not wid:
            raise DaemonError("Launch returned no stable window id; inspect inventory before retrying.", code="protocol", outcome="unknown", detail=result)
        self._ownership[("local", wid)] = {"project": session if session != "no-project" else "no-project",
                                             "project_id": options.get("project_id"), "kind": kind}
        record = self._window_record({**result, "cwd": cwd, "harness_id": harness if kind == "agent" else "", "name": name or result.get("name", kind)}, session, sid)
        record["fields"]["launch_outcome"] = result.get("outcome", "created")
        if prompt:
            try:
                delivery = self.send(record, prompt)
                record["fields"]["prompt_status"] = delivery["outcome"]
                record["fields"]["prompt_result"] = {key: value for key, value in delivery.items() if key != "target"}
            except DaemonError as error:
                record["fields"].update(prompt_status=error.outcome, prompt_error=str(error))
        return record

    def bootstrap(self, argv):
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise DaemonError("UI bootstrap requires a nonempty argv list.", code="invalid_params")
        self.start()
        session, sid, workspace = self._placement("mtm-only", str(Path.home()), ui=True)
        result = self.call("new-window", {"session": session, "name": "Matomeru", "command": argv,
                                          "workspace": workspace, "focus": False, "cwd": str(Path.home())})
        record = self._window_record(result, session, sid)
        record["fields"].update(pane_kind="ui", kind="ui", project="no-project")
        return record

    def close_window(self, target):
        return self.call("close-window", self._target(target))

    def send(self, target, text, queue=False):
        params = self._target(target)
        if not isinstance(text, str) or not text.strip():
            raise DaemonError("A nonempty prompt is required.", target=target, code="invalid_params")
        try:
            if queue:
                result = self.call("queue-prompt", {**params, "text": text})
                return {**result, "outcome": "accepted", "delivery_state": "waiting", "target": target}
            if target["fields"].get("pane_kind") == "agent":
                # The daemon rechecks blocking immediately before submission.
                # A separate readiness read followed by send-text would race an
                # approval prompt. This bounded ask never forces through one.
                result = self.call("ask-agent", {**params, "text": text, "ready_timeout": 1,
                    "timeout": 2000, "stall_timeout": 1000, "settle": 250, "lines": 64})
                delivered = result.get("settled_by") in {"agent-state", "idle"}
                return {**result, "outcome": "delivered" if delivered else "accepted",
                        "delivery_state": "delivered" if delivered else "unknown", "target": target,
                        "note": "Prompt submitted once; timeout or closure does not prove agent completion."}
            result = self.call("send-text", {**params, "text": text, "paste": True, "submit": True})
            return {**result, "outcome": "accepted", "delivery_state": "unknown", "target": target,
                    "note": "Daemon accepted the input; agent receipt or completion is not yet proven."}
        except DaemonError as error:
            error.target = target
            if error.code in {"prompt_stalled", "disconnected", "internal", "timeout"}:
                error.outcome = "unknown"
            elif error.code == "agent_blocked":
                error.outcome = "blocked"
            elif error.code == "not_ready":
                error.outcome = "waiting"
            raise

    def subscribe(self, callback, stop_event):
        """Actual event stream, interruptible reads and capped reconnect backoff."""
        delay = 0.25
        after_seq = None
        boot_id = None
        while not stop_event.is_set() and not self._closed.is_set():
            connection = None
            try:
                callback({"type": "connection", "state": "connecting", "server": self.server})
                connection = self._connect()
                with self._lock:
                    self._subscriptions.add(connection)
                params = {"queue": 256, "types": [
                    "window-created", "window-closed", "window-exit", "window-retitled",
                    "window-focused", "window-moved", "window-minimized", "window-restored",
                    "workspace-switched", "workspace-renamed", "agent-state", "agent-message",
                    "bell", "notification", "mode-changed", "session-created", "session-closed",
                    "client-session-changed", "attention", "host-changed", "prompt",
                    "command-started", "command-finished", "agent-activity", "transcript",
                ]}
                if after_seq is not None:
                    params.update(after_seq=after_seq, boot_id=boot_id)
                result = connection.request("subscribe", params)
                boot_id = result.get("boot_id")
                if after_seq is None:
                    after_seq = result.get("seq", 0)
                self.state = "resynchronizing"
                callback({**result, "type": "connection", "state": "resynchronizing", "server": self.server})
                callback({"type": "resync", "server": self.server, "reason": "subscription established"})
                delay = 0.25
                while not stop_event.is_set() and not self._closed.is_set():
                    try:
                        raw = connection.line(time.monotonic() + 0.5)
                    except (socket.timeout, TimeoutError):
                        continue
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise DaemonError("TUIOS event is not an object.", code="protocol")
                    payload = event
                    if payload.get("seq") is not None:
                        after_seq = payload["seq"]
                    if payload.get("boot_id"):
                        boot_id = payload["boot_id"]
                    if payload.get("host") not in (None, "", "local") and payload.get("type") != "host-changed":
                        continue
                    callback({**payload, "server": self.server})
                    if payload.get("type") == "gap":
                        callback({"type": "resync", "server": self.server, "reason": "event replay gap"})
            except (OSError, ConnectionError, TimeoutError, ValueError, DaemonError) as error:
                if not stop_event.is_set() and not self._closed.is_set():
                    callback({"type": "connection", "state": "disconnected", "server": self.server, "error": str(error)})
                    # A restart may invalidate the old sequence. Resynchronize,
                    # never keep reconnecting forever with an impossible cursor.
                    after_seq, boot_id = None, None
            finally:
                if connection is not None:
                    with self._lock:
                        self._subscriptions.discard(connection)
                    connection.close()
            stop_event.wait(delay)
            delay = min(5.0, delay * 2)

    def open_live(self, target, cols, rows):
        """Open embedded live mode at the caller's measured detail dimensions."""
        try:
            stream = LiveStream(self, target, cols, rows)
        except (OSError, ConnectionError, TimeoutError, ValueError) as error:
            raise DaemonError(f"Cannot open the named pane stream: {error}", code="disconnected",
                              target=target) from error
        with self._lock:
            if self._closed.is_set():
                stream.close()
                raise DaemonError("This TUIOS client has been closed.", code="closed")
            self._streams.add(stream)
        self.state, self.last_error, self.last_success = "current", "", time.time()
        return stream

    @staticmethod
    def _stop_viewer(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def live(self, target, cols=None, rows=None, leader="ctrl+a"):
        """Run the real named-server pane-view client on a detail-sized PTY.

        The caller suspends curses. pane-view owns stream/input/renewable lease;
        ending only that viewer releases the lease without closing the target.
        Return next/previous so the UI can select a stable new target before it
        starts another stream. Escape and Ctrl+Z are ordinary target input.
        """
        import fcntl
        import pty
        import termios
        import tty

        params = self._target(target)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise DaemonError("Live interaction needs a terminal on stdin and stdout.", code="unavailable")
        self.call("get-window", params)  # Refuse absent server/window, never autostart.
        if isinstance(leader, str) and re.fullmatch(r"ctrl\+[a-z]", leader.lower()):
            leader_byte = ord(leader[-1].lower()) - 96
        elif isinstance(leader, str) and len(leader) == 1 and 0 < ord(leader) < 32:
            leader_byte = ord(leader)
        else:
            raise DaemonError("Live leader must be Ctrl+a through Ctrl+z.", code="invalid_params")
        size = os.get_terminal_size(sys.stdout.fileno())
        cols = max(1, min(int(cols or size.columns), 4096))
        rows = max(1, min(int(rows or max(1, size.lines - 2)), 4096))
        executable = shutil.which("tuios", path=self.environment.get("PATH"))
        if not executable:
            raise DaemonError("tuios is not installed on PATH.", code="unavailable")
        master, slave = pty.openpty()
        process = None
        saved = termios.tcgetattr(sys.stdin.fileno())
        action = "detach"
        pending = False
        started = time.monotonic()
        output_seen = False
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            process = subprocess.Popen([executable, "--server", self.server, "pane-view", "--session", params["session"],
                                        "--window", params["window"], "--size", "pane"],
                                       stdin=slave, stdout=slave, stderr=slave, env=self.environment,
                                       start_new_session=True, close_fds=True)
            self._live_process = process
            os.close(slave)
            slave = -1
            tty.setraw(sys.stdin.fileno())
            os.write(sys.stdout.fileno(), b"\x1b[2J\x1b[H")

            def notice(text):
                line = min(rows + 1, size.lines)
                message = _plain(text).replace("\n", " ")[:size.columns]
                os.write(sys.stdout.fileno(), f"\x1b7\x1b[{line};1H\x1b[0m\x1b[2K{message}\x1b8".encode())

            notice(f"LIVE {self.server}/{params['session']}/{params['window'][:8]} | {leader}: d detach, n/p switch, z zoom, ? help")
            detached = False
            while not self._closed.is_set() and not detached:
                ready, _, _ = select.select([master, sys.stdin.fileno()], [], [], 0.2)
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == 5:
                            break
                        raise
                    if not data:
                        break
                    output_seen = True
                    os.write(sys.stdout.fileno(), data)
                if sys.stdin.fileno() in ready:
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        break
                    forward = bytearray()
                    for byte in data:
                        if pending:
                            pending = False
                            if byte == leader_byte:
                                forward.append(byte)
                            elif byte in (ord("d"), ord("n"), ord("p")):
                                action = {ord("d"): "detach", ord("n"): "next", ord("p"): "previous"}[byte]
                                detached = True
                                break
                            elif byte in (ord("?"), ord("h")):
                                notice(f"{leader} then d detach; n/p next/previous; z TUIOS zoom; double leader sends literal. Esc/Ctrl+Z go to pane.")
                            elif byte == ord("z"):
                                try:
                                    self.call("focus-window", params)
                                    self.call("run-command", {"session": params["session"], "command": "ToggleZoom"})
                                    notice("TUIOS zoom requested for selected pane.")
                                except DaemonError as error:
                                    notice(str(error))
                            else:
                                notice("Unknown live leader key consumed; leader ? shows help.")
                        elif byte == leader_byte:
                            pending = True
                        else:
                            forward.append(byte)
                    if forward:
                        os.write(master, forward)
                if process.poll() is not None:
                    if process.returncode not in (0, 3):
                        raise DaemonError(f"Named pane viewer exited with status {process.returncode}; its diagnostic was displayed.")
                    break
                if not output_seen and time.monotonic() - started > 10:
                    raise DaemonError("Named pane viewer produced no response within 10 seconds.", code="timeout")
            return {"action": action, "target": target}
        finally:
            if process is not None:
                self._stop_viewer(process)
            self._live_process = None
            if slave >= 0:
                os.close(slave)
            os.close(master)
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
            os.write(sys.stdout.fileno(), b"\x1b[!p\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?2004l\x1b[=0;1u\x1b[>4m\x1b[r\x1b[0m\x1b[?25h")
