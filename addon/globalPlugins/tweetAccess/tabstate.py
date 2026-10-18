# -*- coding: utf-8 -*-
# NVTweet - tabstate.py: which removable tabs are open, their order and their cached tweets (per account).

import json

from .utils import Post, handle_of, notification_id

OPEN_TABS = "open_tabs"
TAB_ORDER = "tab_order"
LAST_TAB = "last_tab"


def _load(store, aid, key, default):
	raw = store.get_account_meta(aid, key)
	if not raw:
		return default
	try:
		return json.loads(raw)
	except (ValueError, TypeError):
		return default


def _save(store, aid, key, value):
	store.set_account_meta(aid, key, json.dumps(value))


def get_open_tabs(store, aid):
	data = _load(store, aid, OPEN_TABS, [])
	return [e for e in data if isinstance(e, dict) and e.get("type") and e.get("key")] if isinstance(data, list) else []


def add_open_tab(store, aid, entry):
	tabs = [t for t in get_open_tabs(store, aid) if not (t["type"] == entry["type"] and t["key"] == entry["key"])]
	tabs.append(entry)
	_save(store, aid, OPEN_TABS, tabs)


def remove_open_tab(store, aid, tab_type, key):
	tabs = [t for t in get_open_tabs(store, aid) if not (t["type"] == tab_type and t["key"] == key)]
	_save(store, aid, OPEN_TABS, tabs)


def set_tab_name(store, aid, tab_type, key, name):
	tabs = get_open_tabs(store, aid)
	for t in tabs:
		if t["type"] == tab_type and t["key"] == key:
			t["name"] = name
	_save(store, aid, OPEN_TABS, tabs)


def get_order(store, aid):
	data = _load(store, aid, TAB_ORDER, [])
	return data if isinstance(data, list) else []


def set_order(store, aid, order):
	_save(store, aid, TAB_ORDER, order)


def get_last(store, aid):
	data = _load(store, aid, LAST_TAB, None)
	return data if isinstance(data, dict) else None


def set_last(store, aid, identity):
	_save(store, aid, LAST_TAB, identity)


def _cache_key(tab_type, key):
	return "tabcache:%s:%s" % (tab_type, key)


NOTIF_IDS = "notif_ids_v2"  # per-account flag: the cached notifications have their stable ids


def _migrate_notifications(store, aid, posts):
	"""Once per account. Notification ids used to change from one fetch to the next, so every fetch looked
	new. The cached rows get the stable id; duplicates become one row (a read mark is kept) and the
	remembered place follows."""
	moved = {}
	merged = {}
	for p in posts:
		old = p.id
		p.id = notification_id(old // 1000, handle_of(p.author), p.url)
		moved[old] = p.id
		first = merged.get(p.id)
		if first is None:
			merged[p.id] = p
		elif p.is_read:
			first.is_read = True
	focus_key = "tabfocus:notifications:notifications"
	try:
		place = int(store.get_account_meta(aid, focus_key) or 0)
	except ValueError:
		place = 0
	if place in moved:
		store.set_account_meta(aid, focus_key, str(moved[place]))
	return sorted(merged.values(), key=lambda p: p.id, reverse=True)


def get_cache(store, aid, tab_type, key):
	"""The tweets a tab showed last time (a list of users: its rows, as dicts), or None."""
	raw = store.get_account_meta(aid, _cache_key(tab_type, key))
	posts = None
	if raw:
		try:
			data = json.loads(raw)
			if tab_type == "users":
				posts = [d for d in data if isinstance(d, dict) and d.get("id") and d.get("handle")]
			else:
				posts = [Post.from_dict(d) for d in data]
		except (ValueError, TypeError, KeyError):
			posts = None
	if tab_type == "notifications" and not store.get_account_meta(aid, NOTIF_IDS):
		if posts:
			posts = _migrate_notifications(store, aid, posts)
			set_cache(store, aid, tab_type, key, posts)
		store.set_account_meta(aid, NOTIF_IDS, "1")
	return posts


def set_cache(store, aid, tab_type, key, posts):
	store.set_account_meta(aid, _cache_key(tab_type, key), json.dumps([p if isinstance(p, dict) else p.to_dict() for p in posts]))


def delete_cache(store, aid, tab_type, key):
	store.set_account_meta(aid, _cache_key(tab_type, key), "")