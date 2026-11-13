"""올린 기록(SQLite). 끊겨도 이어 올리고, 같은 파일을 같은 앨범에 두 번 넣지 않으려고 쓴다."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums(
  account TEXT NOT NULL, root TEXT NOT NULL, folder TEXT NOT NULL, part INTEGER NOT NULL,
  title TEXT NOT NULL, album_id TEXT NOT NULL, product_url TEXT,
  missing INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  PRIMARY KEY(account, root, folder, part));
CREATE INDEX IF NOT EXISTS albums_title ON albums(account, title);
CREATE INDEX IF NOT EXISTS albums_id ON albums(album_id);
CREATE TABLE IF NOT EXISTS items(
  account TEXT NOT NULL, root TEXT NOT NULL, folder TEXT NOT NULL,
  path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  status TEXT NOT NULL,            -- token | done | failed | failed_perm
  album_id TEXT, upload_token TEXT, token_time REAL, media_id TEXT, error TEXT,
  updated_at REAL NOT NULL,
  PRIMARY KEY(account, root, folder, path, size, mtime_ns));
CREATE INDEX IF NOT EXISTS items_album ON items(album_id, status);
-- 구글 포토에 손으로 이미 올린 폴더(사용자가 '직접 올림'으로 표시). 구글 계정마다, 최상위 폴더(normcase)마다.
-- 열쇠 파일(client)과는 무관하다. 손으로 올린 것은 계정에 속하기 때문이다.
CREATE TABLE IF NOT EXISTS manual(
  google_id TEXT NOT NULL, root TEXT NOT NULL, folder TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(google_id, root, folder));
"""

_UPSERT_KEY = "ON CONFLICT(account, root, folder, path, size, mtime_ns) DO UPDATE SET "


