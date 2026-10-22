"""Declarative concepts and side-effect-free list behavior shared by every UI.

A concept adds field and action metadata, not a new navigation implementation.
User status/type/tag vocabularies are distinct from authoritative daemon state.
"""
from __future__ import annotations

from collections.abc import Mapping
import math
import unicodedata


def _field(label, type="text", *, extensible=False, required=False, choices=(), **extra):
    return {"label": label, "type": type, "extensible": extensible,
            "required": required, "choices": list(choices), **extra}


def _fields(extra):
    return {
        "label": _field("Name", required=True),
        "id": _field("Identity", readonly=True),
        "project_id": _field("Project", "project", choices=("no-project",), nullable=True),
        "tags": _field("Tags", "multiselect", extensible=True),
        "type": _field("Type", "choice", extensible=True),
        "status": _field("User status", "choice", extensible=True),
        "starred": _field("Starred", "boolean", choices=(False, True)),
        "pinned": _field("Pinned", "boolean", choices=(False, True)),
        **extra,
    }


_LIVE_FIELDS = {
    "server": _field("Server", "choice", readonly=True),
    "host": _field("Host", "choice"),
    "session": _field("Session", "choice"),
    "state": _field("Daemon state", "choice", readonly=True),
    "freshness": _field("Freshness", "choice", readonly=True),
}

CONCEPTS = {
    "project": {
        "label": "Projects",
        "fields": _fields({"description": _field("Description", "multiline"),
                           "directory": _field("Directory", "directory")}),
        "actions": ["ui.new_project", "ui.edit", "ui.delete", "ui.context", "ui.pin", "ui.star"],
        "groupings": [[], ["status"], ["tags"], ["type"]],
    },
    "context": {
        "label": "Context",
        "fields": _fields({
            "path": _field("File", "file"), "text": _field("Text", "multiline"),
            "url": _field("URL"), "source": _field("Source identity", "object", readonly=True),
            "source_server": _field("Source server", "choice", readonly=True),
            "source_host": _field("Source host", "choice", readonly=True),
            "source_session": _field("Source session", "choice", readonly=True),
            "source_window": _field("Source window", "choice", readonly=True),
        }),
        "actions": ["ui.new_context", "ui.edit", "ui.open", "ui.delete", "ui.pin", "ui.star"],
        "groupings": [[], ["project_id"], ["type"], ["tags"], ["source_host", "source_session"]],
    },
    "tag": {
        "label": "Tags", "fields": _fields({}),
        "actions": ["ui.edit", "ui.delete", "ui.pin", "ui.star"],
        "groupings": [[], ["project_id"], ["type"]],
    },
    "window": {
        "label": "Panes",
        "fields": _fields({**_LIVE_FIELDS,
            "window": _field("Window identity", readonly=True),
            "terminal_kind": _field("Terminal kind", "choice", readonly=True),
            "cwd": _field("Directory", "directory"),
            "activity_age": _field("Activity age", "number", readonly=True),
            "attention": _field("Attention", "multiselect", readonly=True),
            "harness": _field("Harness", "choice"), "model": _field("Model", "choice"),
            "effort": _field("Effort", "choice"), "speed": _field("Speed", "choice"),
        }),
        "actions": ["ui.new_terminal", "ui.new_agent", "ui.new_vim", "ui.form_terminal", "ui.form_agent",
                    "ui.form_vim", "ui.open", "ui.live", "ui.delete", "ui.send", "ui.queue", "ui.jump",
                    "ui.context", "ui.save_context", "ui.edit", "ui.pin", "ui.star"],
        "groupings": [[], ["project_id"], ["host", "session"], ["state"], ["terminal_kind"]],
    },
    "session": {
        "label": "Sessions", "fields": _fields(dict(_LIVE_FIELDS)),
        "actions": ["ui.new_session", "ui.open", "ui.edit", "ui.delete", "ui.pin", "ui.star"],
        "groupings": [[], ["host"], ["project_id"], ["state"]],
    },
    "host": {
        "label": "Hosts", "fields": _fields({**_LIVE_FIELDS,
            "address": _field("Address"), "connected": _field("Connected", "boolean", readonly=True)}),
        "actions": ["ui.open", "ui.start", "ui.refresh", "ui.pin", "ui.star"],
        "groupings": [[], ["state"], ["connected"]],
    },
}


