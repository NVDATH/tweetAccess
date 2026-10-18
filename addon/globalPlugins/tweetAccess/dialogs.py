# -*- coding: utf-8 -*-
# tweetAccess - dialogs.py: the window that shows one tweet in full.
#
# Top: the tweet. Below: a list of details; Enter opens a link line in the browser,
# Ctrl+C copies the line (the button does the same), F5 loads fresh details.

import webbrowser

import addonHandler
import ui
import wx
from logHandler import log

from . import keymap, soundpack
from .utils import (
	FULL_FORMAT, TAG, announce_now, format_ts, full_title, head_text, info_items, safe_ui, target_id,
)

addonHandler.initTranslation()


class FullTextDialog(wx.Dialog):
	def __init__(self, parent, post, author_mode, plugin):
		super().__init__(parent, title=full_title(post), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._post = post
		self._plugin = plugin
		self._dead = False
		self._items = info_items(post)
		box = wx.TextCtrl(self, value=head_text(post, author_mode), style=wx.TE_MULTILINE | wx.TE_READONLY)
		copy_key = keymap.display("copy_line")
		if copy_key:
			# Translators: Label of the list of details in the full tweet window. {key} is the shortcut that copies the line.
			label_text = _("&Details (Enter opens a link, {key} copies the line):").format(key=copy_key)
		else:
			# Translators: Label of the list of details in the full tweet window.
			label_text = _("&Details (Enter opens a link):")
		label = wx.StaticText(self, label=label_text)
		self.info = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_NO_HEADER | wx.LC_SINGLE_SEL)
		self.info.InsertColumn(0, "", width=580)
		self._fill_info(0)
		# Translators: Buttons of the full tweet window. The shortcut is added after the label, so there is no access key.
		copy_line = wx.Button(self, label=keymap.with_key(_("Copy line"), "copy_line"))
		refresh = wx.Button(self, label=keymap.with_key(_("Refresh details"), "refresh_details"))
		close = wx.Button(self, wx.ID_CANCEL, _("&Close"))

		buttons = wx.BoxSizer(wx.HORIZONTAL)
		buttons.Add(copy_line, 0, wx.RIGHT, 8)
		buttons.Add(refresh, 0, wx.RIGHT, 8)
		buttons.Add(close, 0)
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(box, 2, wx.EXPAND | wx.ALL, 8)
		sizer.Add(label, 0, wx.LEFT | wx.RIGHT, 8)
		sizer.Add(self.info, 1, wx.EXPAND | wx.ALL, 8)
		sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		self.SetSizer(sizer)
		self.SetSize((680, 540))
		self.SetEscapeId(wx.ID_CANCEL)
		self.CentreOnParent()

		self.info.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_activate)
		self.info.Bind(wx.EVT_CONTEXT_MENU, self.on_menu)
		copy_line.Bind(wx.EVT_BUTTON, lambda e: self._copy_line())
		refresh.Bind(wx.EVT_BUTTON, lambda e: self._refresh())
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
		box.SetFocus()

	def Destroy(self):
		self._dead = True  # a result that arrives later is thrown away (see safe_ui)
		return super().Destroy()

	def _fill_info(self, keep):
		self.info.DeleteAllItems()
		for i, (text, url) in enumerate(self._items):
			self.info.InsertItem(i, text)
		if self._items:
			keep = max(0, min(keep, len(self._items) - 1))
			self.info.Focus(keep)
			self.info.Select(keep)

	def _current(self):
		idx = self.info.GetFocusedItem()
		return self._items[idx] if 0 <= idx < len(self._items) else (None, None)

	def _open(self, url):
		if not url.lower().startswith(("http://", "https://")):
			announce_now(_("Only http and https links can be opened."))
			return
		webbrowser.open(url)
		ui.message(_("Opening in the browser"))

	def _copy(self, text, done_text):
		if wx.TheClipboard.Open():
			try:
				wx.TheClipboard.SetData(wx.TextDataObject(text))
			finally:
				wx.TheClipboard.Close()
			announce_now(done_text)
		else:
			announce_now(_("Could not copy to the clipboard."))

	def _copy_line(self):
		text, url = self._current()
		if text is None:
			ui.message(_("No line selected."))
			return
		self._copy(url or text, _("Copied."))

	# ---- refresh: the details come from the cache until this is pressed

	def _refresh(self):
		if self._plugin.account_id() is None:
			return
		if not self._plugin.start_job("tweet", self.on_refreshed, {"id": target_id(self._post)}):
			announce_now(_("Still loading, please wait."))
			return
		announce_now(_("Refreshing details, please wait..."))

	@safe_ui
	def on_refreshed(self, result):
		if not result["ok"]:
			soundpack.play("error")
			announce_now(result["message"])
			return
		if result.get("account_id") != self._plugin.account_id() or not result["posts"]:
			return
		fresh = result["posts"][0]
		post = self._post
		post.stats, post.liked, post.bookmarked = fresh.stats, fresh.liked, fresh.bookmarked
		try:
			self._plugin.store.update_details(
				result["account_id"], post.id, post.stats, post.liked, post.bookmarked,
			)
		except Exception:
			log.error(TAG + "could not save the refreshed details", exc_info=True)
		keep = self.info.GetFocusedItem()
		self._items = info_items(post)
		self._fill_info(keep if keep >= 0 else 0)
		announce_now(_("Details updated."))

	def on_activate(self, event):
		text, url = self._items[event.GetIndex()]
		if url is None:
			ui.message(_("This line is only information."))
			return
		self._open(url)

	def on_menu(self, event):
		text, url = self._current()
		if text is None:
			return
		menu = wx.Menu()
		if url is not None:
			item = menu.Append(wx.ID_ANY, _("Open (Enter)"))
			self.Bind(wx.EVT_MENU, lambda e: self._open(url), item)
		item = menu.Append(wx.ID_ANY, keymap.with_key(_("Copy line"), "copy_line"))
		self.Bind(wx.EVT_MENU, lambda e: self._copy_line(), item)
		item = menu.Append(wx.ID_ANY, keymap.with_key(_("Refresh details"), "refresh_details"))
		self.Bind(wx.EVT_MENU, lambda e: self._refresh(), item)
		self.PopupMenu(menu)
		menu.Destroy()

	def on_char_hook(self, event):
		if keymap.matches(event, "refresh_details"):
			self._refresh()
			return
		if keymap.matches(event, "copy_line") and self.FindFocus() is self.info:
			self._copy_line()
			return
		event.Skip()


