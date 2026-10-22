#!/usr/bin/env python3
"""One bounded acceptance run against a disposable named TUIOS server."""
from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from cmtm.daemon import Daemon
    from cmtm.store import Store

    server = os.environ.get("CMTM_TEST_SERVER", "cmtm-t-" + secrets.token_hex(3))
    if not re.fullmatch(r"cmtm-t-[0-9a-f]{6}", server):
        raise SystemExit("Acceptance requires a unique cmtm-t-<six-hex-digits> server")
    with tempfile.TemporaryDirectory(prefix="cmtm-smoke-") as directory:
        root = pathlib.Path(directory)
        database = root / "app.sqlite3"
        config_home = root / "shared-config"
        config_dirs = root / "shared-system-config"
        shared_config = config_home / "tuios" / "config.toml"
        shared_config.parent.mkdir(parents=True)
        config_dirs.mkdir()
        shared_text = '[hosts.cmtm_fixture]\naddr = "smoke@192.0.2.1"\n'
        shared_config.write_text(shared_text)
        os.environ["XDG_CONFIG_HOME"] = str(config_home)
        os.environ["XDG_CONFIG_DIRS"] = str(config_dirs)
        env = dict(os.environ, TUIOS_SERVER="unrelated", TUIOS_SOCKET="/tmp/cmtm-do-not-use.sock", TERM="xterm-256color")
        base = [sys.executable, "-m", "cmtm", "--server", server, "--database", str(database)]

        def cli(*args, machine=False, success=True, input=None):
            command = base + (["--json"] if machine else []) + list(args)
            result = subprocess.run(command, cwd=ROOT, env=env, input=input, text=True, capture_output=True, timeout=15)
            if success and result.returncode:
                raise AssertionError(f"{args}: {result.returncode}\n{result.stdout}\n{result.stderr}")
            if not success and not result.returncode:
                raise AssertionError(f"Expected failure: {args}")
            return json.loads(result.stdout) if machine else result

        help_result = cli("--help")
        assert "usage:" in help_result.stdout.lower() and not help_result.stdout.lstrip().startswith("{")
        assert "terminal" in cli(success=False).stderr.lower(), "Noninteractive startup must fail before creating resources"
        cli("projects", "create", "Smoke project")
        cli("context", "create", "Durable context", "--project", "Smoke project", "--text", "saved source text")
        store = Store(database)
        records = store.list("context")
        record = next(row for row in records if row["label"] == "Durable context")
        project_id = record["project_id"]
        assert store.get(project_id)["label"] == "Smoke project"
        store.close()
        cli("context", "reassociate", record["id"], "no-project")
        store = Store(database)
        assert store.get(record["id"])["project_id"] is None
        assert not any(row["label"] == "no-project" for row in store.list("project"))
        store.set_flag(record["id"], "starred", True)
        store.close()
        store = Store(database)
        assert store.get(record["id"])["starred"] is True
        try:
            store.save_with_project("context", "", project="Must roll back")
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid context unexpectedly committed")
        assert not any(row["label"] == "Must roll back" for row in store.list("project"))
        store.close()
        print("PASS CLI help, SQLite restart persistence, project association, projectless choice, stars, rollback", flush=True)

        # Real executable dispatch, including a renamed entry point and builtin precedence.
        alias = root / "custom"
        alias.symlink_to(ROOT / "bin" / "cmtm")
        external = root / "custom-smoke"
        external.write_text("#!/usr/bin/env python3\nimport os,sys\nprint(os.environ.get('TUIOS_SERVER'))\nprint('|'.join(sys.argv[1:]))\n")
        external.chmod(0o755)
        shadow = root / "custom-projects"
        shadow.write_text("#!/bin/sh\necho SHOULD_NOT_RUN\n")
        shadow.chmod(0o755)
        dispatch_env = dict(env, PATH=str(root) + os.pathsep + os.environ.get("PATH", ""))
        result = subprocess.run([str(alias), "--server", server, "smoke", "a b", "literal;$", "--json", "--server", "plugin-value"], env=dispatch_env, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert server in result.stdout and "a b|literal;$|--json|--server|plugin-value" in result.stdout, result.stdout
        result = subprocess.run([str(alias), "--server", server, "projects", "invalid"], env=dispatch_env, text=True, capture_output=True, timeout=10)
        assert result.returncode != 0 and "SHOULD_NOT_RUN" not in result.stdout
        print("PASS renamed PATH dispatch, argument preservation, server environment, builtin precedence", flush=True)

        daemon = Daemon(server)
        try:
            manager = startup_acceptance(base, env, daemon, detach=True)
            assert shared_config.read_text() == shared_text, "Startup rewrote shared TUIOS config"
            assert not any("cmtm_fixture" in str(row) for row in daemon.snapshot()), "Shared host auto-connect leaked into isolated daemon"
            target = daemon.launch("terminal", project="Smoke project", cwd=str(root), name="acceptance")
            assert target["fields"]["session"] == "Smoke project", target
            assert target["fields"]["server"] == server, target
            marker = "CMTM_ACCEPT_" + secrets.token_hex(4)
            # Split the literal so echoed command text cannot satisfy readiness.
            daemon.send(target, "printf '%s%s\\n' '" + marker[:10] + "' '" + marker[10:] + "'; printf '%s\\n' \"$XDG_CONFIG_HOME\" \"$XDG_CONFIG_DIRS\"\n")
            deadline = time.monotonic() + 8
            screen = ""
            while time.monotonic() < deadline:
                screen = daemon.capture(target, cols=100, rows=28)
                if marker in screen and str(config_home) in screen and str(config_dirs) in screen:
                    break
                time.sleep(0.05)
            assert marker in screen, screen
            assert str(config_home) in screen and str(config_dirs) in screen, "Daemon isolation leaked private config into launched shell: " + screen
            assert any(row["id"] == target["id"] for row in daemon.snapshot()), "Launched target missing from inventory"
            (ROOT / "acceptance-screen.txt").write_text(screen)
            print("PASS isolated real terminal launch, session placement, input and read-only capture", flush=True)
            stream = daemon.open_live(target, cols=60, rows=18)
            try:
                live_marker = "LIVE_ACCEPT_" + secrets.token_hex(4)
                stream.send(("printf '%s%s\\n' '" + live_marker[:8] + "' '" + live_marker[8:] + "'\r").encode())
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if live_marker in daemon.capture(target):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("Live input never reached the selected pane")
            finally:
                stream.close()
            try:
                stream.send(b"SHOULD_NOT_SEND")
            except Exception:
                pass
            else:
                raise AssertionError("Closed live stream accepted input")
            print("PASS real live stream input and closed-stream protection", flush=True)
            ui_acceptance(base + ["--internal-ui"], env, root)
            store = Store(database)
            assert any(row["label"] == "UI created project" for row in store.list("project")), "TUI form did not persist project"
            store.close()
            sample = root / "example.json"
            sample.write_text('{"items": [{"message": "JSON acceptance"}], "enabled": true}')
            ui_acceptance(base + ["json", str(sample)], env, root, json_mode=True)
            second_manager = startup_acceptance(base, env, daemon, detach=False, expected=manager, live_target=target)
            assert second_manager["id"] == manager["id"], "Repeated launch replaced the running UI"
            survivors = {row["id"] for row in daemon.snapshot()}
            assert manager["id"] in survivors and target["id"] in survivors, "Client exit closed unrelated managed windows"
            startup_acceptance(base, env, daemon, detach=True, expected=manager)
            daemon.close_window(manager)
            startup_acceptance(base, env, daemon, detach=False, missing=True)
            print("PASS cold startup, same UI/inventory/Context resume, hosted q, later reattach and full-screen frame", flush=True)
            daemon.close_window(target)
            assert not any(row["id"] == target["id"] for row in daemon.snapshot()), "Closed target remains live"
            print("PASS explicit target closure", flush=True)
        finally:
            daemon.close()
            subprocess.run(["tuios", "--server", server, "kill-server"], capture_output=True, timeout=8)
    print("ACCEPTANCE PASS", flush=True)


def assert_frame(screen, *, live=False):
    lines = screen.splitlines()
    assert len(lines) == 28, f"Frame must fill all 28 terminal rows: {screen}"
    assert all(len(line) == 100 for line in lines), f"Frame must fill all 100 terminal columns: {screen}"
    assert all(line.startswith("+") and line.endswith("+") for line in (lines[0], lines[-1])), screen
    assert "cmtm" in lines[0] and "cmtm" in lines[-1], screen
    assert all(line.startswith("|") and line.endswith("|") for line in lines[1:-1]), screen
    assert ("LIVE" if live else "NORMAL") in lines[-1], screen


def startup_acceptance(base, env, daemon, *, detach, expected=None, missing=False, live_target=None):
    import fcntl
    import pty
    import select
    import struct
    import termios

    try:
        previous = {row["id"] for row in daemon.snapshot()}
    except Exception:
        previous = set()
    if expected is None and not missing:
        assert not previous, "Noninteractive/data commands unexpectedly started the daemon"
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
    process = subprocess.Popen(base, cwd=ROOT, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = bytearray()
    manager = None
    screen = ""

    def observe(*markers, live=False):
        deadline = time.monotonic() + 8
        screen = ""
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
            if process.poll() is not None:
                break
            screen = daemon.capture(manager, cols=100, rows=28)
            if all(marker in screen for marker in markers):
                assert_frame(screen, live=live)
                return screen
        raise AssertionError(f"Managed UI did not render {markers}: {screen}\n{bytes(output[-4000:])!r}")

    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
            if process.poll() is not None:
                break
            try:
                rows = daemon.snapshot()
                managers = [row for row in rows if row["kind"] == "window" and row["fields"].get("session") == "mtm-only"]
                if managers:
                    assert not missing, "Running daemon with no UI unexpectedly created a replacement"
                    assert len(managers) == 1, "Default launch created duplicate UI windows"
                    manager = managers[0]
                    if expected is not None:
                        assert manager["id"] == expected["id"], "Resume selected a different UI"
                        assert {row["id"] for row in rows} == previous, "Resume changed daemon inventory"
                    screen = daemon.capture(manager, cols=100, rows=28)
                    frame = screen.splitlines()
                    if ("manager placement: verified mtm-only" in screen and "current last OK" in screen
                            and len(frame) == 28 and len(frame[0]) == 100):
                        break
            except Exception as exc:
                if isinstance(exc, AssertionError):
                    raise
        if missing:
            process.wait(timeout=5)
            assert process.returncode != 0, "Missing manager was silently accepted"
            assert b"has no live Matomeru manager" in output, bytes(output[-4000:])
            assert {row["id"] for row in daemon.snapshot()} == previous, "Missing-manager startup mutated inventory"
            print("PASS running daemon with missing UI refuses without creating windows", flush=True)
            return None
        assert manager is not None and "manager placement: verified mtm-only" in screen and "current last OK" in screen, (screen, bytes(output[-4000:]))
        assert_frame(screen)
        inventory = {row["id"] for row in daemon.snapshot()}
        print("DEFAULT STARTUP SCREEN\n" + screen, flush=True)
        if expected is None:
            os.write(master, b"3")
            screen = observe("[3:Context]", "Durable context")
        assert "[3:Context]" in screen and "Durable context" in screen, "UI page/selection did not survive resume: " + screen
        if live_target is not None:
            os.write(master, b"1g")
            observe("[1:Panes]", "Read-only screen", "acceptance [window]")
            os.write(master, b"i")
            observe("LIVE screen", "ctrl+a d", live=True)
            marker = "FRAMED_LIVE_" + secrets.token_hex(4)
            os.write(master, ("printf '%s%s\\n' '" + marker[:8] + "' '" + marker[8:] + "'\r").encode())
            screen = observe("LIVE screen", marker, live=True)
            assert marker in daemon.capture(live_target), "Framed live input reached the wrong target"
            (ROOT / "acceptance-live-screen.txt").write_text(screen)
            print("EMBEDDED LIVE SCREEN\n" + screen, flush=True)
            os.write(master, b"\x01d")
            observe("NORMAL", "Read-only screen")
            os.write(master, b"3")
            screen = observe("[3:Context]", "Durable context")
        (ROOT / "acceptance-manager-screen.txt").write_text(screen)
        os.write(master, b"\x07d" if detach else b"q")
        process.wait(timeout=8)
        assert process.returncode == 0, bytes(output[-4000:])
        rows = daemon.snapshot()
        assert any(row["id"] == manager["id"] for row in rows), "Client quit/detach closed the persistent UI"
        assert {row["id"] for row in rows} == inventory, "Client quit/detach changed daemon inventory"
        screen = daemon.capture(manager, cols=100, rows=28)
        assert "[3:Context]" in screen and "Durable context" in screen, "Detach lost Context state"
        return manager
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        os.close(master)


def ui_acceptance(base, env, root, json_mode=False):
    import fcntl
    import pty
    import select
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
    process = subprocess.Popen(base, cwd=ROOT, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = bytearray()

    def observe(expected, seconds=8):
        deadline = time.monotonic() + seconds
        start = len(output)
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], min(0.2, max(0, deadline - time.monotonic())))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                output.extend(chunk)
                if expected.lower() in bytes(output[start:]).lower():
                    return
            if process.poll() is not None:
                break
        raise AssertionError(f"TUI did not render {expected!r}: {bytes(output[-4000:])!r}")

    try:
        observe(b"1:JSON" if json_mode else b"cmtm")
        if not json_mode:
            os.write(master, b"?")
            observe(b"Help")
            os.write(master, b"\x1b")
            # Wait for the close repaint before sending a key that could be Alt-prefixed.
            if select.select([master], [], [], 2)[0]:
                output.extend(os.read(master, 65536))
            os.write(master, b"np")
            observe(b"New Project")
            os.write(master, b"iUI created project\x13")
            if select.select([master], [], [], 2)[0]:
                output.extend(os.read(master, 65536))
            os.write(master, b"2")
            observe(b"[2:Projects]")
            os.write(master, b"1")
            observe(b"Read-only screen")
            os.write(master, b"i")
            observe(b"ctrl+a d detaches")
            os.write(master, b"\x01d")
            observe(b"NORMAL")
            os.write(master, b"M")
            observe(b"Modes")
            os.write(master, b"\r")
            observe(b"Jump")
            os.write(master, b"\x1b")
            observe(b"Modes")
            os.write(master, b"\x1b")
            if select.select([master], [], [], 2)[0]:
                output.extend(os.read(master, 65536))
            os.write(master, b"3")
            observe(b"Durable context")
        os.write(master, b"q")
        process.wait(timeout=5)
        assert process.returncode == 0, bytes(output[-4000:])
        print("PASS built-in JSON explorer" if json_mode else "PASS curses form persistence, embedded live/detach, help, nested Jump and context", flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        os.close(master)
        (ROOT / ("acceptance-json.ansi" if json_mode else "acceptance-tui.ansi")).write_bytes(output)
        if json_mode:
            assert b"cmtm" in output, "JSON explorer omitted the application frame"


if __name__ == "__main__":
    main()