def _action(label, verb, concept=None, binding="", description="", confirm=False):
    return {"label": label, "verb": verb, "concept": concept, "binding": binding,
            "modes": ["normal"], "description": description or label, "confirm": confirm}


ACTIONS = {
    "ui.quit": _action("Quit", "Quit", binding="q", description="Disconnect this client; leave daemon and windows running"),
    "ui.palette": _action("Command palette", "Open", binding=":", description="Search nested verb-first and object-first actions"),
    "ui.help": _action("Help", "Open", binding="?", description="Search effective bindings for the current mode"),
    "ui.jump": _action("Jump", "Open", binding="J", description="Outline and preview the current target"),
    "ui.context": _action("Context", "Open", binding="C", description="Browse current target and persistent project context"),
    "ui.modes": _action("Modes", "Open", binding="M", description="Choose Jump or Context in the same modal"),
    "ui.search": _action("Search", "Search", binding="/", description="Fuzzy-search the current list"),
    "ui.filter": _action("Filter", "Filter", binding="f", description="Filter any observed or declared field"),
    "ui.sort": _action("Sort", "Sort", binding="s", description="Choose field and direction"),
    "ui.group": _action("Cycle grouping", "Group", binding="z", description="Cycle declared grouping configurations"),
    "ui.group_config": _action("Customize grouping", "Group", binding="Z", description="Choose ordered fields and included group values"),
    "ui.clear": _action("Clear list controls", "Clear", binding="\\", description="Clear query, filters, grouping and scope"),
    "ui.pin": _action("Pin", "Pin", binding="p", description="Toggle presentation pin without changing ownership"),
    "ui.star": _action("Star", "Star", binding="*", description="Toggle star independently of pin"),
    "ui.open": _action("Open", "Open", binding="ENTER", description="Open content read-only; never implicitly enter live mode"),
    "ui.live": _action("Live", "Open", "window", "i", "Explicit input forwarding to selected window"),
    "ui.edit": _action("Edit", "Edit", binding="e", description="Edit or explicitly reassociate selected item"),
    "ui.delete": _action("Delete / Close", "Delete", binding="x", description="Confirm destructive operation", confirm=True),
    "ui.save_context": _action("Save context", "New", "context", "c", "Save context with explicit source and project association"),
    "ui.send": _action("Send prompt", "Send", "window", "a", "Multiline prompt composer with explicit destination"),
    "ui.queue": _action("Queue prompt", "Queue", "window", "A", "Queue a prompt only where daemon supports it"),
    "ui.start": _action("Start isolated server", "Start", binding="S", description="Explicitly start the selected isolated server"),
    "ui.refresh": _action("Refresh", "Refresh", binding="r", description="Resynchronize the shared inventory"),
    "ui.settings": _action("Settings", "Edit", binding=",", description="Edit stored settings, defaults and effective bindings"),
    "ui.new_terminal": _action("New Terminal", "New", "window", "nt", "Quick terminal with the same defaults as its form"),
    "ui.new_agent": _action("New Agent", "New", "window", "na", "Quick agent with supported harness defaults"),
    "ui.new_vim": _action("New Vim", "New", "window", "nv", "Quick managed Vim terminal"),
    "ui.form_terminal": _action("New Terminal form", "New", "window", "Nt", "Review project, placement and directory before launch"),
    "ui.form_agent": _action("New Agent form", "New", "window", "Na", "Review supported agent options before launch"),
    "ui.form_vim": _action("New Vim form", "New", "window", "Nv", "Review managed Vim launch settings"),
    "ui.new_project": _action("New Project", "New", "project", "np", "Create an explicit project"),
    "ui.new_context": _action("New Context", "New", "context", "nc", "Save persistent project context"),
    "ui.new_session": _action("New Session", "New", "session", "ns", "Create an empty session on the isolated server"),
    "ui.operations": _action("TUIOS operations", "Manage", binding="o", description="Discover native verbs; explicit parameters and confirmation"),
}


