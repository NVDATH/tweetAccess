# -*- coding: utf-8 -*-
# NVTweet - settings.py: settings window (Accounts, Display, General, Diagnostics).

import json
import os
import webbrowser

import addonHandler
import config as nvdaConfig
import speech
import ui
import wx
from logHandler import log

import gui
import gui.nvdaControls

from . import keymap, soundpack, tabstate
from .config import BG_TAB_TYPES, DEFAULTS
from .utils import APP_NAME, COOKIE_EXTENSION_URL, TAG, account_label, announce_now, extract_cookies

try:
	from .diagnostics import DiagnosticsPage
except ImportError:
	DiagnosticsPage = None  # diagnostics.py is optional and is not shipped in releases

addonHandler.initTranslation()

TIME_MODE_CHOICES = [
	# Translators: Time format choice.
	(_("Relative (up to 24 hours, then full date and time)"), "relative_24h"),
	# Translators: Time format choice.
	(_("Relative always (minutes, hours, days, months, years ago)"), "relative_always"),
	# Translators: Time format choice.
	(_("Full date and time"), "absolute"),
	# Translators: Time format choice.
	(_("Custom format"), "custom"),
]

AUTHOR_MODE_CHOICES = [
	# Translators: Author display choice.
	(_("Display name"), "name"),
	# Translators: Author display choice.
	(_("User name"), "handle"),
	# Translators: Author display choice.
	(_("Display name and user name"), "both"),
]

SORT_CHOICES = [
	# Translators: Sort order choice.
	(_("Newest first"), True),
	# Translators: Sort order choice.
	(_("Oldest first"), False),
]

BG_INTERVALS = (0, 5, 10, 15, 30, 45, 60)

STRFTIME_REFERENCE_URL = "https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes"

OPTIONAL_TAB_CHOICES = [
	# Translators: Tab name in the optional tabs checklist in Settings > Display.
	("notifications", _("Notifications")),
	# Translators: Tab name in the optional tabs checklist in Settings > Display.
	("chat", _("Chat (experimental)")),
	# Translators: Tab name in the optional tabs checklist in Settings > Display.
	("people", _("People")),
	# Translators: Tab name in the optional tabs checklist in Settings > Display.
	("bookmarks", _("Bookmarks")),
	# Translators: Tab name in the optional tabs checklist in Settings > Display.
	("lists", _("Lists")),
]

ENTER_ACTION_CHOICES = [
	# Translators: Enter key action choice.
	("view_thread", _("View thread")),
	# Translators: Enter key action choice.
	("quotes", _("View quotes")),
	# Translators: Enter key action choice.
	("user", _("View user timeline")),
	# Translators: Enter key action choice.
	("user_info", _("Show user information")),
	# Translators: Enter key action choice.
	("user_pinned", _("View pinned tweet")),
	# Translators: Enter key action choice.
	("user_followers", _("View user followers")),
	# Translators: Enter key action choice.
	("user_following", _("View who the user follows")),
	# Translators: Enter key action choice.
	("user_known_followers", _("View followers of the user that you know")),
	# Translators: Enter key action choice.
	("hashtags", _("Search a hashtag")),
	# Translators: Enter key action choice.
	("full", _("Show full tweet")),
	# Translators: Enter key action choice.
	("reply", _("Reply")),
	# Translators: Enter key action choice.
	("quote", _("Quote tweet")),
	# Translators: Enter key action choice.
	("retweet", _("Retweet / Undo retweet")),
	# Translators: Enter key action choice.
	("like", _("Like / Unlike")),
	# Translators: Enter key action choice.
	("bookmark", _("Bookmark / Remove bookmark")),
	# Translators: Enter key action choice.
	("browser", _("Open in browser")),
	# Translators: Enter key action choice.
	("links", _("Open a link")),
	# Translators: Enter key action choice.
	("image", _("Open image")),
	# Translators: Enter key action choice.
	("bemyeyes", _("Send image to Be My Eyes")),
	# Translators: Enter key action choice.
	("copy_image", _("Copy image to clipboard")),
	# Translators: Enter key action choice.
	("video", _("Open video or GIF")),
	# Translators: Enter key action choice.
	("download", _("Download media")),
	# Translators: Enter key action choice.
	("copy_text", _("Copy tweet text")),
	# Translators: Enter key action choice.
	("copy_link", _("Copy link to tweet")),
	# Translators: Enter key action choice.
	("copy_all", _("Copy author, text and link")),
	# Translators: Enter key action choice.
	("mark_read", _("Toggle read/unread")),
	# Translators: Enter key action choice.
	("menu", _("Open the action menu")),
]

TARGET_CHOICES = [
	# Translators: Choice of what shortcuts act on when a tweet quotes another one.
	("ask", _("Ask every time")),
	# Translators: Choice of what shortcuts act on when a tweet quotes another one.
	("main", _("The tweet shown")),
	# Translators: Choice of what shortcuts act on when a tweet quotes another one.
	("quoted", _("The quoted tweet")),
]

BG_KEYS = {
	"home": "bg_interval", "notifications": "bg_interval_notifications",
	"bookmarks": "bg_interval_bookmarks", "tabs": "bg_interval_tabs", "trends": "bg_interval_trends",
}

UPDATE_ROWS = [  # the tabs every account has, first rows of the Updates table
	# Translators: Row of the Updates table (the Home tab).
	("home", _("Home")),
	# Translators: Row of the Updates table (the Notifications tab).
	("notifications", _("Notifications")),
	# Translators: Row of the Updates table (the Bookmarks tab).
	("bookmarks", _("Bookmarks")),
]


def _bg_label(minutes):
	if minutes == 0:
		# Translators: Automatic update choice.
		return _("Off")
	# Translators: Automatic update choice. {minutes} is the number of minutes.
	return _("Every {minutes} minutes").format(minutes=minutes)


def _clipboard_text():
	data = wx.TextDataObject()
	if not wx.TheClipboard.Open():
		return ""
	try:
		ok = wx.TheClipboard.GetData(data)
	finally:
		wx.TheClipboard.Close()
	return data.GetText() if ok else ""


