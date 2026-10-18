# -*- coding: utf-8 -*-
# NVTweet - storage.py: encrypted cache (sqlcipher3) with accounts and tweets, DPAPI-protected keys.
#
# Layout (NVDA config folder):  NVTweet\nvtweet.db   encrypted database
#                               NVTweet\nvtweet.key  random DB key, protected with
#                                                     Windows DPAPI (this Windows user only)
# Each account's cookies are stored in the database, protected with DPAPI as well.
# If anything about the encrypted store fails, NVTweet keeps working with a
# memory-only cache and says so. It never falls back to an unencrypted file.

import copy
import ctypes
import json
import os
import secrets
import threading
import time
from ctypes import wintypes

import addonHandler
from logHandler import log

from .utils import (
	APP_NAME, COOKIE_VAULT, CURSOR_KEY, DATA_DIR, DB_FILE, FOCUS_KEY, KEY_FILE, TAG, Post,
	hashed_uid, isolated_modules,
)

addonHandler.initTranslation()

# ------------------------------------------------------------------ DPAPI

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
	_fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]


def _make_blob(data):
	buf = ctypes.create_string_buffer(data, len(data))
	return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p)), buf


def _dpapi():
	crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
	kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
	blob_p = ctypes.POINTER(_DATA_BLOB)
	crypt32.CryptProtectData.argtypes = [
		blob_p, wintypes.LPCWSTR, blob_p, ctypes.c_void_p, ctypes.c_void_p,
		wintypes.DWORD, blob_p,
	]
	crypt32.CryptProtectData.restype = wintypes.BOOL
	crypt32.CryptUnprotectData.argtypes = [
		blob_p, ctypes.POINTER(wintypes.LPWSTR), blob_p, ctypes.c_void_p,
		ctypes.c_void_p, wintypes.DWORD, blob_p,
	]
	crypt32.CryptUnprotectData.restype = wintypes.BOOL
	kernel32.LocalFree.argtypes = [ctypes.c_void_p]
	kernel32.LocalFree.restype = ctypes.c_void_p
	return crypt32, kernel32


def dpapi_protect(data):
	crypt32, kernel32 = _dpapi()
	blob_in, keep = _make_blob(data)
	blob_out = _DATA_BLOB()
	if not crypt32.CryptProtectData(
		ctypes.byref(blob_in), APP_NAME, None, None, None,
		_CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
	):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(blob_out.pbData, blob_out.cbData)
	finally:
		kernel32.LocalFree(blob_out.pbData)


def dpapi_unprotect(data):
	crypt32, kernel32 = _dpapi()
	blob_in, keep = _make_blob(data)
	blob_out = _DATA_BLOB()
	if not crypt32.CryptUnprotectData(
		ctypes.byref(blob_in), None, None, None, None,
		_CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
	):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(blob_out.pbData, blob_out.cbData)
	finally:
		kernel32.LocalFree(blob_out.pbData)


def load_or_create_key():
	"""64 hex chars (a raw 256-bit SQLCipher key). Raises on any problem."""
	os.makedirs(DATA_DIR, exist_ok=True)
	if os.path.isfile(KEY_FILE):
		with open(KEY_FILE, "rb") as f:
			key = dpapi_unprotect(f.read()).decode("ascii")
		if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
			raise ValueError("stored key is malformed")
		return key
	key = secrets.token_hex(32)
	tmp = KEY_FILE + ".tmp"
	with open(tmp, "wb") as f:
		f.write(dpapi_protect(key.encode("ascii")))
	os.replace(tmp, KEY_FILE)
	return key


