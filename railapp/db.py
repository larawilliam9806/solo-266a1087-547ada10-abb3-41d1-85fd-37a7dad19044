"""SQLite access helpers and schema."""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import domain

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    downstream    TEXT PRIMARY KEY,
    columns_json  TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS days (
    day          TEXT PRIMARY KEY,
    current_rev  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    day                    TEXT NOT NULL,
    rev                    INTEGER NOT NULL,
    original_name          TEXT NOT NULL,
    original_size          INTEGER NOT NULL,
    original_sha256        TEXT NOT NULL,
    settlement_sha256      TEXT NOT NULL,
    settlement_size        INTEGER NOT NULL,
    ridership_sha256       TEXT NOT NULL,
    ridership_size         INTEGER NOT NULL,
    settlement_fingerprint TEXT NOT NULL,
    ridership_fingerprint  TEXT NOT NULL,
    settlement_version     INTEGER NOT NULL,
    ridership_version      INTEGER NOT NULL,
    settlement_columns     TEXT NOT NULL,
    ridership_columns      TEXT NOT NULL,
    row_count              INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    PRIMARY KEY (day, rev)
);

CREATE TABLE IF NOT EXISTS previews (
    id                     TEXT PRIMARY KEY,
    day                    TEXT NOT NULL,
    base_rev               INTEGER NOT NULL,
    settlement_version     INTEGER NOT NULL,
    ridership_version      INTEGER NOT NULL,
    original_name          TEXT NOT NULL,
    original_size          INTEGER NOT NULL,
    original_sha256        TEXT NOT NULL,
    result_json            TEXT NOT NULL,
    convertible            INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    consumed_at            TEXT
);

