# Clean-room Matomeru: TUIOS management and shared TUI rules

Build a new, standalone terminal application named `cmtm`. The `mtm` spelling below describes the command family; external commands always follow the actual executable basename. Use this outline as its behavioral specification, not as a request to port the old Matomeru implementation.

## 1. Scope

The app manages TUIOS hosts, sessions, windows, terminals, and agents. Managed Vim/Neovim windows are terminal targets, not a separate editor platform. Projects supply names and ownership for session placement, grouping, and associated context. Project context is in scope; do not build a task tracker, general knowledge base, Git/GitHub dashboard, automation service, reminder system, provider router, or general inventory designer.

Keep the shared TUI usable as the set of managed concepts grows. A new concept should reuse list, form, action, help, and palette behavior rather than introduce its own interaction rules. This extensibility does not authorize adding unrelated concepts now.

Everything here is a requirement for the new app, not a claim that it already exists. Resolve implementation details from the installed TUIOS API. Do not invent daemon commands or unsupported agent capabilities.

### Independent modules and thin orchestrator

Separate SQLite storage, concept definitions and list operations, the TUIOS adapter, shared UI/forms/native pickers, and CLI dispatch. Each module owns its rules and exposes a small explicit API. The orchestrator only constructs dependencies, connects callbacks, selects the entry point, and shuts resources down. It does not render views, duplicate persistence rules, or implement daemon operations. See [module contracts](contracts.md).

Optimize for responsiveness and low work per interaction: one event-driven inventory snapshot, bounded background I/O, stable-ID selection, selected-target preview only, indexed SQLite reads, transactional writes, and redraws only after relevant state changes. Avoid per-widget polling, subprocesses per keystroke, speculative caching, and layers that only forward calls.

## 2. Mandatory dedicated TUIOS server

Use the explicitly named TUIOS server **clean-mtm** for the new app's normal operation. Never use the default daemon. The user's existing TUIOS sessions and the new app's sessions must remain separate.

- Every TUIOS command, inventory read, subscription, presence registration, capture, stream, launch, focus, move, close, and shutdown must target this server explicitly. The CLI form is `tuios --server clean-mtm <command>`. An API client must connect to the equivalent named-server endpoint.
- Centralize server selection. Do not let individual operations silently construct a default client. An inherited `TUIOS_SOCKET`, `TUIOS_SERVER`, session, or pane identity from the terminal that launched mtm must not redirect operations to the user's daemon.
- Running `cmtm` without a subcommand starts or reuses the isolated daemon, creates a new UI window in `mtm-only`, and attaches the current terminal. No separate daemon-start/bootstrap commands or environment setup are required. Display the selected server and connection state. Startup failures remain explicit; never fall back to the default daemon or another live server.
- Startup and shutdown must not attach to, restart, kill, move, resize, or clean up the user's existing sessions. Quitting cmtm closes only its current UI window and disconnects its client; other managed windows and the daemon keep running. Detaching leaves the UI window running too.
- Isolate the managed daemon's TUIOS configuration automatically, including host auto-connect configuration. Do not load or modify the user's shared TUIOS host links, and do not change ordinary launched tools' configuration directories merely to isolate TUIOS. Detaching or quitting the UI leaves the daemon and other managed windows running.
- Remote support must preserve the same isolation on every participating host. If the installed remote API cannot target an isolated server on a host, refuse that operation with an explanation instead of using the host's default daemon.
- Run acceptance scenarios on unique servers named `cmtm-t-<six-hex-digits>`, not on the normal `clean-mtm` server. This fits TUIOS's 14-character server-name limit. Tests may stop only their own server and resources. Use finite timeouts and clean up owned processes on failure.
- Launches and delegated CLI commands receive the selected isolated-server context, not stale daemon/session identifiers inherited from the invoking shell.

A server is a daemon boundary. A session is a container inside that server. Do not confuse the dedicated server with the special sessions below.

## 3. Session organization and runtime identity