def _fold(value):
    return unicodedata.normalize("NFKC", str(value)).casefold()


def fuzzy_match(query, text):
    """Match every whitespace-separated query token as a Unicode subsequence."""
    haystack = _fold(text)
    for token in _fold(query).split():
        position = 0
        for character in token:
            found = haystack.find(character, position)
            if found < 0:
                return False
            position = found + 1
    return True


def fuzzy_score(query, text):
    """Ascending rank: exact, prefix, substring, then compact subsequence.

    None denotes no match; callers can retain their original order for ties.
    """
    needle, haystack = _fold(query).strip(), _fold(text)
    if not needle:
        return (0, 0, 0)
    if needle == haystack:
        return (0, 0, 0)
    if haystack.startswith(needle):
        return (1, len(haystack) - len(needle), 0)
    at = haystack.find(needle)
    if at >= 0:
        return (2, at, len(haystack))
    if not fuzzy_match(needle, haystack):
        return None
    distance, start = 0, len(haystack)
    for token in needle.split():
        position, first = 0, None
        for character in token:
            position = haystack.find(character, position)
            if first is None:
                first = position
                start = min(start, position)
            position += 1
        distance += position - (first or 0) - len(token)
    return (3, distance, start)


def ranked_choices(query, choices):
    """Stable fuzzy completion with exact/prefix matches ahead of weak matches."""
    ranked = [(fuzzy_score(query, choice), choice) for choice in choices]
    return [choice for score, choice in sorted(
        (item for item in ranked if item[0] is not None), key=lambda item: item[0])]


def field_value(record, name, default=None):
    """Read outer or dynamic fields, including explicit dotted nested paths."""
    if name in record:
        return record[name]
    fields = record.get("fields") or {}
    if name in fields:
        return fields[name]
    parts = name.split(".")
    value = record if parts[0] in record else fields
    for part in parts:
        if not isinstance(value, Mapping) or part not in value:
            return default
        value = value[part]
    return value


