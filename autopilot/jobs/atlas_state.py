"""Durable Atlas state in the state bucket: one writer at a time, a database that is checked
before use and restored from an earlier generation when it is missing or corrupt.

Objects (prefix atlas/):
  atlas/atlas.db                   the store. Versioned bucket: every upload keeps the previous
                                   generation for 14 days (infra/main.tf lifecycle).
  atlas/lock.json                  the writer lock: {holder, execution, acquired, expires}.
  atlas/snapshots/atlas-<day>.db   one copy per UTC day (kept 45 days), a second line of restore.
  atlas/quarantine/<stamp>.db      a store the publish guard refused (kept 30 days), never used.
  atlas/flags/<name>               one-shot owner switches (allow-shrink, allow-empty-start).

Concurrency: the lock is created with "only if absent" and replaced only with "only if
unchanged" (GCS generation preconditions), and the database itself is uploaded only if its
generation is still the one this run downloaded. Two runs can therefore never both write it:
the loser gets a Conflict and its changes are dropped with an alert, not merged.
"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import time

from common import alert, now

DB_KEY = "atlas/atlas.db"
LOCK_KEY = "atlas/lock.json"
SNAPSHOT_PREFIX = "atlas/snapshots/"
QUARANTINE_PREFIX = "atlas/quarantine/"
FLAG_PREFIX = "atlas/flags/"
REQUIRED_TABLES = ("reports", "patterns", "mentions", "checks")


class Conflict(RuntimeError):
    """A generation precondition failed: someone else wrote the object first."""


class StateError(RuntimeError):
    """The state cannot be used safely; the run stops (fail closed)."""


# ---- generation-aware object access ----------------------------------------------

class Objects:
    """Generation-aware reads and writes on the Store's bucket (or its local directory).

    Local mode keeps every generation under <dir>/.generations/<key>/<n> so tests and dry runs
    exercise the same restore path as the bucket."""

    def __init__(self, store):
        self.store = store
        self.local = store.local

    # local helpers
    def _gdir(self, key):
        return os.path.join(self.local, ".generations", key)

    def _gens_local(self, key):
        d = self._gdir(key)
        return sorted((int(n) for n in os.listdir(d)), reverse=True) if os.path.isdir(d) else []

    def _live_local(self, key):
        p = os.path.join(self.local, key)
        if not os.path.exists(p):
            return 0
        gens = self._gens_local(key)
        return gens[0] if gens else 0

    def generation(self, key):
        """Current generation, 0 when the object does not exist."""
        if self.local:
            return self._live_local(key)
        blob = self.store._b().get_blob(key)
        return blob.generation if blob else 0

    def read(self, key):
        """-> (bytes or None, generation)."""
        if self.local:
            gen = self._live_local(key)
            return (open(os.path.join(self.local, key), "rb").read(), gen) if gen else (None, 0)
        from google.api_core import exceptions as gexc
        blob = self.store._b().get_blob(key)
        if blob is None:
            return None, 0
        try:
            return blob.download_as_bytes(if_generation_match=blob.generation), blob.generation
        except (gexc.NotFound, gexc.PreconditionFailed):
            return self.read(key)  # replaced between the two calls: read the new one

    def write(self, key, data, if_generation):
        """Write only if the live generation is `if_generation` (0 = must not exist). -> new gen."""
        if self.local:
            if self._live_local(key) != if_generation:
                raise Conflict(f"{key}: generation is not {if_generation}")
            gen = (self._gens_local(key) or [0])[0] + 1
            os.makedirs(self._gdir(key), exist_ok=True)
            open(os.path.join(self._gdir(key), str(gen)), "wb").write(data)
            p = os.path.join(self.local, key)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p + ".tmp", "wb").write(data)
            os.replace(p + ".tmp", p)
            return gen
        from google.api_core import exceptions as gexc
        blob = self.store._b().blob(key)
        try:
            blob.upload_from_string(data, if_generation_match=if_generation)
        except gexc.PreconditionFailed as exc:
            raise Conflict(f"{key}: generation is not {if_generation}") from exc
        return blob.generation

    def write_file(self, key, path, if_generation):
        if self.local:
            return self.write(key, open(path, "rb").read(), if_generation)
        from google.api_core import exceptions as gexc
        blob = self.store._b().blob(key)
        try:
            blob.upload_from_filename(path, if_generation_match=if_generation)
        except gexc.PreconditionFailed as exc:
            raise Conflict(f"{key}: generation is not {if_generation}") from exc
        return blob.generation

    def delete(self, key, if_generation=None):
        if self.local:
            if if_generation is not None and self._live_local(key) != if_generation:
                raise Conflict(f"{key}: generation is not {if_generation}")
            p = os.path.join(self.local, key)
            if os.path.exists(p):
                os.remove(p)
            return
        from google.api_core import exceptions as gexc
        try:
            self.store._b().blob(key).delete(if_generation_match=if_generation)
        except gexc.PreconditionFailed as exc:
            raise Conflict(f"{key}: generation is not {if_generation}") from exc
        except gexc.NotFound:
            pass

    def generations(self, key):
        """Every kept generation of `key`, live and replaced, newest first."""
        if self.local:
            return self._gens_local(key)
        blobs = self.store._b().list_blobs(prefix=key, versions=True)
        return sorted((b.generation for b in blobs if b.name == key), reverse=True)

    def read_generation_to(self, key, gen, path):
        if self.local:
            shutil.copyfile(os.path.join(self._gdir(key), str(gen)), path)
            return
        self.store._b().blob(key, generation=gen).download_to_filename(path)


# ---- the writer lock ----------------------------------------------------------------

def execution_id():
    return os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{os.getpid()}"


def _iso(t):
    return t.isoformat().replace("+00:00", "Z")


def _parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


class Lock:
    def __init__(self, objects, holder, ttl_s, at=None):
        self.o, self.holder, self.ttl_s = objects, holder, ttl_s
        self.gen = 0
        self.at = at or now

    def try_acquire(self):
        """-> None when acquired, else the current holder's record."""
        raw, gen = self.o.read(LOCK_KEY)
        t = self.at()
        doc = None
        if raw:
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = {}
            mine = doc.get("execution") == execution_id()
            expired = "expires" not in doc or _parse(doc["expires"]) <= t
            if not (mine or expired):
                return doc
            if expired and not mine:
                alert("atlas_lock_broken", "WARNING",
                      f"took over an expired lock held by {doc.get('holder')} ({doc.get('execution')})")
        mine_doc = {"holder": self.holder, "execution": execution_id(), "acquired": _iso(t),
                    "expires": _iso(t + dt.timedelta(seconds=self.ttl_s))}
        try:
            self.gen = self.o.write(LOCK_KEY, json.dumps(mine_doc).encode(), gen)
        except Conflict:
            raw2, _ = self.o.read(LOCK_KEY)
            return json.loads(raw2) if raw2 else {"holder": "unknown"}
        return None

    def acquire(self, wait_s=0, poll_s=30, sleep=time.sleep):
        """Acquire within wait_s seconds. -> None when acquired, else the holder's record."""
        deadline = time.monotonic() + wait_s
        while True:
            held = self.try_acquire()
            if held is None or time.monotonic() >= deadline:
                return held
            sleep(poll_s)

    def still_held(self):
        """True while the lock object is still the one this holder wrote and has not expired."""
        if not self.gen:
            return False
        raw, gen = self.o.read(LOCK_KEY)
        if not raw or gen != self.gen:
            return False
        try:
            return _parse(json.loads(raw)["expires"]) > self.at()
        except (ValueError, KeyError):
            return False

    def release(self):
        if not self.gen:
            return
        try:
            self.o.delete(LOCK_KEY, if_generation=self.gen)
        except Conflict:
            alert("atlas_lock_lost", "WARNING", f"{self.holder}: the lock was taken over before release")
        finally:
            self.gen = 0


