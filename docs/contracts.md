# Module contracts

Implement `docs/specification.md` in Python 3.11+ with the standard library, SQLite, curses, and installed TUIOS. No legacy application imports. The executable is `cmtm`; `mtm` may be an installation entry point for the specified CLI spelling, but use the actual executable basename for external subcommands.

## Ownership

- `cmtm/store.py`: SQLite persistence, migrations, transactions and project/context ownership. No terminal or daemon imports.
- `cmtm/concepts.py`: declarative concept/field/action definitions and pure shared list operations. No curses, SQLite or subprocess imports.
- `cmtm/daemon.py`: the single TUIOS boundary, isolated server selection, snapshot/event/capture/live/launch operations. No store or curses imports.
- `cmtm/ui.py`, `cmtm/forms.py`, `cmtm/pickers.py`: terminal presentation, input, modal stack and native pickers. Domain changes use store/daemon methods.
- `cmtm/app.py`: thin lifecycle/composition orchestrator; creates services and closes them in reverse order. No rendering or domain rules.
- `cmtm/cli.py`, `cmtm/__main__.py`: argument parsing, human-readable output, JSON explorer and safe external dispatch.

## Shared shapes and APIs

Records are dictionaries with `id`, `kind`, `label`, `project_id` (null means no-project), `fields` (dictionary), `starred` and `pinned` booleans. Kind names: `project`, `context`, `tag`. Live records use kinds `window`, `session`, `host` and the same outer shape; their fields include server, host, session, window, state, cwd and freshness. Never identify records solely by label.

`Store(path)` opens/migrates an explicitly isolated file; `close()` closes it. Methods: `list(kind=None, project_id=None, all_projects=True) -> list[dict]`, `get(id) -> dict|None`, `save(kind,label,fields=None,project_id=None,id=None,starred=None,pinned=None) -> dict`, `delete(id)`, `resolve_project(text) -> str|None` (reuse case-insensitive exact project name, create unmatched, no-project/empty -> None), `save_with_project(kind,label,fields=None,project='no-project',id=None) -> dict` (transactionally resolves project and saves), `set_flag(id,flag,value)`, `settings() -> dict`, `set_setting(key,value)`, `unset_setting(key)`. Fields preserve source identities and context `path`, `text`, `url`. Project renames must refuse active session placement inconsistency unless daemon orchestration explicitly resolves it.

`concepts.CONCEPTS` is a dict of definitions, each with `label`, `fields` (field-name -> metadata dict: label,type,extensible,required,choices), and `actions`. `concepts.ACTIONS` is a dict of action metadata. `filter_records(records,query='',filters=None,sort='label',descending=False,group_by=None,group_scope=None) -> list[dict]` is pure, stable and supports fuzzy matching plus extensible field filters. `fuzzy_match(query,text) -> bool`.

`Daemon(server='clean-mtm')` rejects default/empty server and accepts only `clean-mtm` or `cmtm-t-` followed by six lowercase hexadecimal characters. TUIOS limits server names to 14 characters. `start()`, `close()` disconnects only, `snapshot() -> list[dict]` returns host/session/window records, `capture(target,cols=80,rows=24) -> str` is bounded read-only, `launch(kind,project='no-project',cwd='',name='',harness='',model='',prompt='',host='local',**options) -> dict` returns a window record, `close_window(target)`, `send(target,text,queue=False)`, `live(target)` attaches with explicit named-server CLI and returns on detach, `verbs() -> dict`, `call(verb,params=None) -> dict`. `subscribe(callback,stop_event)` blocks boundedly with reconnect/resync notifications until stopped; callback receives event dict. `.server` is public. Errors raise `DaemonError` with human-readable reason. Target is a live record; canonical daemon identities are in its fields. Remote isolated-server support must be proven or refused clearly. Do not fall back to a default remote daemon.

Embedded live mode uses `open_live(target,cols,rows)` and its handle's `send(bytes)`, `resize(cols,rows)`, `poll()`, `zoom()` and `close()` methods. Closing releases the size lease before discarding the connection. `capture_info` supplies read-only preview metadata; `inspect(target,tab)` supplies actual activity/transcript data. `live()` remains a separate full-screen named-client convenience for CLI attachment, not the TUI's embedded live implementation.

Persist successful live-window ownership through `store.save_with_project(target['kind'], target['label'], fields=target['fields'], project=project, id=target['id'])`. `store.decorate(snapshot)` overlays saved ownership and flags without changing daemon state or inferring a project from a session name. `store.vocabulary(field,kind=None)` supplies completion values and `store.reassociate(id,project)` changes saved associations explicitly.

`Services` in app has `.store`, `.daemon`, `.concepts` and `.settings` (dictionary). `app.open_services(server='clean-mtm',database=None)` returns services; `services.close()` releases them without stopping the daemon. Use the platform user-data directory dedicated to cmtm for default database. Service construction does not start a daemon; the default CLI launch explicitly orchestrates start/reuse, creation of a new UI window in `mtm-only`, and attachment. Internal UI execution must not recursively launch another UI.

`ui.run(services,json_data=None)` owns curses entry/exit. When json_data is not None, run the built-in JSON tree explorer without requiring daemon availability. UI must remain usable while daemon is disconnected. Background workers do bounded I/O; only the main curses thread renders. Reuse list/forms/modal behavior rather than one-off per-page copies.

## Integration rules

Each owner may add private helpers in its owned files. Changing a shared API requires notifying the parent and consumers before editing. Agents must not run builds/tests/formatters mid-flight. Parent owns final verification and publication. No mocks, placeholders or suppressed failures. Keep unsupported platform/daemon operations visible and explicit. Read installed TUIOS docs at `/Users/unixsuperhero/proj/tuios/docs/protocol.md` and CLI help; never guess verb parameter shapes.

## Performance

One shared live snapshot, stable IDs, event-triggered invalidation, bounded capture only for selected target, finite subprocess/network timeouts, stale-result rejection, indexed SQLite queries, transactions for multi-record changes, no full inventory reload per keystroke. Compute filter results only on data/query changes and render only when state changes. No premature caches or global polling loops per widget.
