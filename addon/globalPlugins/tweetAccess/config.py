# -*- coding: utf-8 -*-
# NVTweet - config.py: small settings store backed by the encrypted cache (meta table).

import json
import os
import threading

PREFIX = "cfg:"
BG_TAB_TYPES = ("user", "search", "list", "thread", "quotes")  # open tabs a background check can refresh

DEFAULTS = {
	"sort_newest_first": True,
	"author_mode": "both",          # both / name / handle
	"time_mode": "absolute",        # relative_24h / relative_always / absolute / custom
	"time_pattern": "%Y-%m-%d %H:%M:%S",
	"bg_interval": 15,              # Home: minutes between background checks, 0 = off
	"bg_interval_notifications": 15,
	"bg_interval_bookmarks": 0,
	"bg_interval_trends": 0,        # Search and explore: minutes between background loads of the trends shown last
	"bg_interval_tabs": 0,          # open tabs: minutes for a tab with no setting of its own
	"bg_tab_settings": "",          # JSON {"<type>:<key>": [minutes, speak]} for open tabs that differ from that default
	"bg_announce_off": "",          # comma separated categories (home, notifications, tabs) that are checked silently
	"enter_action": "view_thread",  # what Enter does on a tweet
	"target_mode": "ask",           # on a quote tweet, shortcuts act on: ask / main / quoted
	"auto_pause": 3,                # auto read: seconds to wait after each tweet
	"auto_full": True,              # auto read: the whole message of a long tweet, no "read more"
	"auto_profile": "",             # auto read: name of the NVDA profile whose voice it uses, "" = none
	"feed_pages": 5,                # pages (of about 20) a tab loads when it opens
	"tray_tabs": "",                # JSON list of the tabs (besides Home, Notifications) the tray icon counts
	"show_tray": True,              # notification area icon with the unread count
	"embed_first": True,            # Embed column first (otherwise last)
	"tabs_off": "chat",             # comma separated optional tabs that are switched off (Chat is experimental)
	"search_filter_order": "",      # advanced search, Include list: filter keys comma separated, in the user's order
	"search_omit_order": "",        # advanced search, Leave out list: same, in its own order
	"tray_disabled": "",            # comma separated tabs NOT counted by the tray icon
	"tray_per_tab": False,          # tray text: each tab with its name (otherwise one total)
	"sound_pack": "default",        # folder in SoundPack, "-" = silent
	"sound_disabled": "",           # comma separated event keys switched off
	"download_dir": os.path.join(os.path.expanduser("~"), "Downloads", "NVTweet"),  # where Download saves files
}

RANGES = {
	"bg_interval": (0, 60),
	"bg_interval_notifications": (0, 60),
	"bg_interval_bookmarks": (0, 60),
	"bg_interval_trends": (0, 60),
	"bg_interval_tabs": (0, 60),
	"auto_pause": (1, 10),
	"feed_pages": (1, 5),
}


def _clamp(key, value):
	low, high = RANGES.get(key, (None, None))
	if low is None:
		return value
	return max(low, min(high, value))


IMPORTED = "cfg_imported"  # per-account flag: the old shared settings were copied in once


class Config:
	"""The settings of the active account. All values are cached in memory, so any thread can call
	get(). load(account_id) switches to another account. With no account the shared settings are used."""

	def __init__(self, store, account_id=None):
		self._store = store
		self._lock = threading.Lock()
		self._aid = None
		self._values = dict(DEFAULTS)
		self.load(account_id)

	def load(self, account_id):
		"""Switch to an account's settings. An account seen for the first time starts from the
		settings that every account shared before they became per account."""
		with self._lock:
			self._aid = account_id
			if account_id is not None:
				self._import_shared(account_id)
			self._values = {key: self._read(key, default) for key, default in DEFAULTS.items()}

	def _import_shared(self, account_id):
		if self._store.get_account_meta(account_id, IMPORTED):
			return
		for key in DEFAULTS:
			raw = self._store.get_meta(PREFIX + key)
			if raw is not None and self._store.get_account_meta(account_id, PREFIX + key) is None:
				self._store.set_account_meta(account_id, PREFIX + key, raw)
		self._store.set_account_meta(account_id, IMPORTED, "1")

	def _get(self, name):
		if self._aid is None:
			return self._store.get_meta(name)
		return self._store.get_account_meta(self._aid, name)

	def _put(self, name, raw):
		if self._aid is None:
			self._store.set_meta(name, raw)
		else:
			self._store.set_account_meta(self._aid, name, raw)

	def _read(self, key, default):
		raw = self._get(PREFIX + key)
		if raw is None:
			return default
		try:
			if isinstance(default, bool):
				return raw == "1"
			if isinstance(default, int):
				return _clamp(key, int(raw))
		except ValueError:
			return default
		return raw

	def get(self, key):
		return self._values[key]

	def tab_setting(self, ident):
		"""(minutes between background checks, whether news is spoken) of an open tab ("<type>:<key>"):
		its own setting, else what the Updates page gives tabs without one."""
		try:
			own = json.loads(self.get("bg_tab_settings") or "{}").get(ident)
		except (ValueError, AttributeError):
			own = None
		if isinstance(own, list) and len(own) == 2:
			try:
				return _clamp("bg_interval_tabs", int(own[0])), bool(own[1])
			except (TypeError, ValueError):
				pass
		quiet = "tabs" in {k for k in self.get("bg_announce_off").split(",") if k}
		return self.get("bg_interval_tabs"), not quiet

	def set(self, key, value):
		default = DEFAULTS[key]
		if isinstance(default, bool):
			value = bool(value)
			raw = "1" if value else "0"
		elif isinstance(default, int):
			value = _clamp(key, int(value))
			raw = str(value)
		else:
			value = str(value)
			raw = value
		with self._lock:
			self._values[key] = value
			self._put(PREFIX + key, raw)