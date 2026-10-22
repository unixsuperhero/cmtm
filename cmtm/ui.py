"""Curses shell: one inventory, shared lists/forms, explicit modal input routing."""
from __future__ import annotations

import copy
import curses
import json
import os
import queue
import re
import stat
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import concepts
from .forms import Field, Form, Paste, TextBuffer
from .pickers import pick_path

JSON_NULL = object()
DETAIL_TABS = ('Screen', 'Activity', 'Transcript', 'Turns', 'Metadata')


class IOFailure(str):
    def __new__(cls, error):
        value = super().__new__(cls, str(error))
        value.outcome = getattr(error, 'outcome', 'failed')
        return value
_CONTROL = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[P^_].*?\x1b\\|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]', re.S)


def sanitize(value):
    text = _CONTROL.sub('', str(value))
    return ''.join(c for c in text if c in '\n\t' or unicodedata.category(c) not in ('Cc', 'Cf', 'Cs'))


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def crop(text, width):
    text = sanitize(text).replace('\t', '    ').replace('\n', ' ')
    out, used = [], 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if used + size > max(0, width):
            break
        if used or size:
            out.append(char)
        used += size
    return ''.join(out)


def record_value(record, name):
    return record.get(name, record.get('fields', {}).get(name, ''))


def make_record(identity, label, kind='action', **fields):
    return {'id': identity, 'label': label, 'kind': kind, 'fields': fields, 'project_id': None, 'pinned': False, 'starred': False}


@dataclass
class ListState:
    query: str = ''
    filters: dict = field(default_factory=dict)
    sort: str = 'label'
    descending: bool = False
    group_by: list = field(default_factory=list)
    group_scope: list = field(default_factory=list)
    selected: str | None = None
    index: int = 0
    scroll: int = 0
    tab: str = 'Screen'
    detail_scroll: int = 0
    revision: int = 0
    cache_key: object = None
    cache: list = field(default_factory=list)

    def rows(self, records, revision):
        key = (revision, self.revision, self.query)
        if key != self.cache_key:
            self.cache = concepts.filter_records(records, query=self.query, filters=self.filters, sort=self.sort,
                descending=self.descending, group_by=self.group_by, group_scope=self.group_scope)
            self.cache_key = key
        return self.cache

    def current(self, records):
        if not records:
            return None
        if self.selected:
            for i, record in enumerate(records):
                if record['id'] == self.selected:
                    self.index = i
                    return record
        self.index = min(self.index, len(records) - 1)
        self.selected = records[self.index]['id']
        return records[self.index]

    def move(self, amount, records):
        if records:
            self.current(records)
            self.index = min(len(records) - 1, max(0, self.index + amount))
            self.selected = records[self.index]['id']
            self.detail_scroll = 0


@dataclass
class Modal:
    title: str
    mode: str
    records: list
    origin: dict | None = None
    state: ListState = field(default_factory=ListState)
    searching: bool = False
    search: TextBuffer = field(default_factory=TextBuffer)
    preview_scroll: int = 0
    preview_horizontal: int = 0




