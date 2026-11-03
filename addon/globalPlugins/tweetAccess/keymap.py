# -*- coding: utf-8 -*-
# tweetAccess - keymap.py: the shortcuts of the add-on's own windows (not NVDA gestures).
# Shared by every account: the choices are kept as JSON in the shared database (see configure).
#
# A shortcut is text such as "ctrl+shift+pageup" or "f5"; "" means no shortcut.
# fixed: a bare key (Escape, Space, arrows, Enter) that stays as it is.
# Alt+1..9 and Ctrl+1..9 are not in the table: the windows keep them.

import json

import addonHandler
import wx

addonHandler.initTranslation()

# (id, default shortcut, area, fixed)
_DEFS = [
	("check_updates", "f5", "window", False),
	("check_all", "ctrl+f5", "window", False),
	("search", "ctrl+f", "window", False),
	("new_tweet", "ctrl+n", "window", False),
	("settings", "ctrl+p", "window", False),
	("remove_tab", "ctrl+w", "window", False),
	("rename_tab", "ctrl+f2", "window", False),
	("move_tab_left", "ctrl+shift+pageup", "window", False),
	("move_tab_right", "ctrl+shift+pagedown", "window", False),
	("reopen_tab", "ctrl+shift+t", "window", False),
	("accounts_menu", "alt+w", "window", False),
	("close_window", "escape", "window", True),
	("load_older", "shift+f5", "tab", False),
	("next_unread", "space", "tab", True),
	("open_row", "return", "tab", True),
	("prev_user", "left", "tab", True),
	("next_user", "right", "tab", True),
	("action_menu", "alt+a", "tab", False),
	("thread", "alt+t", "tab", False),
	("user_menu", "alt+u", "tab", False),
	("user_timeline", "", "tab", False),
	("user_info", "", "tab", False),
	("user_pinned", "", "tab", False),
	("user_followers", "", "tab", False),
	("user_following", "", "tab", False),
	("user_known_followers", "", "tab", False),
	("user_message", "", "tab", False),
	("user_follow", "", "tab", False),
	("user_mute", "", "tab", False),
	("user_block", "", "tab", False),
	("user_copy_url", "", "tab", False),
	("user_open_web", "", "tab", False),
	("quotes", "alt+o", "tab", False),
	("reply", "alt+y", "tab", False),
	("retweet", "alt+r", "tab", False),
	("like", "alt+l", "tab", False),
	("bookmark", "alt+b", "tab", False),
	("hashtags", "alt+h", "tab", False),
	("quote_tweet", "", "tab", False),
	("open_browser", "ctrl+o", "tab", False),
	("links", "ctrl+e", "tab", False),
	("full_tweet", "ctrl+space", "tab", False),
	("copy_row", "ctrl+c", "tab", False),
	("copy_text", "", "tab", False),
	("copy_link", "", "tab", False),
	("select_all", "ctrl+a", "tab", False),
	("jump_row", "ctrl+j", "tab", False),
	("edit_search", "ctrl+shift+f", "tab", False),
	("clear_cache", "ctrl+delete", "tab", False),
	("mark_read", "", "tab", False),
	("mark_unread", "", "tab", False),
	("mark_all_read", "", "tab", False),
	("open_image", "", "tab", False),
	("bemyeyes", "", "tab", False),
	("copy_image", "", "tab", False),
	("open_video", "", "tab", False),
	("download", "", "tab", False),
	("send_tweet", "ctrl+return", "compose", False),
	("close_compose", "ctrl+w", "compose", False),
	("refresh_details", "f5", "details", False),
	("copy_line", "ctrl+c", "details", False),
]

_NAMES = {
	"return": "Enter", "pageup": "PageUp", "pagedown": "PageDown", "delete": "Delete", "space": "Space",
	"left": "Left", "right": "Right", "up": "Up", "down": "Down", "escape": "Escape",
	"home": "Home", "end": "End", "insert": "Insert", "backspace": "Backspace", "tab": "Tab",
}

_NAMED = {
	"delete": wx.WXK_DELETE, "return": wx.WXK_RETURN, "space": wx.WXK_SPACE, "pageup": wx.WXK_PAGEUP,
	"pagedown": wx.WXK_PAGEDOWN, "home": wx.WXK_HOME, "end": wx.WXK_END, "insert": wx.WXK_INSERT,
	"backspace": wx.WXK_BACK, "left": wx.WXK_LEFT, "right": wx.WXK_RIGHT, "up": wx.WXK_UP,
	"down": wx.WXK_DOWN, "tab": wx.WXK_TAB, "escape": wx.WXK_ESCAPE,
}
for _n in range(1, 13):
	_NAMED["f%d" % _n] = getattr(wx, "WXK_F%d" % _n)