def user_info_lines(info):
	"""The lines of the user information window. What X did not tell is left out."""
	lines = []
	name, handle = info.get("name") or "", info.get("handle") or ""
	if name:
		# Translators: First line of the user information window. {name} is the display name, {user} the user name.
		lines.append(_("{name} (@{user})").format(name=name, user=handle))
	else:
		lines.append("@" + handle)
	if info.get("bio"):
		# Translators: Line of the user information window. {text} is the bio of the user.
		lines.append(_("bio: {text}").format(text=" ".join(info["bio"].split())))
	if info.get("location"):
		# Translators: Line of the user information window. {text} is the place the user gave.
		lines.append(_("location: {text}").format(text=info["location"]))
	if info.get("joined"):
		# Translators: Line of the user information window. {time} is the date and time the account was created.
		lines.append(_("joined {time}").format(time=format_ts(info["joined"], "custom", FULL_FORMAT)))
	counts = (
		# Translators: Lines of the user information window. {count} is a number.
		("followers", _("followers: {count}")), ("following", _("following: {count}")),
		("tweets", _("tweets: {count}")), ("likes", _("likes given: {count}")),
	)
	for key, text in counts:
		if isinstance(info.get(key), int):
			lines.append(text.format(count=info[key]))
	flags = (
		# Translators: Lines of the user information window about the user and your account: the first is said when it is true, the second when it is false.
		("you_follow", _("you follow this user"), _("you do not follow this user")),
		("follows_you", _("this user follows you"), _("this user does not follow you")),
		("muted", _("you muted this user"), ""),
		("blocked", _("you blocked this user"), ""),
		("protected", _("protected account"), ""),
		("verified", _("verified account"), ""),
		("can_dm", _("you can send this user a direct message"), _("you cannot send this user a direct message")),
	)
	for key, when_true, when_false in flags:
		value = info.get(key)
		text = when_true if value else when_false
		if value is not None and text:
			lines.append(text)
	return lines


class UserInfoDialog(wx.Dialog):
	"""What X says about one user. Read only."""

	def __init__(self, parent, info):
		# Translators: Title of the user information window. {user} is the user name.
		title = _("User information: @{user}").format(user=info.get("handle") or "")
		super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		# Translators: Label of the text box of the user information window.
		label = wx.StaticText(self, label=_("&Information:"))
		box = wx.TextCtrl(self, value="\n".join(user_info_lines(info)), style=wx.TE_MULTILINE | wx.TE_READONLY)
		close = wx.Button(self, wx.ID_CANCEL, _("&Close"))
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
		sizer.Add(box, 1, wx.EXPAND | wx.ALL, 8)
		sizer.Add(close, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		self.SetSizer(sizer)
		self.SetSize((560, 420))
		self.SetEscapeId(wx.ID_CANCEL)
		self.CentreOnParent()
		box.SetFocus()