-- Single-row key/value table for feature flags and other app-level state.
CREATE TABLE IF NOT EXISTS app_state (
    key          TEXT PRIMARY KEY,
    value_json   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- One row per publication describing whether it feeds the ridership supply.
-- Rows are immutable history: when an issue day gets a new revision, the old
-- registration simply stops being the "current" one and is withdrawn by the
-- join against days.current_rev.
CREATE TABLE IF NOT EXISTS supply_registrations (
    issue_day     TEXT NOT NULL,
    rev           INTEGER NOT NULL,
    eligible      INTEGER NOT NULL,
    reason_code   TEXT,
    reason_detail TEXT,
    row_count     INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (issue_day, rev)
);

-- Detail rows of eligible registrations only.  Travel date is the lookup key
-- for the ZIP export; issue_day+rev points back at the source batch.
CREATE TABLE IF NOT EXISTS supply_rows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_day       TEXT NOT NULL,
    rev             INTEGER NOT NULL,
    travel_date     TEXT NOT NULL,
    row_no          INTEGER NOT NULL,
    train_no        TEXT NOT NULL,
    depart_station  TEXT NOT NULL,
    arrive_station  TEXT NOT NULL,
    passenger_count TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publications_day ON publications(day, rev);
CREATE INDEX IF NOT EXISTS idx_previews_expiry ON previews(expires_at);
CREATE INDEX IF NOT EXISTS idx_supply_rows_travel ON supply_rows(travel_date, issue_day, rev);
CREATE INDEX IF NOT EXISTS idx_supply_rows_source ON supply_rows(issue_day, rev);
"""

PREVIEW_TTL_SECONDS = 2 * 60 * 60

SUPPLY_FEATURE_KEY = "ridership_supply_enabled"


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn, *, immediate=False):
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn):
    conn.executescript(SCHEMA)
    now = iso(utcnow())
    for downstream in domain.DOWNSTREAM_ORDER:
        columns = domain.DEFAULT_CONTRACTS[downstream]
        conn.execute(
            "INSERT OR IGNORE INTO contracts VALUES (?, ?, ?, ?, ?)",
            (
                downstream,
                json.dumps(columns, ensure_ascii=False),
                domain.fingerprint(columns),
                1,
                now,
            ),
        )


def get_contract(conn, downstream):
    row = conn.execute(
        """SELECT downstream, columns_json, fingerprint, version, updated_at
           FROM contracts WHERE downstream = ?""",
        (downstream,),
    ).fetchone()
    if row is None:
        return None
    return {
        "downstream": row["downstream"],
        "columns": json.loads(row["columns_json"]),
        "fingerprint": row["fingerprint"],
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def get_contracts(conn):
    return {name: get_contract(conn, name) for name in domain.DOWNSTREAM_ORDER}


def update_contract(conn, downstream, columns):
    """Replace a contract and bump its version.  Never touches publications.

    A no-op edit (identical canonical columns) does not bump the version, so
    it cannot invalidate previews that are still pending confirmation.
    """
    fp = domain.fingerprint(columns)
    current = get_contract(conn, downstream)
    if current is not None and current["fingerprint"] == fp:
        return current
    conn.execute(
        """UPDATE contracts
           SET columns_json = ?, fingerprint = ?, version = version + 1, updated_at = ?
           WHERE downstream = ?""",
        (json.dumps(columns, ensure_ascii=False), fp, iso(utcnow()), downstream),
    )
    return get_contract(conn, downstream)


def day_revision(conn, day):
    row = conn.execute("SELECT current_rev FROM days WHERE day = ?", (day,)).fetchone()
    return int(row["current_rev"]) if row else 0


def get_publication(conn, day, rev):
    row = conn.execute(
        "SELECT * FROM publications WHERE day = ? AND rev = ?", (day, rev)
    ).fetchone()
    return dict(row) if row else None


def list_publications(conn, day, limit=200):
    rows = conn.execute(
        "SELECT * FROM publications WHERE day = ? ORDER BY rev DESC LIMIT ?", (day, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def list_days(conn, limit=200):
    rows = conn.execute(
        """SELECT d.day, d.current_rev, d.updated_at,
                  (SELECT count(*) FROM publications p WHERE p.day = d.day) AS revision_count
           FROM days d ORDER BY d.day DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_preview(conn, *, preview_id, day, base_rev, contract_versions,
                   original_name, original_size, original_sha256, result,
                   convertible):
    now = utcnow()
    expires = now + timedelta(seconds=PREVIEW_TTL_SECONDS)
    conn.execute(
        """INSERT INTO previews (id, day, base_rev, settlement_version,
                                 ridership_version, original_name, original_size,
                                 original_sha256, result_json, convertible,
                                 created_at, expires_at, consumed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            preview_id,
            day,
            base_rev,
            contract_versions["settlement"],
            contract_versions["ridership"],
            original_name,
            original_size,
            original_sha256,
            json.dumps(result, ensure_ascii=False),
            1 if convertible else 0,
            iso(now),
            iso(expires),
        ),
    )
    return iso(expires)


def get_preview(conn, preview_id):
    row = conn.execute("SELECT * FROM previews WHERE id = ?", (preview_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["result"] = json.loads(item.pop("result_json"))
    return item


def delete_preview(conn, preview_id):
    conn.execute("DELETE FROM previews WHERE id = ?", (preview_id,))


def purge_expired_previews(conn, now=None):
    now = now or utcnow()
    rows = conn.execute(
        "SELECT id FROM previews WHERE expires_at < ?", (iso(now),)
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        conn.executemany("DELETE FROM previews WHERE id = ?", [(i,) for i in ids])
    return ids


def wait_for_write_lock(conn, attempts=200, delay=0.05):
    """Best-effort helper used by tests to serialise around a busy database."""
    for _ in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return
        except sqlite3.OperationalError:
            time.sleep(delay)
    raise sqlite3.OperationalError("database is locked")


# ---------------------------------------------------------------------------
# Ridership supply feature flag and registrations
# ---------------------------------------------------------------------------
def get_state(conn, key):
    row = conn.execute(
        "SELECT value_json, updated_at FROM app_state WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return {"value": json.loads(row["value_json"]), "updated_at": row["updated_at"]}


def set_state(conn, key, value):
    conn.execute(
        """INSERT INTO app_state (key, value_json, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               value_json = excluded.value_json,
               updated_at = excluded.updated_at""",
        (key, json.dumps(value, ensure_ascii=False), iso(utcnow())),
    )


def supply_feature_status(conn):
    state = get_state(conn, SUPPLY_FEATURE_KEY)
    if not state or not state["value"]:
        return {"enabled": False, "enabled_at": None}
    return {"enabled": True, "enabled_at": state["updated_at"]}


def enable_supply_feature(conn):
    """Idempotently turn the feature on; the first call fixes the cutoff time."""
    current = supply_feature_status(conn)
    if current["enabled"]:
        return current
    now = iso(utcnow())
    conn.execute(
        "INSERT INTO app_state (key, value_json, updated_at) VALUES (?, ?, ?)",
        (SUPPLY_FEATURE_KEY, json.dumps(True), now),
    )
    return {"enabled": True, "enabled_at": now}


def insert_supply_registration(conn, *, issue_day, rev, eligible, reason_code,
                               reason_detail, row_count, created_at):
    conn.execute(
        """INSERT INTO supply_registrations
           (issue_day, rev, eligible, reason_code, reason_detail, row_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (issue_day, rev, 1 if eligible else 0, reason_code, reason_detail,
         row_count, created_at),
    )


def insert_supply_rows(conn, *, issue_day, rev, rows, created_at):
    conn.executemany(
        """INSERT INTO supply_rows
           (issue_day, rev, travel_date, row_no, train_no, depart_station,
            arrive_station, passenger_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (issue_day, rev, values[3], row_no, values[0], values[1],
             values[2], values[4])
            for row_no, values in enumerate(rows, start=1)
        ],
    )


def get_supply_registration(conn, issue_day, rev):
    row = conn.execute(
        "SELECT * FROM supply_registrations WHERE issue_day = ? AND rev = ?",
        (issue_day, rev),
    ).fetchone()
    return dict(row) if row else None


def current_supply_rows_for_dates(conn, travel_days):
    """Registered rows for the given travel dates from *current* revisions only.

    The join on ``days.current_rev`` is what withdraws an old issue-day
    revision's details the moment a new official revision takes over, even
    when the new revision contributes nothing.
    """
    if not travel_days:
        return []
    placeholders = ",".join("?" for _ in travel_days)
    rows = conn.execute(
        f"""SELECT sr.travel_date, sr.issue_day, sr.rev, sr.row_no,
                   sr.train_no, sr.depart_station, sr.arrive_station,
                   sr.passenger_count, g.created_at AS registered_at
              FROM supply_rows sr
              JOIN days d ON d.day = sr.issue_day
              JOIN supply_registrations g
                ON g.issue_day = sr.issue_day AND g.rev = sr.rev
             WHERE sr.travel_date IN ({placeholders})
               AND d.current_rev = sr.rev
               AND g.eligible = 1
             ORDER BY sr.travel_date, sr.issue_day, sr.rev, sr.row_no""",
        travel_days,
    ).fetchall()
    return [dict(r) for r in rows]


def current_supply_sources(conn, travel_days):
    """Current-revision registrations relevant to this window.

    Includes eligible batches whose rows touch any requested travel date, plus
    every current ineligible batch (so the manifest can explain exclusions).
    """
    if not travel_days:
        return []
    placeholders = ",".join("?" for _ in travel_days)
    rows = conn.execute(
        f"""SELECT * FROM (
                SELECT g.issue_day, g.rev, g.eligible, g.reason_code,
                       g.reason_detail, g.row_count, g.created_at,
                       p.created_at AS published_at
                  FROM supply_registrations g
                  JOIN days d ON d.day = g.issue_day
                  JOIN publications p ON p.day = g.issue_day AND p.rev = g.rev
                 WHERE g.rev = d.current_rev AND g.eligible = 0
                UNION
                SELECT g.issue_day, g.rev, g.eligible, g.reason_code,
                       g.reason_detail, g.row_count, g.created_at,
                       p.created_at AS published_at
                  FROM supply_registrations g
                  JOIN days d ON d.day = g.issue_day
                  JOIN publications p ON p.day = g.issue_day AND p.rev = g.rev
                 WHERE g.rev = d.current_rev AND g.eligible = 1
                   AND EXISTS (
                       SELECT 1 FROM supply_rows sr
                        WHERE sr.issue_day = g.issue_day AND sr.rev = g.rev
                          AND sr.travel_date IN ({placeholders})))
             ORDER BY issue_day""",
        travel_days,
    ).fetchall()
    return [dict(r) for r in rows]


def all_current_publications(conn):
    """Every issue day's current official revision, with publication metadata."""
    rows = conn.execute(
        """SELECT p.day AS issue_day, p.rev, p.created_at AS published_at
             FROM publications p
             JOIN days d ON d.day = p.day AND d.current_rev = p.rev
            ORDER BY p.day"""
    ).fetchall()
    return [dict(r) for r in rows]