All project-owned terminals, agents, and other live panes belong in windows inside a TUIOS session named after that project.

Reserve two special sessions inside `clean-mtm`:

- `no-project` contains windows without project ownership. Selecting `no-project` in a project field explicitly chooses no ownership and places the window in this reserved session; it never creates or selects a fake project.
- `mtm-only` contains the Matomeru UI itself, not ordinary project work.

These are session names, not fake projects or tasks. Project ownership is explicit; a working directory or repository name does not establish it. Creating, moving, or reassigning a project window must preserve its project/session relationship. A conflicting destination must be explained before mutation, not silently accepted as an exception. Naming collisions and renames need an explicit policy that does not merge distinct projects or displace existing windows.

Address each runtime object by server, host, session, and stable window identity. Never identify a target by its display name or current list position alone. Identical names on different hosts remain distinct.

Illustrative state, not a prescribed storage schema:

```js
const target = {
	server: "clean-mtm",
	host: "local",
	session: "website",
	windowId: "w-42",
	kind: "agent",
	project: "website"
};
```

Use TUIOS as the authority for terminal processes and lifecycle. Do not implement a second terminal supervisor. Inspect the installed `tuios --skill` and `tuios list-verbs` contract before choosing integration calls.

## 4. Inventory, creation, and lifecycle

Provide a full Panes view with host, session, project, terminal/agent kind, state, activity age, and attention information. Unassigned, disconnected, and stale targets remain discoverable. Provide session and host management through the same list/action system.

Support creating terminals, agents, and Vim windows, inspecting them, entering live interaction, and explicitly closing them. Expose other supported TUIOS management operations through shared actions rather than separate ad hoc interfaces.

- Keep paired quick-start and editable-form actions. Suggested defaults are `n t`, `n a`, `n v` for quick creation, and their `N`-prefixed counterparts for forms.
- Quick and form creation use the same launch values and placement rules. Show host, session, project, directory, and relevant agent options before a form submits.
- Explicit inputs and restored drafts win over defaults. Focused-item context may supply defaults, but focusing a window must not accidentally select it as the destination for new input.
- Keep local and remote directories, suggestions, validation, and launch settings separate. Never apply a local picker result or local cwd to a remote host.
- Agent creation exposes only supported harness, model, effort, and speed options. Distinguish recognized, installed, and usable capabilities. A catalog entry is not proof of account access or compatibility.
- A prompt composer supports multiline text, visible destination/settings, send now, and queue when supported. Enter inserts a newline in prompt text; sending has a separate explicit binding.
- Show pending, accepted, waiting, delivered, blocked, failed, and unknown outcomes accurately. Starting a window is not proof that a prompt reached it. Preserve the target after partial failure so retry does not silently launch another agent.
- Creation must not rearrange, resize, or steal focus from existing windows merely to make room. Report insufficient space or unsupported placement clearly.
- Closing requires confirmation and a successful daemon result. Closing a window does not complete a project, acknowledge unread output, or imply successful agent work.

Separate working agents from ordinary terminals running commands. Keep attention/review, ordinary panes, and working agents easy to find without losing the selected row during lifecycle transitions. Agent activity, transcript, and turns belong to runtime inspection; do not add document review, extracted knowledge, or a separate task workflow.

## 5. Read-only preview and live interaction

Pane detail offers Screen, Activity, Transcript, and Turns when the source supports them. Missing transcript data must not disable a real terminal preview. Show unavailable or unsupported states rather than fabricated content.

A read-only preview shows actual current terminal content at the bottom of the detail area, above help. Normal terminals show bounded recent scrollback plus the screen. Alternate-screen programs show their current layout.

- Preview sends no input, changes no focus, acquires no resize lease, resizes no target, and acknowledges no work.
- Identify server/host/session/window, capture time, truncation, and loading/current/cached/empty/closed/unavailable states. A failed refresh can retain only the same target's output, labeled cached with its age and error.
- Crop to the available area without resizing the target. Shrink metadata before removing the preview. Show an explicit too-small state when necessary.
- Treat terminal output as untrusted. Render supported styling without executing clipboard, title, hyperlink, or other terminal-control commands.
- Bound capture time and size. Refresh only the selected target and reject late replies after selection changes.

