# -*- coding: utf-8 -*-
# NVTweet - tray.py: notification area icon that shows how many tweets are unread.
# Enter (or a click) on the icon opens the main window; its menu has Open and Settings.
#
# NVDA reads the first SetIcon text as the button's name and later ones as a tooltip that is
# added after the name, so a change is RemoveIcon + SetIcon, done at once. Removing the button
# makes Explorer move its own focus to a neighbouring button, and UIA reports that to NVDA as a
# real focus change. StrayTrayFocusGuard drops such events while the reader is not in the
# taskbar. If the reader is on our icon, Windows throws the focus to an unrelated icon: we put
# it back. Our icon is recognised by the text prefix.

import addonHandler
import api
import speech
import winUser
import wx
import wx.adv
from logHandler import log
from NVDAObjects.UIA import UIA

from .utils import APP_NAME, TAG

addonHandler.initTranslation()

PREFIX = APP_NAME + ":"
TRAY_CLASS = "Windows.UI.Input.InputSite.WindowClass"  # window class of every button of the notification area
TRAY_ROLE_BUTTON = 9  # controlTypes.Role.BUTTON; the Alt+Tab list shares the class but is another role
TRAY_FOREGROUNDS = ("Shell_TrayWnd", "TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow")  # the reader is in the tray


def category_labels():
	# Short names: they share one tooltip.
	return {
		# Translators: Short tab name in the notification area icon's tooltip (Home).
		"home": _("Home"),
		# Translators: Short tab name in the notification area icon's tooltip (Notifications).
		"notifications": _("Noti"),
		# Translators: Short tab name in the notification area icon's tooltip (Bookmarks).
		"bookmarks": _("Bookmarks"),
	}


def tooltip_text(counts, per_tab=False):
	"""counts: list of (short name, unread) with unread > 0. Must start with APP_NAME and a
	colon: that is how our icon is told apart from the others."""
	if not counts:
		# Translators: Tooltip of the notification area icon when nothing is unread. {name} is the add-on name.
		return _("{name}: no unread").format(name=APP_NAME)
	total = sum(n for _name, n in counts)
	if not per_tab:
		# Translators: Tooltip of the notification area icon. {name} is the add-on name, {count} the total unread.
		return _("{name}: {count} unread").format(name=APP_NAME, count=total)
	parts = ["%s %s" % (name, "99+" if n > 99 else n) for name, n in counts]
	# Translators: Tooltip of the notification area icon. {name} is the add-on name, {list} such as "Home 3, Noti 2".
	text = _("{name}: {list}").format(name=APP_NAME, list=", ".join(parts))
	if len(text) > 127:  # Windows tooltip limit: fall back to the total
		text = _("{name}: {count} unread").format(name=APP_NAME, count=total)
	return text


def _focus_on_our_icon():
	try:
		return (api.getFocusObject().name or "").startswith(PREFIX)
	except Exception:
		return False


def _foreground_class():
	"""Window class of the program that has the focus in Windows (thread safe, no NVDA objects)."""
	return winUser.getClassName(winUser.getForegroundWindow())


def _stray_tray_focus(hwnd):
	"""True for a focus event of a taskbar button (our icon's neighbours, the clock, Start...) while the
	reader is not in the taskbar. Explorer moves its own focus around by itself, for instance when an icon
	goes away or changes, or when the reader leaves the tray, and UIA reports it as if the reader had
	moved there. A real visit always has the taskbar as the foreground window."""
	try:
		root = winUser.getClassName(winUser.getAncestor(hwnd, winUser.GA_ROOT))
		return root in TRAY_FOREGROUNDS and _foreground_class() not in TRAY_FOREGROUNDS
	except Exception:
		return False


class StrayTrayFocusGuard(UIA):
	"""Overlay for the taskbar buttons. A focus event Explorer raised by itself would be taken by NVDA as
	a real focus change: it moves its focus to the button, cancels speech and announces the foreground
	window again before any plugin can stop it. So the event is dropped before it enters NVDA's queue,
	the way NVDA's own explorer module drops the task switching window's."""

	def _get_shouldAllowUIAFocusEvent(self):
		if _stray_tray_focus(self.windowHandle):
			log.debug(TAG + "dropped a focus event that Explorer raised by itself")
			return False
		return super()._get_shouldAllowUIAFocusEvent()