class AddAccountDialog(wx.Dialog):
	"""Cookies from the clipboard or a file: read, checked and logged in at once.
	self.result = {"identity", "cookies", "lookup_error"} after a successful login."""

	def __init__(self, parent, plugin):
		# Translators: Title of the add-account window.
		super().__init__(parent, title=_("Add account"), size=(520, 320))
		self._plugin = plugin
		self.result = None
		sizer = wx.BoxSizer(wx.VERTICAL)

		note = wx.StaticText(
			self,
			# Translators: Help text of the add-account window.
			label=_(
				"Log in to x.com in your browser, export your cookies with a browser extension "
				"(copy them, or save a JSON or cookies.txt file), then paste or browse. "
				"You are logged in right away."
			),
		)
		note.Wrap(480)
		sizer.Add(note, 0, wx.ALL, 10)

		# Translators: Button that reads the cookies from the clipboard and logs in.
		self.paste_btn = wx.Button(self, label=_("&Paste cookies from clipboard"))
		# Translators: Button that reads the cookies from an exported cookies file and logs in.
		self.browse_btn = wx.Button(self, label=_("&Browse cookies file..."))
		# Translators: Button that opens a page about browser extensions that export cookies.
		self.help_btn = wx.Button(self, label=_("&Get a cookie export extension..."))
		cancel = wx.Button(self, wx.ID_CANCEL, _("Cancel"))
		self._cancel = cancel
		self._buttons = (self.paste_btn, self.browse_btn, self.help_btn)
		for button in self._buttons:
			sizer.Add(button, 0, wx.LEFT | wx.TOP, 10)
		self.progress = wx.StaticText(self, label="")
		sizer.Add(self.progress, 0, wx.ALL, 10)
		sizer.Add(cancel, 0, wx.ALL, 10)
		self.SetSizer(sizer)
		self.CentreOnParent()
		self._cancelled = False
		self._busy = False

		self.paste_btn.Bind(wx.EVT_BUTTON, lambda e: self._login(_clipboard_text()))
		self.browse_btn.Bind(wx.EVT_BUTTON, self.on_browse)
		self.help_btn.Bind(wx.EVT_BUTTON, lambda e: webbrowser.open(COOKIE_EXTENSION_URL))
		cancel.Bind(wx.EVT_BUTTON, self.on_cancel)
		self.Bind(wx.EVT_CLOSE, self.on_cancel)
		self.paste_btn.SetFocus()

	def on_browse(self, event):
		with wx.FileDialog(
			# Translators: Title of the cookies file picker.
			self, _("Choose a cookies file"),
			wildcard=_("Cookie files") + " (*.txt;*.json)|*.txt;*.json|" + _("All files") + " (*.*)|*.*",
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
		) as dlg:
			if dlg.ShowModal() != wx.ID_OK:
				return
			path = dlg.GetPath()
		try:
			with open(path, "rb") as f:
				text = f.read(2_000_000).decode("utf-8-sig", errors="replace")
		except OSError:
			ui.message(_("Could not read that file."))
			return
		# The file window closing makes NVDA read this window again; let that pass first.
		wx.CallLater(300, self._login, text)

	def _login(self, text):
		if self._cancelled or self._busy:
			return
		cookies, _mode = extract_cookies(text)
		if cookies is None:
			ui.message(_("No auth_token and ct0 were found."))
			return
		if not self._plugin.start_job("login", self.on_login_result, {"cookies": cookies}):
			ui.message(_("Still loading, please wait."))
			return
		speech.cancelSpeech()  # drop what is still being read, so the move to Cancel is heard
		self._set_busy(True)
		wx.CallLater(400, ui.message, _("Logging in, please wait..."))

	def _set_busy(self, busy):
		# The buttons stay enabled: disabling the focused one made Windows throw the focus
		# around and NVDA say "unavailable". _busy blocks a second login instead.
		self._busy = busy
		if busy:
			self.progress.SetLabel(_("Logging in, please wait..."))
			self._cancel.SetFocus()
		else:
			self.progress.SetLabel("")
			self.paste_btn.SetFocus()
		self.Layout()

	def on_cancel(self, event):
		self._cancelled = True  # an answer that arrives later is thrown away
		self.EndModal(wx.ID_CANCEL)

	def on_login_result(self, result):
		if self._cancelled:
			return
		try:
			self._set_busy(False)
			if not result["ok"]:
				ui.message(result["message"])
				return
			self.result = {
				"identity": result["identity"], "cookies": result["cookies"],
				"lookup_error": result.get("lookup_error", ""),
			}
			self.EndModal(wx.ID_OK)
		except RuntimeError:
			pass  # window already closed