Live mode is explicit and visibly different from preview. It forwards input to exactly the selected target. Use a configurable live leader, initially Ctrl+a. Retain detach, next/previous pane, TUIOS zoom/fullscreen, leader help, and doubled-leader literal input. Consume and explain unknown leader combinations rather than sending them accidentally. Bare Escape and Ctrl+z remain target input in live mode.

Only live mode may request a size lease. On switching targets, stop the old stream and release its lease before acquiring the next one. Live sizing uses the measured detail area, not the entire application terminal. Leaving live mode restores shell navigation without leaking keystrokes to a pane.

## 6. Shared shell, navigation, and presentation

Use a slim persistent page switcher and a dedicated full Panes inventory. Do not repeat the entire pane tree on every page. Keep urgent targets reachable through visible attention indicators.

- Every page and subsection retains its own selection, search, filters, sort, grouping, scope, and scroll. Preserve selection by stable identity, not row index.
- An explicitly selected detail tab belongs to the page and remains selected as the user moves between rows. New rows and attention changes must not override it.
- Normal-mode navigation is consistent across lists, trees, forms, modals, help, and completion: `j/k` move, `h/l` change area or fold/unfold, `d/u` move half a visible page, Ctrl+f/Ctrl+b move a page, `g/G` reach the ends, and `{/}` move between sections. Support arrow and paging keys too.
- Text-input mode inserts motion letters as text. Preserve Readline-style editing, Backspace, multiline paste, and Ctrl+n/Ctrl+p suggestion navigation. Do not steal editing keys for global actions.
- Route keys and paste through the same explicit mode/focus rules. Escape follows the active interaction's rules below without accidentally discarding a draft or sending input to a target.
- Keep rows compact. Add a second line only for useful selected detail or an actual live message. Staleness alone does not expand a row.
- Long lists and sectioned panes scroll within their available area. Show overflow and position. Narrow and short terminals must retain usable selection, essential attention, and help rather than clipping controls silently.
- Use readable semantic colors with text/counts, never color alone. Measure terminal-cell widths, align labels and shortcuts, and handle wide characters, combining marks, and wrapped text without broken borders.
- Every managed inventory item supports pinning and starring as distinct actions. A presentation pin must not silently change project ownership, the active session, or list scope.

### Jump and context modal

Provide configurable bindings to open Jump mode directly, open Context mode directly, and open a meta-mode selector listing all available modes, including Jump and Context. Expose effective bindings in help and action discovery; respect text-input and live-mode key routing.

- Jump mode uses a two-pane modal. The left pane outlines the current agent session or terminal; moving the selection updates the right pane with a read-only preview of the selected item's contents. Context mode uses the same layout to browse files, docs, and other available context associated with that agent or terminal. Keep the originating target and its server/host context explicit. Do not invent associations or add automatic knowledge extraction. Show missing or unsupported context honestly.
- Keep content within each pane without wrapping overflow. Crop or scroll overflowing content with visible overflow indicators; do not resize the source terminal. Apply the existing preview safety and stale-response rules.
- Enter on a content item closes the modal and replaces the main screen's content with the selected content, rather than merely updating the preview. Opening terminal content does not implicitly enter live mode.
- Choosing a mode or nested options list replaces the contents of the same modal. Track each replaced level, including its selection, query, filters, scroll, and originating context; show the current nesting path.
- Escape returns exactly one level and restores that level's prior state and context, even with an active query. At the root, Escape closes the modal and restores the underlying view unchanged. Direct Jump and Context entry start at their respective root; entry through the meta-mode selector retains that selector as the parent.

### Project context