def choose_overlay(obj, clsList):
	"""Called for every object NVDA creates, so the first checks are cheap."""
	if getattr(obj, "windowClassName", "") != TRAY_CLASS or not isinstance(obj, UIA):
		return
	if int(obj.role) == TRAY_ROLE_BUTTON:
		clsList.insert(0, StrayTrayFocusGuard)


def fill_app_menu(menu, bind, on_open, on_settings, choices, on_account):
	"""The entries of the notification area icon's menu, also used for the add-on's submenu of NVDA's
	menu: open, settings and, with more than one account, one entry for each. bind(item, handler) connects
	an entry; choices: [(account, text)]. Returns the entries that were added."""
	items = []
	# Translators: Items of the add-on's menus (the notification area icon and NVDA's menu). {name} is the add-on name.
	item = menu.Append(wx.ID_ANY, _("&Open {name}").format(name=APP_NAME))
	bind(item, on_open)
	items.append(item)
	item = menu.Append(wx.ID_ANY, _("{name} &Settings...").format(name=APP_NAME))
	bind(item, on_settings)
	items.append(item)
	if len(choices) > 1:
		items.append(menu.AppendSeparator())
		for acc, text in choices:
			item = menu.Append(wx.ID_ANY, text)
			bind(item, lambda a=acc: on_account(a))
			items.append(item)
	return items


class TrayIcon(wx.adv.TaskBarIcon):
	RESTORE_INTERVAL_MS = 20
	RESTORE_MAX_TRIES = 15

	def __init__(self, on_open, on_settings, counts, per_tab=False, accounts=None, on_account=None):
		super().__init__()
		self._on_open = on_open
		self._on_settings = on_settings
		self._accounts = accounts  # () -> [(account, menu text)]
		self._on_account = on_account  # (account) -> None
		self._icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
		self._text = None
		self._closed = False
		self._tries = 0
		self.set_counts(counts, per_tab)  # the first tooltip is already the real count
		self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self._clicked)
		self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self._clicked)

	def set_counts(self, counts, per_tab=False):
		text = tooltip_text(counts, per_tab)
		if text == self._text:  # touch the icon only when the text changes
			return
		had_focus = False
		if self._text is not None:
			had_focus = _focus_on_our_icon()
			self.RemoveIcon()
		self._text = text
		self.SetIcon(self._icon, text)
		if had_focus:
			self._tries = 0
			wx.CallLater(self.RESTORE_INTERVAL_MS, self._restore_focus)

	def _restore_focus(self):
		if self._closed:
			return
		self._tries += 1
		try:
			focus = api.getFocusObject()
			if (focus.name or "").startswith(PREFIX) and self._tries > 1:
				return  # focus is back on our icon
			parent = focus.parent
			for child in (parent.children if parent else []):
				if (child.name or "").startswith(PREFIX):
					speech.cancelSpeech()  # drop the announcement of the icon Windows jumped to
					child.setFocus()
					break
		except Exception:
			log.error(TAG + "could not restore the tray icon focus", exc_info=True)
			return
		if self._tries < self.RESTORE_MAX_TRIES:
			wx.CallLater(self.RESTORE_INTERVAL_MS, self._restore_focus)

	def _clicked(self, event):
		wx.CallAfter(self._on_open)  # not inside the tray's own click handling

	def CreatePopupMenu(self):
		menu = wx.Menu()
		choices = []
		if self._accounts is not None and self._on_account is not None:
			try:
				choices = self._accounts()
			except Exception:
				log.error(TAG + "could not list the accounts for the tray menu", exc_info=True)
		fill_app_menu(
			menu, lambda item, handler: self.Bind(wx.EVT_MENU, lambda e: handler(), item),
			self._on_open, self._on_settings, choices,
			lambda acc: wx.CallAfter(self._on_account, acc),  # after the menu has closed: switching redraws the icon
		)
		return menu

	def cleanup(self):
		self._closed = True
		try:
			self.RemoveIcon()
		finally:
			self.Destroy()