# ------------------------------------------------------------------ schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
	id        INTEGER PRIMARY KEY AUTOINCREMENT,
	uid       TEXT    NOT NULL UNIQUE,
	handle    TEXT    NOT NULL DEFAULT '',
	name      TEXT    NOT NULL DEFAULT '',
	cookies   BLOB    NOT NULL,
	is_active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tweets (
	account_id   INTEGER NOT NULL,
	id           INTEGER NOT NULL,
	author       TEXT    NOT NULL,
	message      TEXT    NOT NULL,
	created_ts   INTEGER NOT NULL,
	post_url     TEXT    NOT NULL,
	fetched_ts   INTEGER NOT NULL,
	kind         TEXT    NOT NULL DEFAULT '',
	orig_author  TEXT    NOT NULL DEFAULT '',
	quote_author TEXT    NOT NULL DEFAULT '',
	quote_text   TEXT    NOT NULL DEFAULT '',
	source       TEXT    NOT NULL DEFAULT '',
	embeds       TEXT    NOT NULL DEFAULT '',
	liked        INTEGER NOT NULL DEFAULT 0,
	bookmarked   INTEGER NOT NULL DEFAULT 0,
	is_read      INTEGER NOT NULL DEFAULT 0,
	reply_to     TEXT    NOT NULL DEFAULT '',
	stats        TEXT    NOT NULL DEFAULT '',
	PRIMARY KEY (account_id, id)
);
CREATE TABLE IF NOT EXISTS meta (
	key   TEXT PRIMARY KEY,
	value TEXT
);
CREATE TABLE IF NOT EXISTS relations (
	account_id  INTEGER NOT NULL,
	handle      TEXT    NOT NULL,
	uid         TEXT    NOT NULL DEFAULT '',
	following   INTEGER,
	followed_by INTEGER,
	muting      INTEGER,
	blocking    INTEGER,
	PRIMARY KEY (account_id, handle)
);
"""

# Columns added after the first multi-account release; older databases get them in _add_columns.
_LATER_COLUMNS = (
	("reply_to", "TEXT NOT NULL DEFAULT ''"),
	("stats", "TEXT NOT NULL DEFAULT ''"),
)

_TWEET_COLUMNS = (
	"id", "author", "message", "created_ts", "post_url", "fetched_ts", "kind", "orig_author",
	"quote_author", "quote_text", "source", "embeds", "liked", "bookmarked", "is_read",
)
_COLS = (
	"id, author, message, created_ts, post_url, kind, orig_author, quote_author, "
	"quote_text, source, embeds, liked, bookmarked, is_read, reply_to, stats"
)
_UPSERT_COLUMNS = (
	"account_id", "id", "author", "message", "created_ts", "post_url", "fetched_ts", "kind",
	"orig_author", "quote_author", "quote_text", "source", "embeds", "liked", "bookmarked",
	"reply_to", "stats",
)
_UPSERT = (
	"INSERT INTO tweets (%s) VALUES (%s) ON CONFLICT(account_id, id) DO UPDATE SET %s"
	% (
		", ".join(_UPSERT_COLUMNS),
		", ".join("?" * len(_UPSERT_COLUMNS)),
		", ".join("%s=excluded.%s" % (c, c) for c in _UPSERT_COLUMNS if c not in ("account_id", "id", "fetched_ts")),
	)
)
_FLAG_COLUMNS = ("liked", "bookmarked")
_CHUNK = 300  # stay far below SQLite's variable limit
_REL_FLAGS = ("following", "followed_by", "muting", "blocking")  # how the account stands to a user; None = not known
_REL_UPSERT = (
	"INSERT INTO relations (account_id, handle, uid, following, followed_by, muting, blocking) "
	"VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, handle) DO UPDATE SET "
	"uid = CASE WHEN excluded.uid != '' THEN excluded.uid ELSE relations.uid END, "
	"following = COALESCE(excluded.following, relations.following), "
	"followed_by = COALESCE(excluded.followed_by, relations.followed_by), "
	"muting = COALESCE(excluded.muting, relations.muting), "
	"blocking = COALESCE(excluded.blocking, relations.blocking)"
)


def _skey(key, account_id):
	return "%s:%d" % (key, account_id)


def _tweet_args(account_id, p, now):
	return (
		account_id, p.id, p.author, p.message, p.ts, p.url, now, p.kind, p.orig_author,
		p.quote_author, p.quote_text, p.source, p.embeds, int(p.liked), int(p.bookmarked),
		p.reply_to, p.stats,
	)


class _DB:
	"""One encrypted database file. Thread-safe (one connection guarded by a lock)."""

	encrypted = True

	def __init__(self, dbapi, path, key_hex):
		self._lock = threading.RLock()
		os.makedirs(os.path.dirname(path), exist_ok=True)
		self._conn = dbapi.connect(path, check_same_thread=False)
		try:
			self._conn.execute("PRAGMA key = \"x'%s'\"" % key_hex)
			# Fails with "file is not a database" if the key is wrong.
			self._conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
			self._conn.executescript(_SCHEMA)
			self._add_columns()
			self._migrate_single_account()
		except Exception:
			self._conn.close()
			self._conn = None
			raise

	def _add_columns(self):
		cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tweets)")}
		for name, ddl in _LATER_COLUMNS:
			if name not in cols:
				self._conn.execute("ALTER TABLE tweets ADD COLUMN %s %s" % (name, ddl))
		self._conn.commit()

	def _migrate_single_account(self):
		"""Old layout: one `posts` table and one cookie vault. Becomes the first account."""
		conn = self._conn
		tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
		if "posts" not in tables:
			return

		def meta(key):
			row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
			return row[0] if row else None

		try:
			with open(COOKIE_VAULT, "rb") as f:
				cookies = json.loads(dpapi_unprotect(f.read()).decode("utf-8"))
		except Exception:
			cookies = None
		migrated = False
		if cookies and cookies.get("auth_token") and cookies.get("ct0"):
			uid = hashed_uid(cookies["auth_token"])
			conn.execute(
				"INSERT INTO accounts (uid, handle, name, cookies, is_active) VALUES (?, ?, ?, ?, 1)",
				(uid, meta("account_handle") or "", meta("account_name") or "",
				dpapi_protect(json.dumps(cookies).encode("utf-8"))),
			)
			account_id = conn.execute("SELECT id FROM accounts WHERE uid = ?", (uid,)).fetchone()[0]
			old_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
			common = ", ".join(c for c in _TWEET_COLUMNS if c in old_cols)
			conn.execute(
				"INSERT OR IGNORE INTO tweets (account_id, %s) SELECT ?, %s FROM posts" % (common, common),
				(account_id,),
			)
			for key in (CURSOR_KEY, FOCUS_KEY):
				value = meta(key)
				if value:
					conn.execute(
						"INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (_skey(key, account_id), value)
					)
			migrated = True
		conn.execute("DROP TABLE posts")
		conn.execute(
			"DELETE FROM meta WHERE key IN ('account_handle', 'account_name', ?, ?)", (CURSOR_KEY, FOCUS_KEY)
		)
		conn.commit()
		if migrated:
			try:
				os.remove(COOKIE_VAULT)
			except OSError:
				pass

	# ---- accounts

	@staticmethod
	def _account_dict(row, active=None):
		return {
			"id": row[0], "uid": row[1], "handle": row[2], "name": row[3],
			"active": bool(row[4]) if active is None else active,
		}

	def list_accounts(self):
		with self._lock:
			rows = self._conn.execute(
				"SELECT id, uid, handle, name, is_active FROM accounts ORDER BY id"
			).fetchall()
		return [self._account_dict(r) for r in rows]

	def active_account(self):
		"""The active account, or None. If accounts exist but none is active, the first becomes active."""
		with self._lock:
			row = self._conn.execute(
				"SELECT id, uid, handle, name, is_active FROM accounts WHERE is_active = 1"
			).fetchone()
			if row is None:
				row = self._conn.execute(
					"SELECT id, uid, handle, name, is_active FROM accounts ORDER BY id LIMIT 1"
				).fetchone()
				if row is None:
					return None
				self._conn.execute("UPDATE accounts SET is_active = (id = ?)", (row[0],))
				self._conn.commit()
		return self._account_dict(row, active=True)

	def add_account(self, uid, handle, name, cookies):
		"""Add an account, or refresh the cookies/name of a known one. Returns its id."""
		blob = dpapi_protect(json.dumps(cookies).encode("utf-8"))
		legacy = hashed_uid(cookies.get("auth_token", ""))
		with self._lock:
			row = self._conn.execute("SELECT id, handle, name FROM accounts WHERE uid = ?", (uid,)).fetchone()
			if row is None and legacy != uid:
				row = self._conn.execute(
					"SELECT id, handle, name FROM accounts WHERE uid = ?", (legacy,)
				).fetchone()
			if row is not None:
				account_id = row[0]
				self._conn.execute(
					"UPDATE accounts SET uid = ?, handle = ?, name = ?, cookies = ? WHERE id = ?",
					(uid, handle or row[1], name or row[2], blob, account_id),
				)
			else:
				account_id = self._conn.execute(
					"INSERT INTO accounts (uid, handle, name, cookies) VALUES (?, ?, ?, ?)",
					(uid, handle, name, blob),
				).lastrowid
			self._conn.commit()
		return account_id

	def rename_account(self, account_id, name):
		with self._lock:
			self._conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))
			self._conn.commit()

	def set_active(self, account_id):
		with self._lock:
			self._conn.execute("UPDATE accounts SET is_active = (id = ?)", (account_id,))
			self._conn.commit()

	def remove_account(self, account_id):
		with self._lock:
			self._conn.execute("DELETE FROM tweets WHERE account_id = ?", (account_id,))
			self._conn.execute(
				"DELETE FROM meta WHERE key IN (?, ?)",
				(_skey(CURSOR_KEY, account_id), _skey(FOCUS_KEY, account_id)),
			)
			self._conn.execute("DELETE FROM meta WHERE key LIKE ?", ("%%:%d" % account_id,))
			self._conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
			self._conn.commit()

	def get_cookies(self, account_id):
		with self._lock:
			row = self._conn.execute("SELECT cookies FROM accounts WHERE id = ?", (account_id,)).fetchone()
		if row is None:
			return None
		try:
			data = json.loads(dpapi_unprotect(bytes(row[0])).decode("utf-8"))
		except Exception:
			log.error(TAG + "could not read the cookies of an account", exc_info=True)
			return None
		return data if data.get("auth_token") and data.get("ct0") else None

	# ---- one-time import from the shared database

	_COPY_COLUMNS = (
		"account_id", "id", "author", "message", "created_ts", "post_url", "fetched_ts", "kind",
		"orig_author", "quote_author", "quote_text", "source", "embeds", "liked", "bookmarked",
		"is_read", "reply_to", "stats",
	)

	def import_from(self, other, account_id):
		"""Copy this account's tweets and meta from the old shared database, once.
		Existing rows here win. All or nothing; the flag is set in the same transaction."""
		flag = "migrated_from_shared"
		if self.get_meta(flag):
			return
		cols = ", ".join(self._COPY_COLUMNS)
		with other._lock:
			tweets = other._conn.execute(
				"SELECT %s FROM tweets WHERE account_id = ?" % cols, (account_id,)
			).fetchall()
			metas = other._conn.execute(
				"SELECT key, value FROM meta WHERE key LIKE ?", ("%%:%d" % account_id,)
			).fetchall()
		with self._lock:
			try:
				if tweets:
					self._conn.executemany(
						"INSERT OR IGNORE INTO tweets (%s) VALUES (%s)" % (cols, ", ".join("?" * len(self._COPY_COLUMNS))),
						tweets,
					)
				if metas:
					self._conn.executemany("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", metas)
				self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (flag, "1"))
				self._conn.commit()
			except Exception:
				self._conn.rollback()
				raise
		log.info(TAG + "imported %d tweets and %d settings of an account from the shared database" % (len(tweets), len(metas)))

	# ---- tweets

	def load_all(self, account_id):
		if account_id is None:
			return []
		with self._lock:
			rows = self._conn.execute(
				"SELECT %s FROM tweets WHERE account_id = ? ORDER BY id DESC" % _COLS, (account_id,)
			).fetchall()
		return [Post(*r) for r in rows]

	def load_recent(self, account_id, limit):
		with self._lock:
			rows = self._conn.execute(
				"SELECT %s FROM tweets WHERE account_id = ? ORDER BY id DESC LIMIT ?" % _COLS,
				(account_id, int(limit)),
			).fetchall()
		return [Post(*r) for r in rows]

	def _ids_where(self, account_id, ids, extra):
		ids = list(ids)
		found = set()
		with self._lock:
			for i in range(0, len(ids), _CHUNK):
				part = ids[i:i + _CHUNK]
				marks = ",".join("?" * len(part))
				found.update(
					r[0] for r in self._conn.execute(
						"SELECT id FROM tweets WHERE account_id = ? %s AND id IN (%s)" % (extra, marks),
						[account_id] + part,
					)
				)
		return found

	def known_ids(self, account_id, ids):
		return self._ids_where(account_id, ids, "")

	def unread_among(self, account_id, ids):
		return self._ids_where(account_id, ids, "AND is_read = 0")

	def unread_count(self, account_id):
		with self._lock:
			row = self._conn.execute(
				"SELECT COUNT(*) FROM tweets WHERE account_id = ? AND is_read = 0", (account_id,)
			).fetchone()
		return row[0] if row else 0

	def oldest_unread(self, account_id):
		"""The oldest tweet not read yet, or None (auto read)."""
		with self._lock:
			row = self._conn.execute(
				"SELECT %s FROM tweets WHERE account_id = ? AND is_read = 0 ORDER BY id ASC LIMIT 1" % _COLS,
				(account_id,),
			).fetchone()
		return Post(*row) if row else None

	def add_posts(self, account_id, posts):
		"""Insert new tweets or refresh stored ones (read state is kept)."""
		now = int(time.time())
		with self._lock:
			self._conn.executemany(_UPSERT, [_tweet_args(account_id, p, now) for p in posts])
			self._conn.commit()

	def mark_read(self, account_id, ids, read=True):
		with self._lock:
			self._conn.executemany(
				"UPDATE tweets SET is_read = ? WHERE account_id = ? AND id = ?",
				[(int(read), account_id, int(i)) for i in ids],
			)
			self._conn.commit()

	def mark_all_read(self, account_id):
		with self._lock:
			self._conn.execute("UPDATE tweets SET is_read = 1 WHERE account_id = ?", (account_id,))
			self._conn.commit()

	def set_flag(self, account_id, post_id, column, value):
		if column not in _FLAG_COLUMNS:
			raise ValueError(column)
		with self._lock:
			self._conn.execute(
				"UPDATE tweets SET %s = ? WHERE account_id = ? AND id = ?" % column,
				(int(value), account_id, int(post_id)),
			)
			self._conn.commit()

	def update_details(self, account_id, post_id, stats, liked, bookmarked):
		with self._lock:
			self._conn.execute(
				"UPDATE tweets SET stats = ?, liked = ?, bookmarked = ? WHERE account_id = ? AND id = ?",
				(stats, int(liked), int(bookmarked), account_id, int(post_id)),
			)
			self._conn.commit()

	# ---- how the account stands to other users

	@staticmethod
	def _rel_args(account_id, rels):
		return [
			(account_id, r["handle"], r.get("uid") or "") + tuple(
				None if r.get(flag) is None else int(bool(r[flag])) for flag in _REL_FLAGS
			)
			for r in rels if r.get("handle")
		]

	def set_relations(self, account_id, rels):
		"""Keep what is known. rels: dicts with handle (lowercase), uid and any of following, followed_by, muting,
		blocking; a flag that is missing or None leaves what was known unchanged."""
		args = self._rel_args(account_id, rels)
		if not args:
			return
		with self._lock:
			self._conn.executemany(_REL_UPSERT, args)
			self._conn.commit()

	def replace_relation_list(self, account_id, flag, rels):
		"""rels: the whole list of the users the account follows (flag following), mutes (muting) or blocks
		(blocking). Whoever had the flag and is not in it loses it, and the flag counts as known for every other
		user too: not in the table means no."""
		if flag not in ("following", "muting", "blocking"):
			raise ValueError(flag)
		args = self._rel_args(account_id, [dict(r, **{flag: True}) for r in rels])
		with self._lock:
			try:
				self._conn.execute(
					"UPDATE relations SET %s = 0 WHERE account_id = ? AND %s = 1" % (flag, flag), (account_id,)
				)
				self._conn.executemany(_REL_UPSERT, args)
				self._conn.execute(
					"INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
					(_skey("rel_full:" + flag, account_id), str(int(time.time()))),
				)
				self._conn.commit()
			except Exception:
				self._conn.rollback()
				raise

	def relation_state(self, account_id, handle):
		"""{"uid", "following", "followed_by", "muting", "blocking"}: True, False, or None when not known.
		Once the whole list of a flag was read, a user who is not in the table is known not to have it."""
		with self._lock:
			row = self._conn.execute(
				"SELECT uid, %s FROM relations WHERE account_id = ? AND handle = ?" % ", ".join(_REL_FLAGS),
				(account_id, handle.lower()),
			).fetchone()
		state = {"uid": row[0] if row else ""}
		for i, flag in enumerate(_REL_FLAGS):
			value = row[i + 1] if row else None
			if value is not None:
				state[flag] = bool(value)
			else:
				state[flag] = False if self.get_meta(_skey("rel_full:" + flag, account_id)) else None
		return state

	def clear_posts(self, account_id):
		with self._lock:
			self._conn.execute("DELETE FROM tweets WHERE account_id = ?", (account_id,))
			self._conn.execute(
				"DELETE FROM meta WHERE key IN (?, ?)",
				(_skey(CURSOR_KEY, account_id), _skey(FOCUS_KEY, account_id)),
			)
			self._conn.commit()

	# ---- meta

	def get_meta(self, key):
		with self._lock:
			row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
		return row[0] if row and row[0] else None

	def set_meta(self, key, value):
		with self._lock:
			self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
			self._conn.commit()

	def get_account_meta(self, account_id, key):
		return self.get_meta(_skey(key, account_id))

	def set_account_meta(self, account_id, key, value):
		self.set_meta(_skey(key, account_id), value)

	def close(self):
		with self._lock:
			if self._conn is not None:
				try:
					self._conn.close()
				finally:
					self._conn = None


class Store:
	"""Facade over the encrypted files. Accounts, cookies and global settings live in the shared
	database; each account's tweets and meta live in accounts\<id>.db, so removing an account
	deletes its file. The API is the same as one database's: callers pass the account id."""

	encrypted = True

	def __init__(self, dbapi, path, key_hex):
		self._dbapi = dbapi
		self._key = key_hex
		self._dir = os.path.join(os.path.dirname(path), "accounts")
		self._lock = threading.RLock()
		self._dbs = {}  # account id -> _DB (or a MemoryStore if its file could not be opened)
		self._shared = _DB(dbapi, path, key_hex)

	def _path(self, account_id):
		return os.path.join(self._dir, "%d.db" % int(account_id))
	def _db(self, account_id):
		if account_id is None:
			return self._shared
		with self._lock:
			db = self._dbs.get(account_id)
			if db is None:
				try:
					db = _DB(self._dbapi, self._path(account_id), self._key)
				except Exception:
					log.error(
						TAG + "could not open the database of an account (memory only for this run)",
						exc_info=True,
					)
					db = MemoryStore()
				if isinstance(db, _DB):
					try:
						db.import_from(self._shared, account_id)
					except Exception:
						log.error(TAG + "could not import an account from the shared database", exc_info=True)
				self._dbs[account_id] = db
			return db

	# ---- accounts and global settings: the shared database

	def list_accounts(self):
		return self._shared.list_accounts()

	def active_account(self):
		return self._shared.active_account()

	def add_account(self, uid, handle, name, cookies):
		return self._shared.add_account(uid, handle, name, cookies)

	def rename_account(self, account_id, name):
		self._shared.rename_account(account_id, name)

	def set_active(self, account_id):
		self._shared.set_active(account_id)

	def get_cookies(self, account_id):
		return self._shared.get_cookies(account_id)

	def get_meta(self, key):
		return self._shared.get_meta(key)

	def set_meta(self, key, value):
		self._shared.set_meta(key, value)

	def remove_account(self, account_id):
		self._shared.remove_account(account_id)
		with self._lock:
			db = self._dbs.pop(account_id, None)
		if db is not None:
			db.close()
		path = self._path(account_id)
		for suffix in ("", "-journal", "-wal", "-shm"):
			try:
				os.remove(path + suffix)
			except OSError:
				pass

	# ---- tweets and per-account meta: the account's own file

	def load_all(self, account_id):
		return self._db(account_id).load_all(account_id)

	def load_recent(self, account_id, limit):
		return self._db(account_id).load_recent(account_id, limit)

	def known_ids(self, account_id, ids):
		return self._db(account_id).known_ids(account_id, ids)

	def unread_among(self, account_id, ids):
		return self._db(account_id).unread_among(account_id, ids)

	def unread_count(self, account_id):
		return self._db(account_id).unread_count(account_id)

	def oldest_unread(self, account_id):
		return self._db(account_id).oldest_unread(account_id)

	def add_posts(self, account_id, posts):
		self._db(account_id).add_posts(account_id, posts)

	def mark_read(self, account_id, ids, read=True):
		self._db(account_id).mark_read(account_id, ids, read)

	def mark_all_read(self, account_id):
		self._db(account_id).mark_all_read(account_id)

	def set_flag(self, account_id, post_id, column, value):
		self._db(account_id).set_flag(account_id, post_id, column, value)

	def update_details(self, account_id, post_id, stats, liked, bookmarked):
		self._db(account_id).update_details(account_id, post_id, stats, liked, bookmarked)

	def set_relations(self, account_id, rels):
		self._db(account_id).set_relations(account_id, rels)

	def replace_relation_list(self, account_id, flag, rels):
		self._db(account_id).replace_relation_list(account_id, flag, rels)

	def relation_state(self, account_id, handle):
		return self._db(account_id).relation_state(account_id, handle)

	def clear_posts(self, account_id):
		self._db(account_id).clear_posts(account_id)

	def get_account_meta(self, account_id, key):
		return self._db(account_id).get_account_meta(account_id, key)

	def set_account_meta(self, account_id, key, value):
		self._db(account_id).set_account_meta(account_id, key, value)

	def close(self):
		with self._lock:
			dbs, self._dbs = list(self._dbs.values()), {}
		for db in dbs:
			db.close()
		self._shared.close()