- Support saving files, documents, and other context items with an explicit project association. When saving context from an agent or terminal, prefill its current project, but allow choosing another project or `no-project` before submission.
- Keep the context item's identity and source agent/terminal identity separate from its project association. Record the source server, host, session, and window when available. Do not infer ownership from a file path or fabricate missing provenance.
- Context mode supports both current-agent/terminal context and project context. Let the user select a project and browse, search, filter, preview, and open its associated items even after the source window closes.
- Allow explicitly changing or removing a context item's project association. `no-project` means no project association, not a fake project record. Reassigning or closing the source window must not silently move or delete saved context.
- Persist saved context and its project association across application restarts. Saving or reassociating is explicit; highlighting, previewing, and cancelling do not modify associations. This does not require automatic context extraction or a task hierarchy.

### SQLite persistence

Use SQLite for durable application-owned state, including projects, saved context and project associations, extensible field values, pins, stars, and stored settings. TUIOS remains authoritative for live hosts, sessions, windows, and processes.

- Keep the clean-room application's database separate from existing Matomeru databases and TUIOS daemon storage. Tests use their own temporary databases.
- Commit related changes in one transaction, including creating an unmatched value and associating it with the submitted record. Failed or cancelled operations leave no partial records.
- Use versioned schema migrations that preserve existing data. Report database failures without silently replacing the database or falling back to ephemeral storage.

## 7. Non-negotiable search, filter, sort, and grouping

Apply these rules to every inventory list, including newly registered concepts. Search and filtering also work in help, palettes, pickers, and choice lists.

- Search across relevant labels and identifiers with visible matches and matched/total counts.
- Every field whose values form a finite but extensible set is filterable, including tags, concept types, and user-defined statuses. Derive available filters from field definitions and observed values, not a closed list of today's concepts.
- Every list supports appropriate sorting, grouping, and scoping to one or more groups. Make active sort, filters, group order, and scope visible and easy to clear. Filtering absence is not deletion.
- Provide a configurable lowercase letter to cycle grouping configurations, with the uppercase version opening a customization modal. Use `z/Z` as the default pair so `g/G` retain their navigation meaning.
- The customization modal chooses grouping fields, their order, and the groups included in scope. Preserve the user's choice when data refreshes.
- In create/edit/update forms, typing in a user-extensible field fuzzy-filters its existing options. When nonempty entered text has no match, stage it as a new value without requiring a separate vocabulary-management screen.
- Show creation intent before submission. Typing, previewing, cancelling, or a failed submission must not persist staged values. Successful submission creates and uses the new value together. Empty input never creates a value.
- Preserve field validation and authoritative daemon/harness enums. Unmatched input must not invent daemon protocol states or unsupported harness options; implicit creation applies only to user-extensible values.

Example behavior:

```js
const list = {
	query: "api",
	filters: { kind: ["agent"], tags: ["backend"] },
	sort: { field: "lastActivity", direction: "descending" },
	groupBy: ["project", "session"],
	groupScope: ["website"]
};
// Entering a new tag in a form stages it and creates it only on submit.
```

## 8. Shared forms and filesystem selection

Use one form interaction system. Presentation can vary without changing field behavior, completion, validation, or submission semantics. Do not reproduce old renderer internals or carry a legacy layout solely for compatibility.

- Show labels, full focused value, hints, defaults, required/missing state, validation errors, and unsaved changes. A failed submit preserves the draft.
- Normally required fields must allow an explicit no-value choice where absence is valid; project must offer `no-project`. Distinguish this deliberate choice from an unanswered required field, preserve it against defaults and suggestions, and retain all other field and operation validation.
- Support direct field shortcuts, numeric/jump labels, a fuzzy field finder, Tab/Shift+Tab, editable suggestions, option cycling, and labeled choices. Accelerators must not collide with navigation keys.
- Support text, multiline text, numbers, booleans, choices, multi-selection, and linked parent/child fields. Rank exact and prefix matches before weaker fuzzy matches.
- Selecting a child fills its known parent. An explicitly selected parent filters children; changing it clears incompatible children with an explanation. Defaults must not trap the user in the initial scope.
- Preserve manually edited values even while similar suggestions remain. Changing one dependency refreshes only affected fields.
- Bind asynchronous completions, source reads, and picker results to stable field identities and request generations. Stale replies cannot overwrite a newer edit or a different field.
- Every local filesystem-path operation has an actual native OS picker: file picker, directory picker, or save dialog as appropriate, with single/multiple selection as needed. Wire the chosen paths into the operation. Manual entry and completion supplement the picker; they never replace it.
- A local OS picker cannot select remote filesystem paths. Make the target host explicit and do not present local paths as remote selections.

