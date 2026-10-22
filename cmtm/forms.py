"""Shared form state and input editing, independent of curses rendering."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .concepts import fuzzy_match


class Paste(str):
    """A bracketed paste is text, never a sequence of application commands."""


def ranked(query, options):
    q = str(query).casefold()
    matches = [str(value) for value in options if fuzzy_match(q, str(value))]
    return sorted(matches, key=lambda value: (value.casefold() != q, not value.casefold().startswith(q), len(value), value.casefold()))


@dataclass
class Field:
    name: str
    label: str
    type: str = 'text'
    value: Any = ''
    default: Any = ''
    required: bool = False
    choices: list = field(default_factory=list)
    extensible: bool = False
    hint: str = ''
    picker: str | None = None
    multiple: bool = False
    parent: str | None = None
    parents: dict = field(default_factory=dict)
    shortcut: str = ''
    validator: Callable | None = None


class TextBuffer:
    def __init__(self, text=''):
        self.text = str(text)
        self.cursor = len(self.text)
        self.kill = ''

    def insert(self, text):
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)

    def handle(self, key, multiline=False):
        before = self.text
        if isinstance(key, Paste):
            self.insert(str(key) if multiline else str(key).replace('\n', ' '))
            return before != self.text
        if key in ('LEFT', '\x02'):
            self.cursor = max(0, self.cursor - 1)
        elif key in ('RIGHT', '\x06'):
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key in ('HOME', '\x01'):
            self.cursor = self.text.rfind('\n', 0, self.cursor) + 1
        elif key in ('END', '\x05'):
            end = self.text.find('\n', self.cursor)
            self.cursor = len(self.text) if end < 0 else end
        elif key in ('BACKSPACE', '\x7f', '\x08'):
            if self.cursor:
                self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
                self.cursor -= 1
        elif key == 'DELETE':
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        elif key == '\x0b':
            end = self.text.find('\n', self.cursor)
            end = len(self.text) if end < 0 else end
            self.kill = self.text[self.cursor:end]
            self.text = self.text[:self.cursor] + self.text[end:]
        elif key == '\x15':
            start = self.text.rfind('\n', 0, self.cursor) + 1
            self.kill = self.text[start:self.cursor]
            self.text = self.text[:start] + self.text[self.cursor:]
            self.cursor = start
        elif key == '\x17':
            start = self.cursor
            while start and self.text[start - 1].isspace():
                start -= 1
            while start and not self.text[start - 1].isspace():
                start -= 1
            self.kill = self.text[start:self.cursor]
            self.text = self.text[:start] + self.text[self.cursor:]
            self.cursor = start
        elif key == '\x19':
            self.insert(self.kill)
        elif key in ('ENTER', '\n', '\r') and multiline:
            self.insert('\n')
        elif key in ('UP', 'DOWN') and multiline:
            start = self.text.rfind('\n', 0, self.cursor) + 1
            col = self.cursor - start
            if key == 'UP' and start:
                previous = self.text.rfind('\n', 0, start - 1) + 1
                self.cursor = min(start - 1, previous + col)
            elif key == 'DOWN':
                next_line = self.text.find('\n', self.cursor)
                if next_line >= 0:
                    end = self.text.find('\n', next_line + 1)
                    self.cursor = min(len(self.text) if end < 0 else end, next_line + 1 + col)
        elif isinstance(key, str) and (len(key) == 1 and key >= ' ' or '\n' in key and len(key) > 1):
            self.insert(key if multiline else key.replace('\n', ' '))
        return before != self.text


class Form:
    def __init__(self, title, fields, submit, *, submit_label='Save', source=None):
        self.title = title
        self.fields = fields
        self.submit = submit
        self.submit_label = submit_label
        self.source = source
        self.index = 0
        self.scroll = 0
        self.editing = False
        self.finding = False
        self.finder = TextBuffer()
        self.suggestion = -1
        self.errors = {}
        self.message = ''
        self.busy = False
        self.closed = False
        self.generation = {f.name: 0 for f in fields}
        self.buffers = {f.name: TextBuffer(self._string(f.value)) for f in fields}
        self.initial = self.values(validate=False)

    @staticmethod
    def _string(value):
        if isinstance(value, bool):
            return 'yes' if value else 'no'
        if isinstance(value, (list, tuple)):
            return ', '.join(str(v) for v in value)
        return '' if value is None else str(value)

    @property
    def current(self):
        return self.fields[self.index]

    @property
    def dirty(self):
        return self.values(validate=False) != self.initial

    def field(self, name):
        return next(f for f in self.fields if f.name == name)

    def options(self, item=None):
        item = item or self.current
        choices = list(item.choices)
        if item.parent and item.parents:
            parent_value = self.buffers[item.parent].text
            if parent_value:
                choices = [c for c in choices if item.parents.get(str(c)) == parent_value]
        value = self.buffers[item.name].text
        if item.type == 'multi':
            value = value.split(',')[-1].strip()
        return ranked(value, choices)

    def set(self, name, value):
        item = self.field(name)
        self.buffers[name] = TextBuffer(self._string(value))
        self.generation[name] += 1
        self.errors.pop(name, None)
        if item.parent and str(value) in item.parents:
            parent = item.parents[str(value)]
            self.buffers[item.parent] = TextBuffer(parent)
            self.generation[item.parent] += 1
        for child in self.fields:
            if child.parent == name:
                child_value = self.buffers[child.name].text
                if child_value and child.parents.get(child_value) != str(value):
                    self.buffers[child.name] = TextBuffer()
                    self.generation[child.name] += 1
                    self.message = f'{child.label} cleared: incompatible with {item.label}'

    def staged(self):
        result = []
        for item in self.fields:
            value = self.buffers[item.name].text.strip()
            if item.extensible and value:
                values = [v.strip() for v in value.split(',')] if item.type == 'multi' else [value]
                for part in values:
                    if part and not any(part.casefold() == str(choice).casefold() for choice in item.choices):
                        result.append(f'{item.label}: {part}')
        return result

    def values(self, validate=True):
        result, errors = {}, {}
        for item in self.fields:
            text = self.buffers[item.name].text
            value = text if item.type == 'multiline' else text.strip()
            if validate and item.required and not value:
                errors[item.name] = 'Required; choose an explicit no-value option where offered'
            if item.type == 'number':
                try:
                    value = int(value) if value else None
                except ValueError:
                    if validate:
                        errors[item.name] = 'Enter a whole number'
            elif item.type == 'boolean':
                if validate and value.lower() not in ('yes', 'no', 'true', 'false', '1', '0'):
                    errors[item.name] = 'Choose yes or no'
                value = value.lower() in ('yes', 'true', '1')
            elif item.type == 'multi':
                value = [v.strip() for v in value.split(',') if v.strip()]
            if validate and item.type in ('choice', 'multi') and not item.extensible:
                check = value if isinstance(value, list) else [value]
                if any(v and v not in item.choices for v in check):
                    errors[item.name] = 'Choose a supported value: ' + ', '.join(map(str, item.choices))
            if validate and item.validator:
                error = item.validator(value)
                if error:
                    errors[item.name] = error
            result[item.name] = value
        if validate:
            self.errors = errors
            if errors:
                self.index = next(i for i, f in enumerate(self.fields) if f.name in errors)
                return None
        return result

    def accept_suggestion(self):
        options = self.options()
        if options:
            chosen = options[max(0, self.suggestion) % len(options)]
            if self.current.type == 'multi':
                parts = self.buffers[self.current.name].text.split(',')[:-1]
                self.set(self.current.name, ', '.join(parts + [chosen]))
            else:
                self.set(self.current.name, chosen)
            self.suggestion = -1

    def handle(self, key):
        if self.busy:
            self.message = 'Operation pending; draft retained until the result arrives'
            return None
        if isinstance(key, Paste):
            if self.editing:
                name = self.current.name
                buffer = self.buffers[name]
                buffer.handle(key, self.current.type == 'multiline')
                text, cursor = buffer.text, buffer.cursor
                self.set(name, text)
                self.buffers[name].cursor = cursor
            elif self.finding:
                self.finder.handle(key)
            else:
                self.message = 'Paste ignored outside text editing; press i or Enter to edit a field'
            return None
        if key == '\x13':
            values = self.values()
            if values is not None:
                return ('submit', values)
            return None
        if key == 'F4':
            return ('picker', self.current)
        if key == 'F2':
            self.finding = not self.finding
            return None
        if self.finding:
            if key == 'ESC':
                self.finding = False
            elif key == 'ENTER':
                self.finding = False
                self.editing = True
            else:
                self.finder.handle(key)
                matches = ranked(self.finder.text, [f.label for f in self.fields])
                if matches:
                    self.index = next(i for i, f in enumerate(self.fields) if f.label == matches[0])
            return None
        if key in ('TAB', 'BTAB'):
            self.index = (self.index + (1 if key == 'TAB' else -1)) % len(self.fields)
            self.suggestion = -1
            return None
        if key == 'ESC':
            if self.editing:
                self.editing = False
                return None
            return ('cancel', None)
        if self.editing:
            if key in ('\x0e', '\x10'):
                options = self.options()
                if options:
                    self.suggestion = (self.suggestion + (1 if key == '\x0e' else -1)) % len(options)
            elif key == '\x19' and self.suggestion >= 0:
                self.accept_suggestion()
            elif key == 'ENTER' and self.current.type != 'multiline':
                if self.suggestion >= 0:
                    self.accept_suggestion()
                self.editing = False
            else:
                name = self.current.name
                if self.buffers[name].handle(key, self.current.type == 'multiline'):
                    text, cursor = self.buffers[name].text, self.buffers[name].cursor
                    self.set(name, text)
                    self.buffers[name].cursor = cursor
                    self.suggestion = -1
            return None
        if key in ('j', 'DOWN', '\x0e', '}'):
            self.index = min(len(self.fields) - 1, self.index + 1)
        elif key in ('k', 'UP', '\x10', '{'):
            self.index = max(0, self.index - 1)
        elif key in ('g', 'HOME'):
            self.index = 0
        elif key in ('G', 'END'):
            self.index = len(self.fields) - 1
        elif key in ('d', 'PAGEDOWN', '\x06'):
            self.index = min(len(self.fields) - 1, self.index + 5)
        elif key in ('u', 'PAGEUP', '\x02'):
            self.index = max(0, self.index - 5)
        elif key in ('l', 'RIGHT', 'ENTER', 'i'):
            if self.current.type == 'boolean':
                self.set(self.current.name, self.buffers[self.current.name].text.lower() not in ('yes', 'true', '1'))
            else:
                self.editing = True
        elif key in ('h', 'LEFT'):
            self.editing = False
        elif key == ' ' and self.current.choices:
            choices = list(map(str, self.current.choices))
            old = self.buffers[self.current.name].text
            pos = choices.index(old) if old in choices else -1
            self.set(self.current.name, choices[(pos + 1) % len(choices)])
        elif isinstance(key, str) and key.isdigit() and key != '0':
            self.index = min(len(self.fields) - 1, int(key) - 1)
        elif key == '/':
            self.finding = True
        else:
            for index, item in enumerate(self.fields):
                if item.shortcut and key == item.shortcut:
                    self.index, self.editing = index, True
                    break
        return None