# ---- the database -------------------------------------------------------------------

def db_problem(path):
    """None when the file is a usable Atlas store, else a short reason."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return "missing or empty"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                return f"quick_check: {row[0] if row else 'no answer'}"
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in REQUIRED_TABLES if t not in have]
            if missing:
                return "missing tables: " + ", ".join(missing)
            if conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0] == 0:
                return "no entries"
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return f"not a database: {exc}"
    return None


def flag(store, name):
    return store.exists(FLAG_PREFIX + name)


def clear_flag(store, name):
    Objects(store).delete(FLAG_PREFIX + name)


def fetch_db(store, path, job):
    """Download the store to `path`, restoring the newest sound generation when the live one
    is missing or corrupt. -> generation the run must upload against (0 = new object)."""
    o = Objects(store)
    raw, gen = o.read(DB_KEY)
    if raw is not None:
        open(path, "wb").write(raw)
        problem = db_problem(path)
        if problem is None:
            return gen
    else:
        problem = "missing"
    for g in o.generations(DB_KEY):
        if g == gen:
            continue
        o.read_generation_to(DB_KEY, g, path)
        if db_problem(path) is None:
            gen = o.write_file(DB_KEY, path, gen)  # the restored copy becomes the live one
            alert("atlas_db_restored", "WARNING", f"atlas.db was {problem}; restored generation {g}", job)
            return gen
    snaps = [k for k in list_keys(store, SNAPSHOT_PREFIX)]
    for key in sorted(snaps, reverse=True):
        data, _ = o.read(key)
        open(path, "wb").write(data or b"")
        if db_problem(path) is None:
            gen = o.write_file(DB_KEY, path, gen)
            alert("atlas_db_restored", "WARNING", f"atlas.db was {problem}; restored {key}", job)
            return gen
    if os.path.exists(path):
        os.remove(path)
    if flag(store, "allow-empty-start"):
        alert("atlas_db_empty_start", "WARNING", f"atlas.db was {problem}; starting an empty store", job)
        return gen
    alert("atlas_db_unusable", "ERROR",
          f"atlas.db is {problem} and no sound generation or snapshot exists; seed it "
          "(RUNBOOK: Atlas) or set atlas/flags/allow-empty-start", job)
    raise StateError(f"atlas.db is {problem} and nothing sound to restore")


def save_db(store, path, gen):
    """Upload only if nobody replaced the store since this run read it. -> new generation."""
    problem = db_problem(path)
    if problem:
        raise StateError(f"refusing to upload an unusable atlas.db ({problem})")
    return Objects(store).write_file(DB_KEY, path, gen)


def snapshot_daily(store, path, at=None):
    key = f"{SNAPSHOT_PREFIX}atlas-{(at or now()).strftime('%Y%m%d')}.db"
    if store.exists(key):
        return None
    try:
        Objects(store).write_file(key, path, 0)
    except Conflict:
        return None
    return key


def quarantine(store, path, reason, at=None):
    key = f"{QUARANTINE_PREFIX}{(at or now()).strftime('%Y%m%dT%H%M%SZ')}.db"
    Objects(store).write_file(key, path, 0)
    store.put_json(key + ".json", {"reason": reason})
    return key


def list_keys(store, prefix):
    if store.local:
        base = os.path.join(store.local, prefix)
        if not os.path.isdir(base):
            return []
        return [prefix + n for n in sorted(os.listdir(base)) if not n.endswith((".tmp", ".lock"))]
    return [b.name for b in store._b().list_blobs(prefix=prefix)]
