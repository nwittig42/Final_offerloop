"""A tiny in-memory Firestore for route/service tests.

Supports what the onboarding-v2 send and entitlement code uses: nested
collection/document paths, get / set(merge) / update / create / delete,
dotted-path-free updates with DELETE_FIELD, and the two concurrency
primitives: create() raising Conflict when the doc exists, and
write_option(last_update_time=...) preconditions raising FailedPrecondition
when the doc changed since it was read. update_time is a per-doc counter.
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace

from firebase_admin import firestore
from google.api_core import exceptions as gexc

_clock = itertools.count(1)


class Snap:
    def __init__(self, data, update_time):
        self._data = data
        self.exists = data is not None
        self.update_time = update_time

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class Doc:
    def __init__(self, db, path):
        self.db, self.path = db, path
        self.id = path.rsplit("/", 1)[-1]

    def collection(self, name):
        return Col(self.db, f"{self.path}/{name}")

    def _check(self, option):
        if option is not None:
            cur = self.db.times.get(self.path)
            if cur is None:
                raise gexc.NotFound(self.path)
            if cur != option.last_update_time:
                raise gexc.FailedPrecondition(self.path)

    def _stamp(self):
        t = next(_clock)
        self.db.times[self.path] = t
        self.db.writes.append(self.path)
        return SimpleNamespace(update_time=t)

    def get(self, **_):
        return Snap(self.db.store.get(self.path), self.db.times.get(self.path))

    def create(self, data):
        if self.path in self.db.store:
            raise gexc.Conflict(self.path)
        self.db.store[self.path] = dict(data)
        return self._stamp()

    def set(self, data, merge=False):
        if merge and self.path in self.db.store:
            self.db.store[self.path] = _merge(self.db.store[self.path], data)
        else:
            self.db.store[self.path] = {k: v for k, v in data.items() if v is not firestore.DELETE_FIELD}
        return self._stamp()

    def update(self, data, option=None):
        if self.path not in self.db.store:
            raise gexc.NotFound(self.path)
        self._check(option)
        cur = self.db.store[self.path]
        for k, v in data.items():
            if v is firestore.DELETE_FIELD:
                cur.pop(k, None)
            else:
                cur[k] = v
        return self._stamp()

    def delete(self, option=None):
        self._check(option)
        self.db.store.pop(self.path, None)
        self.db.times.pop(self.path, None)
        self.db.writes.append(self.path)


def _merge(base, data):
    out = dict(base)
    for k, v in data.items():
        if v is firestore.DELETE_FIELD:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Col:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def document(self, doc_id):
        return Doc(self.db, f"{self.path}/{doc_id}")


class FakeDB:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.times = {p: next(_clock) for p in self.store}
        self.writes: list[str] = []

    def collection(self, name):
        return Col(self, name)

    def write_option(self, last_update_time=None):
        return SimpleNamespace(last_update_time=last_update_time)

    def doc(self, path):
        return self.store.get(path)