def _values(value):
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _token(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return _fold(value)


def _matches(value, allowed):
    allowed = _values(allowed)
    if not allowed:
        return True
    wanted = {_token(item) for item in allowed}
    actual = _values(value)
    if not actual:
        actual = [None]
    return any(_token(item) in wanted for item in actual)


def _matches_field(record, name, allowed):
    if name == "project_id":
        allowed = [None if isinstance(value, str) and value.casefold() == "no-project" else value
                   for value in _values(allowed)]
    return _matches(field_value(record, name), allowed)


def _search_parts(value):
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _search_parts(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _search_parts(item)
    elif value is not None:
        yield str(value)


def _sort_key(value):
    if value is None:
        return (5, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return (5, "")
        return (1, value)
    if isinstance(value, str):
        return (2, _fold(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return (3, tuple(sorted((_sort_key(item) for item in value))))
    if isinstance(value, Mapping):
        return (4, tuple(sorted((str(key), _sort_key(item)) for key, item in value.items())))
    return (6, str(value))


def _sort_fields(sort, descending):
    if isinstance(sort, Mapping):
        return [(sort.get("field", "label"), sort.get("direction") in ("descending", "desc")
                 or bool(sort.get("descending", False)))]
    if isinstance(sort, str):
        return [(sort.lstrip("-"), descending or sort.startswith("-"))] if sort else []
    result = []
    for entry in sort or ():
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            direction = entry[1]
            result.append((entry[0], direction is True or direction in ("desc", "descending")))
        else:
            result.extend(_sort_fields(entry, descending))
    return result


def _in_scope(record, groups, scope):
    if not scope:
        return True
    if isinstance(scope, Mapping):
        return all(_matches_field(record, name, allowed) for name, allowed in scope.items())
    if not groups:
        return True
    # A scalar scopes the outer group; a tuple/list scopes a complete group path.
    for path in _values(scope):
        if isinstance(path, (list, tuple)):
            if len(path) <= len(groups) and all(
                    _matches_field(record, name, [value]) for name, value in zip(groups, path)):
                return True
        elif _matches_field(record, groups[0], [path]):
            return True
    return False


def filter_records(records, query="", filters=None, sort="label", descending=False,
                   group_by=None, group_scope=None):
    """Return original records in stable filtered/sorted/grouped order.

    Filters are AND between fields and OR within each field's allowed values.
    Empty filter selections are inactive; [None] selects absence. Dynamic and
    dotted fields work identically to declared ones. Scope may map fields to
    allowed values, list outer group values, or list multi-field group paths.
    Missing sort values stay last in either direction. Group order is field
    order then natural typed ascending value order; ties retain input order.
    """
    groups = [group_by] if isinstance(group_by, str) else list(group_by or ())
    selected = []
    for record in records:
        if query and not fuzzy_match(query, " ".join(_search_parts(record))):
            continue
        if filters and not all(_matches_field(record, field, allowed)
                               for field, allowed in filters.items()):
            continue
        if not _in_scope(record, groups, group_scope):
            continue
        selected.append(record)
    for name, reverse in reversed(_sort_fields(sort, descending)):
        present, missing = [], []
        for record in selected:
            value = field_value(record, name)
            absent = value is None or (isinstance(value, float) and math.isnan(value))
            (missing if absent else present).append(record)
        present.sort(key=lambda record: _sort_key(field_value(record, name)), reverse=reverse)
        selected = present + missing
    if groups:
        selected.sort(key=lambda record: tuple(_sort_key(field_value(record, name)) for name in groups))
    return selected


def filter_options(records, concept=None):
    """Derive filter choices from definitions plus observed dynamic values.

    Declared freeform content is omitted from menus, but remains explicitly
    filterable through filter_records. New concept/field definitions need no
    changes to this function. The input records and definitions stay untouched.
    """
    records = list(records)
    if isinstance(concept, str):
        definition = CONCEPTS.get(concept, {})
    else:
        definition = concept or {}
    definitions = dict(definition.get("fields", {}))
    if not definition:
        for kind in {record.get("kind") for record in records}:
            definitions.update(CONCEPTS.get(kind, {}).get("fields", {}))
    names = set(definitions)
    for record in records:
        names.update(record.get("fields") or {})
        names.update(("kind", "project_id", "starred", "pinned"))
    result = {}
    for name in sorted(names):
        metadata = definitions.get(name)
        if metadata and not (metadata.get("extensible") or metadata.get("choices")
                             or metadata.get("type") in ("choice", "multiselect", "boolean", "project")):
            continue
        values = {}
        for value in (metadata or {}).get("choices", ()):
            values.setdefault(_token(value), value)
        for record in records:
            for value in _values(field_value(record, name)):
                if isinstance(value, Mapping):
                    continue
                values.setdefault(_token(value), value)
        result[name] = sorted(values.values(), key=_sort_key)
    return result


def effective_actions(bindings=None, mode="normal"):
    """Return shared action metadata with remaps, rejecting mode-local conflicts."""
    bindings = bindings or {}
    unknown = set(bindings).difference(ACTIONS)
    if unknown:
        raise ValueError("Unknown action binding: " + ", ".join(sorted(unknown)))
    used, result = {}, {}
    for name, metadata in ACTIONS.items():
        if mode not in metadata.get("modes", ("normal",)):
            continue
        item = dict(metadata)
        item["binding"] = bindings.get(name, metadata.get("binding", ""))
        binding = item["binding"]
        if not isinstance(binding, str):
            raise ValueError(f"Binding for {name} must be text")
        if binding:
            if binding in used:
                raise ValueError(f"Binding {binding!r} conflicts between {used[binding]} and {name}")
            for other, other_name in used.items():
                if binding.startswith(other) or other.startswith(binding):
                    raise ValueError(f"Binding prefix conflicts between {other_name} and {name}")
            used[binding] = name
        result[name] = item
    return result
