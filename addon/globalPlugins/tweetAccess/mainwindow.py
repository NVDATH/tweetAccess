# -*- coding: utf-8 -*-
# NVTweet - mainwindow.py: the main window, built like NVSky's: one toolbar shared by every
# tab, a plain wx.Notebook (not AUI, which NVDA reads badly) and removable tabs.
#
#   Ctrl+W close the tab, Ctrl+P settings, Ctrl+F2 rename the tab, Ctrl+1..9 go to a tab,
#   Ctrl+Shift+PageUp/PageDown move the tab, Ctrl+F5 check every tab, F5 check this tab,
#   Escape close the window.

import webbrowser

import addonHandler
import ui
import wx
from logHandler import log

from . import keymap, soundpack, tabstate
from .dialogs import UserInfoDialog
from .searchdialog import SearchDialog
from .tabs import QuotesPanel, ThreadPanel, create_feed_tab, make_temp_tab
from .useractions import populate_user_menu, relation_texts
from .userstab import UsersTab
from .utils import (
	APP_NAME, TAG, announce_now, author_handle, handle_from_url, load_stats, main_title, quotes_tab_name,
	hashtags_of, mentions_of, relevant_users, safe_ui, target_id, users_tab_name,
)

addonHandler.initTranslation()


class MainWindow(wx.Frame):
	def __init__(self, parent, plugin):
		super().__init__(parent, title=APP_NAME, size=(1000, 600))
		self._plugin = plugin
		self.closed = False
		self._dead = False
		self.home = None
		self._noFocusGrab = False
		# True while tabs are being built: page-changed events then stay silent.
		self._activationSuppressed = True

		# One panel holds everything: a Frame has no Tab traversal of its own.
		panel = wx.Panel(self)
		frame_sizer = wx.BoxSizer(wx.VERTICAL)
		frame_sizer.Add(panel, 1, wx.EXPAND)
		self.SetSizer(frame_sizer)

		sizer = wx.BoxSizer(wx.VERTICAL)
		toolbar = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Toolbar buttons. The shortcut is added after the label, so there is no access key.
		self.checkUpdatesButton = wx.Button(panel, label=_("Check for updates"))
		self.searchButton = wx.Button(panel, label=_("Search"))
		self.newButton = wx.Button(panel, label=_("New tweet"))
		self.settingsButton = wx.Button(panel, label=_("Settings"))
		self.removeTabButton = wx.Button(panel, label=_("Remove current tab"))
		self._button_labels = [
			(button, button.GetLabel(), action_id) for button, action_id in (
				(self.checkUpdatesButton, "check_updates"), (self.searchButton, "search"),
				(self.newButton, "new_tweet"), (self.settingsButton, "settings"),
				(self.removeTabButton, "remove_tab"),
			)
		]
		self.refresh_labels()
		# Translators: Toolbar button that closes the window.
		self.closeButton = wx.Button(panel, label=_("&Close"))
		for button in (
			self.checkUpdatesButton, self.searchButton, self.newButton, self.settingsButton, self.removeTabButton,
			self.closeButton,
		):
			toolbar.Add(button, 0, wx.RIGHT, 5)
		sizer.Add(toolbar, 0, wx.ALL, 8)

		self.notebook = wx.Notebook(panel)
		sizer.Add(self.notebook, 1, wx.EXPAND)
		panel.SetSizer(sizer)

		self.checkUpdatesButton.Bind(wx.EVT_BUTTON, self.onCheckForUpdates)
		self.searchButton.Bind(wx.EVT_BUTTON, lambda e: self.search())
		self.newButton.Bind(wx.EVT_BUTTON, lambda e: self._plugin.open_compose())
		self.settingsButton.Bind(wx.EVT_BUTTON, lambda e: self._plugin.open_settings())
		self.removeTabButton.Bind(wx.EVT_BUTTON, lambda e: self.removeCurrentTab())
		self.closeButton.Bind(wx.EVT_BUTTON, lambda e: self.Close())
		self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self.onPageChanging)
		self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.onPageChanged)
		self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
		self.Bind(wx.EVT_CLOSE, self.onClose)
		self.update_title()
		self.CentreOnScreen()

	def update_title(self):
		"""Tab name first, then the account, then the add-on name."""
		index = self.notebook.GetSelection()
		tab = getattr(self.notebook.GetPage(index), "TAB_NAME", "") if index != wx.NOT_FOUND else ""
		self.SetTitle(main_title(tab, self._plugin.account_label()))

	# ------------------------------------------------------------ calls from the plugin

	def merge_result(self, result):
		return self.home.merge_result(result) if self.home is not None else 0

	def refresh_labels(self):
		"""The shortcuts shown on the toolbar buttons follow the Shortcuts page."""
		for button, text, action_id in self._button_labels:
			label = keymap.with_key(text, action_id)
			if button.GetLabel() != label:
				button.SetLabel(label)
		self.Layout()

	def reload_display(self):
		for panel in self.getOpenTabs():
			panel.reload_display()

	def flush_state(self):
		for panel in self.getOpenTabs():
			panel.flush_state()

	def focus_current(self):
		index = self.notebook.GetSelection()
		if index != wx.NOT_FOUND:
			self.notebook.GetPage(index)._restoreFocusPosition()

	def rebuild(self, builder, focus=True):
		"""The active account changed: drop every tab and build them again.
		focus=False: never take focus (the settings window still has the user's attention)."""
		self._activationSuppressed = True
		self._noFocusGrab = not focus
		# Adding a page makes wx activate this whole window (the log shows it comes from AddPage).
		# A window that is not the active one is disabled while it is rebuilt: it cannot take the
		# focus from the other window then.
		shield = not focus and not self.IsActive()
		if shield:
			self.Disable()
		try:
			self.flush_state()
			for panel in self.getOpenTabs():
				panel._dead = True
				for value in vars(panel).values():
					if isinstance(value, wx.Timer):
						value.Stop()
			self.notebook.DeleteAllPages()
			self.home = None
			builder()
			self.update_title()
			self.activate_initial(focus)
		finally:
			if shield:
				self.Enable()
		if not focus:
			# addTab queued focus grabs: lift the block only after they have run
			wx.CallAfter(setattr, self, "_noFocusGrab", False)

	# ------------------------------------------------------------ tabs

	def addTab(self, panel, label, select=True, removable=True, play_sound=True, position=None):
		panel.TAB_REMOVABLE = removable
		if removable and play_sound:
			soundpack.play("open_tab")
		if position is None:
			self.notebook.AddPage(panel, label, select)
		else:
			self.notebook.InsertPage(min(position, self.notebook.GetPageCount()), panel, label, select)
		self._updateRemoveTabButton()
		if select:
			wx.CallAfter(self._focusPanel, panel)

	@safe_ui
	def _focusPanel(self, panel):
		if self._noFocusGrab:
			return
		try:
			panel._restoreFocusPosition()
		except RuntimeError:
			pass  # the tab is gone already

	def getOpenTabs(self):
		return [self.notebook.GetPage(i) for i in range(self.notebook.GetPageCount())]

	def _getTabIdentity(self, panel):
		key = getattr(panel, "TAB_KEY", None)
		if key:
			return {"kind": "permanent", "key": key}
		kind, key = getattr(panel, "TAB_TEMP_TYPE", None), getattr(panel, "TAB_TEMP_KEY", None)
		if kind and key is not None:
			return {"kind": kind, "key": key}
		return None

	def _origin_key(self):
		index = self.notebook.GetSelection()
		identity = self._getTabIdentity(self.notebook.GetPage(index)) if index != wx.NOT_FOUND else None
		return identity["key"] if identity and identity["kind"] == "permanent" else None

	def jump_to_permanent(self, key):
		if not key:
			return
		for i, panel in enumerate(self.getOpenTabs()):
			if getattr(panel, "TAB_KEY", None) == key:
				self.notebook.SetSelection(i)
				return

	def focusTabByIdentity(self, identity):
		for i, panel in enumerate(self.getOpenTabs()):
			if self._getTabIdentity(panel) == identity:
				self.notebook.SetSelection(i)
				return True
		return False

	def _persist_tab_order(self):
		aid = self._plugin.account_id()
		if aid is None:
			return
		order = []
		for panel in self.getOpenTabs():
			identity = self._getTabIdentity(panel)
			if identity:
				order.append([identity["kind"], identity["key"]])
		tabstate.set_order(self._plugin.store, aid, order)

	def _updateRemoveTabButton(self):
		index = self.notebook.GetSelection()
		if index == wx.NOT_FOUND:
			self.removeTabButton.Hide()
		else:
			self.removeTabButton.Show(getattr(self.notebook.GetPage(index), "TAB_REMOVABLE", True))
		self.removeTabButton.GetContainingSizer().Layout()

	def onPageChanging(self, evt):
		# Focus on the notebook before the swap: a focused control that gets hidden makes
		# Windows move focus into the new page too, which doubles NVDA's announcement.
		# Never while this window is not the active one (a rebuild from the settings window, a
		# background sync): SetFocus on a control activates its whole window and steals the focus.
		if not self._activationSuppressed and not self._noFocusGrab and self.IsActive():
			self.notebook.SetFocus()
		evt.Skip()

	def onPageChanged(self, evt):
		index = evt.GetSelection()
		self.update_title()
		if index != wx.NOT_FOUND and not self._activationSuppressed:
			panel = self.notebook.GetPage(index)
			onActivated = getattr(panel, "onTabActivated", None)
			if callable(onActivated):
				onActivated()
			identity = self._getTabIdentity(panel)
			aid = self._plugin.account_id()
			if identity and aid is not None:
				tabstate.set_last(self._plugin.store, aid, identity)
		self._updateRemoveTabButton()
		evt.Skip()

	def activate_initial(self, focus=True):
		"""After all tabs were added: select the tab used last, silently.
		focus=False: do not move focus and do not speak."""
		aid = self._plugin.account_id()
		last = tabstate.get_last(self._plugin.store, aid) if aid is not None else None
		index = 0
		for i, panel in enumerate(self.getOpenTabs()):
			if last and self._getTabIdentity(panel) == last:
				index = i
				break
		if self.notebook.GetPageCount():
			if index != self.notebook.GetSelection():
				self.notebook.SetSelection(index)
		self._activationSuppressed = False
		self.update_title()
		self._updateRemoveTabButton()
		if not focus:
			return
		if self.notebook.GetPageCount():
			wx.CallAfter(self._focusPanel, self.notebook.GetPage(index))
		if self.home is not None:
			wx.CallAfter(self.home.initial_announce)

	def moveCurrentTab(self, delta):
		index = self.notebook.GetSelection()
		if index == wx.NOT_FOUND:
			return
		new_index = index + delta
		if new_index < 0 or new_index >= self.notebook.GetPageCount():
			ui.message(_("Can't move the tab further in that direction."))
			return
		panel = self.notebook.GetPage(index)
		label = self.notebook.GetPageText(index)
		self._activationSuppressed = True  # RemovePage and InsertPage would each announce
		try:
			self.notebook.RemovePage(index)
			self.notebook.InsertPage(new_index, panel, label, select=True)
		finally:
			self._activationSuppressed = False
		panel.onTabActivated()
		self._persist_tab_order()

	def removeCurrentTab(self):
		index = self.notebook.GetSelection()
		if index == wx.NOT_FOUND:
			return
		panel = self.notebook.GetPage(index)
		if not getattr(panel, "TAB_REMOVABLE", True):
			ui.message(_("This tab can't be removed."))
			return
		self._plugin.remember_closed_tab(panel, index)  # before its data goes: Ctrl+Shift+T brings it back
		panel.onTabRemoved()
		soundpack.play("close_tab")
		self.notebook.DeletePage(index)
		self._persist_tab_order()

	def reopen_closed_tab(self):
		"""Ctrl+Shift+T: the tab removed last comes back, with its tweets, at the place it had."""
		item = self._plugin.pop_closed_tab()
		if item is None:
			ui.message(_("No closed tab to bring back."))
			return
		entry, posts, position, focus_id = item
		if self.focusTabByIdentity({"kind": entry["type"], "key": entry["key"]}):
			return  # the same tab was opened again meanwhile
		aid = self._plugin.account_id()
		store = self._plugin.store
		if focus_id is not None:  # before the tab is built: it reads its place when it is made
			store.set_account_meta(aid, "tabfocus:%s:%s" % (entry["type"], entry["key"]), str(focus_id))
		panel = make_temp_tab(self.notebook, self._plugin, entry, posts)
		if panel is None:
			return
		tabstate.add_open_tab(store, aid, entry)
		tabstate.set_cache(store, aid, entry["type"], entry["key"], posts)
		self.addTab(panel, panel.TAB_NAME, select=True, removable=True, position=position)
		self._persist_tab_order()

	def renameCurrentTab(self):
		index = self.notebook.GetSelection()
		if index == wx.NOT_FOUND:
			return
		panel = self.notebook.GetPage(index)
		if not getattr(panel, "TAB_REMOVABLE", True):
			ui.message(_("This tab can't be renamed."))
			return
		dlg = wx.TextEntryDialog(
			# Translators: Prompt and title of the rename-tab window.
			self, _("New tab name:"), _("Rename tab"), value=panel.TAB_NAME,
		)
		if dlg.ShowModal() == wx.ID_OK:
			name = dlg.GetValue().strip()
			if name:
				panel.onTabRenamed(name)
				self.notebook.SetPageText(index, name)
				self.update_title()
				ui.message(_("Tab renamed to {name}.").format(name=name))
		dlg.Destroy()

	# ------------------------------------------------------------ refreshing

	def onCheckForUpdates(self, evt=None):
		index = self.notebook.GetSelection()
		if index != wx.NOT_FOUND:
			self.notebook.GetPage(index).onCheckForUpdates(None)

	def checkAllOpenTabs(self):
		"""Ctrl+F5: every tab one after the other, one summary at the end."""
		panels = self.getOpenTabs()
		changed_names = []
		ui.message(_("Checking all open tabs for updates, please wait..."))

		def step(i):
			if self.closed:
				return
			if i >= len(panels):
				if changed_names:
					soundpack.play("ready")
					ui.message(_("Updates in: {names}.").format(names=", ".join(changed_names)))
				else:
					ui.message(_("No new updates in any open tab."))
				return

			def done(changed, panel=panels[i]):
				if changed:
					changed_names.append(panel.TAB_NAME)
				wx.CallLater(300, step, i + 1)

			try:
				started = panels[i].refresh(done)
			except RuntimeError:
				started = False
			if not started:
				ui.message(_("Still loading, please wait."))

		step(0)

	# ------------------------------------------------------------ thread and quotes

	def open_related(self, kind, post):
		"""Fetch the thread or the quotes of a tweet, then open them in a tab of their own."""
		tweet_id = target_id(post)
		if kind == "quotes" and self.focusTabByIdentity({"kind": "quotes", "key": tweet_id}):
			return
		origin = self._origin_key()
		handle = handle_from_url(post.url)
		job = lambda result: self._related_fetched(kind, tweet_id, post, origin, handle, result)
		options = {"id": tweet_id, "handle": handle, "pages": self._plugin.config.get("feed_pages")}
		if not self._plugin.start_job(kind, job, options):
			announce_now(_("Still loading, please wait."))
			return
		# announce_now: this runs right after a menu closed, and the focus announcement would mask it
		announce_now(_("Loading thread, please wait.") if kind == "thread" else _("Loading quotes, please wait."))

	@safe_ui
	def _related_fetched(self, kind, tweet_id, post, origin, handle, result):
		if not result["ok"]:
			soundpack.play("error")
			announce_now(result["message"])
			return
		aid = self._plugin.account_id()
		if result.get("account_id") != aid:
			return  # the account was switched meanwhile
		posts = result["posts"]
		store = self._plugin.store
		if kind == "thread":
			if not posts:
				announce_now(_("Could not load the thread."))
				return
			key = str(posts[0].id)
			if self.focusTabByIdentity({"kind": "thread", "key": key}):
				return
			panel = ThreadPanel(self.notebook, self._plugin, posts, tweet_id, key, origin_key=origin)
			entry = {"type": "thread", "key": key, "target": tweet_id, "origin": origin, "name": panel.TAB_NAME}
		else:
			if not posts:
				if load_stats(post.stats).get("quotes") == 0:
					announce_now(_("This tweet has no quotes."))
				else:
					announce_now(
						# Translators: {name} is the add-on name.
						_("No quotes were found. If the tweet shows quotes on the web, X did not return them to {name}.")
						.format(name=APP_NAME)
					)
				return
			name = quotes_tab_name(post)
			panel = QuotesPanel(self.notebook, self._plugin, posts, tweet_id, name, origin_key=origin, handle=handle)
			entry = {
				"type": "quotes", "key": tweet_id, "target": tweet_id, "origin": origin, "name": name,
				"handle": handle,
			}
		tabstate.add_open_tab(store, aid, entry)
		tabstate.set_cache(store, aid, entry["type"], entry["key"], posts)
		self.addTab(panel, panel.TAB_NAME, select=True, removable=True)
		self._persist_tab_order()

	# ------------------------------------------------------------ feeds: user timeline, search, list

	def search(self):
		if self._plugin.account_id() is None:
			ui.message(_("Add an account first."))
			return
		dlg = SearchDialog(self, self._search_suggestions(), self._plugin.config)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			query, product = dlg.values()
		finally:
			dlg.Destroy()
		if not query:
			return
		self.open_search(query, product)

	def open_search(self, query, product="Latest"):
		"""A search tab. Its name is the query exactly as it is sent to X."""
		# Translators: Name of a search tab. {query} is the search text exactly as sent to X.
		name = _("Search: {query}").format(query=query)
		self.open_feed("search", "%s|%s" % (product, query), {"query": query, "product": product}, name)

	def _search_suggestions(self):
		"""Hashtags and @users of the tweet in focus, to add to a search."""
		index = self.notebook.GetSelection()
		panel = self.notebook.GetPage(index) if index != wx.NOT_FOUND else None
		current = getattr(panel, "_current_post", None)
		post = current() if callable(current) else None
		if post is None:
			return []
		return ["#" + t for t in hashtags_of(post)] + ["@" + u for u in mentions_of(post.message)]

	def _pick_user(self, post):
		"""The user name to act on: the one user the tweet is about, else the reader's choice.
		None when there is none or the reader cancels."""
		users = relevant_users(post)
		if not users:
			announce_now(_("This tweet has no user name."))
			return None
		if len(users) == 1:
			return users[0][1]
		dlg = wx.SingleChoiceDialog(
			# Translators: Prompt and title of the window that asks which user to act on.
			self, _("Choose a user:"), _("User"), [label for label, _user in users],
		)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return None
			return users[dlg.GetSelection()][1]
		finally:
			dlg.Destroy()

	def open_user_menu(self, post):
		"""Alt+U: pick the user once, then show the user menu."""
		handle = self._pick_user(post)
		if handle:
			self.show_user_menu(post, handle, self._known_uid(post, handle))

	def _relation_state(self, handle):
		"""How the account stands to a user, from the table kept in the cache: following, muting, blocking and
		the uid; a relation is None when it is not known."""
		aid = self._plugin.account_id()
		state = self._plugin.store.relation_state(aid, handle) if aid is not None else {}
		return {key: state.get(key) for key in ("following", "muting", "blocking", "uid")}

	def show_user_menu(self, post, handle, uid=""):
		"""The user menu of a user. Follow, mute and block say what they will do, so how the account stands to the
		user must be known: it is in the table when a list, a profile or an earlier action told it. Otherwise X is
		asked once and the answer is kept. post is None in a list of users."""
		state = self._relation_state(handle)
		if any(state[key] is None for key in ("following", "muting", "blocking")):
			job = lambda result: self._menu_after_check(post, handle, uid, result)
			if self._plugin.start_job("relation", job, {"handle": handle, "uid": uid}):
				announce_now(_("Checking status, please wait..."))
				return
		self._popup_user_menu(post, handle, uid, state)

	@safe_ui
	def _menu_after_check(self, post, handle, uid, result):
		if result["ok"]:
			if result.get("account_id") != self._plugin.account_id():
				return  # the account was switched meanwhile
		else:
			soundpack.play("error")
			announce_now(result["message"])
		self._popup_user_menu(post, handle, uid, self._relation_state(handle))

	def _popup_user_menu(self, post, handle, uid, state):
		menu = wx.Menu()
		populate_user_menu(menu, self, lambda action: self.run_user_action(action, post, handle, uid), state)
		self.PopupMenu(menu)
		menu.Destroy()

	def run_user_action(self, action, post, handle=None, uid=""):
		"""One entry of the user menu, from the menu or from its own shortcut. handle: already chosen (the users
		of the tweet are asked about otherwise); uid: the id of that user when it is known. post is None in a
		list of users."""
		handle = handle or self._pick_user(post)
		if not handle:
			return
		uid = uid or self._known_uid(post, handle)
		handlers = {
			"user_timeline": lambda: self.open_user(post, handle, uid),
			"user_info": lambda: self.show_user_info(post, handle),
			"user_pinned": lambda: self.open_pinned(post, handle),
			"user_followers": lambda: self.open_users("followers", handle, uid),
			"user_following": lambda: self.open_users("following", handle, uid),
			"user_known_followers": lambda: self.open_users("known_followers", handle, uid),
			"user_follow": lambda: self.change_relation("follow", handle, uid),
			"user_mute": lambda: self.change_relation("mute", handle, uid),
			"user_block": lambda: self.change_relation("block", handle, uid),
			"user_copy_url": lambda: self.copy_user_url(post, handle),
			"user_open_web": lambda: self.open_user_web(post, handle),
		}
		handlers[action]()

	@staticmethod
	def _known_uid(post, handle):
		"""The id of the author of a tweet (the only user whose id the tweet tells); "" for anyone else."""
		if post is not None and handle == author_handle(post):
			return load_stats(post.stats).get("author_id") or ""
		return ""

	def open_user(self, post, handle=None, uid=""):
		"""The timeline of a user of this tweet (handle: already chosen, uid: its id when known)."""
		handle = handle or self._pick_user(post)
		if not handle:
			return
		uid = uid or self._known_uid(post, handle)
		# Translators: Name of a user's timeline tab. {user} is the user name.
		name = _("Timeline of @{user}").format(user=handle)
		self.open_feed("user", handle, {"handle": handle, "uid": uid}, name)

	_RELATION_STATE = {"follow": "following", "mute": "muting", "block": "blocking"}
	_RELATION_UNDO = {"follow": "unfollow", "mute": "unmute", "block": "unblock"}

	def change_relation(self, kind, handle, uid=""):
		"""Follow, mute or block a user, or undo it, whichever the table says is the case now. Only when the
		table does not know (a shortcut used before the menu was ever shown) X is asked first."""
		state = self._relation_state(handle)
		active = state[self._RELATION_STATE[kind]]
		if active is None:
			job = lambda result: self._relation_checked(kind, handle, result)
			if not self._plugin.start_job("relation", job, {"handle": handle, "uid": uid}):
				announce_now(_("Still loading, please wait."))
				return
			announce_now(_("Checking status, please wait..."))
			return
		self._ask_relation(kind, handle, uid or state["uid"], active)

	@safe_ui
	def _relation_checked(self, kind, handle, result):
		if not result["ok"]:
			soundpack.play("error")
			announce_now(result["message"])
			return
		if result.get("account_id") != self._plugin.account_id():
			return  # the account was switched meanwhile
		self._ask_relation(kind, handle, result["uid"], result["state"][self._RELATION_STATE[kind]])

	# What cannot always be undone right away asks first: a block ends the follows in both directions and an
	# unblock does not bring them back, and a protected account must approve a follow again after an unfollow.
	_RELATION_ASKS = ("block", "unfollow")

	def _ask_relation(self, kind, handle, uid, active):
		"""active: the relation is on now, so the action undoes it. What can be undone is done at once and said at
		once, before X has answered (an error is told afterwards); a block or an unfollow asks first."""
		action = self._RELATION_UNDO[kind] if active else kind
		if action in self._RELATION_ASKS:
			dlg = wx.MessageDialog(
				# Translators: Title of a Yes/No question window.
				self, relation_texts()[action][0].format(user=handle), _("Confirm"), wx.YES_NO | wx.NO_DEFAULT,
			)
			try:
				if dlg.ShowModal() != wx.ID_YES:
					return
			finally:
				dlg.Destroy()
		job = lambda done: self._relation_done(action, handle, done)
		options = {"handle": handle, "uid": uid, "do": action}
		if not self._plugin.start_job("relation", job, options, progress=False):
			announce_now(_("Still loading, please wait."))
			return
		announce_now(relation_texts()[action][1].format(user=handle))  # said before X answers: it rarely refuses
		self._tell_tabs("relation_changed", handle, action)

	@safe_ui
	def _relation_done(self, action, handle, result):
		if result["ok"]:
			return  # it was said already
		soundpack.play("error")
		announce_now(_("Action failed: {error}").format(error=result["message"]))
		self._tell_tabs("relation_failed", handle, action)

	def _tell_tabs(self, hook_name, handle, action):
		"""The open tabs with lists of users follow a relation that was changed, or take the user back."""
		for panel in self.getOpenTabs():
			hook = getattr(panel, hook_name, None)
			if callable(hook):
				hook(handle, action)

	def open_users(self, kind, handle, uid=""):
		"""A tab with the followers, the following or the followers you know of a user, one tab for each."""
		key = "%s:%s" % (kind, handle)
		if self.focusTabByIdentity({"kind": "users", "key": key}):
			return
		origin = self._origin_key()
		job = lambda result: self._users_fetched(kind, handle, key, origin, result)
		options = {
			"kind": kind, "params": {"handle": handle, "uid": uid}, "chain": key,
			"pages": self._plugin.config.get("feed_pages"),
		}
		if not self._plugin.start_job("users", job, options):
			announce_now(_("Still loading, please wait."))
			return
		announce_now(_("Loading, please wait."))

	@safe_ui
	def _users_fetched(self, kind, handle, key, origin, result):
		if not result["ok"]:
			soundpack.play("error")
			announce_now(result["message"])
			return
		aid = self._plugin.account_id()
		if result.get("account_id") != aid:
			return  # the account was switched meanwhile
		rows = result["users"]
		if not rows:
			announce_now(_("No users found."))
			return
		if self.focusTabByIdentity({"kind": "users", "key": key}):
			return
		params = {"kind": kind, "handle": handle, "uid": result.get("uid") or ""}
		name = users_tab_name(kind, handle)
		panel = UsersTab(self.notebook, self._plugin, params, rows, name, origin_key=origin)
		store = self._plugin.store
		tabstate.add_open_tab(store, aid, {
			"type": "users", "key": key, "params": params, "origin": origin, "name": name,
		})
		tabstate.set_cache(store, aid, "users", key, rows)
		self.addTab(panel, panel.TAB_NAME, select=True, removable=True)
		self._persist_tab_order()

	def show_user_info(self, post, handle):
		if not self._plugin.start_job("user", self._user_info_fetched, {"handle": handle}):
			announce_now(_("Still loading, please wait."))
			return
		announce_now(_("Loading user information, please wait."))

	@safe_ui
	def _user_info_fetched(self, result):
		if not result["ok"]:
			soundpack.play("error")
			announce_now(result["message"])
			return
		if result.get("account_id") != self._plugin.account_id():
			return  # the account was switched meanwhile
		dlg = UserInfoDialog(self, result["info"])
		try:
			dlg.ShowModal()
		finally:
			dlg.Destroy()

	def open_pinned(self, post, handle):
		"""The pinned tweet of a user opens as a thread tab."""
		origin = self._origin_key()
		job = lambda result: self._pinned_fetched(handle, origin, result)
		options = {"handle": handle, "pages": self._plugin.config.get("feed_pages")}
		if not self._plugin.start_job("pinned", job, options):
			announce_now(_("Still loading, please wait."))
			return
		announce_now(_("Loading the pinned tweet, please wait."))

	@safe_ui
	def _pinned_fetched(self, handle, origin, result):
		if result["ok"] and (result.get("pinned") is None or result.get("unavailable")):
			if result.get("unavailable"):
				announce_now(_("The pinned tweet of @{user} is not available any more.").format(user=handle))
			else:
				announce_now(_("@{user} has no pinned tweet.").format(user=handle))
			return
		self._related_fetched("thread", result.get("pinned"), None, origin, handle, result)

	def copy_user_url(self, post, handle):
		if wx.TheClipboard.Open():
			try:
				wx.TheClipboard.SetData(wx.TextDataObject("https://x.com/" + handle))
			finally:
				wx.TheClipboard.Close()
			announce_now(_("Profile URL copied."))
		else:
			announce_now(_("Could not copy to the clipboard."))

	def open_user_web(self, post, handle):
		webbrowser.open("https://x.com/" + handle)
		announce_now(_("Opening in the browser"))

	def open_feed(self, source, key, params, name):
		"""Fetch the first page of a remote feed, then open it in a tab of its own."""
		if self.focusTabByIdentity({"kind": source, "key": key}):
			return
		origin = self._origin_key()
		job = lambda result: self._feed_fetched(source, key, params, name, origin, result)
		options = {
			"source": source, "params": params, "chain": key, "pages": self._plugin.config.get("feed_pages"),
			"pinned_lookup": source == "user",  # a user's pinned tweet is looked up once, when the tab opens
		}
		if not self._plugin.start_job("feed", job, options):
			announce_now(_("Still loading, please wait."))
			return
		announce_now(_("Loading, please wait."))

	@safe_ui
	def _feed_fetched(self, source, key, params, name, origin, result):
		if not result["ok"]:
			soundpack.play("error")
			announce_now(result["message"])
			return
		aid = self._plugin.account_id()
		if result.get("account_id") != aid:
			return  # the account was switched meanwhile
		posts = result["posts"]
		if not posts:
			announce_now(_("No tweets found."))
			return
		if self.focusTabByIdentity({"kind": source, "key": key}):
			return
		params = dict(params)
		if result.get("uid"):
			params["uid"] = result["uid"]
		panel = create_feed_tab(self.notebook, self._plugin, source, key, params, posts, name, origin_key=origin)
		store = self._plugin.store
		tabstate.add_open_tab(store, aid, {
			"type": source, "key": key, "params": params, "origin": origin, "name": name,
		})
		tabstate.set_cache(store, aid, source, key, posts)
		self.addTab(panel, panel.TAB_NAME, select=True, removable=True)
		self._persist_tab_order()

	# ------------------------------------------------------------ accounts

	def show_accounts_menu(self):
		"""Alt+W: pick an account. The choice is made after the menu has closed."""
		choices = self._plugin.account_choices()
		if not choices:
			ui.message(_("Add an account first."))
			return
		menu = wx.Menu()
		for acc, text in choices:
			item = menu.Append(wx.ID_ANY, text)
			self.Bind(wx.EVT_MENU, lambda e, a=acc: wx.CallAfter(self._plugin.choose_account, a, True), item)
		self.PopupMenu(menu)
		menu.Destroy()

	# ------------------------------------------------------------ keyboard and closing

	def _key_actions(self):
		"""(action id, what it does) of the main window; the keys come from the Shortcuts page."""
		return (
			("remove_tab", self.removeCurrentTab), ("settings", self._plugin.open_settings),
			("search", self.search), ("new_tweet", self._plugin.open_compose),
			("rename_tab", self.renameCurrentTab), ("check_all", self.checkAllOpenTabs),
			("check_updates", self.onCheckForUpdates),  # for when focus is outside a tab; tabs handle it themselves
			("move_tab_left", lambda: self.moveCurrentTab(-1)), ("move_tab_right", lambda: self.moveCurrentTab(1)),
			("accounts_menu", self.show_accounts_menu), ("reopen_tab", self.reopen_closed_tab),
		)

	def onCharHook(self, evt):
		key = evt.GetKeyCode()
		if keymap.matches(evt, "close_window"):
			self.Close()
			return
		if evt.ControlDown() and not evt.ShiftDown() and ord("1") <= key <= ord("9"):
			index = key - ord("1")
			if index < self.notebook.GetPageCount():
				self.notebook.SetSelection(index)
			return
		for action_id, handler in self._key_actions():
			if keymap.matches(evt, action_id):
				handler()
				return
		evt.Skip()

	def onClose(self, evt):
		self.closed = True
		self._dead = True
		for panel in self.getOpenTabs():
			try:
				panel.flush_state()
			except Exception:
				log.error(TAG + "could not save a tab", exc_info=True)
			panel._dead = True
			for value in vars(panel).values():
				if isinstance(value, wx.Timer):
					value.Stop()
		self._plugin.main_closed(self)
		self.Destroy()