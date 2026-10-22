"""Native, local-only filesystem dialogs. No manual-entry fallback."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


class PickerError(RuntimeError):
    pass


def _execute(args: list[str], *, env=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired as exc:
        raise PickerError("Native picker timed out after 120 seconds") from exc
    except OSError as exc:
        raise PickerError(f"Cannot open native picker: {exc}") from exc


def pick_path(kind='file', multiple=False, initial='', title='Choose path', host='local'):
    """Return selected path(s), None on cancel; errors never imply cancellation."""
    if host not in ('', 'local', 'localhost'):
        raise PickerError(f"Native picker is local only; {host!r} requires remote path entry")
    if kind not in ('file', 'directory', 'save'):
        raise PickerError(f"Unknown picker kind: {kind}")
    if multiple and kind == 'save':
        raise PickerError('Save dialogs accept one destination at a time')
    if sys.platform == 'darwin':
        # Arguments, not interpolated AppleScript, carry all user-controlled text.
        command = {'file': 'choose file', 'directory': 'choose folder', 'save': 'choose file name'}[kind]
        options = ' with multiple selections allowed' if multiple else ''
        script = f'''on run argv
set dialogTitle to item 1 of argv
set initialPath to item 2 of argv
try
if initialPath is not "" then
set chosen to {command} with prompt dialogTitle default location (POSIX file initialPath){options}
else
set chosen to {command} with prompt dialogTitle{options}
end if
if class of chosen is not list then set chosen to {{chosen}}
set resultText to ""
repeat with p in chosen
set resultText to resultText & POSIX path of p & linefeed
end repeat
return resultText
on error errText number errNumber
if errNumber is -128 then return "__CMTM_CANCEL__"
error errText number errNumber
end try
end run'''
        initial = os.path.expanduser(initial)
        if initial and not os.path.isdir(initial):
            initial = os.path.dirname(initial)
        result = _execute(['osascript', '-e', script, title, initial])
        if result.stdout.strip() == '__CMTM_CANCEL__':
            return None
    elif sys.platform.startswith('linux'):
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            raise PickerError('Native picker needs a graphical Linux desktop (DISPLAY or WAYLAND_DISPLAY)')
        if shutil.which('zenity'):
            args = ['zenity', '--file-selection', '--title', title]
            if kind == 'directory':
                args.append('--directory')
            elif kind == 'save':
                args.extend(['--save', '--confirm-overwrite'])
            if multiple:
                args.extend(['--multiple', '--separator=\n'])
            if initial:
                args.extend(['--filename', os.path.expanduser(initial)])
        elif shutil.which('kdialog'):
            operation = {'file': '--getopenfilename', 'directory': '--getexistingdirectory', 'save': '--getsavefilename'}[kind]
            args = ['kdialog', '--title', title, operation, os.path.expanduser(initial) or os.getcwd()]
            if multiple:
                if kind == 'directory':
                    raise PickerError('KDialog does not support multiple directory selection; install Zenity')
                args.extend(['--multiple', '--separate-output'])
        else:
            raise PickerError('No supported native Linux picker found; install Zenity or KDialog')
        result = _execute(args)
        if result.returncode == 1:
            if result.stderr.strip():
                raise PickerError(result.stderr.strip())
            return None
    else:
        raise PickerError(f'Native filesystem picker is unsupported on {sys.platform}')
    if result.returncode:
        raise PickerError(result.stderr.strip() or f'Native picker failed (exit {result.returncode})')
    paths = result.stdout.rstrip('\n').splitlines()
    if not paths:
        raise PickerError('Native picker returned no path')
    # Native dialog APIs expose newline-separated filenames. Refuse ambiguity in
    # single-select dialogs rather than selecting a different filesystem object.
    if not multiple and len(paths) != 1:
        raise PickerError('Native picker returned an ambiguous newline-containing path')
    return paths if multiple else paths[0]