class AccountsPage(wx.Panel):
	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._plugin = plugin
		self._accounts = []
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Label above the list of accounts.
		sizer.Add(wx.StaticText(self, label=_("Accounts:")), 0, wx.LEFT | wx.TOP, 10)
		self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
		# Translators: Column header of the accounts list.
		self.list.InsertColumn(0, _("Account"), width=420)
		sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)

		# Translators: Checkbox of the accounts page. The selected account is checked for news in the background.
		self.bg_check = wx.CheckBox(self, label=_("&Enable this account in background"))
		sizer.Add(self.bg_check, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

		# Translators: Buttons of the accounts page.
		self.add_btn = wx.Button(self, label=_("&Add account..."))
		self.active_btn = wx.Button(self, label=_("&Set as active"))
		self.rename_btn = wx.Button(self, label=_("Re&name..."))
		self.remove_btn = wx.Button(self, label=_("&Remove account"))
		row = wx.BoxSizer(wx.HORIZONTAL)
		for button in (self.add_btn, self.active_btn, self.rename_btn, self.remove_btn):
			row.Add(button, 0, wx.RIGHT, 8)
		sizer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		self.SetSizer(sizer)

		self.add_btn.Bind(wx.EVT_BUTTON, self.on_add)
		self.active_btn.Bind(wx.EVT_BUTTON, self.on_set_active)
		self.rename_btn.Bind(wx.EVT_BUTTON, self.on_rename)
		self.remove_btn.Bind(wx.EVT_BUTTON, self.on_remove)
		self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: self._sync_bg_check())
		self.bg_check.Bind(wx.EVT_CHECKBOX, self.on_bg_toggle)
		self.refresh()

	def refresh(self, focus_id=None):
		self._accounts = self._plugin.store.list_accounts()
		self.list.DeleteAllItems()
		select = 0
		for i, acc in enumerate(self._accounts):
			# Translators: Marker after the active account in the accounts list.
			marker = _(" (active)") if acc["active"] else ""
			self.list.InsertItem(i, account_label(acc) + marker)
			if acc["id"] == focus_id or (focus_id is None and acc["active"]):
				select = i
		has_accounts = bool(self._accounts)
		for button in (self.active_btn, self.rename_btn, self.remove_btn):
			button.Show(has_accounts)
		self.bg_check.Show(has_accounts)
		if has_accounts:
			self.list.Focus(select)
			self.list.Select(select)
			self._sync_bg_check()
		self.Layout()

	def focus_first(self):
		(self.list if self._accounts else self.add_btn).SetFocus()

	def _selected(self):
		index = self.list.GetFirstSelected()
		return self._accounts[index] if 0 <= index < len(self._accounts) else None

	def _sync_bg_check(self):
		acc = self._selected()
		if acc is not None:
			self.bg_check.SetValue(self._plugin.bg_enabled(acc["id"]))

	def on_bg_toggle(self, event):
		"""Applied at once, like the account buttons."""
		acc = self._selected()
		if acc is not None:
			self._plugin.set_bg_enabled(acc["id"], self.bg_check.GetValue())

	def on_add(self, event):
		gui.mainFrame.prePopup()
		dlg = AddAccountDialog(self, self._plugin)
		try:
			confirmed = dlg.ShowModal() == wx.ID_OK
			result = dlg.result
		finally:
			dlg.Destroy()
			gui.mainFrame.postPopup()
		if not confirmed or not result:
			return
		identity = result["identity"]
		self.GetTopLevelParent().apply_account_pages()  # what was typed goes to the account it was typed for
		account_id = self._plugin.add_account(identity, result["cookies"])
		self.refresh(focus_id=account_id)
		self.GetTopLevelParent().account_changed()
		acc = next(a for a in self._accounts if a["id"] == account_id)
		text = _("Log in success! Logged in as {account}.").format(account=account_label(acc))
		if not (identity["name"] or identity["handle"]):
			text += "\n" + _("The account name could not be found. You can set one with Rename.")
		wx.MessageBox(text, _("Log in"), wx.OK | wx.ICON_INFORMATION, self)
		self.list.SetFocus()
		self._plugin.after_login()

	def on_set_active(self, event):
		acc = self._selected()
		if acc is None:
			ui.message(_("No account selected."))
			return
		self.GetTopLevelParent().apply_account_pages()  # what was typed goes to the account it was typed for
		changed = self._plugin.switch_account(acc["id"])
		self.refresh(focus_id=acc["id"])
		if changed:
			self.GetTopLevelParent().account_changed()
		if not changed:
			# Translators: Announced when Set as active is pressed on the account that is already active. {account} is its name.
			ui.message(_("{account} is already the active account.").format(account=account_label(acc)))

	def on_rename(self, event):
		acc = self._selected()
		if acc is None:
			ui.message(_("No account selected."))
			return
		dlg = wx.TextEntryDialog(
			# Translators: Prompt and title of the rename-account window.
			self, _("Account name:"), _("Rename account"), value=acc["name"] or acc["handle"],
		)
		confirmed = dlg.ShowModal() == wx.ID_OK
		name = dlg.GetValue().strip()
		dlg.Destroy()
		if not confirmed or not name:
			return
		self._plugin.rename_account(acc["id"], name)
		self.refresh(focus_id=acc["id"])
		ui.message(_("Renamed."))

	def on_remove(self, event):
		acc = self._selected()
		if acc is None:
			ui.message(_("No account selected."))
			return
		answer = wx.MessageBox(
			# Translators: Confirmation before removing an account. {account} is its name.
			_("Remove {account}? This deletes its cached tweets and stored cookies from this computer.").format(
				account=account_label(acc)
			),
			# Translators: Title of the remove-account confirmation.
			_("Remove account"),
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self,
		)
		if answer != wx.YES:
			return
		self.GetTopLevelParent().apply_account_pages()  # the active account keeps what was typed
		self._plugin.remove_account(acc["id"])
		self.refresh()
		self.GetTopLevelParent().account_changed()
		self.add_btn.SetFocus()
		ui.message(_("Removed {account}.").format(account=account_label(acc)))

	def apply(self):
		pass