_BY_CODE = {code: name for name, code in _NAMED.items()}
_FKEYS = {code for name, code in _NAMED.items() if name[1:].isdigit()}
# keys that edit text: not for the new tweet window's actions
_EDIT_KEYS = {
	_NAMED[n] for n in (
		"left", "right", "up", "down", "home", "end", "pageup", "pagedown", "backspace", "delete", "insert",
	)
}
_EDIT_LETTERS = {ord(c) for c in "ACVXZY"}
_MODIFIER_CODES = {
	getattr(wx, n) for n in ("WXK_SHIFT", "WXK_CONTROL", "WXK_ALT", "WXK_WINDOWS_LEFT", "WXK_WINDOWS_RIGHT")
	if hasattr(wx, n)
}

_load = None
_save = None
_bindings = None  # action id -> the shortcut in use ("" = none)
_parsed = {}      # action id -> (ctrl, alt, shift, key code)
_labels = None


def actions():
	return [d[0] for d in _DEFS]


def area(action_id):
	return next(d[2] for d in _DEFS if d[0] == action_id)


def default(action_id):
	return next(d[1] for d in _DEFS if d[0] == action_id)


def is_fixed(action_id):
	return next(d[3] for d in _DEFS if d[0] == action_id)


def configure(load, save):
	"""load() -> JSON text or None; save(text). Called once the shared database is open."""
	global _load, _save, _bindings
	_load, _save, _bindings = load, save, None


def _name_to_code(part):
	if part in _NAMED:
		return _NAMED[part]
	upper = part.upper()
	if len(part) == 1 and len(upper) == 1 and 33 <= ord(upper) <= 126:
		return ord(upper)
	return None


def _parse(text):
	"""(ctrl, alt, shift, key code) or None."""
	if not isinstance(text, str):
		return None
	parts = [p.strip().lower() for p in text.split("+") if p.strip()]
	mods = {"ctrl": False, "alt": False, "shift": False}
	key = None
	for i, part in enumerate(parts):
		if part in mods:
			mods[part] = True
		elif key is None and i == len(parts) - 1:
			key = _name_to_code(part)
			if key is None:
				return None
		else:
			return None
	if key is None:
		return None
	return mods["ctrl"], mods["alt"], mods["shift"], key


def _to_text(spec):
	ctrl, alt, shift, key = spec
	parts = [n for n, on in (("ctrl", ctrl), ("alt", alt), ("shift", shift)) if on]
	parts.append(_BY_CODE.get(key) or chr(key).lower())
	return "+".join(parts)


def _norm(text):
	spec = _parse(text)
	return _to_text(spec) if spec else ""


def _ensure():
	global _bindings
	if _bindings is not None:
		return
	data = {}
	try:
		raw = _load() if _load else None
		loaded = json.loads(raw) if raw else {}
		if isinstance(loaded, dict):
			data = loaded
	except (ValueError, TypeError):
		pass
	_bindings = {}
	_parsed.clear()
	for action_id, dflt, _area, fixed in _DEFS:
		chosen = dflt
		if not fixed and isinstance(data.get(action_id), str):
			chosen = data[action_id]
		spec = _parse(chosen)
		if spec is None and chosen:  # unreadable: the default
			spec = _parse(dflt)
		_bindings[action_id] = _to_text(spec) if spec else ""
		if spec:
			_parsed[action_id] = spec


def matches(evt, action_id):
	"""True when this key event is the action's shortcut (exactly these modifiers)."""
	_ensure()
	spec = _parsed.get(action_id)
	if spec is None:
		return False
	code = evt.GetKeyCode()
	if code == wx.WXK_NUMPAD_ENTER:
		code = wx.WXK_RETURN
	return code == spec[3] and (evt.ControlDown(), evt.AltDown(), evt.ShiftDown()) == spec[:3]


def _key_display(key):
	name = _BY_CODE.get(key)
	if name is None:
		return chr(key).upper()
	return _NAMES.get(name) or (name.upper() if name[1:].isdigit() else name.capitalize())


def display_text(text):
	"""'ctrl+shift+pageup' -> 'Ctrl+Shift+PageUp'; '' when there is none."""
	spec = _parse(text)
	if spec is None:
		return ""
	ctrl, alt, shift, key = spec
	parts = [n for n, on in (("Ctrl", ctrl), ("Alt", alt), ("Shift", shift)) if on]
	parts.append(_key_display(key))
	return "+".join(parts)


def display(action_id):
	"""The shortcut in use, as shown in labels; '' when there is none."""
	_ensure()
	return display_text(_bindings.get(action_id, ""))


def with_key(text, action_id):
	"""'Search' -> 'Search (Ctrl+F)'; the text alone when the action has no shortcut."""
	key = display(action_id)
	return "%s (%s)" % (text, key) if key else text


