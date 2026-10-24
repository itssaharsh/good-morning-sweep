"""One SQLite file beside the copied pictures holds everything the sweep learns, so a run can stop and resume."""
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  phone_path TEXT UNIQUE NOT NULL,
  local_path TEXT NOT NULL,
  folder TEXT NOT NULL,          -- wa_images, wa_sent, wa_video, screenshots, camera
  bytes INTEGER NOT NULL,
  mtime REAL,
  sha1 TEXT,
  dhash TEXT,
  width INTEGER,
  height INTEGER
);
CREATE TABLE IF NOT EXISTS verdicts (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  kind TEXT,                     -- what the model says the picture is
  words TEXT,                    -- the words it read on the picture
  people INTEGER,                -- 1 when it says this is a real photo of real people
  screen TEXT,                   -- screenshot type (otp, payment, delivery, chat, other)
  raw TEXT,                      -- the model's answer as it came
  ms INTEGER,
  error TEXT,                    -- set when every attempt failed; the file is then kept
  model TEXT,
  at REAL
);
CREATE TABLE IF NOT EXISTS checks (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  blur REAL,                     -- variance of the Laplacian: low means blurry
  dark REAL                      -- mean brightness 0-255
);
CREATE TABLE IF NOT EXISTS suggestions (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  bucket TEXT NOT NULL,          -- forwards, copies, similar, blurry, screenshots
  reason TEXT NOT NULL,          -- written for the person reviewing
  rule TEXT NOT NULL,            -- which guard rule decided it
  keep_id INTEGER,               -- for copies: the file that is kept
  preselected INTEGER NOT NULL   -- 1: ticked for deletion when the review opens
);
CREATE TABLE IF NOT EXISTS decisions (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  action TEXT NOT NULL CHECK (action IN ('keep','delete')),
  at REAL
);
CREATE TABLE IF NOT EXISTS deletions (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  ok INTEGER NOT NULL,
  message TEXT,
  at REAL
);
CREATE TABLE IF NOT EXISTS labels (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  label TEXT NOT NULL CHECK (label IN ('junk','keep')),
  at REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS files_sha1 ON files(sha1);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    if "gone" not in cols:   # 1 when the file was missing from the phone's latest listing
        con.execute("ALTER TABLE files ADD COLUMN gone INTEGER NOT NULL DEFAULT 0")
    if "root" not in cols:   # the phone folder it was copied from
        con.execute("ALTER TABLE files ADD COLUMN root TEXT")
    con.commit()
    return con


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
    con.commit()


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def now():
    return time.time()