class Store:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                   timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._db.close()

    def _rows(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _write_many(self, sql, rows):
        if not rows:
            return
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(sql, rows)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # ── 앨범 ──
    def albums_for_folder(self, account, root, folder):
        rows = self._rows("SELECT part, title, album_id, product_url, missing FROM albums "
                          "WHERE account=? AND root=? AND folder=? ORDER BY part",
                          (account, root, folder))
        return [dict(part=r[0], title=r[1], album_id=r[2], product_url=r[3], missing=bool(r[4]))
                for r in rows]

    def add_album(self, account, root, folder, part, title, album_id, product_url):
        self._write_many("INSERT OR REPLACE INTO albums VALUES(?,?,?,?,?,?,?,0,?)",
                         [(account, root, folder, part, title, album_id, product_url, time.time())])

    def mark_album_missing(self, album_id):
        self._write_many("UPDATE albums SET missing=1 WHERE album_id=?", [(album_id,)])

    def forget_folder(self, account, root, folder):
        """구글에서 지워진 앨범을 잊어 다음 올리기 때 새로 만들게 한다. 그 폴더의 올린 기록도 지운다."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("DELETE FROM items WHERE account=? AND root=? AND folder=?",
                                 (account, root, folder))
                self._db.execute("DELETE FROM albums WHERE account=? AND root=? AND folder=?",
                                 (account, root, folder))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def titles_in_use(self, account):
        return {r[0] for r in self._rows("SELECT title FROM albums WHERE account=?", (account,))}

    def count_done(self, album_id):
        return self._rows("SELECT COUNT(*) FROM items WHERE album_id=? AND status='done'",
                          (album_id,))[0][0]

    # ── 파일 ──
    def item_map(self, account, root, folder):
        rows = self._rows("SELECT path, size, mtime_ns, status, album_id, upload_token, token_time, "
                          "error FROM items WHERE account=? AND root=? AND folder=?",
                          (account, root, folder))
        return {(r[0], r[1], r[2]): dict(status=r[3], album_id=r[4], token=r[5], token_time=r[6],
                                         error=r[7]) for r in rows}

    def save_tokens(self, account, root, folder, pairs):
        """pairs: [(MediaFile, 업로드 토큰)]. batchCreate 전에 끊겨도 토큰을 다시 쓰려고 적는다."""
        now = time.time()
        self._write_many(
            "INSERT INTO items(account, root, folder, path, size, mtime_ns, status, upload_token, "
            "token_time, updated_at) VALUES(?,?,?,?,?,?,'token',?,?,?) " + _UPSERT_KEY +
            "status='token', upload_token=excluded.upload_token, token_time=excluded.token_time, "
            "error=NULL, updated_at=excluded.updated_at",
            [(account, root, folder, f.path, f.size, f.mtime_ns, tok, now, now) for f, tok in pairs])

    def mark_done(self, account, root, folder, rows):
        """rows: [(MediaFile, album_id, media_id)]"""
        now = time.time()
        self._write_many(
            "INSERT INTO items(account, root, folder, path, size, mtime_ns, status, album_id, "
            "media_id, updated_at) VALUES(?,?,?,?,?,?,'done',?,?,?) " + _UPSERT_KEY +
            "status='done', album_id=excluded.album_id, media_id=excluded.media_id, "
            "upload_token=NULL, token_time=NULL, error=NULL, updated_at=excluded.updated_at",
            [(account, root, folder, f.path, f.size, f.mtime_ns, aid, mid, now)
             for f, aid, mid in rows])

    def mark_failed(self, account, root, folder, rows):
        """rows: [(MediaFile, album_id, 오류, 다시 해도 안 될 실패인지)]. 일시 실패는 토큰을 남겨 다시 쓴다."""
        now = time.time()
        self._write_many(
            "INSERT INTO items(account, root, folder, path, size, mtime_ns, status, album_id, error, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) " + _UPSERT_KEY +
            "status=excluded.status, album_id=excluded.album_id, error=excluded.error, "
            "upload_token=CASE WHEN excluded.status='failed_perm' THEN NULL ELSE upload_token END, "
            "updated_at=excluded.updated_at",
            [(account, root, folder, f.path, f.size, f.mtime_ns,
              "failed_perm" if perm else "failed", aid, (err or "")[:500], now)
             for f, aid, err, perm in rows])

    def folder_status(self, account, root):
        """폴더별 상태 개수와 앨범 목록."""
        out = {}
        for folder, status, n in self._rows(
                "SELECT folder, status, COUNT(*) FROM items WHERE account=? AND root=? "
                "GROUP BY folder, status", (account, root)):
            out.setdefault(folder, {})[status] = n
        for folder, part, title, url, missing in self._rows(
                "SELECT folder, part, title, product_url, missing FROM albums "
                "WHERE account=? AND root=? ORDER BY folder, part", (account, root)):
            out.setdefault(folder, {}).setdefault("albums", []).append(
                dict(part=part, title=title, url=url, missing=bool(missing)))
        return out

    # ── 손으로 올린 폴더 표시 ──
    def manual_folders(self, google_id, root):
        return {r[0] for r in self._rows("SELECT folder FROM manual WHERE google_id=? AND root=?",
                                         (google_id, root))}

    def set_manual(self, google_id, root, folders, on):
        if on:
            now = time.time()
            self._write_many("INSERT OR IGNORE INTO manual(google_id, root, folder, created_at) "
                             "VALUES(?,?,?,?)", [(google_id, root, f, now) for f in folders])
        else:
            self._write_many("DELETE FROM manual WHERE google_id=? AND root=? AND folder=?",
                             [(google_id, root, f) for f in folders])

    def failed_examples(self, account, root, limit=50):
        return self._rows("SELECT path, error FROM items WHERE account=? AND root=? AND "
                          "status IN ('failed', 'failed_perm') ORDER BY updated_at DESC LIMIT ?",
                          (account, root, limit))

    def reset_failed(self, account, root):
        self._write_many("UPDATE items SET status='failed' WHERE account=? AND root=? AND "
                         "status='failed_perm'", [(account, root)])
