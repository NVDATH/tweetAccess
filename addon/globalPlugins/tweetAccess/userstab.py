# -*- coding: utf-8 -*-
# tweetAccess - userstab.py: a tab with a list of users (the followers, the following or the followers you know
# of one user). Rows are plain dicts {id, handle, name, bio} kept in the account's cache like the tweets of the
# other tabs, so the tab comes back with its rows after a restart. F5 reads the list again, Shift+F5 the next
# page. Alt+U and every shortcut of the user menu act on the user of the focused row. Enter runs the Enter key
# action: a user action runs on the row's user, any other action opens the user menu.

import addonHandler
import ui
import wx
from logHandler import log

from . import keymap, soundpack, tabstate
from .useractions import ENTER_USER_ACTIONS, USER_ACTIONS
from .utils import TAG, announce_now, safe_ui

addonHandler.initTranslation()

FOCUS_SAVE_DELAY_MS = 800


class UsersTab(wx.Panel):
	TAB_TEMP_TYPE = "users"

	def __init__(self, parent, plugin, params, rows, name, origin_key=None):
		super().__init__(parent)
		self._plugin = plugin
		self._cfg = plugin.config
		self._dead = False
		self._params = dict(params)  # kind, handle and, once known, uid
		self._kind = self._params["kind"]
		self.TAB_TEMP_KEY = self._params.get("key") or "%s:%s" % (self._kind, self._params["handle"])
		self.TAB_NAME = name
		self._originTabKey = origin_key
		self._tab_aid = plugin.account_id()
		self._rows = [dict(r) for r in rows]
		self._tried = bool(rows)  # a tab with nothing saved loads itself the first time it is shown
		self._parked = False  # focus is waiting on the toolbar button because the list is empty
		self._quiet = 0  # >0: focus events come from our own code, not the reader
		self._focus_pending = None
		self.park_control = None  # where the focus goes when the list is emptied (a People tab gives its radio group)
		self._left = {}  # handle -> (index, row) of a user who left the list at once, for when X refuses

		self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
		# Translators: Column headers of a list of users.
		self.list.InsertColumn(0, _("User name"), width=170)
		self.list.InsertColumn(1, _("Display name"), width=200)
		self.list.InsertColumn(2, _("Bio"), width=420)
		# Translators: Button that opens the user menu (Alt+U) of the user in the list.
		self.user_btn = wx.Button(self, label=_("&User action"))
		self.status = wx.StatusBar(self)
		# Translators: Accessible name of the status bar of a tab with a list of users.
		self.status.SetName(_("Users status"))
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
		sizer.Add(self.user_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		sizer.Add(self.status, 0, wx.EXPAND)
		self.SetSizer(sizer)

		self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_activate)
		self.list.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.on_item_focused)
		self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_user_action)
		self.user_btn.Bind(wx.EVT_BUTTON, self.on_user_action)
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
		self._focus_timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._on_focus_timer, self._focus_timer)
		self._render(self._remembered_id())

	# ------------------------------------------------------------ the list

	def _focus_key(self):
		return "tabfocus:users:%s" % self.TAB_TEMP_KEY

	def _remembered_id(self):
		return self._plugin.store.get_account_meta(self._tab_aid, self._focus_key()) or None

	def _render(self, keep_id=None):
		"""Fill the list from the rows; the focus goes to the row of keep_id, else to the first one."""
		self._quiet += 1
		self.list.Freeze()
		try:
			self.list.DeleteAllItems()
			for i, row in enumerate(self._rows):
				self.list.InsertItem(i, "@" + row["handle"])
				self.list.SetItem(i, 1, row["name"])
				self.list.SetItem(i, 2, row["bio"])
		finally:
			self.list.Thaw()
			self._quiet -= 1
		if self._rows:
			self._select(next((i for i, r in enumerate(self._rows) if r["id"] == keep_id), 0))
		self._update_status()
		self._leave_toolbar_button()
		if not self._rows:
			wx.CallAfter(self._park)

	def _select(self, idx):
		self._quiet += 1
		try:
			self.list.Focus(idx)
			self.list.Select(idx)
			self.list.EnsureVisible(idx)
		finally:
			self._quiet -= 1

	def _update_status(self):
		total = len(self._rows)
		# Translators: Status bar of a tab with a list of users. {total} is the number of users.
		self.status.SetStatusText(_("{total} users").format(total=total))
		self.user_btn.Show(total > 0)
		self.user_btn.Enable(total > 0)
		self.Layout()

	def _focused_index(self):
		idx = self.list.GetFocusedItem()
		if idx < 0:
			idx = self.list.GetFirstSelected()
		return idx if 0 <= idx < len(self._rows) else -1

	def _focused_row(self):
		idx = self._focused_index()
		return self._rows[idx] if idx >= 0 else None

	def _current_post_id(self):
		"""The id of the focused user (named like the tweet tabs' method: a removed tab keeps its place)."""
		row = self._focused_row()
		return row["id"] if row is not None else None

	def _persist(self):
		try:
			tabstate.set_cache(self._plugin.store, self._tab_aid, "users", self.TAB_TEMP_KEY, self._rows)
		except Exception:
			log.error(TAG + "could not save the users of a tab", exc_info=True)

	def snapshot(self):
		return [dict(r) for r in self._rows]

	# ------------------------------------------------------------ focus and place

	def _restoreFocusPosition(self, moveFocus=True):
		if not moveFocus:
			return
		if self._rows:
			self._parked = False
			self.list.SetFocus()
		else:
			self._parked = True
			button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
			if button is not None:
				button.SetFocus()

	def _leave_toolbar_button(self):
		"""The list was empty, so focus waited on the toolbar button: now it has rows, move it in.
		Only when focus is still on that button: a reader who is elsewhere is not disturbed."""
		if not self._parked or not self._rows:
			return
		button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
		try:
			if button is not None and self.IsShownOnScreen() and self.FindFocus() is button:
				self._parked = False
				self.list.SetFocus()
		except RuntimeError:
			pass

	def _park(self):
		"""An emptied list that keeps the focus makes NVDA say "unknown": the focus moves to another control."""
		try:
			if self.FindFocus() is not self.list or self._rows:
				return
			target = self.park_control or getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
			if target is not None:
				self._parked = True
				target.SetFocus()
		except RuntimeError:
			pass

	def ensure_loaded(self):
		"""A list with nothing saved reads itself the first time it is shown."""
		if not self._tried and not self._rows:
			self._tried = True
			self.refresh()

	def onTabActivated(self):
		ui.message(_("{name} tab").format(name=self.TAB_NAME))
		self._restoreFocusPosition()
		self.ensure_loaded()

	def on_item_focused(self, event):
		if not self._quiet:
			row = self._focused_row()
			if row is not None:
				self._focus_pending = row["id"]
				self._focus_timer.StartOnce(FOCUS_SAVE_DELAY_MS)
		event.Skip()

	def _on_focus_timer(self, event):
		self.flush_state()

	def flush_state(self):
		self._focus_timer.Stop()
		if self._focus_pending is None:
			return
		user_id, self._focus_pending = self._focus_pending, None
		try:
			self._plugin.store.set_account_meta(self._tab_aid, self._focus_key(), str(user_id))
		except Exception:
			log.error(TAG + "could not save the reading position of a tab", exc_info=True)

	def reload_display(self):
		pass

	# ------------------------------------------------------------ loading

	def _options(self, more=False):
		options = {
			"kind": self._kind, "params": self._params, "chain": self.TAB_TEMP_KEY,
			"pages": self._cfg.get("feed_pages"),
			# the account's own following, muted and blocked are read whole: that is what the table of relations needs
			"all": bool(self._params.get("own")) and self._kind in ("following", "muted", "blocked"),
		}
		if more:
			options["have"] = [r["id"] for r in self._rows]
		return options

	def refresh(self, done=None, quiet=False):
		"""Read the list again. With done, stay silent and call done(changed) at the end."""
		callback = lambda result: self.on_refreshed(result, done, quiet)
		if not self._plugin.start_job("users", callback, self._options(), progress=not quiet, background=quiet):
			if done is None:
				ui.message(_("Still loading, please wait."))
			return False
		if done is None:
			ui.message(_("Loading, please wait."))
		return True

	def onCheckForUpdates(self, evt=None):
		self.refresh()

	@safe_ui
	def on_refreshed(self, result, done, quiet=False):
		if not result["ok"]:
			if not quiet:
				soundpack.play("error")
				ui.message(result["message"])
			if done:
				done(False)
			return
		if result.get("uid") and not self._params.get("uid"):
			self._params["uid"] = result["uid"]
		rows = result["users"]
		changed = [r["id"] for r in rows] != [r["id"] for r in self._rows]
		keep_id = self._current_post_id()
		self._rows = rows
		self._render(keep_id)
		self._persist()
		if done:
			done(changed)
			return
		# Translators: Announced after reading a list of users again. {count} is the number of users.
		text = _("{count} users.").format(count=len(rows))
		if result.get("limited"):
			text += ". " + _("X limited the requests, so only part of the list was loaded. Try again later.")
		ui.message(text)

	def onFetchOlder(self):
		"""Shift+F5: the next page after the users the tab shows."""
		if not self._rows:
			ui.message(_("Nothing loaded yet."))
			return
		if not self._plugin.start_job("users", self.on_more_result, self._options(more=True)):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading more users, please wait."))

	@safe_ui
	def on_more_result(self, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		known = {r["id"] for r in self._rows}
		fresh = [r for r in result["users"] if r["id"] not in known]
		keep_id = self._current_post_id()
		self._rows.extend(fresh)
		self._render(keep_id)
		self._persist()
		# Translators: Announced after Shift+F5 in a list of users. {count} is the number of users added.
		text = _("{count} more users loaded.").format(count=len(fresh)) if fresh else _("No more users.")
		if result.get("limited"):
			text += " " + _("X limited the requests, so only part of the list was loaded. Try again later.")
		ui.message(text)

	# ------------------------------------------------------------ actions on the user of a row

	def _run(self, action, row):
		"""An entry of the user menu on this row's user. User names are lowercase in tab names and keys."""
		self.GetTopLevelParent().run_user_action(action, None, row["handle"].lower(), row["id"])

	def _menu(self, row):
		self.GetTopLevelParent().show_user_menu(None, row["handle"].lower(), row["id"])

	def on_user_action(self, event=None):
		row = self._focused_row()
		if row is None:
			ui.message(_("No user selected."))
			return
		self._menu(row)

	def on_activate(self, event):
		idx = event.GetIndex()
		if not 0 <= idx < len(self._rows):
			return
		row = self._rows[idx]
		action = ENTER_USER_ACTIONS.get(self._cfg.get("enter_action"))  # Settings > General > Enter key action
		if action:
			self._run(action, row)
		else:
			self._menu(row)

	def _copy_row(self):
		row = self._focused_row()
		if row is None:
			return
		who = "%s (@%s)" % (row["name"], row["handle"]) if row["name"] else "@" + row["handle"]
		text = "%s (https://x.com/%s)" % (", ".join(p for p in (who, row["bio"]) if p), row["handle"])
		if wx.TheClipboard.Open():
			try:
				wx.TheClipboard.SetData(wx.TextDataObject(text))
			finally:
				wx.TheClipboard.Close()
			announce_now(_("Copied."))
		else:
			announce_now(_("Could not copy to the clipboard."))

	def on_char_hook(self, event):
		if keymap.matches(event, "check_updates"):
			self.onCheckForUpdates(None)
			return
		if keymap.matches(event, "load_older"):
			self.onFetchOlder()
			return
		if keymap.matches(event, "user_menu"):
			self.on_user_action()
			return
		for action in USER_ACTIONS:
			if keymap.matches(event, action):
				row = self._focused_row()
				if row is None:
					ui.message(_("No user selected."))
				else:
					self._run(action, row)
				return
		if self.FindFocus() is self.list and keymap.matches(event, "copy_row"):
			self._copy_row()
			return
		event.Skip()

	_UNDO_OF_LIST = {"following": "unfollow", "muted": "unmute", "blocked": "unblock"}

	def _is_mine(self):
		"""Is this one of the account's own lists: the users it follows, mutes or blocks?"""
		if self._kind in ("muted", "blocked") or self._params.get("own"):
			return True
		acc = next((a for a in self._plugin.store.list_accounts() if a["id"] == self._tab_aid), None)
		return self._kind == "following" and self._params["handle"].lower() == ((acc or {}).get("handle") or "").lower()

	def relation_changed(self, handle, action):
		"""The reader undid a relation: the list of the users the account follows, mutes or blocks loses that user
		at once, before X has answered."""
		if self._UNDO_OF_LIST.get(self._kind) != action or not self._is_mine():
			return
		rows = [r for r in self._rows if r["handle"].lower() != handle]
		if len(rows) == len(self._rows):
			return
		index = next(i for i, r in enumerate(self._rows) if r["handle"].lower() == handle)
		self._left[handle] = (index, self._rows[index])
		self._rows = rows
		self._render(rows[min(index, len(rows) - 1)]["id"] if rows else None)
		self._persist()

	def relation_failed(self, handle, action):
		"""X refused what was said done at once: the user is back in the list, where they were."""
		left = self._left.pop(handle, None)
		if left is None or self._UNDO_OF_LIST.get(self._kind) != action:
			return
		if any(r["handle"].lower() == handle for r in self._rows):
			return
		index, row = left
		self._rows.insert(min(index, len(self._rows)), row)
		self._render(row["id"])
		self._persist()

	# ------------------------------------------------------------ removable tab

	def onTabRemoved(self):
		self._dead = True
		for value in vars(self).values():
			if isinstance(value, wx.Timer):
				value.Stop()
		store = self._plugin.store
		tabstate.remove_open_tab(store, self._tab_aid, "users", self.TAB_TEMP_KEY)
		tabstate.delete_cache(store, self._tab_aid, "users", self.TAB_TEMP_KEY)
		store.set_account_meta(self._tab_aid, self._focus_key(), "")
		self.GetTopLevelParent().jump_to_permanent(self._originTabKey)

	def onTabRenamed(self, new_name):
		self.TAB_NAME = new_name
		tabstate.set_tab_name(self._plugin.store, self._tab_aid, "users", self.TAB_TEMP_KEY, new_name)


class PeopleTab(wx.Panel):
	"""The account's own people in one tab: whom it follows, who follows it, the users it muted and blocked, after
	NVSky's People tab. Each list is a UsersTab inside this tab, and the radio group picks the one that shows.
	The lists are shown from the cache; one that was never read reads itself, and F5 reads it again. The lists of
	whom it follows, mutes and blocks are read whole: that is what tells Follow, Mute and Block what the account
	does now (see service._users). Ctrl+F5 leaves this tab alone, it would read them all."""

	TAB_KEY = "people"

	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._plugin = plugin
		self._dead = False
		# Translators: Name of the People tab.
		self.TAB_NAME = _("People")
		aid = plugin.account_id()
		acc = next((a for a in plugin.store.list_accounts() if a["id"] == aid), None)
		own = ((acc or {}).get("handle") or "").lower()
		self._kinds = ("following", "followers", "muted", "blocked")
		names = {
			# Translators: Choices of the list in the People tab, which are also the names of the lists.
			"following": _("Following"), "followers": _("Followers"),
			"muted": _("Muted users"), "blocked": _("Blocked users"),
		}
		# The radio group is made before the lists: Tab goes through the controls in the order they were made, and
		# it must reach the group first. The focus itself goes to the list (see _restoreFocusPosition).
		sizer = wx.BoxSizer(wx.VERTICAL)
		self.kindRadio = wx.RadioBox(
			# Translators: Label of the radio group of lists in the People tab.
			self, label=_("&List:"), choices=[names[k] for k in self._kinds], majorDimension=1,
			style=wx.RA_SPECIFY_ROWS,
		)
		sizer.Add(self.kindRadio, 0, wx.ALL, 8)
		self._children = []
		for kind in self._kinds:
			key = "people:%s" % kind
			rows = tabstate.get_cache(plugin.store, aid, "users", key) or []
			child = UsersTab(self, plugin, {"kind": kind, "handle": own, "own": True, "key": key}, rows, names[kind])
			child.park_control = self.kindRadio
			self._children.append(child)
			sizer.Add(child, 1, wx.EXPAND)
		self.SetSizer(sizer)
		self._sizer = sizer
		saved = plugin.store.get_account_meta(aid, "people_list")
		self.kindRadio.SetSelection(self._kinds.index(saved) if saved in self._kinds else 0)
		self._show()
		self.kindRadio.Bind(wx.EVT_RADIOBOX, self._on_kind)

	def _active(self):
		return self._children[max(0, self.kindRadio.GetSelection())]

	def _show(self):
		active = self._active()
		for child in self._children:
			self._sizer.Show(child, child is active)
		self.Layout()

	def _on_kind(self, event):
		self._show()
		self._plugin.store.set_account_meta(
			self._plugin.account_id(), "people_list", self._kinds[self.kindRadio.GetSelection()]
		)
		self._active().ensure_loaded()

	def onTabActivated(self):
		ui.message(_("{name} tab").format(name=self.TAB_NAME))
		self._active().ensure_loaded()
		self._restoreFocusPosition()

	def _restoreFocusPosition(self, moveFocus=True):
		if not moveFocus:
			return
		child = self._active()
		if child._rows:
			child._restoreFocusPosition()
		else:
			self.kindRadio.SetFocus()

	def refresh(self, done=None, quiet=False):
		if done is not None:  # Ctrl+F5 and the background checks: the lists are read whole, only F5 does that
			done(False)
			return True
		return self._active().refresh()

	def onCheckForUpdates(self, evt=None):
		self._active().refresh()

	def flush_state(self):
		for child in self._children:
			child.flush_state()

	def reload_display(self):
		pass

	def relation_changed(self, handle, action):
		for child in self._children:
			child.relation_changed(handle, action)

	def relation_failed(self, handle, action):
		for child in self._children:
			child.relation_failed(handle, action)