"""Human-first commands and argv-only PATH extension dispatch."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from . import __version__, concepts
from .app import open_services
from .daemon import isolated_environment


_ALIASES = {
    "projects": "project", "contexts": "context", "tags": "tag",
    "windows": "window", "panes": "window", "sessions": "session", "hosts": "host",
}
_PERSISTENT = {"project", "context", "tag"}
_LIVE = {"window", "session", "host"}
_BUILTINS = {"tui", "json", "bootstrap", "settings", "daemon", "launch",
             "capture", "send", "close", "live", "verbs", "call"}


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return value


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _globals(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", default="clean-mtm", help="isolated TUIOS server (default: clean-mtm)")
    parser.add_argument("--database", help="isolated application SQLite database path")
    parser.add_argument("--json", action="store_true", dest="machine", help="explicit machine-readable JSON output")


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Manage an isolated TUIOS server. No automatic daemon startup.",
        epilog="Unknown commands execute <invoked-name>-<command> from PATH. Put application global options before the command; external arguments are forwarded literally.")
    _globals(parser)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("tui", help="open the UI in this terminal; never move the current session")
    viewer = commands.add_parser("json", help="explore JSON interactively; file, '-' for stdin, or a native picker")
    viewer.add_argument("file", nargs="?")
    bootstrap = commands.add_parser("bootstrap", help="explicitly start isolated server and launch the UI in mtm-only")
    bootstrap.add_argument("--attach", action="store_true", help="enter the created UI window after launch")

    names = set(concepts.CONCEPTS) | set(_ALIASES) | _PERSISTENT | _LIVE
    for name in sorted(names - _BUILTINS):
        kind = _ALIASES.get(name, name)
        command = commands.add_parser(name, help=f"manage {kind} records")
        command.set_defaults(kind=kind)
        actions = command.add_subparsers(dest="action")
        listing = actions.add_parser("list", help="list records using shared search and filters")
        listing.add_argument("--query", default="")
        listing.add_argument("--project", help="exact project name, id, or no-project (does not create)")
        listing.add_argument("--filter", action="append", default=[], metavar="FIELD=VALUE")
        listing.add_argument("--sort", default="label")
        listing.add_argument("--descending", action="store_true")
        listing.add_argument("--group", action="append", default=[])
        listing.add_argument("--scope", action="append", default=[])
        show = actions.add_parser("show", help="inspect a record by stable id")
        show.add_argument("id")
        for flag in ("star", "pin"):
            sub = actions.add_parser(flag, help=f"set presentation {flag}; never changes ownership")
            sub.add_argument("id")
            sub.add_argument("value", choices=("on", "off"))
        if kind in _PERSISTENT:
            create = actions.add_parser("create", help="save a new record")
            create.add_argument("label")
            create.add_argument("--project", default="no-project")
            create.add_argument("--fields", type=_json_object, default={})
            update = actions.add_parser("update", help="update a record, preserving unspecified fields")
            update.add_argument("id")
            update.add_argument("--label")
            update.add_argument("--project")
            update.add_argument("--fields", type=_json_object, default={})
            if kind == "context":
                for action in (create, update):
                    for field in ("path", "text", "url", "source_server", "source_host", "source_session", "source_window"):
                        action.add_argument("--" + field.replace("_", "-"))
                reassociate = actions.add_parser("reassociate", help="explicitly change ownership; no-project removes it")
                reassociate.add_argument("id")
                reassociate.add_argument("project")
            delete = actions.add_parser("delete", help="delete saved state only, never daemon windows")
            delete.add_argument("id")
            delete.add_argument("--yes", action="store_true", help="confirm deletion")

    settings = commands.add_parser("settings", help="inspect, set, or reset durable settings")
    actions = settings.add_subparsers(dest="action")
    actions.add_parser("list")
    get = actions.add_parser("get")
    get.add_argument("key")
    setting = actions.add_parser("set")
    setting.add_argument("key")
    setting.add_argument("value", help="JSON value or plain text")
    reset = actions.add_parser("reset")
    reset.add_argument("key")

    daemon = commands.add_parser("daemon", help="explicit isolated-server lifecycle and management")
    actions = daemon.add_subparsers(dest="action")
    actions.add_parser("start")
    actions.add_parser("status")
    actions.add_parser("verbs", help="discover actual supported daemon verbs")
    call = actions.add_parser("call", help="invoke an actual daemon verb")
    _call_arguments(call)
    commands.add_parser("verbs", help="discover actual supported daemon verbs")
    _call_arguments(commands.add_parser("call", help="invoke an actual supported daemon management verb"))
    launch = commands.add_parser("launch", help="create a terminal, agent, or Vim window")
    launch.add_argument("kind", choices=("terminal", "agent", "vim"))
    for field, default in (("project", "no-project"), ("cwd", ""), ("name", ""),
                           ("harness", ""), ("model", ""), ("prompt", ""), ("host", "local")):
        launch.add_argument("--" + field, default=default)
    launch.add_argument("--options", type=_json_object, default={}, help="additional supported daemon launch options")
    capture = commands.add_parser("capture", help="bounded read-only terminal preview")
    capture.add_argument("id", help="stable window id from windows list")
    capture.add_argument("--cols", type=int, default=80)
    capture.add_argument("--rows", type=int, default=24)
    send = commands.add_parser("send", help="explicitly send text to one stable window target")
    send.add_argument("id")
    send.add_argument("text")
    send.add_argument("--queue", action="store_true")
    close = commands.add_parser("close", help="explicitly close one daemon window")
    close.add_argument("id")
    close.add_argument("--yes", action="store_true", help="confirm closure")
    live = commands.add_parser("live", help="explicit live interaction with one stable window target")
    live.add_argument("id")
    return parser


def _call_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("verb")
    parser.add_argument("--params", type=_json_object, default={})


def _emit(value: Any, machine: bool) -> None:
    if machine:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        _human(value)


def _human(value: Any, indent: str = "") -> None:
    if isinstance(value, list):
        if not value:
            print(indent + "No records.")
        for index, item in enumerate(value):
            if isinstance(item, dict) and "id" in item and "label" in item:
                flags = "".join((" [starred]" if item.get("starred") else "", " [pinned]" if item.get("pinned") else ""))
                print(_scalar(f"{indent}{item['id']}  {item.get('kind', '')}  {item['label']}{flags}"))
                fields = item.get("fields", {})
                summary = [f"{key}={fields[key]}" for key in ("server", "host", "session", "state", "cwd") if fields.get(key) is not None]
                summary.append(f"project={item.get('project_id') or 'no-project'}")
                print(_scalar(indent + "  " + "  ".join(summary)))
            else:
                print(f"{indent}{index + 1}:")
                _human(item, indent + "  ")
    elif isinstance(value, dict):
        if not value:
            print(indent + "No stored values.")
        for key, item in value.items():
            if isinstance(item, (list, dict)):
                print(f"{indent}{_scalar(key)}:")
                _human(item, indent + "  ")
            else:
                print(f"{indent}{_scalar(key)}: {_scalar(item)}")
    else:
        print(indent + _scalar(value))


def _scalar(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    # Do not let remote labels or saved text execute terminal control sequences.
    return "".join(char if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159) else "�" for char in str(value))


def _project_id(store: Any, text: str) -> str | None:
    if text.casefold() in ("", "no-project"):
        return None
    rows = store.list(kind="project")
    matches = [row for row in rows if row["id"] == text or row["label"].casefold() == text.casefold()]
    if len(matches) != 1:
        raise ValueError(f"project not found or ambiguous: {text}")
    return matches[0]["id"]


def _record(services: Any, kind: str, identity: str) -> dict[str, Any]:
    if kind in _LIVE:
        matches = [row for row in services.daemon.snapshot() if row["kind"] == kind and row["id"] == identity]
        if len(matches) != 1:
            raise ValueError(f"{kind} stable id not found or ambiguous: {identity}")
        return services.store.decorate(matches)[0]
    row = services.store.get(identity)
    if row is None or row["kind"] != kind:
        raise ValueError(f"{kind} record not found: {identity}")
    return row


def _confirm(accepted: bool, message: str) -> None:
    if accepted:
        return
    if not sys.stdin.isatty():
        raise ValueError(message + "; pass --yes to confirm")
    if input(_scalar(message) + " Type yes to confirm: ").strip() != "yes":
        raise ValueError("cancelled; no changes made")


def _records(services: Any, args: argparse.Namespace) -> Any:
    store, kind = services.store, args.kind
    action = args.action or "list"
    if action == "list":
        rows = services.daemon.snapshot() if kind in _LIVE else store.list(kind=kind)
        rows = [row for row in rows if row["kind"] == kind]
        if kind in _LIVE:
            rows = store.decorate(rows)
        if getattr(args, "project", None) is not None:
            project_id = _project_id(store, args.project)
            rows = [row for row in rows if row.get("project_id") == project_id]
        filters: dict[str, list[str]] = {}
        for expression in getattr(args, "filter", []):
            field, separator, value = expression.partition("=")
            if not separator or not field:
                raise ValueError("filters must use FIELD=VALUE")
            filters.setdefault(field, []).append(value)
        return concepts.filter_records(rows, query=getattr(args, "query", ""), filters=filters,
            sort=getattr(args, "sort", "label"), descending=getattr(args, "descending", False),
            group_by=getattr(args, "group", None) or None, group_scope=getattr(args, "scope", None) or None)
    if action == "create":
        fields = _fields(args)
        if kind == "project":
            if args.project != "no-project":
                raise ValueError("projects cannot be owned by another project")
            return store.save(kind, args.label, fields=fields)
        return store.save_with_project(kind, args.label, fields=fields, project=args.project)
    row = _record(services, kind, args.id)
    if action == "show":
        return row
    if action in ("star", "pin"):
        store.set_flag(args.id, "starred" if action == "star" else "pinned", args.value == "on")
        return store.decorate([row])[0] if kind in _LIVE else store.get(args.id)
    if action == "delete":
        _confirm(args.yes, f"Delete saved {kind} {row['label']} ({row['id']})?")
        store.delete(args.id)
        return {"deleted": args.id}
    if action == "reassociate":
        return store.reassociate(args.id, args.project)
    if action == "update":
        fields = dict(row.get("fields", {}))
        fields.update(_fields(args))
        label = args.label if args.label is not None else row["label"]
        if args.project is not None:
            if kind == "project":
                raise ValueError("projects cannot be owned by another project")
            return store.save_with_project(kind, label, fields=fields, project=args.project, id=args.id)
        return store.save(kind, label, fields=fields, project_id=row.get("project_id"), id=args.id)
    raise ValueError(f"unsupported record action: {action}")


def _fields(args: argparse.Namespace) -> dict[str, Any]:
    fields = dict(args.fields)
    for key in ("path", "text", "url", "source_server", "source_host", "source_session", "source_window"):
        value = getattr(args, key, None)
        if value is not None:
            fields[key] = value
    return fields


def _json_view(services: Any, filename: str | None) -> None:
    from . import ui
    if filename is None and sys.stdin.isatty():
        from .pickers import pick_path
        filename = pick_path(kind="file", title="Choose JSON file")
        if filename is None:
            return
    if filename is None or filename == "-":
        data = json.load(sys.stdin)
    else:
        with Path(filename).expanduser().open(encoding="utf-8") as source:
            data = json.load(source)
    data = ui.JSON_NULL if data is None else data
    if sys.stdin.isatty():
        ui.run(services, json_data=data)
        return
    # JSON consumes the pipe, but curses must read keys from the controlling tty.
    try:
        terminal = os.open("/dev/tty", os.O_RDONLY)
    except OSError as exc:
        raise ValueError("the JSON explorer requires an interactive controlling terminal") from exc
    saved = os.dup(0)
    try:
        os.dup2(terminal, 0)
        ui.run(services, json_data=data)
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(terminal)


def _execute(services: Any, args: argparse.Namespace) -> Any:
    command = args.command or "tui"
    if command == "tui":
        from .ui import run
        run(services)
        return None
    if command == "json":
        _json_view(services, args.file)
        return None
    if command == "bootstrap":
        checkout_entry = Path(__file__).resolve().parent.parent / "bin" / "cmtm"
        entry = [str(checkout_entry)] if checkout_entry.is_file() else ["-m", "cmtm"]
        argv = [sys.executable, *entry, "--server", args.server]
        if args.database:
            argv.extend(("--database", str(Path(args.database).expanduser().resolve())))
        argv.append("tui")
        result = services.daemon.bootstrap(argv)
        if args.attach:
            services.daemon.live(result)
        return result
    if hasattr(args, "kind") and command != "launch":
        return _records(services, args)
    if command == "settings":
        action = args.action or "list"
        if action == "list":
            return services.store.settings()
        if action == "get":
            values = services.store.settings()
            if args.key not in values:
                raise ValueError(f"no stored override for {args.key}; default is in effect")
            return {args.key: values[args.key]}
        if action == "set":
            services.store.set_setting(args.key, _value(args.value))
            return {args.key: services.store.settings()[args.key]}
        services.store.unset_setting(args.key)
        return {"reset": args.key, "value": "default"}
    if command == "daemon":
        action = args.action or "status"
        if action == "start":
            result = services.daemon.start()
            return result if result is not None else {"server": args.server, "state": "started"}
        if action == "status":
            rows = services.daemon.snapshot()
            return {"server": args.server, "state": "connected", "records": len(rows)}
        if action == "verbs":
            return services.daemon.verbs()
        return services.daemon.call(args.verb, args.params)
    if command == "verbs":
        return services.daemon.verbs()
    if command == "call":
        return services.daemon.call(args.verb, args.params)
    if command == "launch":
        options = {key: getattr(args, key) for key in ("project", "cwd", "name", "harness", "model", "prompt", "host")}
        conflicts = (options.keys() | {"project_id"}) & args.options.keys()
        if conflicts:
            raise ValueError("--options cannot override dedicated or resolved launch fields: " + ", ".join(sorted(conflicts)))
        options.update(args.options)
        project = args.project.strip()
        project_id = None
        if not project or project.casefold() == "no-project":
            project = "no-project"
        else:
            for row in services.store.list(kind="project"):
                if row["id"] == project or row["label"].casefold() == project.casefold():
                    project = row["label"]
                    project_id = row["id"]
                    break
        options["project"] = project
        options["project_id"] = project_id
        result = services.daemon.launch(args.kind, **options)
        try:
            services.store.save_with_project(result["kind"], result["label"],
                fields=result["fields"], project=project, id=result["id"])
        except Exception as exc:
            raise RuntimeError(f"Window {result['id']} launched, but saving its project association failed: {exc}. "
                               "Do not retry launch; inspect this existing target.") from exc
        return services.store.decorate([result])[0]
    target = _record(services, "window", args.id)
    if command == "capture":
        return services.daemon.capture(target, cols=args.cols, rows=args.rows)
    if command == "send":
        result = services.daemon.send(target, args.text, queue=args.queue)
        return result if result is not None else {"target": args.id, "state": "accepted", "delivery": "unknown"}
    if command == "close":
        _confirm(args.yes, f"Close window {target['label']} ({args.id}) on {args.server}?")
        services.daemon.close_window(target)
        return {"closed": args.id, "server": args.server}
    if command == "live":
        services.daemon.live(target)
        return None
    raise ValueError(f"unsupported command: {command}")


def _program_name() -> str:
    name = Path(sys.argv[0]).name
    return "cmtm" if name == "__main__.py" else name


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    prog = _program_name()
    # Only the prefix belongs to us: flags following an external command are its
    # literal argv, even if they happen to share our global option names.
    boundary = 0
    while boundary < len(arguments):
        option = arguments[boundary]
        if option in ("--server", "--database"):
            boundary += min(2, len(arguments) - boundary)
        elif option == "--json" or option.startswith(("--server=", "--database=")):
            boundary += 1
        else:
            break
    globals_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    _globals(globals_parser)
    global_args = globals_parser.parse_args(arguments[:boundary])
    remaining = arguments[boundary:]
    known = _BUILTINS | set(_ALIASES) | set(concepts.CONCEPTS) | _PERSISTENT | _LIVE
    command = remaining[0] if remaining and not remaining[0].startswith("-") else None
    try:
        if command is not None and command not in known:
            if "/" in command or "\\" in command:
                raise ValueError("external subcommand must be a name, not a path")
            executable = shutil.which(f"{prog}-{command}")
            if executable is None:
                raise ValueError(f"unknown command {command!r}; no {prog}-{command} executable on PATH")
            environment = isolated_environment(global_args.server)
            if global_args.database:
                environment["CMTM_DATABASE"] = str(Path(global_args.database).expanduser().resolve())
            if global_args.machine:
                environment["CMTM_OUTPUT"] = "json"
            os.execvpe(executable, [executable, *remaining[1:]], environment)
        parser = _parser(prog)
        args = parser.parse_args(remaining)
        args.server = global_args.server
        args.database = global_args.database
        args.machine = global_args.machine
        with open_services(server=args.server, database=args.database) as services:
            result = _execute(services, args)
        if result is not None:
            _emit(result, args.machine)
        return 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 141
    except Exception as exc:
        if global_args.machine:
            print(json.dumps({"error": str(exc), "server": global_args.server}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"{prog}: {_scalar(exc)}", file=sys.stderr)
        return 1
