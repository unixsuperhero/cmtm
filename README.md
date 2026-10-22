# cmtm

A clean-room terminal application for an isolated TUIOS server, with project-associated context stored in SQLite.

The application uses the named server `clean-mtm`. It must never fall back to the default TUIOS daemon. Project windows live in project-named sessions; projectless windows use `no-project`, and the application can run in `mtm-only`.

## Run

Requires Python 3.11 or newer with curses and an installed `tuios` executable.

```sh
python3 -m cmtm --help
python3 -m cmtm
```

Run from this checkout, or install with `python3 -m pip install .` and use `cmtm`. Only the `cmtm` executable is installed, so an existing `mtm` installation is not replaced. The application does not silently start a daemon.

```sh
python3 -m cmtm daemon start
python3 -m cmtm bootstrap --attach
```

`bootstrap` starts a UI window in the isolated server's `mtm-only` session. Running `python3 -m cmtm` directly opens the UI in the current terminal without moving that terminal into another daemon.

```sh
python3 -m cmtm projects create Website
python3 -m cmtm context create Decisions --project Website --text "Use SQLite"
python3 -m cmtm context list --project Website
python3 -m cmtm json example.json
```

Put global `--server`, `--database`, and `--json` options before the command. Output is human-readable by default. Unknown command names dispatch to `cmtm-<command>` executables on PATH; built-in commands always win.

## Controls

| Key | Action |
| --- | --- |
| `1` through `6` | Panes, Projects, Context, Sessions, Hosts, Settings |
| `?` / `:` | Effective help / nested command palette |
| `J` / `C` / `M` | Jump / Context / mode-selector modal |
| `j k`, `g G`, `d u` | Move, ends, half-page navigation |
| `/`, `f`, `s`, `z` / `Z` | Search, filter, sort, cycle / customize grouping |
| `n t`, `n a`, `n v` | Quick terminal, agent, or Vim creation |
| `N t`, `N a`, `N v` | Editable launch forms |
| `n p`, `n c`, `c` | New project, new context, save selected context |
| `e`, `x` | Edit / confirmed delete or close |
| `p`, `*` | Pin / star |
| `i` | Explicit live input to selected window |
| `Ctrl+a` then `d` | Leave live mode without closing the target |
| `Ctrl+s`, `F4` | Submit form / native path picker |
| `q` | Exit client without stopping its daemon |

Jump/Context Escape returns one nesting level with its state intact. The command palette has separate search-first Escape behavior. Form editing preserves drafts; cancellation never creates vocabulary values or projects.

## Design

- [Specification](docs/specification.md): required interaction, isolation, project/context, SQLite, and CLI behavior.
- [Module contracts](docs/contracts.md): independently owned modules and their APIs.

`app` is the thin composition root. Storage owns SQLite transactions and associations; concepts own shared field/action definitions and list operations; the daemon adapter owns all TUIOS calls; the terminal UI owns focus, selection, forms and modal state; the CLI owns argument parsing and presentation.

The implementation uses the Python standard library rather than a separate service or UI framework. Background I/O is bounded. A shared live snapshot and stable record identities let refresh update data without resetting user input. SQLite stores application-owned data, not TUIOS process state.

## Safety and scope

Existing Matomeru databases and TUIOS default-server sessions are not application storage or test fixtures. Verification uses unique `cmtm-t-<six-hex-digits>` servers and temporary databases. The short names fit TUIOS's 14-character limit. Secrets remain in their owning tools.

Context can be associated with projects or left projectless. This is not a task tracker or an automatic knowledge-extraction service. Remote operations currently refuse because the available host-link API does not establish separate named-server endpoints. Startup also refuses active remote host auto-follow configuration rather than letting a named daemon connect to the user's default remote hosts. It does not rewrite user configuration. If choosing a separate `XDG_CONFIG_HOME`, do so explicitly and account for its effect on launched programs.

Native file-picker support depends on the operating system and desktop tools; an unavailable picker is reported rather than silently replaced by manual path entry. Agent model, effort, and speed options require authoritative capability metadata; unsupported options are rejected instead of inventing harness flags.

## Verification

Use a single compact acceptance pass covering real CLI persistence, daemon launch/capture isolation, and keyboard-driven TUI interaction. Platform-specific behavior must be reported separately. See the behavioral criteria at the end of the specification.

```sh
bash scripts/verify-vps.sh
```

This copies a source snapshot to `vps2`, creates a disposable configuration directory and database, bounds the run to 120 seconds, and stops only its uniquely named test server. `CMTM_VERIFY_TIMEOUT` can select a deadline from 1 to 900 seconds. The snapshot retains `verification.log` and captured terminal output.

Verified on Linux (`vps2`) with the installed TUIOS: SQLite reopen persistence, project/context reassociation and rollback, renamed CLI dispatch and argument preservation, isolated terminal launch/input/capture, live-stream closure, keyboard-driven project creation, embedded live attach/detach, nested Jump navigation, context browsing, JSON exploration, and a live inventory with verified `mtm-only` placement. The complete verification command finished in 4.09 seconds; this is acceptance evidence, not a performance benchmark.

Native macOS execution, desktop picker dialogs, and agent-provider authentication were not exercised. Remote mutations and unavailable model/effort/speed catalogs are explicitly rejected as described above.