## 9. Actions, palette, help, and settings

Define actions once and reuse them in page bindings, row actions, the command palette, and CLI where applicable. Use consistent verbs such as New, Edit, Delete, Open, and Close. Reject conflicting bindings within the same mode and expose effective remaps everywhere.

The palette supports both verb-first routes such as New → Terminal and object-first routes such as a selected window → its actions. Both execute the same operation. Use nested single-column lists with breadcrumbs, aligned hints, and back navigation that preserves each level's query and highlight.

Open the palette ready to search. With typed text, Escape first leaves search while preserving it; the next Escape closes. With an empty query, Escape closes immediately. Include unbound actions in discovery.

These search-first Escape rules apply only to the command palette. The Jump/Context modal restores its parent level or closes at its root.

Help is generated from effective bindings, current mode, context, and the live leader. It supports search, wraps descriptions, scrolls on short terminals, and restores the underlying view unchanged. Do not maintain a second hard-coded shortcut list.

Settings use shared forms and show stored value versus default. Save only changed settings, support reset-to-default, and apply presentation settings without restarting where possible. Secrets remain with their owning tools or credential stores, not ordinary display settings.

## 10. Refresh, connection state, and user-visible truth

Prefer daemon events and one shared inventory snapshot over independent full polls in every component. Reconnect and resynchronize through the selected isolated server. Retain cached data as stale after disconnect; a failed read is not an empty inventory and must not prune windows or ownership.

Heartbeats, periodic refreshes, and background operations must never reset selection, scroll, filters, grouping, scope, modal state, form input, or keyboard focus. Update only the affected data. Cancel obsolete requests and ignore replies for old page/section/target identities.

Show connecting, disconnected, resynchronizing, current, stale, unknown, pending, failed, and successful states with reasons and last-success information. Agent flags compose: working, blocked, needs-input, unread, queued, completed, and subagents must not hide one another. Preserve unfamiliar source values in detail. Unknown is not zero, ready, or success.

Reading or highlighting a row is not acknowledgement. Any acknowledgement is a deliberate action with a defined clearing rule. Distinguish source disconnection from an idle target, a closed window from an unavailable one, and accepted input from proven delivery.

## 11. Non-negotiable CLI behavior

CLI output is human-readable by default for every subcommand, especially help. JSON output requires an explicit machine-output option. The dedicated JSON viewer is not permission to dump JSON from unrelated commands.

Support third-party subcommands as executable binaries on PATH, like Git's external subcommands:

- `mtm foo` may execute `mtm-foo`.
- When the executable has another name, resolve `<current_bin_name>-<subcommand_name>`.
- Built-in subcommands and registered concepts/units always win. Only an unknown first subcommand word is eligible for external dispatch. Invalid arguments to a known command must not fall through to an external binary.
- These are PATH executables, not plugins or configuration entries. Pass arguments as arguments, preserve standard input/output/error and exit status, and do not evaluate a constructed shell command.
- Preserve the app's explicitly selected isolated-server context when invoking an external subcommand. Never supply the user's inherited default-server context accidentally.

Build `mtm json` into mtm itself. It is an interactive JSON explore/search/filter TUI, not a third-party executable. Reuse shared navigation and search behavior, expose nested object/array structure and value types, and report invalid input clearly. If it asks for a local input file, provide the native file picker required above.