def effective():
	"""action id -> shortcut text in use."""
	_ensure()
	return dict(_bindings)


def save(bindings):
	"""Keeps only what differs from the defaults."""
	global _bindings
	overrides = {}
	for action_id, dflt, _area, fixed in _DEFS:
		if fixed:
			continue
		text = bindings.get(action_id, dflt)
		if _norm(text) != _norm(dflt):
			overrides[action_id] = _norm(text)
	if _save:
		_save(json.dumps(overrides))
	_bindings = None


def event_to_text(evt):
	"""Shortcut text of a key press; None for a modifier alone; '' for a key that cannot be used."""
	code = evt.GetKeyCode()
	if code == wx.WXK_NUMPAD_ENTER:
		code = wx.WXK_RETURN
	if code in _MODIFIER_CODES:
		return None
	if code == ord("+") or not (code in _BY_CODE or 33 <= code <= 126):
		return ""
	return _to_text((evt.ControlDown(), evt.AltDown(), evt.ShiftDown(), code))


def _overlap(a, b):
	"""Areas whose keys reach the same window: the main window and its tabs share the keyboard."""
	return a == b or {a, b} == {"window", "tab"}


def conflicts(action_id, text, bindings=None):
	"""Ids of the other actions that already use this shortcut where it would clash."""
	spec = _parse(text)
	if spec is None:
		return []
	current = bindings if bindings is not None else effective()
	mine = area(action_id)
	found = []
	for other, _dflt, other_area, _fixed in _DEFS:
		if other != action_id and _overlap(mine, other_area) and _parse(current.get(other, "")) == spec:
			found.append(other)
	return found


def check(action_id, text, bindings=None):
	"""A translated error when the shortcut must not be used, else None. '' (no shortcut) is fine."""
	if not text:
		return None
	spec = _parse(text)
	if spec is None:
		# Translators: Shortcut error: the pressed key can't be a shortcut.
		return _("That key can't be used.")
	ctrl, alt, shift, key = spec
	if key in (wx.WXK_ESCAPE, wx.WXK_TAB) or (alt and key == wx.WXK_F4):
		# Translators: Shortcut error: Escape, Tab and Alt+F4 can't be reassigned.
		return _("This key is reserved.")
	if ord("1") <= key <= ord("9") and (ctrl or alt):
		# Translators: Shortcut error: Ctrl+number goes to a tab, Alt+number reads a tweet.
		return _("Ctrl or Alt with the digits 1 to 9 is used to go to a tab or to read a tweet.")
	printable = key not in _BY_CODE
	if printable and not (ctrl or alt):
		# Translators: Shortcut error: a letter, digit or symbol alone would stop typing and type-ahead search working.
		return _("Letters, digits and symbols need Ctrl or Alt.")
	if printable and ctrl and alt:
		# Translators: Shortcut error: Ctrl+Alt is how some keyboard layouts type extra characters.
		return _("Ctrl+Alt with a letter, digit or symbol is not allowed.")
	if not printable and not (ctrl or alt) and key not in _FKEYS:
		# Translators: Shortcut error: keys like Delete or Enter alone are fixed.
		return _("This key needs Ctrl or Alt.")
	if area(action_id) == "compose" and ((ctrl and key in _EDIT_LETTERS) or key in _EDIT_KEYS):
		# Translators: Shortcut error: the new tweet window needs this key to edit text.
		return _("This key is used for editing text.")
	clashes = conflicts(action_id, text, bindings)
	if clashes:
		# Translators: Shortcut error. {} is a comma separated list of the actions that already use the shortcut.
		return _("Already used by: {}.").format(", ".join(label(c) for c in clashes))
	return None


def area_label(area_id):
	return {
		# Translators: Area name on the Shortcuts page.
		"window": _("Main window"),
		# Translators: Area name on the Shortcuts page.
		"tab": _("Tweet lists"),
		# Translators: Area name on the Shortcuts page.
		"compose": _("New tweet window"),
		# Translators: Area name on the Shortcuts page.
		"details": _("Full tweet window"),
	}.get(area_id, area_id)


