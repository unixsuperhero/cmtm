# cmtm

A clean-room terminal application for an isolated TUIOS server, with project-associated context stored in SQLite.

The application uses the named server `clean-mtm`. It must never fall back to the default TUIOS daemon. Project windows live in project-named sessions; projectless windows use `no-project`, and the application can run in `mtm-only`.

## Run

Requires Python 3.11 or newer with curses and an installed `tuios` executable.

```sh
bin/cmtm
```

That's the complete startup command. If `clean-mtm` is not running, cmtm starts the detached daemon and creates its UI in `mtm-only`. If the daemon is already running, cmtm attaches to the existing UI without creating windows or resetting its screen, selection, or drafts. No separate startup commands or XDG environment setup are needed.

The UI has a persistent `cmtm` frame around all four edges of the terminal, including embedded terminal and agent interaction. The frame identifies normal versus live input; work stays inside it rather than replacing the application with a bare shell. Select a work window in Panes and press `i` for embedded live input. There is no separate public raw-target `live` shortcut.

Run from this checkout, or install with `python3 -m pip install .` and run `cmtm` from anywhere. Only the `cmtm` executable is installed, so an existing `mtm` installation is not replaced. `python3 -m cmtm` does the same thing.

`q` detaches viewers from the shared UI without exiting it. `Ctrl+g` then `d` detaches only your terminal. The daemon, UI, and work windows keep running; the next `cmtm` resumes that same UI. The outer viewer uses `Ctrl+g` so embedded-live `Ctrl+a` controls pass through unchanged. Standalone JSON exploration still exits normally.

If the daemon is running but its UI was explicitly closed or crashed, cmtm reports the missing UI instead of creating a replacement window. It never restarts that daemon or closes work windows automatically.

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
| `q` | Detach shared UI viewers; preserve daemon, UI state and work |

Jump/Context Escape returns one nesting level with its state intact. The command palette has separate search-first Escape behavior. Form editing preserves drafts; cancellation never creates vocabulary values or projects.

## Design

- [Specification](docs/specification.md): required interaction, isolation, project/context, SQLite, and CLI behavior.
- [Module contracts](docs/contracts.md): independently owned modules and their APIs.

`app` is the thin composition root. Storage owns SQLite transactions and associations; concepts own shared field/action definitions and list operations; the daemon adapter owns all TUIOS calls; the terminal UI owns focus, selection, forms and modal state; the CLI owns argument parsing and presentation.

The implementation uses the Python standard library rather than a separate service or UI framework. Background I/O is bounded. A shared live snapshot and stable record identities let refresh update data without resetting user input. SQLite stores application-owned data, not TUIOS process state.

## Safety and scope

Existing Matomeru databases and TUIOS default-server sessions are not application storage or test fixtures. Verification uses unique `cmtm-t-<six-hex-digits>` servers and temporary databases. The short names fit TUIOS's 14-character limit. Secrets remain in their owning tools.

Context can be associated with projects or left projectless. This is not a task tracker or an automatic knowledge-extraction service. Remote operations currently refuse because the available host-link API does not establish separate named-server endpoints. The managed daemon uses private TUIOS configuration automatically instead of loading your shared host auto-connect links. Your existing TUIOS configuration remains unchanged, and ordinary launched programs retain their configuration environment.

Private TUIOS configuration is generated under `cmtm-config` beside the selected named-server socket. Shared TUIOS customization is intentionally not imported. Existing running named daemons are reused, not reconfigured or restarted.

Native file-picker support depends on the operating system and desktop tools; an unavailable picker is reported rather than silently replaced by manual path entry. Agent model, effort, and speed options require authoritative capability metadata; unsupported options are rejected instead of inventing harness flags.

## Verification

Use a single compact acceptance pass covering real CLI persistence, daemon launch/capture isolation, and keyboard-driven TUI interaction. Platform-specific behavior must be reported separately. See the behavioral criteria at the end of the specification.

```sh
bash scripts/verify-vps.sh
```

This copies a source snapshot to `vps2`, creates a disposable configuration directory and database, bounds the run to 120 seconds, and stops only its uniquely named test server. `CMTM_VERIFY_TIMEOUT` can select a deadline from 1 to 900 seconds. The snapshot retains `verification.log` and captured terminal output.

The acceptance scenario covers cold plain-command startup despite active shared host links; unchanged shared configuration and restored shell configuration environment; resuming the same manager identity, inventory, and Context page after detach; hosted `q` preserving the UI and allowing a later reattach; refusal to create a missing UI on an already-running daemon; all four full-screen frame edges in normal and embedded-live modes; non-TTY startup refusal without resource creation; SQLite persistence and rollback; CLI dispatch; terminal input/capture; TUI project creation; nested navigation; context browsing; JSON exploration; and verified `mtm-only` placement. The run retains `acceptance-manager-screen.txt` and `acceptance-live-screen.txt` alongside terminal recordings. These describe coverage, not a substitute for the recorded result of a run.

Latest result: `bash scripts/verify-vps.sh` completed on Linux (`vps2`) with `ACCEPTANCE PASS`. The retained normal and embedded-live captures both filled a 100×28 terminal with intact cmtm borders on all four edges.

Native macOS execution, desktop picker dialogs, and agent-provider authentication were not exercised. Remote mutations and unavailable model/effort/speed catalogs are explicitly rejected as described above.