## 12. Acceptance

Prove the actual application behavior, not merely that it compiles. Use uniquely named throwaway TUIOS servers on vps2 with finite deadlines. Never use the user's existing sessions as fixtures. Native macOS dialog behavior requires separate explicit permission and platform verification; Linux results do not prove it.

Use one compact acceptance pass that combines the scenarios below rather than building a large test suite. Exercise real CLI commands, SQLite restart persistence, and the actual TUI on one uniquely named test daemon. Record exactly what ran and any platform-specific limits; do not treat unexercised paths as verified.

Behavioral criteria:

1. Start the app from a shell with unrelated TUIOS environment variables. Observe that every operation targets `clean-mtm`, that default-server sessions remain untouched, and that losing the dedicated server never triggers fallback.
2. Create project-owned windows and explicitly choose `no-project` in create/edit/update forms despite the normally required project field. Verify absent project ownership, placement in the reserved `no-project` session, and no fake project creation. Keep Matomeru in `mtm-only`, and verify placement and identity through rename/reassignment or a clearly explained refusal.
3. Exercise terminal and agent creation, launch failure, partial prompt delivery, confirmed closure, and independent host identities without duplicate launches or accidental mutations.
4. Preview a normal terminal and an alternate-screen editor without changing their focus, input, or dimensions. Switch selection during a delayed capture and prove that old output cannot replace the new target.
5. Enter and leave live mode, switch targets, send a literal leader, and verify stream/lease cleanup and input routing.
6. Search, filter, sort, group, and scope lists. Cycle groups with the lowercase binding and customize with uppercase. In create/edit/update forms, prove that extensible-field input fuzzy-filters existing options, unmatched nonempty text visibly stages a new value, and successful submission creates and uses it. Prove that empty input, typing, previewing, cancellation, and failed submission create nothing, and that authoritative daemon/harness enums and validation remain enforced.
7. Edit a form and scroll a list while events, refreshes, reconnects, and delayed completions occur. Preserve user state and reject stale replies.
8. Exercise forms, help, palette nesting, long text, Unicode, and overflow at narrow, normal, wide, and short terminal sizes. Confirm page-level tab memory and effective remapped keys.
9. Verify human-readable CLI help, built-in/registered-command precedence over same-named PATH executables, external dispatch under a renamed binary, and the built-in JSON explorer.
10. Prove test cleanup affects only the unique test server and its owned resources. Report any unverified platform or remote-isolation behavior explicitly.
11. Exercise direct and remapped Jump, Context, and meta-mode bindings for both an agent and a terminal. Verify the outline, cursor-following previews, available files/docs context, and explicit unavailable states. Confirm overflow stays within the two panes without wrapping or resizing the target. Enter on content replaces the main screen without entering live mode. Traverse the meta-mode selector and nested lists, then use Escape with an active query to restore each prior selection, query, filter, scroll, and context. Root Escape restores the underlying view. Separately verify the command palette's search-first Escape behavior.
12. Save context from a project-owned agent and verify the project prefill, explicit override, and `no-project` option. Browse the saved item from project context after closing its source window and restarting the app. Reassign the source window and confirm saved ownership remains unchanged. Explicitly reassociate or clear the item's project and verify the corresponding project lists update; cancelling or previewing changes nothing.
13. Verify SQLite persistence by saving application state, restarting, and reading it back. Confirm project/context associations survive, failed transactions leave no partial records, and schema migration preserves existing data. Verify the application and test databases do not modify existing Matomeru or TUIOS storage.

## Source boundary

Rewritten from Znbu's TUIOS/pane management and shared TUI requirements, incorporating `docs/features/non-negotiable.md`, `~/notes/mtm-must-haves`, the explicit dedicated-server instruction, the projectless/unmatched-field requirements, project-associated context, and the explicit SQLite requirement. The old product's implementation statuses, backlog identifiers, schema details, and unrelated features are intentionally not part of this clean-room specification.