class MemoryStore:
	"""Same API, nothing is written to disk. Used when the encrypted store is unavailable."""

	encrypted = False

	def __init__(self):
		self._lock = threading.RLock()
		self._accounts = {}   # id -> dict
		self._next_id = 1
		self._tweets = {}     # account id -> {tweet id: Post}
		self._relations = {}  # account id -> {handle: {"uid", and the flags that are known}}
		self._meta = {}

	@staticmethod
	def _public(account_id, a, active=None):
		return {
			"id": account_id, "uid": a["uid"], "handle": a["handle"], "name": a["name"],
			"active": a["active"] if active is None else active,
		}

	def list_accounts(self):
		with self._lock:
			return [self._public(i, self._accounts[i]) for i in sorted(self._accounts)]

	def active_account(self):
		with self._lock:
			for i in sorted(self._accounts):
				if self._accounts[i]["active"]:
					return self._public(i, self._accounts[i])
			if not self._accounts:
				return None
			first = min(self._accounts)
			self._accounts[first]["active"] = True
			return self._public(first, self._accounts[first])

	def add_account(self, uid, handle, name, cookies):
		legacy = hashed_uid(cookies.get("auth_token", ""))
		with self._lock:
			found = next((i for i, a in self._accounts.items() if a["uid"] == uid), None)
			if found is None and legacy != uid:
				found = next((i for i, a in self._accounts.items() if a["uid"] == legacy), None)
			if found is None:
				found = self._next_id
				self._next_id += 1
				self._accounts[found] = {"uid": uid, "handle": handle, "name": name, "cookies": cookies, "active": False}
			else:
				a = self._accounts[found]
				a.update(uid=uid, handle=handle or a["handle"], name=name or a["name"], cookies=cookies)
			return found

	def rename_account(self, account_id, name):
		with self._lock:
			if account_id in self._accounts:
				self._accounts[account_id]["name"] = name

	def set_active(self, account_id):
		with self._lock:
			for i, a in self._accounts.items():
				a["active"] = i == account_id

	def remove_account(self, account_id):
		with self._lock:
			self._accounts.pop(account_id, None)
			suffix = ":%d" % account_id
			for key in [k for k in self._meta if k.endswith(suffix)]:
				del self._meta[key]
			self._tweets.pop(account_id, None)
			self._relations.pop(account_id, None)
			self._meta.pop(_skey(CURSOR_KEY, account_id), None)
			self._meta.pop(_skey(FOCUS_KEY, account_id), None)

	def get_cookies(self, account_id):
		with self._lock:
			a = self._accounts.get(account_id)
			return dict(a["cookies"]) if a else None

	def _posts(self, account_id):
		return self._tweets.setdefault(account_id, {})

	def load_all(self, account_id):
		if account_id is None:
			return []
		with self._lock:
			posts = sorted(self._posts(account_id).values(), key=lambda p: p.id, reverse=True)
			return [copy.copy(p) for p in posts]

	def load_recent(self, account_id, limit):
		return self.load_all(account_id)[:int(limit)]

	def known_ids(self, account_id, ids):
		with self._lock:
			posts = self._posts(account_id)
			return {i for i in ids if i in posts}

	def unread_among(self, account_id, ids):
		with self._lock:
			posts = self._posts(account_id)
			return {i for i in ids if i in posts and not posts[i].is_read}

	def unread_count(self, account_id):
		with self._lock:
			return sum(1 for p in self._posts(account_id).values() if not p.is_read)

	def oldest_unread(self, account_id):
		with self._lock:
			unread = [p for p in self._posts(account_id).values() if not p.is_read]
			return copy.copy(min(unread, key=lambda p: p.id)) if unread else None

	def add_posts(self, account_id, posts):
		with self._lock:
			store = self._posts(account_id)
			for p in posts:
				old = store.get(p.id)
				new = copy.copy(p)
				new.is_read = old.is_read if old is not None else False
				store[p.id] = new

	def mark_read(self, account_id, ids, read=True):
		with self._lock:
			store = self._posts(account_id)
			for i in ids:
				if i in store:
					store[i].is_read = bool(read)

	def mark_all_read(self, account_id):
		with self._lock:
			for p in self._posts(account_id).values():
				p.is_read = True

	def set_flag(self, account_id, post_id, column, value):
		if column not in _FLAG_COLUMNS:
			raise ValueError(column)
		with self._lock:
			store = self._posts(account_id)
			if post_id in store:
				setattr(store[post_id], column, bool(value))

	def update_details(self, account_id, post_id, stats, liked, bookmarked):
		with self._lock:
			post = self._posts(account_id).get(int(post_id))
			if post is not None:
				post.stats, post.liked, post.bookmarked = stats, bool(liked), bool(bookmarked)

	def set_relations(self, account_id, rels):
		with self._lock:
			table = self._relations.setdefault(account_id, {})
			for r in rels:
				if not r.get("handle"):
					continue
				row = table.setdefault(r["handle"], {"uid": ""})
				if r.get("uid"):
					row["uid"] = r["uid"]
				for flag in _REL_FLAGS:
					if r.get(flag) is not None:
						row[flag] = bool(r[flag])

	def replace_relation_list(self, account_id, flag, rels):
		if flag not in ("following", "muting", "blocking"):
			raise ValueError(flag)
		with self._lock:
			for row in self._relations.setdefault(account_id, {}).values():
				if row.get(flag):
					row[flag] = False
			self.set_relations(account_id, [dict(r, **{flag: True}) for r in rels])
			self.set_account_meta(account_id, "rel_full:" + flag, str(int(time.time())))

	def relation_state(self, account_id, handle):
		with self._lock:
			row = self._relations.get(account_id, {}).get(handle.lower())
		state = {"uid": row["uid"] if row else ""}
		for flag in _REL_FLAGS:
			if row is not None and flag in row:
				state[flag] = row[flag]
			else:
				state[flag] = False if self.get_account_meta(account_id, "rel_full:" + flag) else None
		return state

	def clear_posts(self, account_id):
		with self._lock:
			self._tweets.pop(account_id, None)
			self._meta.pop(_skey(CURSOR_KEY, account_id), None)
			self._meta.pop(_skey(FOCUS_KEY, account_id), None)

	def get_meta(self, key):
		with self._lock:
			return self._meta.get(key) or None

	def set_meta(self, key, value):
		with self._lock:
			self._meta[key] = value

	def get_account_meta(self, account_id, key):
		return self.get_meta(_skey(key, account_id))

	def set_account_meta(self, account_id, key, value):
		self.set_meta(_skey(key, account_id), value)

	def close(self):
		pass


def open_store():
	"""Returns (store, note). note is a user-facing sentence when we had to fall back."""
	try:
		with isolated_modules():
			from sqlcipher3 import dbapi2 as dbapi
	except Exception as e:
		log.warning(TAG + "sqlcipher3 could not be imported (%s: %s)" % (type(e).__name__, e))
		return MemoryStore(), _("Cache is memory-only: sqlcipher3 is not installed.")
	try:
		key = load_or_create_key()
	except Exception:
		log.error(TAG + "could not load or create the database key", exc_info=True)
		return MemoryStore(), _("Cache is memory-only: the database key is unavailable.")
	try:
		store = Store(dbapi, DB_FILE, key)
	except Exception:
		log.error(
			TAG + "could not open the encrypted database (left untouched on disk)",
			exc_info=True,
		)
		return MemoryStore(), _("Cache is memory-only: the database could not be opened.")
	return store, None