class DisplayPage(wx.Panel):
	PER_ACCOUNT = True  # holds the settings of the active account

	def __init__(self, parent, plugin):
		super().__init__(parent)
		cfg = plugin.config
		self._cfg = cfg
		sizer = wx.BoxSizer(wx.VERTICAL)

		def label(text):
			sizer.Add(wx.StaticText(self, label=text), 0, wx.LEFT | wx.TOP, 10)

		# Translators: Label of the sort order choice.
		label(_("Sort &order:"))
		self.sort = wx.Choice(self, choices=[text for text, _v in SORT_CHOICES])
		self.sort.SetSelection(0 if cfg.get("sort_newest_first") else 1)
		sizer.Add(self.sort, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label of the author display choice.
		label(_("Show &author as:"))
		self.author = wx.Choice(self, choices=[text for text, _v in AUTHOR_MODE_CHOICES])
		values = [v for _t, v in AUTHOR_MODE_CHOICES]
		self.author.SetSelection(values.index(cfg.get("author_mode")) if cfg.get("author_mode") in values else 2)
		sizer.Add(self.author, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label of the embed column position choice.
		label(_("Embed &column:"))
		# Translators: Embed column position choices.
		self.embed = wx.Choice(self, choices=[_("First column"), _("Last column")])
		self.embed.SetSelection(0 if cfg.get("embed_first") else 1)
		sizer.Add(self.embed, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label above the checklist of optional tabs in Settings > Display.
		label(_("Show these ta&bs (Home is always shown):"))
		self.tabs_list = gui.nvdaControls.CustomCheckListBox(
			self, choices=[text for _key, text in OPTIONAL_TAB_CHOICES]
		)
		tabs_off = {k for k in cfg.get("tabs_off").split(",") if k}
		self.tabs_list.CheckedItems = [
			i for i, (key, _text) in enumerate(OPTIONAL_TAB_CHOICES) if key not in tabs_off
		]
		self.tabs_list.SetSelection(0)
		sizer.Add(self.tabs_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label of the time format choice.
		label(_("Date and &time format:"))
		self.time_mode = wx.Choice(self, choices=[text for text, _v in TIME_MODE_CHOICES])
		modes = [v for _t, v in TIME_MODE_CHOICES]
		self.time_mode.SetSelection(modes.index(cfg.get("time_mode")) if cfg.get("time_mode") in modes else 2)
		sizer.Add(self.time_mode, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label of the custom time pattern field.
		label(_("Custo&m format (strftime pattern, used when Custom format is chosen):"))
		self.pattern = wx.TextCtrl(self, value=cfg.get("time_pattern"))
		sizer.Add(self.pattern, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Button that opens the strftime format reference page in a browser.
		self.format_help = wx.Button(self, label=_("&strftime format reference..."))
		sizer.Add(self.format_help, 0, wx.LEFT | wx.TOP, 10)
		self.format_help.Bind(wx.EVT_BUTTON, lambda e: webbrowser.open(STRFTIME_REFERENCE_URL))

		# Translators: Checkbox that shows an icon in the notification area with the number of unread tweets.
		self.tray = wx.CheckBox(self, label=_("Show an &icon in the notification area with the unread count"))
		self.tray.SetValue(cfg.get("show_tray"))
		sizer.Add(self.tray, 0, wx.ALL, 10)

		# Translators: Label above the checklist of tabs counted by the notification area icon.
		self.tray_categories_label = wx.StaticText(self, label=_("Count &unread items from these tabs in the icon:"))
		sizer.Add(self.tray_categories_label, 0, wx.LEFT, 10)
		self._tray_items = self._tray_choices(plugin)
		self.tray_categories = gui.nvdaControls.CustomCheckListBox(
			self, choices=[label for _ident, label in self._tray_items]
		)
		tray_off = {k for k in cfg.get("tray_disabled").split(",") if k}
		try:
			tray_on = set(json.loads(cfg.get("tray_tabs") or "[]"))
		except ValueError:
			tray_on = set()
		self.tray_categories.CheckedItems = [
			i for i, (ident, _label) in enumerate(self._tray_items)
			if (ident not in tray_off if ident in ("home", "notifications") else ident in tray_on)
		]
		self.tray_categories.SetSelection(0)
		sizer.Add(self.tray_categories, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

		# Translators: Label of the dropdown choosing how the notification area icon words its unread counts.
		self.tray_text_label = wx.StaticText(self, label=_("Icon te&xt:"))
		sizer.Add(self.tray_text_label, 0, wx.LEFT, 10)
		self.tray_text = wx.Choice(self, choices=[
			# Translators: Icon text choice: one total across the chosen tabs, like "NVTweet: 5 unread".
			_("Total unread count"),
			# Translators: Icon text choice: each tab's count with its name, like "NVTweet: Home 3, Noti 2".
			_("Unread count of each tab, with tab names"),
		])
		self.tray_text.SetSelection(1 if cfg.get("tray_per_tab") else 0)
		sizer.Add(self.tray_text, 0, wx.LEFT | wx.BOTTOM, 10)

		# Translators: Button that opens Windows' taskbar settings, where NVDA's notification area icons can be shown.
		self.taskbar_btn = wx.Button(self, label=_("Open &Windows taskbar settings..."))
		sizer.Add(self.taskbar_btn, 0, wx.LEFT | wx.BOTTOM, 10)

		self.SetSizer(sizer)
		self.time_mode.Bind(wx.EVT_CHOICE, self._update_pattern_state)
		self._update_pattern_state(None)
		self.taskbar_btn.Bind(wx.EVT_BUTTON, lambda e: os.startfile("ms-settings:taskbar"))
		self.tray.Bind(wx.EVT_CHECKBOX, lambda e: self._update_taskbar_button())
		self._update_taskbar_button()

	def _update_pattern_state(self, event):
		custom = TIME_MODE_CHOICES[self.time_mode.GetSelection()][1] == "custom"
		self.pattern.Enable(custom)
		self.format_help.Enable(custom)

	@staticmethod
	def _tray_choices(plugin):
		"""(id, label) of everything the icon can count: Home, Notifications, Bookmarks and every saved tab."""
		items = [("home", _("Home")), ("notifications", _("Notifications")), ("bookmarks", _("Bookmarks"))]
		aid = plugin.account_id()
		if aid is not None:
			for entry in tabstate.get_open_tabs(plugin.store, aid):
				if entry["type"] != "users":  # a list of users has nothing unread
					items.append(("%s:%s" % (entry["type"], entry["key"]), entry.get("name") or entry["key"]))
		return items

	def _update_taskbar_button(self):
		shown = self.tray.GetValue()
		for control in (
			self.taskbar_btn, self.tray_categories_label, self.tray_categories,
			self.tray_text_label, self.tray_text,
		):
			control.Show(shown)
			control.Enable(shown)
		self.Layout()

	def focus_first(self):
		self.sort.SetFocus()

	def apply(self):
		cfg = self._cfg
		cfg.set("sort_newest_first", SORT_CHOICES[self.sort.GetSelection()][1])
		cfg.set("author_mode", AUTHOR_MODE_CHOICES[self.author.GetSelection()][1])
		cfg.set("time_mode", TIME_MODE_CHOICES[self.time_mode.GetSelection()][1])
		cfg.set("time_pattern", self.pattern.GetValue())
		cfg.set("embed_first", self.embed.GetSelection() == 0)
		cfg.set("show_tray", self.tray.GetValue())
		checked = set(self.tray_categories.CheckedItems)
		picked = {ident for i, (ident, _label) in enumerate(self._tray_items) if i in checked}
		cfg.set("tray_disabled", ",".join(k for k in ("home", "notifications") if k not in picked))
		cfg.set("tray_tabs", json.dumps(sorted(p for p in picked if p not in ("home", "notifications"))))
		cfg.set("tray_per_tab", self.tray_text.GetSelection() == 1)
		tabs_on = set(self.tabs_list.CheckedItems)
		cfg.set("tabs_off", ",".join(
			key for i, (key, _text) in enumerate(OPTIONAL_TAB_CHOICES) if i not in tabs_on
		))


class GeneralPage(wx.Panel):
	PER_ACCOUNT = True  # holds the settings of the active account

	def __init__(self, parent, plugin):
		super().__init__(parent)
		cfg = plugin.config
		self._cfg = cfg
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Label of the Enter key action choice.
		sizer.Add(wx.StaticText(self, label=_("&Enter key action on a tweet:")), 0, wx.LEFT | wx.TOP, 10)
		self.enter_action = wx.Choice(self, choices=[text for _key, text in ENTER_ACTION_CHOICES])
		action_keys = [key for key, _text in ENTER_ACTION_CHOICES]
		current_action = cfg.get("enter_action")
		self.enter_action.SetSelection(action_keys.index(current_action) if current_action in action_keys else 0)
		sizer.Add(self.enter_action, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label of the choice of which tweet shortcuts act on when a tweet quotes another one.
		sizer.Add(wx.StaticText(self, label=_("&Quote tweet actions apply to:")), 0, wx.LEFT | wx.TOP, 10)
		self.target_mode = wx.Choice(self, choices=[text for _key, text in TARGET_CHOICES])
		target_keys = [key for key, _text in TARGET_CHOICES]
		current_target = cfg.get("target_mode")
		self.target_mode.SetSelection(target_keys.index(current_target) if current_target in target_keys else 0)
		sizer.Add(self.target_mode, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label of the auto read pause setting.
		sizer.Add(wx.StaticText(self, label=_("&Auto read: seconds to wait between tweets:")), 0, wx.LEFT | wx.TOP, 10)
		self.auto_pause = wx.SpinCtrl(
			self, min=1, max=10, initial=cfg.get("auto_pause"),
			# Translators: Accessible name of the auto read pause field.
			name=_("Auto read pause in seconds"),
		)
		sizer.Add(self.auto_pause, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Checkbox: auto read speaks the whole message of long tweets.
		self.auto_full = wx.CheckBox(self, label=_("Auto read: read the &whole message of long tweets"))
		self.auto_full.SetValue(cfg.get("auto_full"))
		sizer.Add(self.auto_full, 0, wx.LEFT | wx.TOP, 10)

		# Translators: Label of the choice of the NVDA configuration profile whose voice auto read uses.
		sizer.Add(wx.StaticText(self, label=_("Auto read &voice profile:")), 0, wx.LEFT | wx.TOP, 10)
		self._profiles = [""] + [name for name in nvdaConfig.conf.listProfiles() if name]
		# Translators: First choice of the auto read voice profile list: no profile, the voice NVDA uses now.
		self.auto_profile = wx.Choice(self, choices=[_("None (current voice)")] + self._profiles[1:])
		current_profile = cfg.get("auto_profile")
		self.auto_profile.SetSelection(self._profiles.index(current_profile) if current_profile in self._profiles else 0)
		sizer.Add(self.auto_profile, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label of the choice of how many tweets a tab loads when it opens (X gives 20 per page).
		sizer.Add(wx.StaticText(self, label=_("&Load how many tweets when a tab opens:")), 0, wx.LEFT | wx.TOP, 10)
		self.feed_pages = wx.Choice(self, choices=[str(20 * n) for n in range(1, 6)])
		self.feed_pages.SetSelection(max(1, min(5, cfg.get("feed_pages"))) - 1)
		sizer.Add(self.feed_pages, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label of the download folder field.
		sizer.Add(wx.StaticText(self, label=_("Download &folder:")), 0, wx.LEFT | wx.TOP, 10)
		self.folder = wx.TextCtrl(self, value=cfg.get("download_dir"))
		sizer.Add(self.folder, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		# Translators: Button that chooses the download folder.
		self.browse_btn = wx.Button(self, label=_("&Browse..."))
		sizer.Add(self.browse_btn, 0, wx.ALL, 10)
		self.SetSizer(sizer)
		self.browse_btn.Bind(wx.EVT_BUTTON, self.on_browse)

	def on_browse(self, event):
		dlg = wx.DirDialog(self, _("Choose the folder for downloads"), defaultPath=self.folder.GetValue())
		try:
			if dlg.ShowModal() == wx.ID_OK:
				self.folder.SetValue(dlg.GetPath())
		finally:
			dlg.Destroy()

	def focus_first(self):
		self.enter_action.SetFocus()

	def apply(self):
		self._cfg.set("enter_action", ENTER_ACTION_CHOICES[self.enter_action.GetSelection()][0])
		self._cfg.set("target_mode", TARGET_CHOICES[self.target_mode.GetSelection()][0])
		self._cfg.set("auto_pause", self.auto_pause.GetValue())
		self._cfg.set("auto_full", self.auto_full.GetValue())
		self._cfg.set("auto_profile", self._profiles[self.auto_profile.GetSelection()])
		self._cfg.set("feed_pages", self.feed_pages.GetSelection() + 1)
		self._cfg.set("download_dir", self.folder.GetValue().strip() or DEFAULTS["download_dir"])


SOUND_EVENT_LABELS = {
	# Translators: Sound event label.
	"like": _("Like"),
	# Translators: Sound event label.
	"unlike": _("Unlike"),
	# Translators: Sound event label.
	"bookmark": _("Bookmark"),
	# Translators: Sound event label.
	"unbookmark": _("Remove bookmark"),
	# Translators: Sound event label.
	"retweet": _("Retweet"),
	# Translators: Sound event label.
	"unretweet": _("Undo retweet"),
	# Translators: Sound event label.
	"send_post": _("Tweet posted"),
	# Translators: Sound event label.
	"max_length": _("Tweet is too long"),
	# Translators: Sound event label.
	"auto_start": _("Auto read started"),
	# Translators: Sound event label.
	"auto_stop": _("Auto read stopped"),
	# Translators: Sound event label.
	"open_tab": _("Tab opened"),
	# Translators: Sound event label.
	"close_tab": _("Tab closed"),
	# Translators: Sound event label.
	"boundary": _("Reached the end of a list"),
	# Translators: Sound event label.
	"error": _("Error"),
	# Translators: Sound event label.
	"ready": _("Loading finished with new tweets"),
	# Translators: Sound event label.
	"notification": _("New tweets found in the background"),
	# Translators: Sound event label.
	"embed_image": _("Tweet with an image focused"),
	# Translators: Sound event label.
	"embed_video": _("Tweet with a video focused"),
	# Translators: Sound event label.
	"embed_gif": _("Tweet with an animated GIF focused"),
	# Translators: Sound event label.
	"embed_link": _("Tweet with a link focused"),
	# Translators: Sound event label.
	"embed_quote": _("Quote tweet focused"),
	# Translators: Sound event label.
	"read_more": _("Tweet with a cut message focused (read more)"),
	# Translators: Sound event label. {name} is the add-on name.
	"main_open": _("{name} window opened").format(name=APP_NAME),
	# Translators: Sound event label. {name} is the add-on name.
	"main_close": _("{name} window closed").format(name=APP_NAME),
}


class SoundPage(wx.Panel):
	PER_ACCOUNT = True  # holds the settings of the active account

	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._cfg = plugin.config
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Label of the sound pack choice.
		sizer.Add(wx.StaticText(self, label=_("Sound &pack:")), 0, wx.LEFT | wx.TOP, 10)
		self.pack = wx.Choice(self)
		sizer.Add(self.pack, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

		note = wx.StaticText(
			self,
			# Translators: Help text under the sound pack choice.
			label=_(
				"Sound packs are subfolders of the add-on's SoundPack folder. Add your own by "
				"creating a subfolder with .wav files named after the events. A pack does not "
				"need every file: a missing sound stays silent."
			),
		)
		note.Wrap(560)
		sizer.Add(note, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		# Translators: Label above the list of sound events.
		sizer.Add(wx.StaticText(self, label=_("&Play a sound for:")), 0, wx.LEFT | wx.TOP, 10)
		self.events = gui.nvdaControls.CustomCheckListBox(
			self, choices=[SOUND_EVENT_LABELS.get(key, key) for key in soundpack.EVENT_KEYS]
		)
		sizer.Add(self.events, 1, wx.EXPAND | wx.ALL, 10)
		self.SetSizer(sizer)

		self._packs = [soundpack.SILENT_PACK] + soundpack.list_packs()
		# Translators: Sound pack choice that turns every sound off.
		self.pack.Set([_("Silent / No sound")] + self._packs[1:])
		selected = self._cfg.get("sound_pack")
		self.pack.SetSelection(self._packs.index(selected) if selected in self._packs else 0)
		disabled = {k for k in self._cfg.get("sound_disabled").split(",") if k}
		self.events.CheckedItems = [i for i, key in enumerate(soundpack.EVENT_KEYS) if key not in disabled]
		if soundpack.EVENT_KEYS:
			self.events.SetSelection(0)

	def focus_first(self):
		self.pack.SetFocus()

	def apply(self):
		index = self.pack.GetSelection()
		name = self._packs[index] if 0 <= index < len(self._packs) else soundpack.SILENT_PACK
		self._cfg.set("sound_pack", name)
		checked = {soundpack.EVENT_KEYS[i] for i in self.events.CheckedItems}
		self._cfg.set("sound_disabled", ",".join(k for k in soundpack.EVENT_KEYS if k not in checked))
		soundpack.reload()


class ShortcutDialog(wx.Dialog):
	"""Waits for the new shortcut of one action. self.text is the accepted shortcut, or None."""

	def __init__(self, parent, action_id, bindings):
		# Translators: Title of the window that waits for a new shortcut. {action} is the action name.
		super().__init__(parent, title=_("New shortcut for {action}").format(action=keymap.label(action_id)))
		self._action = action_id
		self._bindings = bindings
		self.text = None
		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Prompt of the window that waits for a new shortcut.
		sizer.Add(wx.StaticText(self, label=_("Press the new shortcut. Escape cancels.")), 0, wx.ALL, 15)
		self.cancel_btn = wx.Button(self, wx.ID_CANCEL, _("Cancel"))
		sizer.Add(self.cancel_btn, 0, wx.ALIGN_CENTER | wx.BOTTOM, 15)
		self.SetSizerAndFit(sizer)
		self.CentreOnParent()
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
		self.cancel_btn.SetFocus()

	def on_char_hook(self, event):
		if event.GetKeyCode() == wx.WXK_ESCAPE and not event.HasAnyModifiers():
			self.EndModal(wx.ID_CANCEL)
			return
		text = keymap.event_to_text(event)
		if text is None:
			return  # a modifier alone: wait for the key
		if not text:
			ui.message(_("That key can't be used."))
			return
		error = keymap.check(self._action, text, self._bindings)
		if error:
			ui.message("%s: %s" % (keymap.display_text(text), error))
			return
		self.text = text
		self.EndModal(wx.ID_OK)


class KeymapPage(wx.Panel):
	"""Every action of the add-on's own windows with its shortcut. Shared by all accounts."""

	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._ids = keymap.actions()
		self._bindings = keymap.effective()  # what is shown; saved by apply()
		sizer = wx.BoxSizer(wx.VERTICAL)

		note = wx.StaticText(
			self,
			# Translators: Help text of the Shortcuts page.
			label=_(
				"These are the shortcuts of the add-on's own windows, the same for every account. "
				"Shortcuts that work anywhere in Windows are set in NVDA's Input gestures."
			),
		)
		note.Wrap(560)
		sizer.Add(note, 0, wx.ALL, 10)

		# Translators: Label above the list of actions on the Shortcuts page.
		sizer.Add(wx.StaticText(self, label=_("&Actions (Enter changes the shortcut):")), 0, wx.LEFT, 10)
		self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
		# Translators: Column headers of the actions list on the Shortcuts page.
		self.list.InsertColumn(0, _("Action"), width=300)
		self.list.InsertColumn(1, _("Shortcut"), width=170)
		self.list.InsertColumn(2, _("Area"), width=150)
		sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)

		# Translators: Buttons of the Shortcuts page.
		self.change_btn = wx.Button(self, label=_("&Change shortcut..."))
		self.remove_btn = wx.Button(self, label=_("Remo&ve shortcut"))
		self.reset_btn = wx.Button(self, label=_("&Reset shortcut"))
		self.reset_all_btn = wx.Button(self, label=_("Reset &all shortcuts"))
		row = wx.BoxSizer(wx.HORIZONTAL)
		for button in (self.change_btn, self.remove_btn, self.reset_btn, self.reset_all_btn):
			row.Add(button, 0, wx.RIGHT, 8)
		sizer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		self.SetSizer(sizer)

		for index, action_id in enumerate(self._ids):
			self.list.InsertItem(index, keymap.label(action_id))
			self.list.SetItem(index, 1, self._shortcut_text(action_id))
			self.list.SetItem(index, 2, keymap.area_label(keymap.area(action_id)))
		if self.list.GetItemCount():
			self.list.Focus(0)
			self.list.Select(0)

		self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self.on_change(None))
		self.change_btn.Bind(wx.EVT_BUTTON, self.on_change)
		self.remove_btn.Bind(wx.EVT_BUTTON, self.on_remove)
		self.reset_btn.Bind(wx.EVT_BUTTON, self.on_reset)
		self.reset_all_btn.Bind(wx.EVT_BUTTON, self.on_reset_all)

	def _shortcut_text(self, action_id):
		text = keymap.display_text(self._bindings.get(action_id, ""))
		if not text:
			# Translators: Shortcut column of the Shortcuts page: the action has no shortcut.
			return _("Not assigned")
		if keymap.is_fixed(action_id):
			# Translators: Shortcut column of the Shortcuts page. {key} is a key that cannot be changed.
			return _("{key} (fixed)").format(key=text)
		return text

	def _selected(self):
		"""The selected action id, or None after saying so. A fixed one is refused too."""
		index = self.list.GetFirstSelected()
		if not 0 <= index < len(self._ids):
			ui.message(_("No action selected."))
			return None
		action_id = self._ids[index]
		if keymap.is_fixed(action_id):
			ui.message(_("This shortcut can't be changed."))
			return None
		return action_id

	def _set(self, action_id, text):
		self._bindings[action_id] = text
		self.list.SetItem(self._ids.index(action_id), 1, self._shortcut_text(action_id))
		shown = keymap.display_text(text)
		if shown:
			# Translators: Announced after a shortcut was set. {key} is the shortcut.
			announce_now(_("Assigned {key}.").format(key=shown))
		else:
			# Translators: Announced after a shortcut was removed.
			announce_now(_("Shortcut removed."))

	def on_change(self, event):
		action_id = self._selected()
		if action_id is None:
			return
		gui.mainFrame.prePopup()
		dlg = ShortcutDialog(self, action_id, self._bindings)
		try:
			confirmed = dlg.ShowModal() == wx.ID_OK
			text = dlg.text
		finally:
			dlg.Destroy()
			gui.mainFrame.postPopup()
		self.list.SetFocus()
		if confirmed and text:
			self._set(action_id, text)

	def on_remove(self, event):
		action_id = self._selected()
		if action_id is not None:
			self._set(action_id, "")

	def on_reset(self, event):
		action_id = self._selected()
		if action_id is None:
			return
		text = keymap.default(action_id)
		error = keymap.check(action_id, text, self._bindings)
		if error:
			ui.message(error)  # the default key is used by another action now
			return
		self._set(action_id, text)

	def on_reset_all(self, event):
		answer = wx.MessageBox(
			# Translators: Confirmation before resetting every shortcut.
			_("Reset every shortcut to its default?"),
			# Translators: Title of the reset-all-shortcuts confirmation.
			_("Reset all shortcuts"),
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self,
		)
		if answer != wx.YES:
			return
		for index, action_id in enumerate(self._ids):
			if not keymap.is_fixed(action_id):
				self._bindings[action_id] = keymap.default(action_id)
				self.list.SetItem(index, 1, self._shortcut_text(action_id))
		self.list.SetFocus()
		announce_now(_("All shortcuts reset."))

	def focus_first(self):
		self.list.SetFocus()

	def apply(self):
		keymap.save(self._bindings)


class UpdatesPage(wx.Panel):
	"""Background checks of the active account: which tabs speak when they find news, and how often each
	tab is checked. A tab with no setting of its own follows the "new tabs" setting."""

	PER_ACCOUNT = True  # holds the settings of the active account

	def __init__(self, parent, plugin):
		super().__init__(parent)
		cfg = plugin.config
		self._cfg = cfg
		quiet = {k for k in cfg.get("bg_announce_off").split(",") if k}
		try:
			saved = set(json.loads(cfg.get("bg_tab_settings") or "{}"))
		except (ValueError, TypeError):
			saved = set()
		self._tab_ids = []  # the open tabs ("<type>:<key>"), in list order
		names = {}
		aid = plugin.account_id()
		for entry in (tabstate.get_open_tabs(plugin.store, aid) if aid is not None else []):
			if entry["type"] in BG_TAB_TYPES:
				ident = "%s:%s" % (entry["type"], entry["key"])
				self._tab_ids.append(ident)
				names[ident] = entry.get("name") or entry["key"]
		self._minutes = {ident: cfg.get(key) for ident, key in BG_KEYS.items()}  # home, notifications, bookmarks, tabs
		self._speak = {ident: ident not in quiet for ident in ("home", "notifications", "tabs")}
		for ident in self._tab_ids:
			self._minutes[ident], self._speak[ident] = cfg.tab_setting(ident)
		self._own = {ident for ident in self._tab_ids if ident in saved}  # tabs the reader gave a setting of their own

		labels = dict(UPDATE_ROWS)
		labels.update(names)
		# Translators: Last item of the speak checklist on the Updates page: the setting of tabs opened later.
		labels["tabs"] = _("New tabs")
		self._speak_ids = ["home", "notifications"] + self._tab_ids + ["tabs"]

		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label above the checklist of tabs whose background checks speak when they find news (bookmarks never speak).
		sizer.Add(wx.StaticText(self, label=_("&Speak when a background check finds news in:")), 0, wx.LEFT | wx.TOP, 10)
		self.speak_list = gui.nvdaControls.CustomCheckListBox(
			self, choices=[labels[ident] for ident in self._speak_ids], size=(-1, 120)
		)
		self.speak_list.CheckedItems = [i for i, ident in enumerate(self._speak_ids) if self._speak[ident]]
		self.speak_list.SetSelection(0)
		sizer.Add(self.speak_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

		scroll = wx.ScrolledWindow(self, style=wx.VSCROLL)
		scroll.SetScrollRate(0, 20)
		inner = wx.BoxSizer(wx.VERTICAL)
		rows = [
			# Translators: Label of a background check interval choice (Home).
			("home", _("Check &Home for new tweets:")),
			# Translators: Label of a background check interval choice (Notifications).
			("notifications", _("Check &Notifications:")),
			# Translators: Label of a background check interval choice (Bookmarks).
			("bookmarks", _("Check boo&kmarks:")),
			# Translators: Label of a background check interval choice (the trends of the Search and explore tab).
			("trends", _("Check t&rends:")),
		]
		# Translators: Label of the background check interval choice of one open tab. {name} is the tab name.
		rows += [(ident, _("Check {name}:").format(name=names[ident].replace("&", "&&"))) for ident in self._tab_ids]
		# Translators: Label of the background check interval choice of tabs opened later, and of tabs with no setting of their own.
		rows.append(("tabs", _("Check new &tabs, and tabs with no setting of their own:")))
		self._choices = {}
		for ident, text in rows:
			inner.Add(wx.StaticText(scroll, label=text), 0, wx.LEFT | wx.TOP, 10)
			choice = wx.Choice(scroll, choices=[_bg_label(m) for m in BG_INTERVALS])
			minutes = self._minutes[ident]
			choice.SetSelection(BG_INTERVALS.index(minutes) if minutes in BG_INTERVALS else 3)
			inner.Add(choice, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
			self._choices[ident] = choice
		scroll.SetSizer(inner)
		scroll.FitInside()
		sizer.Add(scroll, 1, wx.EXPAND | wx.TOP, 10)

		note = wx.StaticText(
			self,
			# Translators: Help text of the Updates page.
			label=_(
				"Automatic checks use the same unofficial connection as F5. Longer intervals are gentler "
				"on your account. A tab follows the setting for new tabs until you change it for that tab."
			),
		)
		note.Wrap(560)
		sizer.Add(note, 0, wx.ALL, 10)
		self.SetSizer(sizer)
		# Nothing is bound to the checklist or the choices: a bound EVT_CHECKLISTBOX made NVDA stop announcing
		# the state, and nothing needs to happen before OK or Apply (see apply). Cancel just drops the page.
		self._remember_shown()

	def _read(self):
		"""(minutes, speak) of every row as the page shows them now. Nothing is kept until apply()."""
		minutes = {ident: BG_INTERVALS[choice.GetSelection()] for ident, choice in self._choices.items()}
		checked = set(self.speak_list.CheckedItems)
		speak = {ident: i in checked for i, ident in enumerate(self._speak_ids)}
		return minutes, speak

	def _remember_shown(self):
		"""What the tabs show now: a tab that shows something else at apply() was set by the reader."""
		minutes, speak = self._read()
		self._shown = {ident: (minutes[ident], speak[ident]) for ident in self._tab_ids}

	def _show(self, minutes, speak):
		"""Put the values that were just applied on the page: tabs that follow the new tabs setting moved."""
		for ident in self._tab_ids:
			value = minutes[ident]
			self._choices[ident].SetSelection(BG_INTERVALS.index(value) if value in BG_INTERVALS else 3)
		for i, ident in enumerate(self._speak_ids):
			if ident in self._tab_ids and self.speak_list.IsChecked(i) != speak[ident]:
				self.speak_list.Check(i, speak[ident])
		self._remember_shown()

	def focus_first(self):
		self.speak_list.SetFocus()

	def apply(self):
		cfg = self._cfg
		minutes, speak = self._read()
		default = (minutes["tabs"], speak["tabs"])
		own = {}
		for ident in self._tab_ids:
			value = (minutes[ident], speak[ident])
			if ident in self._own or value != self._shown[ident]:  # the reader gave this tab a setting of its own
				if value != default:  # a tab like the new tabs setting just follows it
					own[ident] = [value[0], value[1]]
			else:  # no setting of its own: it follows the new tabs setting
				minutes[ident], speak[ident] = default
		for ident, key in BG_KEYS.items():
			cfg.set(key, minutes[ident])
		cfg.set("bg_announce_off", ",".join(k for k in ("home", "notifications", "tabs") if not speak[k]))
		cfg.set("bg_tab_settings", json.dumps(own) if own else "")
		self._own = set(own)
		self._show(minutes, speak)


def _settings_title(plugin, page=""):
	"""Title of the settings window: the page shown, the add-on name, and whose settings the pages show."""
	account = plugin.account_label()
	if account:
		# Translators: Title of the settings window. {name} is the add-on name, {account} the account whose settings it shows.
		title = _("{name} settings - {account}").format(name=APP_NAME, account=account)
	else:
		# Translators: Title of the settings window.
		title = _("{name} settings").format(name=APP_NAME)
	return "%s - %s" % (page, title) if page else title


class SettingsDialog(wx.Dialog):
	def __init__(self, parent, plugin):
		super().__init__(
			parent,
			title=_settings_title(plugin), size=(700, 620),
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
		)
		self._plugin = plugin
		self.closed = False
		self._activation_suppressed = True  # AddPage fires page-changed events while building

		root = wx.BoxSizer(wx.VERTICAL)
		self.notebook = wx.Notebook(self)
		self._pages = [
			# Translators: Settings page names.
			(AccountsPage(self.notebook, plugin), _("Accounts")),
			(GeneralPage(self.notebook, plugin), _("General")),
			# Translators: Settings page name (background checks, tab by tab).
			(UpdatesPage(self.notebook, plugin), _("Updates")),
			(DisplayPage(self.notebook, plugin), _("Display")),
			(SoundPage(self.notebook, plugin), _("Sound")),
			# Translators: Settings page name (the shortcuts of the add-on's windows).
			(KeymapPage(self.notebook, plugin), _("Shortcuts")),
		]
		if DiagnosticsPage is not None:
			self._pages.append((DiagnosticsPage(self.notebook, plugin), DiagnosticsPage.TITLE))
		for page, title in self._pages:
			self.notebook.AddPage(page, title)
		root.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 10)

		# Translators: Settings window buttons.
		self.ok_btn = wx.Button(self, wx.ID_OK, _("OK"))
		self.cancel_btn = wx.Button(self, wx.ID_CANCEL, _("Cancel"))
		self.apply_btn = wx.Button(self, wx.ID_APPLY, _("Appl&y"))
		self.ok_btn.SetDefault()
		row = wx.BoxSizer(wx.HORIZONTAL)
		row.Add(self.ok_btn, 0, wx.RIGHT, 8)
		row.Add(self.cancel_btn, 0, wx.RIGHT, 8)
		row.Add(self.apply_btn, 0)
		root.Add(row, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 10)
		self.SetSizer(root)
		self.CentreOnScreen()

		self.ok_btn.Bind(wx.EVT_BUTTON, self.on_ok)
		self.cancel_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
		self.apply_btn.Bind(wx.EVT_BUTTON, self.on_apply)
		self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self.on_page_changing)
		self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_page_changed)
		self.Bind(wx.EVT_CLOSE, self.on_close)
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

		self._activation_suppressed = False
		wx.CallAfter(self._activate_page, 0)

	def apply_account_pages(self):
		"""Before the account changes: what was typed on the pages that hold an account's settings
		goes to the account it was typed for."""
		for page, _title in self._pages:
			if getattr(page, "PER_ACCOUNT", False):
				try:
					page.apply()
				except Exception:
					log.error(TAG + "could not apply a settings page", exc_info=True)

	def account_changed(self):
		"""Another account became active: the pages that hold its settings are built again from its values."""
		self._activation_suppressed = True  # no announcements, no focus moves while pages are swapped
		try:
			for index, (page, title) in enumerate(self._pages):
				if not getattr(page, "PER_ACCOUNT", False):
					continue
				fresh = type(page)(self.notebook, self._plugin)
				self.notebook.DeletePage(index)
				self.notebook.InsertPage(index, fresh, title)
				self._pages[index] = (fresh, title)
		finally:
			self._activation_suppressed = False
		self._update_title(self.notebook.GetSelection())

	def on_page_changing(self, event):
		# Real focus on the notebook before the swap avoids a doubled announcement.
		self.notebook.SetFocus()
		event.Skip()

	def on_page_changed(self, event):
		event.Skip()
		if not self._activation_suppressed:
			self._activate_page(event.GetSelection())

	def _update_title(self, index):
		"""The title names the page that is shown."""
		name = self._pages[index][1] if 0 <= index < len(self._pages) else ""
		self.SetTitle(_settings_title(self._plugin, name))

	def _activate_page(self, index):
		if self.closed or not 0 <= index < len(self._pages):
			return
		self._update_title(index)
		page, title = self._pages[index]
		# Translators: Announced when a settings page is shown. {name} is the page name.
		ui.message(_("{name} tab").format(name=title))
		page.focus_first()

	def _apply(self):
		for page, _title in self._pages:
			try:
				page.apply()
			except Exception:
				log.error(TAG + "could not apply a settings page", exc_info=True)
		self._plugin.settings_changed()

	def on_ok(self, event):
		self._apply()
		self.Close()

	def on_apply(self, event):
		self._apply()
		ui.message(_("Settings applied."))

	def on_char_hook(self, event):
		key = event.GetKeyCode()
		if key == wx.WXK_ESCAPE:
			self.Close()
			return
		if event.ControlDown() and not event.ShiftDown() and ord("1") <= key <= ord("9"):
			index = key - ord("1")
			if index < self.notebook.GetPageCount():
				self.notebook.SetSelection(index)
			return
		event.Skip()

	def on_close(self, event):
		self.closed = True
		self._plugin.settings_closed(self)
		self.Destroy()