def label(action_id):
	global _labels
	if _labels is None:
		_labels = {
			# Translators: Shortcut action name.
			"check_updates": _("Check for updates"),
			# Translators: Shortcut action name.
			"check_all": _("Check all open tabs for updates"),
			# Translators: Shortcut action name.
			"search": _("Go to the search field (Search and explore tab)"),
			# Translators: Shortcut action name.
			"new_tweet": _("New tweet (New chat in the Chat tab)"),
			# Translators: Shortcut action name.
			"settings": _("Open settings"),
			# Translators: Shortcut action name.
			"remove_tab": _("Remove current tab"),
			# Translators: Shortcut action name.
			"rename_tab": _("Rename current tab"),
			# Translators: Shortcut action name.
			"move_tab_left": _("Move tab left"),
			# Translators: Shortcut action name.
			"move_tab_right": _("Move tab right"),
			# Translators: Shortcut action name.
			"reopen_tab": _("Bring back the last removed tab"),
			# Translators: Shortcut action name.
			"accounts_menu": _("Account menu"),
			# Translators: Shortcut action name.
			"close_window": _("Close the main window"),
			# Translators: Shortcut action name.
			"load_older": _("Load older tweets"),
			# Translators: Shortcut action name.
			"next_unread": _("Go to the next unread tweet"),
			# Translators: Shortcut action name.
			"open_row": _("Run the Enter key action (set in General)"),
			# Translators: Shortcut action name.
			"prev_user": _("Previous tweet involving the same user"),
			# Translators: Shortcut action name.
			"next_user": _("Next tweet involving the same user"),
			# Translators: Shortcut action name.
			"action_menu": _("Tweet action menu"),
			# Translators: Shortcut action name.
			"thread": _("View thread"),
			# Translators: Shortcut action name. The user menu offers the actions about a user of the tweet.
			"user_menu": _("User menu"),
			# Translators: Shortcut action name.
			"user_timeline": _("View user timeline"),
			# Translators: Shortcut action name.
			"user_info": _("Show user information"),
			# Translators: Shortcut action name.
			"user_pinned": _("View pinned tweet"),
			# Translators: Shortcut action name.
			"user_followers": _("View user followers"),
			# Translators: Shortcut action name.
			"user_following": _("View who the user follows"),
			# Translators: Shortcut action name.
			"user_known_followers": _("View followers of the user that you know"),
			# Translators: Shortcut action name.
			"user_message": _("Start a chat with the user"),
			# Translators: Shortcut action name. The state is checked first: it follows or unfollows.
			"user_follow": _("Follow or unfollow the user"),
			# Translators: Shortcut action name. The state is checked first: it mutes or unmutes.
			"user_mute": _("Mute or unmute the user"),
			# Translators: Shortcut action name. The state is checked first: it blocks or unblocks.
			"user_block": _("Block or unblock the user"),
			# Translators: Shortcut action name.
			"user_copy_url": _("Copy user profile URL"),
			# Translators: Shortcut action name.
			"user_open_web": _("Open user profile on x.com"),
			# Translators: Shortcut action name.
			"quotes": _("View quotes"),
			# Translators: Shortcut action name.
			"reply": _("Reply"),
			# Translators: Shortcut action name.
			"retweet": _("Retweet or undo retweet"),
			# Translators: Shortcut action name.
			"like": _("Like or unlike"),
			# Translators: Shortcut action name.
			"bookmark": _("Bookmark or remove bookmark"),
			# Translators: Shortcut action name.
			"hashtags": _("Search a hashtag"),
			# Translators: Shortcut action name.
			"quote_tweet": _("Quote tweet"),
			# Translators: Shortcut action name.
			"open_browser": _("Open in browser"),
			# Translators: Shortcut action name.
			"links": _("Open a link"),
			# Translators: Shortcut action name.
			"full_tweet": _("Show full tweet"),
			# Translators: Shortcut action name.
			"copy_row": _("Copy author, text and link"),
			# Translators: Shortcut action name.
			"copy_text": _("Copy tweet text"),
			# Translators: Shortcut action name.
			"copy_link": _("Copy link to tweet"),
			# Translators: Shortcut action name.
			"select_all": _("Select all tweets"),
			# Translators: Shortcut action name.
			"jump_row": _("Jump to a row number"),
			# Translators: Shortcut action name. It works in the tab of a search.
			"edit_search": _("Edit this search"),
			# Translators: Shortcut action name.
			"clear_cache": _("Clear the cache of this tab"),
			# Translators: Shortcut action name.
			"mark_read": _("Mark as read"),
			# Translators: Shortcut action name.
			"mark_unread": _("Mark as unread"),
			# Translators: Shortcut action name.
			"mark_all_read": _("Mark all as read"),
			# Translators: Shortcut action name.
			"open_image": _("Open image"),
			# Translators: Shortcut action name.
			"bemyeyes": _("Send image to Be My Eyes"),
			# Translators: Shortcut action name.
			"copy_image": _("Copy image to clipboard"),
			# Translators: Shortcut action name.
			"open_video": _("Open video or GIF"),
			# Translators: Shortcut action name.
			"download": _("Download media"),
			# Translators: Shortcut action name.
			"send_tweet": _("Post the tweet or send the message"),
			# Translators: Shortcut action name.
			"close_compose": _("Close the new tweet window"),
			# Translators: Shortcut action name.
			"refresh_details": _("Refresh the details"),
			# Translators: Shortcut action name.
			"copy_line": _("Copy the selected line"),
		}
	return _labels.get(action_id, action_id)