"""SQLite job/result state. No Base64, credentials, or signed download URLs."""
import json
import sqlite3
import time

from .models import VideoError

TERMINAL = {"COMPLETE", "PARTIAL", "FAILED", "CANCELLED"}


class JobStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, quota_user TEXT NOT NULL,
                fingerprint TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                state TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', seconds REAL NOT NULL DEFAULT 0,
                reply_owner TEXT NOT NULL DEFAULT 'tool', cache_key TEXT NOT NULL DEFAULT '',
                attachment_ref TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, fingerprint);
            CREATE TABLE IF NOT EXISTS daily_usage (
                day TEXT NOT NULL, quota_user TEXT NOT NULL, jobs INTEGER NOT NULL,
                seconds REAL NOT NULL DEFAULT 0, PRIMARY KEY(day, quota_user));
        """)
        rows = self.db.execute("SELECT id FROM jobs WHERE state NOT IN ('COMPLETE','PARTIAL','FAILED','CANCELLED')").fetchall()
        for row in rows:
            self.update(row["id"], "FAILED", {"error": "INTERRUPTED_NO_AUTO_RESEND"})

    @staticmethod
    def owner(caller):
        return json.dumps(caller.scope, separators=(",", ":"))

    @staticmethod
    def quota_user(caller):
        # Daily per-user budgets span sessions and bot instances.
        return caller.user

    def update(self, job_id, state, data=None, **fields):
        allowed = {"seconds", "cache_key", "reply_owner", "attachment_ref"}
        if fields.keys() - allowed:
            raise ValueError("invalid store field")
        values = {"state": state, **fields}
        if data is not None:
            values["data"] = json.dumps(data, ensure_ascii=False, allow_nan=False)
        self.db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                        [*values.values(), job_id])
        self.db.commit()

    def authorized(self, job_id, caller):
        row = self.db.execute("SELECT * FROM jobs WHERE id=? AND owner=? AND expires>?",
                              (job_id, self.owner(caller), time.time())).fetchone()
        if row is None:
            raise VideoError("JOB_UNAVAILABLE")
        return dict(row)

    def snapshot(self, job_id, caller):
        row = self.authorized(job_id, caller)
        return {"job_id": row["id"], "status": row["state"], "created_at": row["created"],
                "expires_at": row["expires"], **json.loads(row["data"])}

    def duplicate(self, caller, fingerprint):
        return self.db.execute("SELECT id FROM jobs WHERE owner=? AND fingerprint=? AND expires>? ORDER BY created DESC LIMIT 1",
                               (self.owner(caller), fingerprint, time.time())).fetchone()

    def cached(self, caller, cache_key):
        row = self.db.execute("SELECT data FROM jobs WHERE owner=? AND cache_key=? AND state='COMPLETE' AND expires>? LIMIT 1",
                              (self.owner(caller), cache_key, time.time())).fetchone()
        return json.loads(row["data"]) if row else None

    @staticmethod
    def day():
        # UTC avoids host timezone changes causing quota resets.
        return time.strftime("%Y-%m-%d", time.gmtime())

    def reserve(self, job_id, caller, fingerprint, limits):
        day, user = self.day(), self.quota_user(caller)
        usage = self.db.execute("SELECT jobs,seconds FROM daily_usage WHERE day=? AND quota_user=?", (day, user)).fetchone()
        total = self.db.execute("SELECT COALESCE(SUM(jobs),0),COALESCE(SUM(seconds),0) FROM daily_usage WHERE day=?", (day,)).fetchone()
        if (usage and (usage[0] >= limits.daily_jobs_per_user or usage[1] >= limits.daily_seconds_per_user)
                or total[0] >= limits.daily_total_jobs or total[1] >= limits.daily_total_seconds):
            raise VideoError("DAILY_BUDGET_EXCEEDED")
        now = time.time()
        with self.db:
            self.db.execute("INSERT INTO daily_usage(day,quota_user,jobs) VALUES(?,?,1) ON CONFLICT(day,quota_user) DO UPDATE SET jobs=jobs+1", (day, user))
            self.db.execute("INSERT INTO jobs(id,owner,quota_user,fingerprint,created,expires,state) VALUES(?,?,?,?,?,?,'QUEUED')",
                            (job_id, self.owner(caller), user, fingerprint, now, now + limits.result_ttl))

    def reserve_seconds(self, job_id, caller, seconds, limits):
        day, user = self.day(), self.quota_user(caller)
        usage = self.db.execute("SELECT seconds FROM daily_usage WHERE day=? AND quota_user=?", (day, user)).fetchone()
        total = self.db.execute("SELECT COALESCE(SUM(seconds),0) FROM daily_usage WHERE day=?", (day,)).fetchone()[0]
        if (usage[0] if usage else 0) + seconds > limits.daily_seconds_per_user or total + seconds > limits.daily_total_seconds:
            raise VideoError("DAILY_DURATION_EXCEEDED")
        with self.db:
            self.db.execute("INSERT INTO daily_usage VALUES(?,?,0,?) ON CONFLICT(day,quota_user) DO UPDATE SET seconds=seconds+excluded.seconds", (day, user, seconds))
            self.db.execute("UPDATE jobs SET seconds=? WHERE id=?", (seconds, job_id))

    def cleanup(self):
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE expires<=? AND state IN ('COMPLETE','PARTIAL','FAILED','CANCELLED')", (time.time(),))
            self.db.execute("DELETE FROM daily_usage WHERE day < date('now','-7 days')")