class Application:
    PAGES = ('Panes', 'Projects', 'Context', 'Sessions', 'Hosts', 'Settings')

    def __init__(self, screen, services, json_data=None):
        self.screen, self.services = screen, services
        self.store, self.daemon = services.store, services.daemon
        self.settings = dict(services.settings)
        self.json_mode = json_data is not None
        self.json_data = None if json_data is JSON_NULL else json_data
        self.pages = ('JSON',) if self.json_mode else self.PAGES
        self.page = self.pages[0]
        self.states = {page: ListState() for page in self.pages}
        self.inventory, self.persisted = [], []
        self.revision = 0
        self.connection = 'JSON explorer' if self.json_mode else 'connecting'
        self.placement = '' if self.json_mode else 'manager placement: verifying mtm-only'
        self.last_success = None
        self.status = ''
        self.modals = []
        self.form = None
        self.confirm = None
        self.searching = False
        self.search_buffer = TextBuffer()
        self.prefix = ''
        self.opened = None
        self.preview = {}
        self.preview_target = None
        self.preview_generation = 0
        self.preview_requested = 0
        self.inspection = {}
        self.outcomes = {}
        self.prompt_drafts = {}
        self.result_queue = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix='cmtm-io')
        self.pending = set()
        self.stop = threading.Event()
        self.refresh_at = 0
        self.dirty = True
        self.running = True
        self.focus_detail = False
        self.paste = None
        self.live_stream = None
        self.live_target = None
        self.live_transition = False
        self.live_prefix = False
        self.live_buffer = bytearray()
        self.capability_catalog = None
        self.interaction_generation = 0
        self.binding_errors = []
        self.bindings = self.effective_bindings()
        self.json_stack = []
        self.json_root = self.json_records(self.json_data, '$') if self.json_mode else []
        self.reload_store()
        if not self.json_mode:
            self.event_thread = threading.Thread(target=self.events, name='cmtm-events', daemon=True)
            self.event_thread.start()
            self.refresh()

    def effective_bindings(self):
        overrides = self.settings.get('bindings', {})
        if not isinstance(overrides, dict):
            self.binding_errors.append('Stored bindings must be an object; defaults retained')
            overrides = {}
        invalid = [action for action, key in overrides.items() if action not in concepts.ACTIONS or not isinstance(key, str)]
        if invalid:
            self.binding_errors.append('Invalid stored binding entries ignored: ' + ', '.join(map(str, invalid)))
            overrides = {action: key for action, key in overrides.items() if action not in invalid}
        result = {}
        # Catalog actions without UI handlers remain discoverable through native operations.
        for action in concepts.ACTIONS:
            key = overrides.get(action, concepts.ACTIONS[action].get('binding', ''))
            if action == 'ui.group_config' and 'ui.group' in overrides and 'ui.group_config' not in overrides:
                key = overrides['ui.group'].upper()
            if action == 'ui.group' and key and (len(key) != 1 or not key.isalpha() or not key.islower()):
                self.binding_errors.append('Grouping cycle binding must be a lowercase letter')
                continue
            if not key:
                continue
            if any(key.startswith(existing) or existing.startswith(key) for existing in result if key != existing):
                self.binding_errors.append(f'Binding sequence prefix conflict for {key!r}; {action} disabled')
                continue
            if key in result:
                self.binding_errors.append(f'Binding conflict {key!r}: {result[key]} / {action}; later binding disabled')
            elif key in ('j', 'k', 'h', 'l', 'd', 'u', 'g', 'G', '{', '}', 'TAB', 'BTAB', 'ESC', 'UP', 'DOWN', 'LEFT', 'RIGHT', 'HOME', 'END', 'PAGEUP', 'PAGEDOWN', '\x06', '\x02') or key[:1].isdigit():
                self.binding_errors.append(f'Binding {key!r} conflicts with navigation; {action} disabled')
            else:
                result[key] = action
        return result

    def events(self):
        try:
            self.daemon.subscribe(lambda event: self.result_queue.put(('event', event, None)), self.stop)
        except Exception as exc:
            self.result_queue.put(('event_error', None, str(exc)))

    def submit_io(self, name, operation, callback):
        if name in self.pending:
            return False
        self.pending.add(name)
        future = self.executor.submit(operation)
        def complete(done):
            try:
                value, error = done.result(), None
            except Exception as exc:
                value, error = None, IOFailure(exc)
            self.result_queue.put(('result', (name, callback, value), error))
        future.add_done_callback(complete)
        return True

    def reload_store(self):
        try:
            self.persisted = self.store.list()
            self.inventory = self.store.decorate(self.inventory)
            self.revision += 1
            self.dirty = True
        except Exception as exc:
            self.status = f'Database error: {exc}'

    def refresh(self):
        if self.json_mode:
            return
        def received(records, error):
            if error:
                self.connection = f'disconnected; cached inventory stale: {error}'
                for record in self.inventory:
                    record['fields']['freshness'] = 'stale'
                self.revision += 1
            else:
                self.inventory = self.store.decorate(records)
                inherited_window = os.environ.get('TUIOS_WINDOW_ID') or os.environ.get('TUIOS_PANE_ID')
                same_boundary = os.environ.get('TUIOS_SERVER') == self.daemon.server and os.environ.get('TUIOS_SOCKET') == str(getattr(self.daemon, 'socket_path', ''))
                verified = same_boundary and any(record['kind'] == 'window' and record.get('fields', {}).get('window') == inherited_window
                    and record['fields'].get('session') == 'mtm-only' and str(record['fields'].get('pid')) == str(os.getpid()) for record in records)
                self.placement = 'manager placement: verified mtm-only' if verified else 'standalone/unverified; run cmtm to open a managed UI'
                self.last_success = time.time()
                self.connection = 'current'
                self.revision += 1
            self.refresh_at = time.monotonic() + 5
        self.submit_io('snapshot', self.daemon.snapshot, received)

    def drain(self):
        for _ in range(100):
            try:
                kind, payload, error = self.result_queue.get_nowait()
            except queue.Empty:
                break
            self.dirty = True
            if kind == 'event':
                self.refresh_at = min(self.refresh_at, time.monotonic() + .15)
            elif kind == 'event_error':
                self.status = f'Event stream unavailable: {error}; bounded snapshot refresh remains active'
            elif kind == 'result':
                name, callback, value = payload
                self.pending.discard(name)
                try:
                    callback(value, error)
                except Exception as exc:
                    self.status = f'Operation result error: {exc}'
        if not self.json_mode and time.monotonic() >= self.refresh_at:
            self.refresh()
        self.ensure_preview()
        self.pump_live()
        active_file = 'file:' + self.opened['id'] if self.opened else None
        while len(self.preview) > 32:
            key = next(key for key in self.preview if key not in (self.preview_target, active_file))
            del self.preview[key]
        while len(self.inspection) > 64:
            del self.inspection[next(iter(self.inspection))]

    def page_records(self):
        if self.page == 'JSON':
            return self.json_root
        if self.page == 'Settings':
            defaults = self.setting_defaults()
            return [make_record('setting:' + key, key, 'setting', value=self.settings.get(key, default), default=default,
                    stored=key in self.settings) for key, default in defaults.items()]
        kind = {'Panes': 'window', 'Projects': 'project', 'Context': 'context', 'Sessions': 'session', 'Hosts': 'host'}[self.page]
        if kind in ('window', 'session', 'host'):
            records = [r for r in self.inventory if r['kind'] == kind]
            seen = {r['id'] for r in records}
            for record in self.persisted:
                if record['kind'] == kind and record['id'] not in seen:
                    cached = copy.deepcopy(record)
                    cached['fields']['freshness'] = 'stale'
                    if cached['fields'].get('state') != 'closed':
                        cached['fields']['state'] = 'unavailable; runtime identity not in snapshot'
                    records.append(cached)
            return records
        return [r for r in self.persisted if r['kind'] == kind]

    def rows(self):
        if self.modals:
            modal = self.modals[-1]
            return modal.state.rows(modal.records, (self.revision, id(modal.records)))
        return self.states[self.page].rows(self.page_records(), self.revision)

    def state(self):
        return self.modals[-1].state if self.modals else self.states[self.page]

    def selected(self):
        return self.state().current(self.rows())

    def project_name(self, project_id):
        if not project_id:
            return 'no-project'
        project = next((r for r in self.persisted if r['id'] == project_id), None)
        return project['label'] if project else f'unknown project {project_id}'

    def origin(self):
        if self.modals:
            return self.modals[-1].origin
        record = self.opened or self.selected()
        return record if record and record['kind'] in ('window', 'project', 'context') else None

    def target_for_preview(self):
        if self.modals:
            modal = self.modals[-1]
            if modal.mode not in ('jump', 'context', 'project-context'):
                return None
            selected = self.selected()
            return selected.get('fields', {}).get('target') if selected else None
        record = self.opened or self.selected()
        return record if record and record.get('kind') == 'window' else None

    def ensure_preview(self):
        target = self.target_for_preview()
        identity = target['id'] if target else None
        if identity != self.preview_target:
            self.preview_target = identity
            self.preview_generation += 1
            self.preview_requested = 0
        if not target or self.form or self.confirm:
            return
        if target.get('fields', {}).get('state') == 'closed':
            self.preview[identity] = {'text': '', 'state': 'closed', 'time': None}
            return
        now = time.monotonic()
        if now - self.preview_requested < 1.5 or 'capture' in self.pending:
            return
        self.preview_requested = now
        generation = self.preview_generation
        height, width = self.screen.getmaxyx()
        cols, rows = max(10, width // 2 - 3), max(2, height - 12)
        old = self.preview.get(identity)
        if old is None:
            self.preview[identity] = {'text': '', 'state': 'loading', 'time': None}
        def received(value, error):
            if generation != self.preview_generation or identity != self.preview_target:
                return
            if error:
                if old and old.get('text'):
                    self.preview[identity] = dict(old, state='cached', error=error)
                else:
                    self.preview[identity] = {'text': '', 'state': 'unavailable', 'error': error, 'time': None}
            else:
                if isinstance(value, dict):
                    text = value.get('content', value.get('text', value.get('screen', '')))
                    metadata = value
                else:
                    text, metadata = value, {}
                self.preview[identity] = {'text': sanitize(text), 'state': 'current' if text else 'empty',
                    'time': time.time(), 'metadata': metadata}
            self.dirty = True
        operation = lambda: self.daemon.capture_info(target, cols=cols, rows=rows) if hasattr(self.daemon, 'capture_info') else self.daemon.capture(target, cols=cols, rows=rows)
        self.submit_io('capture', operation, received)
        self.ensure_inspection(target)

    def ensure_inspection(self, target):
        tab = self.states[self.page].tab.lower()
        if self.modals:
            selected = self.selected()
            tab = selected.get('fields', {}).get('tab', 'screen') if selected else 'screen'
        if tab == 'screen':
            return
        key = (target['id'], tab)
        if tab == 'metadata':
            self.inspection[key] = {'text': json.dumps(target, ensure_ascii=False, indent=2), 'at': time.monotonic()}
            return
        existing = self.inspection.get(key)
        if existing and time.monotonic() - existing['at'] < 5:
            return
        if not hasattr(self.daemon, 'inspect'):
            self.inspection[key] = {'text': f'{tab.title()} unsupported by this daemon adapter', 'at': time.monotonic()}
            return
        def received(value, error):
            self.inspection[key] = {'text': error or json.dumps(value, ensure_ascii=False, indent=2), 'at': time.monotonic()}
        self.submit_io('inspect', lambda: self.daemon.inspect(target, tab=tab), received)

    def put(self, y, x, text, width=None, attr=0):
        h, w = self.screen.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        text = crop(text, max(0, min(w - x - (1 if y == h - 1 else 0), width if width is not None else w - x)))
        if self.settings.get('color', True) and curses.has_colors():
            if y == 0:
                attr |= curses.color_pair(1)
            elif y == 1:
                attr |= curses.color_pair(4 if self.connection == 'current' else 2)
            elif y == h - 3 and text:
                attr |= curses.color_pair(3 if any(word in text.lower() for word in ('failed', 'error', 'refused', 'unavailable')) else 2)
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            pass

    def lines(self, text, y, x, height, width, offset=0, horizontal=0):
        source = sanitize(text).expandtabs(4).splitlines() or ['(empty)']
        offset = min(max(0, offset), max(0, len(source) - max(1, height)))
        visible = source[offset:offset + max(0, height)]
        for i, line in enumerate(visible):
            rendered = line[horizontal:]
            overflow = cell_width(rendered) > width
            self.put(y + i, x, crop(rendered, max(0, width - 1)) + ('>' if overflow else ''), width)
        if len(source) > offset + height and height > 0:
            self.put(y + height - 1, x + max(0, width - 18), f'v {offset + height}/{len(source)}', min(width, 18), curses.A_DIM)

    def render(self):
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        if h < 6 or w < 24:
            self.put(0, 0, 'Terminal too small (24x6 minimum)')
            self.put(1, 0, f'{self.page} | {self.connection}')
            self.put(h - 1, 0, 'Resize; q quits; ? help')
            self.screen.refresh()
            return
        page_text = ' '.join(f'[{i + 1}:{page}]' if page == self.page else f'{i + 1}:{page}' for i, page in enumerate(self.pages))
        self.put(0, 0, 'cmtm | ' + page_text, w, curses.A_BOLD)
        attention = sum(bool(r.get('fields', {}).get('unread') or r.get('fields', {}).get('needs_input') or r.get('fields', {}).get('blocked')) for r in self.inventory)
        age = '' if self.last_success is None else f' last OK {int(time.time() - self.last_success)}s ago'
        self.put(1, 0, f'{self.daemon.server} | {self.connection}{age} | attention {attention}', w, curses.A_DIM)
        self.render_main(2, h - 5, w)
        if self.modals:
            self.render_modal(2, h - 5, w)
        if self.form:
            self.render_form(2, h - 5, w)
        if self.confirm:
            self.put(h // 2 - 1, 1, ' ' * (w - 2), w - 2, curses.A_REVERSE)
            self.put(h // 2, 1, self.confirm[0], w - 2, curses.A_REVERSE)
            self.put(h // 2 + 1, 1, 'y / Enter: confirm    n / Escape: cancel', w - 2, curses.A_REVERSE)
        message = self.status or ('; '.join(self.binding_errors))
        self.put(h - 3, 0, message, w, curses.A_BOLD)
        if self.form:
            help_line = 'Ctrl+s submit | Esc edit/back | Tab fields | F2 find | F4 OS picker | Ctrl+n/p choices, Ctrl+y accept'
        elif self.searching or self.modals and self.modals[-1].searching:
            help_line = 'Search: type text | Enter browse | Esc back (palette: leave search first)'
        else:
            help_line = self.help_footer()
        self.put(h - 2, 0, help_line, w, curses.A_DIM)
        self.put(h - 1, 0, self.input_status(), w)
        self.screen.refresh()

    def help_footer(self):
        wanted = ('ui.help', 'ui.palette', 'ui.jump', 'ui.context', 'ui.group', 'ui.group_config', 'ui.live')
        reverse = {action: key for key, action in self.bindings.items()}
        return 'j/k move | h/l area | ' + ' | '.join(f'{reverse[a]} {concepts.ACTIONS[a]["label"]}' for a in wanted if a in reverse)

    def input_status(self):
        if self.live_target or self.live_transition:
            return 'LIVE — input forwards only to the explicitly attached target' + (' | leader pending' if self.live_prefix else '')
        if self.form:
            return ('EDIT' if self.form.editing else 'FORM') + (' * unsaved' if self.form.dirty else '') + ' | ' + self.form.message
        if self.modals:
            modal = self.modals[-1]
            return f'{modal.mode.upper()} /{modal.state.query} | Esc restores one level | {len(self.rows())}/{len(modal.records)}'
        state = self.states[self.page]
        return f'{"SEARCH" if self.searching else "NORMAL"} /{state.query} | {self.prefix} {self.placement}'

    def list_summary(self, state, count, total):
        return f'{count}/{total} sort:{state.sort}{" desc" if state.descending else ""} group:{",".join(state.group_by) or "none"} scope:{",".join(map(str, state.group_scope)) or "all"} filters:{state.filters or "none"}'

    def render_list(self, rows, state, y, x, height, width):
        state.current(rows)
        if not rows:
            self.put(y, x, 'No matching items (clear filters with \\)', width, curses.A_DIM)
            return
        if state.index < state.scroll:
            state.scroll = state.index
        elif state.index >= state.scroll + height:
            state.scroll = state.index - height + 1
        state.scroll = max(0, state.scroll)
        for line, record in enumerate(rows[state.scroll:state.scroll + height]):
            selected = record['id'] == state.selected
            flags = ('P' if record.get('pinned') else '-') + ('*' if record.get('starred') else '-')
            fields = record.get('fields', {})
            flags += '!' if any(fields.get(name) for name in ('blocked', 'needs_input', 'unread')) else ' '
            flags += 'W' if fields.get('working') else ' '
            if record['kind'] == 'window':
                detail = f' {fields.get("host", "?")}/{fields.get("session", "?")} {fields.get("pane_kind", "unknown")} {fields.get("state", "unknown")}'
                attention = [name for name in ('working', 'blocked', 'needs_input', 'unread', 'queued', 'completed', 'subagents') if fields.get(name)]
                detail += (' !' + ','.join(attention)) if attention else ''
                if fields.get('activity_age_ms') is not None:
                    detail += f' {int(fields["activity_age_ms"]) // 1000}s'
            else:
                detail = ' ' + str(fields.get('hint', fields.get('state', fields.get('type', ''))))
            grouping = ' / '.join(str(record_value(record, name)) for name in state.group_by)
            label = f'{flags} {record["label"]}{detail}'
            if grouping:
                label = f'{grouping} | {label}'
            attr = curses.A_REVERSE if selected else 0
            if selected and self.focus_detail:
                attr = curses.A_BOLD
            self.put(y + line, x, label, width, attr)
            if state.query:
                position = 0
                for character in state.query.casefold():
                    found = label.casefold().find(character, position)
                    if found < 0:
                        break
                    column = cell_width(label[:found])
                    if column < width:
                        self.put(y + line, x + column, label[found:found + 1], width - column, attr | curses.A_BOLD | curses.A_UNDERLINE)
                    position = found + 1
        self.put(y + height - 1, x + max(0, width - 12), f'{state.index + 1}/{len(rows)}', min(12, width), curses.A_DIM)

    def render_main(self, y, height, width):
        state = self.states[self.page]
        records = self.page_records()
        rows = state.rows(records, self.revision)
        selected = state.current(rows)
        self.put(y, 0, self.list_summary(state, len(rows), len(records)), width, curses.A_DIM)
        left = max(20, min(width // 2, 56))
        if width < 60:
            if not self.focus_detail and not self.opened:
                self.render_list(rows, state, y + 1, 0, max(1, height - 1), width)
                return
            left = 0
        else:
            self.render_list(rows, state, y + 1, 0, max(1, height - 1), left - 1)
            for line in range(y + 1, y + height):
                self.put(line, left - 1, '|', 1, curses.A_DIM)
        self.render_detail(self.opened or selected, state, y + 1, left, max(1, height - 1), width - left)

    def render_detail(self, record, state, y, x, height, width):
        if not record:
            self.lines('No selection.\nUse New actions or start the isolated server.\nDisconnected inventory is retained, never treated as deletion.', y, x, height, width)
            return
        fields = record.get('fields', {})
        self.put(y, x, f'{record["label"]} [{record["kind"]}]', width, curses.A_BOLD)
        if record['kind'] == 'window':
            self.put(y + 1, x, ' | '.join(f'[{tab}]' if state.tab == tab else tab for tab in DETAIL_TABS), width)
            self.put(y + 2, x, f'{fields.get("server", self.daemon.server)}/{fields.get("host", "?")}/{fields.get("session", "?")}/{fields.get("window", record["id"])}', width, curses.A_DIM)
            self.put(y + 3, x, f'project:{self.project_name(record.get("project_id"))} state:{fields.get("state", "unknown")} {self.outcomes.get(record["id"], "")}', width)
            if state.tab != 'Screen':
                entry = self.inspection.get((record['id'], state.tab.lower()), {})
                self.lines(entry.get('text', 'Loading supported source…'), y + 4, x, max(1, height // 2 - 3), width, state.detail_scroll)
                preview_y = y + max(5, height // 2 + 1)
            else:
                preview_y = y + min(5, max(2, height - 3))
            self.render_capture(record, preview_y, x, y + height - preview_y, width, state.detail_scroll if state.tab == 'Screen' else 0)
        else:
            text = self.content_text(record)
            self.lines(text, y + 1, x, height - 1, width, state.detail_scroll)

    def render_capture(self, target, y, x, height, width, offset=0):
        if height < 2 or width < 12:
            self.put(y, x, 'Preview too small', width)
            return
        entry = self.preview.get(target['id'], {'state': 'loading'})
        age = '' if not entry.get('time') else f' {int(time.time() - entry["time"])}s ago'
        metadata = entry.get('metadata', {})
        truncation = ' truncated' if metadata.get('truncated') else ''
        label = 'LIVE screen' if self.live_target and self.live_target['id'] == target['id'] else 'Read-only screen'
        self.put(y, x, f'{label}: {entry["state"]}{age}{truncation} {entry.get("error", "")}', width, curses.A_DIM)
        text = entry.get('text') or '(no terminal content available)'
        start = max(0, len(text.splitlines()) - (height - 1) - offset)
        self.lines(text, y + 1, x, height - 1, width, start)

    def content_text(self, record):
        fields = record.get('fields', {})
        if record['kind'] == 'context':
            source = fields.get('source', {k: v for k, v in fields.items() if k.startswith('source_')})
            header = f'Project: {self.project_name(record.get("project_id"))}\nSource: {json.dumps(source, ensure_ascii=False)}\n'
            if fields.get('path'):
                cache_key = 'file:' + record['id']
                cached = self.preview.get(cache_key)
                if cached is None:
                    self.preview[cache_key] = {'text': 'Loading saved file…'}
                    self.load_context_file(record)
                return header + f'Path: {fields["path"]}\n' + self.preview[cache_key]['text']
            return header + str(fields.get('text') or fields.get('url') or '(No stored text, path or URL)')
        if record['kind'] == 'json':
            return f'Path: {fields["path"]}\nType: {fields["type"]}\n' + json.dumps(fields['value'], ensure_ascii=False, indent=2)
        if record['kind'] in ('action', 'notice'):
            return record['label'] + '\n' + str(fields.get('reason', fields.get('hint', 'Enter opens this nested list')))
        return json.dumps(record, ensure_ascii=False, indent=2, default=str)

    def load_context_file(self, record):
        fields = record['fields']
        host = fields.get('host', 'local')
        def operation():
            if host not in ('local', 'localhost', ''):
                raise ValueError(f'Remote file preview unavailable for {host}; local filesystem is not substituted')
            descriptor = os.open(os.path.expanduser(fields['path']), os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('Context preview accepts regular files, not devices, pipes or directories')
                raw = stream.read(262145)
            return raw[:262144].decode('utf-8', 'replace') + ('\n[truncated at 256 KiB]' if len(raw) > 262144 else '')
        def received(value, error):
            current = next((item for item in self.persisted if item['id'] == record['id']), None)
            if not current or current.get('fields', {}).get('path') != fields.get('path') or current.get('fields', {}).get('host', 'local') != host:
                self.preview.pop('file:' + record['id'], None)
                return
            self.preview['file:' + record['id']] = {'text': sanitize(value) if not error else f'Unavailable: {error}'}
        self.submit_io('file:' + record['id'], operation, received)

    def render_modal(self, y, height, width):
        modal = self.modals[-1]
        for row in range(y, y + height):
            self.put(row, 0, ' ' * width, width)
        self.put(y, 0, ' > '.join(m.title for m in self.modals), width, curses.A_BOLD)
        rows = self.rows()
        selected = modal.state.current(rows)
        origin = modal.origin
        identity = ''
        if origin:
            f = origin.get('fields', {})
            identity = f' origin:{f.get("server", self.daemon.server)}/{f.get("host", "local")}/{f.get("session", "-")}/{f.get("window", origin["id"])}'
        self.put(y + 1, 0, self.list_summary(modal.state, len(rows), len(modal.records)) + identity, width, curses.A_DIM)
        two_panes = modal.mode in ('jump', 'context', 'project-context')
        left = max(12, width // 2) if two_panes else width
        list_height = max(1, height // 2) if modal.mode == 'help' else max(1, height - 2)
        self.render_list(rows, modal.state, y + 2, 0, list_height, left - (1 if two_panes else 0))
        if modal.mode == 'help' and selected:
            description = selected['fields'].get('description', '')
            wrapped, line = [], ''
            for word in sanitize(description).split():
                if line and cell_width(line + ' ' + word) > width:
                    wrapped.append(line)
                    line = ''
                while cell_width(word) > width:
                    segment = crop(word, width)
                    wrapped.append(segment)
                    word = word[len(segment):]
                line = (line + ' ' + word).strip()
            if line:
                wrapped.append(line)
            self.lines('\n'.join(wrapped), y + 2 + list_height, 0, max(1, height - list_height - 2), width, modal.preview_scroll)
        if two_panes:
            for line in range(y + 2, y + height):
                self.put(line, left - 1, '|', 1, curses.A_DIM)
            if selected:
                fields = selected.get('fields', {})
                target = fields.get('target')
                if target:
                    tab = fields.get('tab', 'screen')
                    if tab == 'screen':
                        self.render_capture(target, y + 2, left, height - 2, width - left, modal.preview_scroll)
                    else:
                        text = self.inspection.get((target['id'], tab), {}).get('text', 'Loading supported source…')
                        self.lines(text, y + 2, left, height - 2, width - left, modal.preview_scroll, modal.preview_horizontal)
                else:
                    self.lines(self.content_text(selected), y + 2, left, height - 2, width - left, modal.preview_scroll, modal.preview_horizontal)

    def render_form(self, y, height, width):
        form = self.form
        for row in range(y, y + height):
            self.put(row, 0, ' ' * width, width)
        self.put(y, 0, form.title + (' * unsaved' if form.dirty else '') + (' [pending]' if form.busy else ''), width, curses.A_BOLD)
        field_height = max(1, min(len(form.fields), height // 2))
        if form.index < form.scroll:
            form.scroll = form.index
        elif form.index >= form.scroll + field_height:
            form.scroll = form.index - field_height + 1
        for i, item in enumerate(form.fields[form.scroll:form.scroll + field_height]):
            index = i + form.scroll
            value = form.buffers[item.name].text.replace('\n', ' ↵ ')
            error = form.errors.get(item.name, '')
            label = f'{index + 1}. {item.label}{" *" if item.required else ""}: {value}'
            if error:
                label += f' ! {error}'
            self.put(y + 1 + i, 0, label, width, curses.A_REVERSE if index == form.index else 0)
        detail_y = y + 1 + field_height
        item = form.current
        value = form.buffers[item.name]
        self.put(detail_y, 0, f'{item.label} | default:{item.default!s} | {item.hint}', width, curses.A_DIM)
        room = max(1, y + height - detail_y - 4)
        # Display the cursor neighborhood, keeping long values fully reachable.
        before = value.text[:value.cursor]
        line = before.count('\n')
        self.lines(value.text[:value.cursor] + '▏' + value.text[value.cursor:], detail_y + 1, 0, room, width,
            max(0, line - room + 1), max(0, len(before.rsplit('\n', 1)[-1]) - width + 4))
        options = form.options()
        if options:
            hint = 'Choices: ' + ' | '.join(('[' + option + ']') if i == form.suggestion else option for i, option in enumerate(options[:12]))
            self.put(y + height - 3, 0, hint, width)
        staged = form.staged()
        if staged:
            self.put(y + height - 2, 0, 'Create on successful submit: ' + '; '.join(staged), width, curses.A_BOLD)
        elif form.finding:
            self.put(y + height - 2, 0, 'Find field: ' + form.finder.text, width)
        self.put(y + height - 1, 0, f'Ctrl+s: {form.submit_label} | {form.index + 1}/{len(form.fields)} | Esc preserves draft until discard confirmed', width, curses.A_DIM)

    @staticmethod
    def json_records(value, path):
        if isinstance(value, dict):
            entries = value.items()
        elif isinstance(value, list):
            entries = enumerate(value)
        else:
            entries = [(path, value)]
        result = []
        for key, child in entries:
            child_path = f'{path}[{json.dumps(key, ensure_ascii=False)}]'
            kind = 'null' if child is None else 'boolean' if isinstance(child, bool) else 'object' if isinstance(child, dict) else 'array' if isinstance(child, list) else 'number' if isinstance(child, (int, float)) else 'string'
            hint = f'{kind} ({len(child)})' if isinstance(child, (dict, list)) else f'{kind}: {child}'
            result.append(make_record(child_path, str(key), 'json', path=child_path, value=child, type=kind, hint=hint))
        return result

    def close(self):
        self.stop.set()
        if self.live_stream:
            self.live_stream.close()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def run(self):
        self.screen.keypad(True)
        self.screen.timeout(100)
        curses.raw()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            for number, foreground in ((1, curses.COLOR_CYAN), (2, curses.COLOR_YELLOW), (3, curses.COLOR_RED), (4, curses.COLOR_GREEN)):
                try:
                    curses.init_pair(number, foreground, -1)
                except curses.error:
                    curses.init_pair(number, foreground, curses.COLOR_BLACK)
        sys.stdout.write('\x1b[?2004h')
        sys.stdout.flush()
        try:
            while self.running:
                self.drain()
                if self.dirty:
                    self.render()
                    self.dirty = False
                key = self.read_key()
                if key is not None:
                    try:
                        self.handle(key)
                    except Exception as exc:
                        self.status = f'Error: {exc}'
                    self.dirty = True
        finally:
            sys.stdout.write('\x1b[?2004l')
            sys.stdout.flush()
            self.close()

    def read_key(self):
        try:
            key = self.screen.get_wch()
        except curses.error:
            return None
        if self.paste is not None and isinstance(key, str):
            self.paste += key
            if self.paste.endswith('\x1b[201~'):
                value = self.paste[:-6]
                self.paste = None
                return Paste(value)
            if len(self.paste) > 1048576:
                self.paste = None
                self.status = 'Paste rejected: exceeds 1 MiB'
            return None
        names = {curses.KEY_UP: 'UP', curses.KEY_DOWN: 'DOWN', curses.KEY_LEFT: 'LEFT', curses.KEY_RIGHT: 'RIGHT',
            curses.KEY_HOME: 'HOME', curses.KEY_END: 'END', curses.KEY_NPAGE: 'PAGEDOWN', curses.KEY_PPAGE: 'PAGEUP',
            curses.KEY_BACKSPACE: 'BACKSPACE', curses.KEY_DC: 'DELETE', curses.KEY_BTAB: 'BTAB', curses.KEY_RESIZE: 'RESIZE',
            curses.KEY_ENTER: 'ENTER', curses.KEY_F2: 'F2', curses.KEY_F4: 'F4'}
        names[curses.KEY_IC] = 'INSERT'
        names.update({getattr(curses, f'KEY_F{number}'): f'F{number}' for number in range(1, 13)})
        if isinstance(key, int):
            return names.get(key, f'KEY:{key}')
        if key == '\x1b':
            self.screen.timeout(15)
            sequence = ''
            for _ in range(5):
                try:
                    char = self.screen.get_wch()
                except curses.error:
                    break
                if isinstance(char, str):
                    sequence += char
                else:
                    curses.unget_wch(char)
                    break
            self.screen.timeout(100)
            if sequence == '[200~':
                self.paste = ''
                return None
            for char in reversed(sequence):
                curses.unget_wch(char)
            return 'ESC'
        return {'\n': 'ENTER', '\r': 'ENTER', '\t': 'TAB', '\x7f': 'BACKSPACE'}.get(key, key)

    def handle(self, key):
        self.interaction_generation += 1
        if self.live_target or self.live_transition:
            self.handle_live(key)
            return
        if key == 'RESIZE':
            return
        if self.confirm:
            if key in ('y', 'ENTER'):
                callback = self.confirm[1]
                self.confirm = None
                callback()
            elif key in ('n', 'ESC'):
                self.confirm = None
            return
        if self.form:
            form = self.form
            old_project = form.buffers.get('project', TextBuffer()).text
            old_host = form.buffers.get('host', TextBuffer('local')).text
            result = form.handle(key)
            if isinstance(form.source, dict) and 'filter_options' in form.source:
                field_name = form.buffers['field'].text
                form.field('values').choices = [str(value) for value in form.source['filter_options'].get(field_name, [])]
            if 'session' in form.buffers and 'project' in form.buffers and form.buffers['project'].text != old_project:
                if form.buffers['session'].text == old_project:
                    form.set('session', form.buffers['project'].text)
                else:
                    form.message = 'Project changed; manually edited Session preserved and must match before launch'
            if 'host' in form.buffers and form.buffers['host'].text != old_host:
                for path_field in ('cwd', 'path'):
                    if path_field in form.buffers:
                        form.set(path_field, '')
                form.message = 'Filesystem host changed; old path cleared rather than reused across hosts'
            if result:
                action, value = result
                if action == 'submit':
                    try:
                        form.submit(value)
                    except Exception as exc:
                        form.message = str(exc)
                        form.busy = False
                elif action == 'cancel':
                    if form.dirty:
                        self.confirm = ('Discard this unsaved draft?', lambda: setattr(self, 'form', None))
                    else:
                        self.form = None
                elif action == 'picker':
                    self.form_picker(form, value)
            return
        if self.modals:
            self.handle_modal(key)
            return
        if self.searching:
            if key in ('ESC', 'ENTER'):
                self.searching = False
            else:
                self.search_buffer.handle(key)
                self.states[self.page].query = self.search_buffer.text
            return
        if isinstance(key, Paste):
            self.status = 'Paste ignored outside text input; use / search or edit a form'
            return
        if self.prefix:
            combination = self.prefix + key
            self.prefix = ''
            if combination in self.bindings:
                self.action(self.bindings[combination])
            else:
                self.status = f'No binding for {combination!r}'
            return
        if key in self.bindings:
            self.action(self.bindings[key])
        elif any(binding.startswith(key) and len(binding) > len(key) for binding in self.bindings):
            self.prefix = key
        elif key in ('TAB', 'BTAB'):
            tabs = DETAIL_TABS
            state = self.states[self.page]
            state.tab = tabs[(tabs.index(state.tab) + (1 if key == 'TAB' else -1)) % len(tabs)]
            self.preview_requested = 0
        elif key.isdigit() and 0 < int(key) <= len(self.pages):
            self.page = self.pages[int(key) - 1]
            self.opened = None
        elif key in ('{', '}'):
            self.page = self.pages[(self.pages.index(self.page) + (1 if key == '}' else -1)) % len(self.pages)]
            self.opened = None
        elif key == 'ESC':
            if self.opened:
                self.opened = None
            elif self.json_stack:
                self.json_root, self.states['JSON'] = self.json_stack.pop()
                self.revision += 1
            self.focus_detail = False
        elif key in ('h', 'LEFT'):
            self.focus_detail = False
        elif key in ('l', 'RIGHT'):
            self.focus_detail = True
        else:
            if self.navigate(key, self.state(), self.rows(), self.focus_detail) and not self.focus_detail:
                self.opened = None

    def navigate(self, key, state, rows, detail=False):
        height = max(1, self.screen.getmaxyx()[0] - 9)
        moves = {'j': 1, 'DOWN': 1, 'k': -1, 'UP': -1, 'd': max(1, height // 2), 'u': -max(1, height // 2),
            '\x06': height, 'PAGEDOWN': height, '\x02': -height, 'PAGEUP': -height,
            'g': -1000000, 'HOME': -1000000, 'G': 1000000, 'END': 1000000}
        if key in moves:
            if detail:
                state.detail_scroll = max(0, state.detail_scroll + moves[key])
            else:
                state.move(moves[key], rows)
            return True
        return False

    def action(self, action):
        record = self.opened or self.selected()
        if action == 'ui.quit':
            self.running = False
        elif action == 'ui.palette':
            self.palette()
        elif action == 'ui.help':
            self.help()
        elif action in ('ui.jump', 'ui.context', 'ui.modes'):
            self.open_mode(action.split('.')[-1])
        elif action == 'ui.search':
            self.searching = True
            self.search_buffer = TextBuffer(self.state().query)
        elif action in ('ui.filter', 'ui.sort', 'ui.group_config'):
            self.list_controls(action.split('.')[-1])
        elif action == 'ui.group':
            self.cycle_groups()
        elif action == 'ui.clear':
            state = self.state()
            state.query, state.filters, state.group_by, state.group_scope = '', {}, [], []
            state.revision += 1
        elif action in ('ui.pin', 'ui.star'):
            if record:
                flag = 'pinned' if action == 'ui.pin' else 'starred'
                self.store.set_flag(record['id'], flag, not record.get(flag))
                self.reload_store()
        elif action == 'ui.open':
            self.open_record(record)
        elif action == 'ui.live':
            self.live(record)
        elif action == 'ui.edit':
            self.edit_record(record)
        elif action == 'ui.delete':
            self.delete_record(record)
        elif action in ('ui.save_context', 'ui.new_context'):
            self.context_form(source=record if record and record['kind'] in ('window', 'project') else None)
        elif action in ('ui.send', 'ui.queue'):
            self.composer(record, queue_prompt=action == 'ui.queue')
        elif action == 'ui.start':
            self.operation('start server', self.daemon.start)
        elif action == 'ui.refresh':
            self.refresh_at = 0
        elif action == 'ui.settings':
            self.settings_form()
        elif action == 'ui.new_project':
            self.project_form()
        elif action == 'ui.new_session':
            self.session_form()
        elif action == 'ui.operations':
            self.operations()
        elif action.startswith('ui.new_') or action.startswith('ui.form_'):
            kind = action.rsplit('_', 1)[-1]
            self.launch_form(kind, quick=action.startswith('ui.new_'))
        else:
            metadata = concepts.ACTIONS.get(action, {})
            self.status = metadata.get('description', f'No UI handler for {action}')

    def operation(self, name, callback, success=None, form=None, failure=None):
        if 'operation' in self.pending:
            raise ValueError('Another mutation is pending; wait for its result before submitting')
        if form:
            form.busy = True
            form.message = f'{name}: pending'
        self.status = f'{name}: pending'
        def received(value, error):
            if form:
                form.busy = False
            if error:
                self.status = f'{name}: {getattr(error, "outcome", "failed")} — {error}'
                if form:
                    form.message = self.status
                if failure:
                    failure(error)
                return
            try:
                if success:
                    success(value)
                self.status = f'{name}: accepted' + (f' — {value}' if value and not isinstance(value, dict) else '')
                if form and self.form is form:
                    self.form = None
                self.refresh_at = 0
            except Exception as exc:
                self.status = f'{name}: daemon accepted but local persistence failed: {exc}; do not repeat launch'
                if form:
                    form.message = self.status
        self.submit_io('operation', callback, received)

    def form_picker(self, form, item):
        if not item.picker:
            form.message = f'{item.label} is not a filesystem-path field'
            return
        host = form.buffers.get('host', TextBuffer('local')).text or 'local'
        generation = form.generation[item.name]
        host_generation = form.generation.get('host', 0)
        value = form.buffers[item.name].text
        def received(result, error):
            if self.form is not form or form.generation[item.name] != generation or form.generation.get('host', 0) != host_generation:
                return
            if error:
                form.message = error
            elif result is not None:
                form.set(item.name, result if not isinstance(result, list) else '\n'.join(result))
        self.submit_io('picker', lambda: pick_path(item.picker, item.multiple, value, item.label, host), received)

    def project_choices(self):
        return ['no-project'] + [r['label'] for r in self.persisted if r['kind'] == 'project']

    def project_field(self, value='no-project'):
        return Field('project', 'Project', 'choice', value=value, default='no-project', required=True,
            choices=self.project_choices(), extensible=True, hint='no-project explicitly chooses no ownership; unmatched name stages a project')

    def context_form(self, record=None, source=None):
        fields = record.get('fields', {}) if record else {}
        project_id = record.get('project_id') if record else source.get('project_id') if source else None
        if not record and source and source['kind'] == 'project':
            project_id = source['id']
        source_identity = fields.get('source')
        if source and source['kind'] == 'window':
            sf = source['fields']
            source_identity = {key: sf.get(key) for key in ('server', 'host', 'session', 'window') if sf.get(key) is not None}
            source_identity['id'] = source['id']
        items = [Field('label', 'Name', value=record['label'] if record else '', required=True), self.project_field(self.project_name(project_id)),
            Field('type', 'Context type', 'choice', fields.get('type', 'document'), choices=['document', 'file', 'url'] + self.store.vocabulary('type', 'context'), extensible=True),
            Field('host', 'Filesystem host', 'choice', fields.get('host', source.get('fields', {}).get('host', 'local') if source else 'local'), choices=sorted({'local'} | {r['fields'].get('host', 'local') for r in self.inventory})),
            Field('path', 'File path', value=fields.get('path', ''), picker='file', hint='F4 native local file picker; remote files are never read locally'),
            Field('text', 'Document text', 'multiline', fields.get('text', '')),
            Field('url', 'URL', value=fields.get('url', '')),
            Field('tags', 'Tags', 'multi', fields.get('tags', []), choices=self.store.vocabulary('tags', 'context'), extensible=True),
            Field('status', 'Status', 'choice', fields.get('status', ''), choices=self.store.vocabulary('status', 'context'), extensible=True)]
        def submit(values):
            if not any(values[key] for key in ('path', 'text', 'url')):
                raise ValueError('Provide a file path, document text or URL')
            data = dict(fields)
            data.update({key: value for key, value in values.items() if key not in ('label', 'project')})
            if source_identity is not None:
                data['source'] = source_identity
            saved = self.store.save_with_project('context', values['label'], data, project=values['project'], id=record['id'] if record else None)
            self.preview.pop('file:' + saved['id'], None)
            self.form = None
            self.reload_store()
            self.status = 'Context saved; source identity and project association remain independent'
        self.form = Form('Edit Context' if record else 'Save Context', items, submit)

    def project_form(self, record=None):
        fields = record.get('fields', {}) if record else {}
        items = [Field('label', 'Name', value=record['label'] if record else '', required=True),
            Field('directory', 'Default local directory', value=fields.get('directory', ''), picker='directory'),
            Field('tags', 'Tags', 'multi', fields.get('tags', []), choices=self.store.vocabulary('tags', 'project'), extensible=True),
            Field('status', 'Status', 'choice', fields.get('status', ''), choices=self.store.vocabulary('status', 'project'), extensible=True)]
        def submit(values):
            self.store.save('project', values['label'], {k: v for k, v in values.items() if k != 'label'}, id=record['id'] if record else None)
            self.form = None
            self.reload_store()
        self.form = Form('Edit Project' if record else 'New Project', items, submit)

    def launch_defaults(self):
        record = self.selected()
        project = self.project_name(record.get('project_id')) if record else 'no-project'
        if record and record['kind'] == 'project':
            project = record['label']
        host = record.get('fields', {}).get('host', 'local') if record else 'local'
        cwd = ''
        if record and record['kind'] == 'project' and host == 'local':
            cwd = record['fields'].get('directory', '')
        elif record and record['kind'] == 'window':
            cwd = record['fields'].get('cwd', '')
        if not cwd and host == 'local':
            cwd = self.settings.get('launch_directory', '')
        return {'project': project, 'host': host, 'cwd': cwd, 'name': '', 'harness': self.settings.get('agent_harness', ''), 'prompt': ''}

    def launch_form(self, kind, quick=False):
        if kind == 'agent' and self.capability_catalog is None:
            self.status = 'Discovering recognized and installed agent harnesses…'
            generation = self.interaction_generation
            def received(value, error):
                if error:
                    self.status = f'Agent capabilities unavailable: {error}'
                    return
                self.capability_catalog = value
                if generation != self.interaction_generation:
                    self.status = 'Agent capabilities loaded; previous launch request superseded by navigation'
                    return
                self.launch_form(kind, quick)
            self.submit_io('capabilities', self.daemon.capabilities, received)
            return
        defaults = self.launch_defaults()
        harnesses = self.capability_catalog.get('harnesses', []) if self.capability_catalog else []
        installed = [row['harness'] for row in harnesses if row.get('installed') and row.get('harness')]
        if kind == 'agent' and not defaults['harness'] and installed:
            defaults['harness'] = installed[0]
        fields = [Field('name', 'Window name', value=defaults['name']), self.project_field(defaults['project']),
            Field('host', 'Host', 'choice', defaults['host'], choices=sorted({'local'} | {r.get('fields', {}).get('host', 'local') for r in self.inventory}), hint='Remote isolated-server support must be proven by the adapter'),
            Field('cwd', 'Working directory', value=defaults['cwd'], picker='directory'),
            Field('session', 'Session placement', value=defaults['project'], hint='Must equal project name or reserved no-project; not an independent override')]
        if kind == 'vim':
            fields.append(Field('file', 'Open file', picker='file', hint='Optional existing local file; F4 opens the native file picker'))
        if kind == 'agent':
            fields.extend([Field('harness', 'Harness', 'choice', defaults['harness'], choices=[row['harness'] for row in harnesses if row.get('harness')], required=True,
                hint='; '.join(f'{row.get("harness")}: recognized, installed={row.get("installed")}, usable={row.get("usable", "unknown")}' for row in harnesses) + ' | Model/effort/speed unavailable: no authoritative API catalog'),
                Field('prompt', 'Initial prompt', 'multiline', defaults['prompt'], hint='Launch first, then explicit delivery; partial failure retains target')])
        launched = {}
        def submit(values):
            if launched.get('target'):
                target = launched['target']
                if values['project'].casefold() != launched['project'].casefold():
                    raise ValueError(f'Target already launched in {launched["project"]}; save its original ownership, then use Edit to move it')
                self.store.save_with_project('window', target['label'], target['fields'], project=launched['project'], id=target['id'])
                self.form = None
                self.reload_store()
                self.status = 'Existing launched target ownership saved; no second process launched'
                return
            if values['session'] != values['project']:
                raise ValueError('Session must match explicit project ownership; choose project first and update Session')
            options = {k: v for k, v in values.items() if k != 'session'}
            prompt = options.pop('prompt', '')
            filename = options.pop('file', '')
            if filename:
                options['args'] = [filename]
            project = options['project']
            existing_project = next((r for r in self.persisted if r['kind'] == 'project' and r['label'].casefold() == project.casefold()), None)
            if existing_project:
                options['project'] = project = existing_project['label']
                options['project_id'] = existing_project['id']
            generation = self.interaction_generation
            def success(target):
                launched.update(target=target, project=project)
                self.inventory = [r for r in self.inventory if r['id'] != target['id']] + [target]
                self.revision += 1
                if generation == self.interaction_generation:
                    self.opened = target
                form.submit_label = 'Save existing target ownership (never relaunch)'
                self.store.save_with_project('window', target['label'], target['fields'], project=project, id=target['id'])
                self.reload_store()
                if generation == self.interaction_generation:
                    self.page = 'Panes'
                    self.states['Panes'].selected = target['id']
                self.outcomes[target['id']] = 'launched; prompt not sent' if prompt else 'launched; delivery unknown'
                if prompt:
                    # Queue a separate result, preserving the returned stable target.
                    self.result_queue.put(('result', ('initial-prompt', lambda _v, _e: self.send_prompt(target, prompt, False), None), None))
            def launch():
                try:
                    return self.daemon.launch(kind, **options)
                except Exception as exc:
                    target = getattr(exc, 'target', None)
                    if target:
                        target['fields']['launch_error'] = str(exc)
                        target['fields']['launch_outcome'] = 'partial failure; target retained'
                        return target
                    raise
            self.operation(f'Launch {kind}', launch, success, self.form if not quick else None)
        form = Form(f'New {kind.title()} — explicit placement', fields, submit, submit_label='Launch')
        if quick:
            values = form.values()
            if values is not None:
                submit(values)
        else:
            self.form = form

    def composer(self, target, queue_prompt=False, text=''):
        if not target or target['kind'] != 'window':
            raise ValueError('Select an explicit window destination; selection is never inferred from a new launch')
        if not text:
            text = self.prompt_drafts.get(target['id'], '')
        f = target['fields']
        title = f'{"Queue" if queue_prompt else "Send"} → {f.get("server", self.daemon.server)}/{f.get("host")}/{f.get("session")}/{f.get("window")}'
        fields = [Field('text', 'Prompt', 'multiline', text, required=True, hint='Enter inserts newline; Ctrl+s sends explicitly'),
            Field('queue', 'Queue instead of send now', 'boolean', queue_prompt, choices=['yes', 'no'], hint='Unsupported queues fail explicitly; no send-now fallback')]
        def submit(values):
            self.send_prompt(target, values['text'], values['queue'], self.form)
        self.form = Form(title, fields, submit, submit_label='Queue' if queue_prompt else 'Send now', source=target)
        self.form.editing = True

    def send_prompt(self, target, text, queue_prompt, form=None):
        self.outcomes[target['id']] = 'pending'
        self.prompt_drafts[target['id']] = text
        def success(result):
            if isinstance(result, dict):
                outcome = result.get('outcome', result.get('state', result.get('status', 'accepted; delivery unknown')))
                outcome = str(outcome) + '; delivery ' + str(result.get('delivery_state', 'unknown'))
            else:
                outcome = 'accepted; delivery unknown'
            self.outcomes[target['id']] = str(outcome)
            self.prompt_drafts.pop(target['id'], None)
        self.operation('Queue prompt' if queue_prompt else 'Send prompt', lambda: self.daemon.send(target, text, queue=queue_prompt), success, form,
            failure=lambda error: self.outcomes.update({target['id']: getattr(error, 'outcome', 'failed') + '; draft retained: ' + str(error)}))

    def session_form(self, record=None):
        if record and record.get('fields', {}).get('host', 'local') != 'local':
            raise ValueError('Remote session mutation refused: isolated named-server routing is not proven')
        items = [Field('name', 'Session name', value=record['label'] if record else '', required=True)]
        def submit(values):
            if record:
                if record['label'] in ('no-project', 'mtm-only'):
                    raise ValueError('Reserved session names cannot be changed')
                if any(r['kind'] == 'project' and r['label'] == record['label'] for r in self.persisted):
                    raise ValueError('Project-owned session rename is refused until project/window placement can be changed atomically')
                params = {'session': record['fields'].get('session', record['label']), 'name': values['name']}
                verb = 'rename-session'
            else:
                params, verb = {'name': values['name'], 'window': False}, 'new-session'
            self.operation(verb, lambda: self.daemon.call(verb, params), form=self.form)
        self.form = Form('Edit Session' if record else 'New Session', items, submit)

    def edit_record(self, record):
        if not record:
            return
        kind = record['kind']
        if kind == 'project':
            self.project_form(record)
        elif kind == 'context':
            self.context_form(record)
        elif kind == 'session':
            self.session_form(record)
        elif kind == 'setting':
            self.settings_form()
        elif kind == 'window':
            self.reassign_form(record)
        elif kind == 'host':
            self.status = 'Remote host mutations refused: isolated named-server routing is not proven; local host is managed by TUIOS'
        else:
            self.status = 'This item is read-only'

    def reassign_form(self, target):
        if target.get('fields', {}).get('host', 'local') != 'local':
            raise ValueError('Remote reassociation refused: isolated named-server routing is not proven')
        fields = [self.project_field(self.project_name(target.get('project_id')))]
        def submit(values):
            project = values['project']
            if project.casefold() == 'mtm-only':
                raise ValueError('mtm-only is reserved for the manager UI, not project work')
            existing = next((r for r in self.persisted if r['kind'] == 'project' and r['label'].casefold() == project.casefold()), None)
            if existing:
                project = existing['label']
            f = target['fields']
            def operation():
                snapshot = self.daemon.snapshot()
                destination_exists = any(r['kind'] == 'session' and r['fields'].get('session', r['label']) == project and r['fields'].get('host', 'local') == f.get('host', 'local') for r in snapshot)
                if destination_exists and project != 'no-project' and existing is None:
                    raise ValueError('An existing session conflicts with this unmatched project; no windows were moved')
                if not destination_exists:
                    self.daemon.call('new-session', {'name': project, 'window': False})
                return self.daemon.call('move-window', {'session': f['session'], 'window': f['window'], 'to_session': project, 'follow': False})
            def success(_):
                updated = dict(f, session=project)
                target['fields'] = updated
                self.opened = target
                self.store.save_with_project('window', target['label'], updated, project=project, id=target['id'])
                self.reload_store()
            self.operation('Reassign window', operation, success, self.form)
        self.form = Form('Reassociate window (saved context stays unchanged)', fields, submit)

    def delete_record(self, record):
        if not record:
            return
        def remove():
            if record['kind'] == 'window':
                def closed(_):
                    self.outcomes[record['id']] = 'closed'
                    record['fields']['state'] = 'closed'
                    self.store.save('window', record['label'], record['fields'], project_id=record.get('project_id'), id=record['id'])
                    self.inventory = [r for r in self.inventory if r['id'] != record['id']]
                    self.preview[record['id']] = {'text': '', 'state': 'closed', 'time': time.time()}
                    self.reload_store()
                self.operation('Close window', lambda: self.daemon.close_window(record), closed)
            elif record['kind'] in ('project', 'context', 'tag'):
                self.store.delete(record['id'])
                self.reload_store()
            elif record['kind'] == 'setting':
                self.store.unset_setting(record['label'])
                self.settings = self.store.settings()
                self.reload_store()
            else:
                self.status = 'Use discovered native operations for supported session/host lifecycle; no invented delete operation'
        self.confirm = (f'{"Close" if record["kind"] == "window" else "Delete"} {record["label"]}? Ownership and unread state are not completion.', remove)

    def open_record(self, record):
        if not record:
            return
        if record['kind'] == 'json' and isinstance(record['fields']['value'], (dict, list)):
            self.json_stack.append((self.json_root, self.states['JSON']))
            self.json_root = self.json_records(record['fields']['value'], record['fields']['path'])
            self.states['JSON'] = ListState()
            self.revision += 1
        elif record['kind'] == 'project':
            self.project_context(record)
        elif record['kind'] == 'setting':
            self.settings_form()
        else:
            self.opened = record
            self.focus_detail = True

    def live_size(self):
        height, width = self.screen.getmaxyx()
        cols = width if width < 60 else width - max(20, min(width // 2, 56))
        return max(1, cols), max(1, height - 12)

    def live(self, target):
        if not target or target['kind'] != 'window':
            raise ValueError('Select a real window for live interaction')
        if self.live_stream or self.live_transition:
            return
        self.live_transition = True
        self.status = 'Live connecting; input is not forwarded until the lease is ready'
        cols, rows = self.live_size()
        def received(stream, error):
            self.live_transition = False
            if error:
                self.status = f'Live unavailable: {error}'
                return
            self.live_stream, self.live_target = stream, target
            self.opened = target
            self.focus_detail = True
            self.states[self.page].tab = 'Screen'
            self.live_prefix = False
            curses.raw()
            self.status = f'LIVE → {target["label"]}; {self.settings.get("live_leader", "ctrl+a")} d detaches; Escape/Ctrl+z go to target'
            self.preview_requested = 0
        self.submit_io('live-open', lambda: self.daemon.open_live(target, cols=cols, rows=rows), received)

    def leave_live(self, next_target=None):
        stream = self.live_stream
        if not stream:
            return
        self.live_transition = True
        self.live_target = None
        self.live_stream = None
        self.live_buffer.clear()
        def received(_, error):
            self.live_transition = False
            curses.raw()
            self.status = f'Live lease release failed: {error}' if error else 'Live detached; lease released; navigation restored'
            if next_target and not error:
                self.live(next_target)
        self.submit_io('live-close', stream.close, received)

    def handle_live(self, key):
        if self.live_transition:
            self.status = 'Live transition pending; input consumed, not sent to any target'
            return
        stream, target = self.live_stream, self.live_target
        if not stream or not target:
            return
        if isinstance(key, Paste):
            data = str(key).encode()
            if len(self.live_buffer) + len(data) <= 65536:
                self.live_buffer.extend(data)
            else:
                self.status = 'Live paste rejected: exceeds available 64 KiB input buffer'
            return
        if key == 'RESIZE':
            cols, rows = self.live_size()
            self.submit_io('live-resize', lambda: stream.resize(cols, rows),
                lambda _, error: setattr(self, 'status', f'Live resize failed: {error}') if error else None)
            return
        leader_setting = self.settings.get('live_leader', 'ctrl+a').lower()
        leader = chr(ord(leader_setting[-1]) & 31) if leader_setting.startswith('ctrl+') and len(leader_setting) == 6 else '\x01'
        if self.live_prefix:
            self.live_prefix = False
            if key == leader:
                self.live_buffer.extend(leader.encode())
            elif key in ('d', 'q'):
                self.leave_live()
            elif key in ('n', 'p'):
                targets = [r for r in self.inventory if r['kind'] == 'window']
                if targets:
                    index = next((i for i, r in enumerate(targets) if r['id'] == target['id']), 0)
                    self.leave_live(targets[(index + (1 if key == 'n' else -1)) % len(targets)])
            elif key == '?':
                self.status = f'LIVE leader {leader_setting}: d detach; n/p switch; z zoom; ? help; doubled leader literal. Unknown combinations consumed.'
            elif key == 'z':
                self.submit_io('live-zoom', stream.zoom,
                    lambda _, error: setattr(self, 'status', error or 'Live zoom accepted'))
            else:
                self.status = f'Unknown live leader combination {key!r}; consumed, not forwarded'
        elif key == leader:
            self.live_prefix = True
            self.status = 'LIVE leader: d detach | n/p switch | z zoom | ? help | repeat sends literal'
        else:
            sequences = {'ENTER': '\r', 'TAB': '\t', 'ESC': '\x1b', 'BACKSPACE': '\x7f',
                'UP': '\x1b[A', 'DOWN': '\x1b[B', 'RIGHT': '\x1b[C', 'LEFT': '\x1b[D',
                'HOME': '\x1b[H', 'END': '\x1b[F', 'DELETE': '\x1b[3~',
                'PAGEUP': '\x1b[5~', 'PAGEDOWN': '\x1b[6~', 'BTAB': '\x1b[Z'}
            sequences.update({'INSERT': '\x1b[2~', 'F1': '\x1bOP', 'F2': '\x1bOQ', 'F3': '\x1bOR', 'F4': '\x1bOS',
                'F5': '\x1b[15~', 'F6': '\x1b[17~', 'F7': '\x1b[18~', 'F8': '\x1b[19~',
                'F9': '\x1b[20~', 'F10': '\x1b[21~', 'F11': '\x1b[23~', 'F12': '\x1b[24~'})
            value = sequences.get(key, key if len(key) == 1 or '\n' in key else '')
            if len(self.live_buffer) + len(value.encode()) > 65536:
                self.status = 'Live input backpressure: pending buffer is full; input rejected visibly'
            else:
                self.live_buffer.extend(value.encode())

    def pump_live(self):
        stream = self.live_stream
        if not stream:
            return
        try:
            for event in stream.poll():
                self.preview_requested = 0
                self.dirty = True
                if event.get('type') in ('error', 'closed'):
                    self.status = 'Live stream: ' + str(event.get('message', event.get('reason', event['type'])))
                    self.leave_live()
                    return
            if self.live_buffer and 'live-send' not in self.pending:
                data = bytes(self.live_buffer)
                self.live_buffer.clear()
                self.submit_io('live-send', lambda: stream.send(data),
                    lambda _, error: setattr(self, 'status', f'Live send failed: {error}') if error else None)
        except Exception as exc:
            self.status = f'Live disconnected: {exc}'
            self.leave_live()

    @staticmethod
    def setting_defaults():
        return {'live_leader': 'ctrl+a', 'launch_directory': '', 'agent_harness': '', 'bindings': {}, 'color': True}

    def settings_form(self):
        defaults = self.setting_defaults()
        fields = []
        for key, default in defaults.items():
            value = self.settings.get(key, default)
            kind = 'boolean' if isinstance(default, bool) else 'multiline' if key == 'bindings' else 'text'
            fields.append(Field(key, key.replace('_', ' ').title(), kind, json.dumps(value, indent=2) if key == 'bindings' else value,
                default=default, picker='directory' if key == 'launch_directory' else None,
                hint=('Stored' if key in self.settings else 'Default') + '; empty value resets text settings; bindings is action-ID → key JSON'))
        fields.append(Field('reset', 'Reset all settings to defaults', 'boolean', False))
        def submit(values):
            overrides = json.loads(values['bindings'] or '{}')
            if not isinstance(overrides, dict) or any(not isinstance(v, str) for v in overrides.values()):
                raise ValueError('Bindings must be a JSON object of action IDs to key strings')
            unknown = set(overrides) - set(concepts.ACTIONS)
            if unknown:
                raise ValueError('Unknown action IDs: ' + ', '.join(sorted(unknown)))
            if not re.fullmatch(r'ctrl\+[a-z]', values['live_leader'].lower()):
                raise ValueError('Live leader must be ctrl+a through ctrl+z')
            old = self.settings
            proposed = {k: v for k, v in values.items() if k != 'reset'}
            proposed['bindings'] = overrides
            self.settings = {} if values['reset'] else proposed
            self.binding_errors = []
            bindings = self.effective_bindings()
            if self.binding_errors:
                error = '; '.join(self.binding_errors)
                self.settings = old
                self.binding_errors = []
                raise ValueError(error)
            changes, resets = {}, []
            for key, default in defaults.items():
                value = self.settings.get(key, default)
                if value == default or value == '':
                    resets.append(key)
                elif old.get(key, default) != value:
                    changes[key] = value
            try:
                self.store.set_settings(changes, reset=resets)
            except Exception:
                self.settings = old
                raise
            self.settings = self.store.settings()
            self.bindings = bindings
            self.services.settings.clear()
            self.services.settings.update(self.settings)
            self.form = None
            self.revision += 1
        self.form = Form('Settings — stored values versus defaults', fields, submit)

    def list_fields(self, records):
        names = {'label', 'kind', 'project_id', 'pinned', 'starred'}
        for record in records:
            names.update(record.get('fields', {}))
            definition = concepts.CONCEPTS.get(record['kind'], {})
            names.update(definition.get('fields', {}))
        return sorted(names)

    def list_controls(self, control):
        state = self.state()
        records = self.modals[-1].records if self.modals else self.page_records()
        names = self.list_fields(records)
        if control == 'filter':
            items = [Field('field', 'Field', 'choice', choices=names, required=True), Field('values', 'Included values (comma-separated)', 'multi', extensible=False, hint='Empty removes this field filter; choose declared or observed values for the selected field')]
            def submit(values):
                if values['values']:
                    state.filters[values['field']] = values['values']
                else:
                    state.filters.pop(values['field'], None)
                state.revision += 1
                self.form = None
        elif control == 'sort':
            items = [Field('field', 'Sort field', 'choice', state.sort, choices=names), Field('descending', 'Descending', 'boolean', state.descending)]
            def submit(values):
                state.sort, state.descending = values['field'], values['descending']
                state.revision += 1
                self.form = None
        else:
            group_values = sorted({str(record_value(r, name)) for r in records for name in names if not isinstance(record_value(r, name), (dict, list))})
            items = [Field('fields', 'Grouping fields in order', 'multi', state.group_by, choices=names),
                Field('scope', 'Included group values', 'multi', state.group_scope, choices=group_values, extensible=False,
                    hint='Scope values apply to the outer grouping field; empty includes every group; scope survives refresh')]
            def submit(values):
                state.group_by, state.group_scope = values['fields'], values['scope']
                state.revision += 1
                self.form = None
        self.form = Form(control.replace('_', ' ').title(), items, submit)
        if control == 'filter':
            self.form.source = {'filter_options': concepts.filter_options(records)}
            self.form.field('values').choices = []

    def cycle_groups(self):
        state = self.state()
        record = self.selected()
        definition = concepts.CONCEPTS.get(record['kind'], {}) if record else {}
        configurations = definition.get('groupings') or [[], ['kind'], ['project_id'], ['host', 'session']]
        configurations = [list(config) for config in configurations]
        if [] not in configurations:
            configurations.insert(0, [])
        index = configurations.index(state.group_by) if state.group_by in configurations else -1
        state.group_by = configurations[(index + 1) % len(configurations)]
        state.revision += 1

    def palette(self):
        records = [make_record('route:new', 'New', route='new', hint='Terminal / Agent / Vim / Project / Context / Session'),
            make_record('route:selected', 'Selected item', route='selected', hint='Object-first actions')]
        records += [make_record(action, meta['label'], action=action, hint=self.binding_for(action) + ' ' + meta.get('description', '')) for action, meta in concepts.ACTIONS.items()]
        self.modals.append(Modal('Palette', 'palette', records, self.origin(), searching=True))

    def binding_for(self, action):
        return next((key for key, value in self.bindings.items() if value == action), '(unbound)')

    def help(self):
        records = [make_record('help:' + action, f'{self.binding_for(action):12} {meta["label"]}', 'help',
            description=meta.get('description', ''), hint=meta.get('description', '')) for action, meta in concepts.ACTIONS.items()]
        records += [make_record('help:nav:' + key, key, 'help', hint=description, description=description) for key, description in [
            ('j/k or arrows', 'Move selection; in text editing letters insert normally'), ('h/l', 'Change list/detail focus; modal content stays cropped'),
            ('d/u, Ctrl+f/b', 'Half/full page; g/G reaches ends'), ('{/}', 'Previous/next page or section'),
            ('Tab/Shift+Tab', 'Page-level detail tabs; in forms move fields'), ('Ctrl+s', 'Submit current form or send prompt; Enter in multiline inserts newline'),
            ('F2 / F4', 'Find form field / native local OS path picker'), ('Ctrl+n/p, Ctrl+y', 'Navigate and accept form suggestions'),
            ('Escape', 'Jump/Context: one level, root restores underlying view; palette: leave active nonempty search first'),
            (self.settings.get('live_leader', 'ctrl+a'), 'Live leader: d detach, n/p next/previous, z zoom, ? help; double leader sends literal; bare Esc/Ctrl+z remain target input')]]
        self.modals.append(Modal('Help — effective bindings', 'help', records, self.origin()))

    def open_mode(self, mode):
        origin = self.origin()
        if mode == 'modes':
            records = [make_record('mode:jump', 'Jump', mode='jump', order=0), make_record('mode:context', 'Context', mode='context', order=1)]
            self.modals.append(Modal('Modes', 'modes', records, origin, state=ListState(sort='order')))
        elif mode == 'jump':
            records = []
            if origin and origin['kind'] == 'window':
                for tab in ('screen', 'activity', 'transcript', 'turns'):
                    records.append(make_record(origin['id'] + ':' + tab, f'{origin["label"]} / {tab.title()}', 'content', target=origin, tab=tab))
                f = origin['fields']
                for record in self.inventory:
                    rf = record.get('fields', {})
                    if record['kind'] == 'window' and record['id'] != origin['id'] and all(rf.get(k) == f.get(k) for k in ('server', 'host', 'session')):
                        records.append(make_record('jump:' + record['id'], record['label'], 'content', target=record, tab='screen'))
                records.append(make_record('jump:context', 'Associated Context', route='context'))
            else:
                records.append(make_record('unavailable', 'No current agent or terminal selected', 'notice', reason='Select a pane to browse its actual current session outline'))
            for index, entry in enumerate(records):
                entry['fields']['order'] = index
            self.modals.append(Modal('Jump', 'jump', records, origin, state=ListState(sort='order')))
        else:
            records = []
            if origin and origin['kind'] == 'window':
                for record in self.persisted:
                    source = record.get('fields', {}).get('source', {})
                    if record['kind'] == 'context' and (source.get('id') == origin['id'] or source and all(source.get(k) == origin['fields'].get(k) for k in ('server', 'host', 'session', 'window'))):
                        records.append(record)
            records.append(make_record('context:projects', 'Browse project context', route='projects', hint='Saved items survive source closure'))
            if not any(r['kind'] == 'context' for r in records):
                records.insert(0, make_record('context:empty', 'No saved context for this target', 'notice', reason='Save context explicitly; no automatic extraction is performed'))
            self.modals.append(Modal('Context', 'context', records, origin))

    def project_context(self, project):
        project_id = project['id'] if project else None
        records = [r for r in self.persisted if r['kind'] == 'context' and r.get('project_id') == project_id]
        self.modals.append(Modal(f'Project: {project["label"] if project else "no-project"}', 'project-context', records, self.origin()))

    def handle_modal(self, key):
        modal = self.modals[-1]
        if isinstance(key, Paste):
            if modal.searching:
                modal.search.handle(key)
                modal.state.query = modal.search.text
            else:
                self.status = 'Paste ignored outside modal search'
            return
        if key == 'ESC':
            if modal.mode == 'palette' and modal.searching and modal.state.query:
                modal.searching = False
            else:
                self.modals.pop()
            return
        if modal.searching:
            if key == 'ENTER':
                modal.searching = False
                self.activate_modal()
            elif key in ('UP', 'DOWN', '\x0e', '\x10'):
                modal.state.move(1 if key in ('DOWN', '\x0e') else -1, self.rows())
            else:
                modal.search.handle(key)
                modal.state.query = modal.search.text
            return
        if key == '/':
            modal.searching = True
            modal.search = TextBuffer(modal.state.query)
        elif key == 'ENTER':
            self.activate_modal()
        elif key == self.binding_for('ui.group'):
            self.cycle_groups()
        elif key in (self.binding_for('ui.group_config'), self.binding_for('ui.filter'), self.binding_for('ui.sort')):
            self.action(self.bindings[key])
        elif key == '\\':
            modal.state.query, modal.state.filters, modal.state.group_scope = '', {}, []
            modal.state.revision += 1
        elif key in ('h', 'LEFT'):
            modal.preview_horizontal = max(0, modal.preview_horizontal - 8)
        elif key in ('l', 'RIGHT'):
            modal.preview_horizontal += 8
        elif key in ('{', '}'):
            modal.preview_scroll = max(0, modal.preview_scroll + (5 if key == '}' else -5))
        else:
            old = modal.state.selected
            self.navigate(key, modal.state, self.rows())
            if old != modal.state.selected:
                modal.preview_scroll = modal.preview_horizontal = 0
                self.preview_requested = 0

    def activate_modal(self):
        modal = self.modals[-1]
        record = self.selected()
        if not record:
            return
        fields = record['fields']
        if modal.mode == 'modes':
            self.open_mode(fields['mode'])
        elif modal.mode == 'palette':
            route = fields.get('route')
            if route:
                actions = [a for a in concepts.ACTIONS if a.startswith('ui.new_') or a.startswith('ui.form_')] if route == 'new' else ['ui.open', 'ui.live', 'ui.edit', 'ui.delete', 'ui.pin', 'ui.star', 'ui.send', 'ui.queue', 'ui.save_context', 'ui.jump', 'ui.context']
                entries = [make_record(a, concepts.ACTIONS[a]['label'], action=a, hint=self.binding_for(a)) for a in actions]
                self.modals.append(Modal(record['label'], 'palette', entries, modal.origin, searching=True))
            else:
                action = fields.get('action')
                self.modals.clear()
                if action:
                    self.action(action)
        elif modal.mode == 'projects':
            self.project_context(None if record['id'] == 'no-project' else record)
        elif modal.mode in ('jump', 'context', 'project-context'):
            if fields.get('route') == 'context':
                self.open_mode('context')
            elif fields.get('route') == 'projects':
                projects = [make_record('no-project', 'no-project', 'project')] + [r for r in self.persisted if r['kind'] == 'project']
                self.modals.append(Modal('Choose project', 'projects', projects, modal.origin))
            elif record['kind'] == 'notice':
                self.status = fields.get('reason', record['label'])
            else:
                target = fields.get('target')
                self.opened = target or record
                if target:
                    self.states[self.page].tab = fields.get('tab', 'screen').title()
                self.modals.clear()
                self.focus_detail = True
        elif modal.mode == 'verbs':
            self.native_operation_form(record)
        elif modal.mode == 'help':
            self.status = fields.get('description', '')

    def operations(self):
        generation = self.interaction_generation
        def received(value, error):
            if error:
                self.status = f'Verb discovery unavailable: {error}'
                return
            if generation != self.interaction_generation:
                self.status = 'Verb discovery completed; superseded view was not reopened'
                return
            entries = value.get('verbs', value) if isinstance(value, dict) else value
            if isinstance(entries, dict):
                records = [make_record('verb:' + name, name, 'verb', schema=metadata, hint=metadata.get('description', '') if isinstance(metadata, dict) else str(metadata)) for name, metadata in entries.items()]
            elif isinstance(entries, list):
                records = [make_record('verb:' + str(entry.get('name', entry.get('verb', ''))), str(entry.get('name', entry.get('verb', ''))), 'verb', schema=entry, hint=entry.get('description', '')) if isinstance(entry, dict) else make_record('verb:' + str(entry), str(entry), 'verb') for entry in entries]
            else:
                raise ValueError('Native verb catalog has an unsupported shape; no commands invented')
            self.modals.append(Modal('TUIOS native operations', 'verbs', records, self.origin(), searching=True))
        self.submit_io('verbs', self.daemon.verbs, received)

    def native_operation_form(self, record):
        target = self.modals[-1].origin
        if target and target.get('fields', {}).get('host', 'local') != 'local':
            raise ValueError('Remote native operation refused: isolated named-server routing is not proven')
        managed = {'new-window': 'New Terminal/Vim', 'start-agent': 'New Agent', 'move-window': 'Edit window project',
            'close-window': 'Close window', 'new-session': 'New Session', 'rename-session': 'Edit Session'}
        if record['label'] in managed:
            raise ValueError(f'Use {managed[record["label"]]} in the shared palette; raw lifecycle calls would bypass explicit project placement')
        params = {}
        if target and target['kind'] == 'window':
            params = {key: target['fields'][key] for key in ('session', 'window') if key in target['fields']}
        fields = [Field('params', 'Native parameter object', 'multiline', json.dumps(params, indent=2), required=True,
            hint=json.dumps(record.get('fields', {}).get('schema', {}), ensure_ascii=False))]
        def submit(values):
            params = json.loads(values['params'])
            if not isinstance(params, dict):
                raise ValueError('Parameters must be a JSON object')
            form = self.form
            self.confirm = (f'Execute {record["label"]} on {self.daemon.server}? Native operations may mutate runtime state.',
                lambda: self.operation(record['label'], lambda: self.daemon.call(record['label'], params), form=form))
        self.form = Form(record['label'] + ' — discovered native operation', fields, submit, submit_label='Review confirmation')


def run(services, json_data=None):
    """Enter the interactive shell. JSON_NULL requests an explorer for JSON null."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('Interactive UI requires a terminal on stdin and stdout')
    return curses.wrapper(lambda screen: Application(screen, services, json_data).run())
