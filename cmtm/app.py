"""Composition and ownership of application services."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from . import concepts
from .daemon import Daemon
from .store import Store


def default_database() -> Path:
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "cmtm" / "state.sqlite3"


class Services:
    """Own one store and one isolated daemon client; startup is the CLI's responsibility."""

    def __init__(self, store: Store, daemon: Daemon) -> None:
        self.store = store
        self.daemon = daemon
        self.concepts = concepts
        self.settings: dict[str, Any] = store.settings()
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.store.close()
        finally:
            self.daemon.close()

    def __enter__(self) -> Services:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_services(server: str = "clean-mtm", database: str | Path | None = None) -> Services:
    daemon = Daemon(server=server)
    try:
        path = Path(database).expanduser() if database is not None else default_database()
        path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path)
    except BaseException:
        daemon.close()
        raise
    try:
        return Services(store, daemon)
    except BaseException:
        try:
            store.close()
        finally:
            daemon.close()
        raise
