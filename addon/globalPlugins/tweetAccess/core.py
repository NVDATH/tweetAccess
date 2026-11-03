# -*- coding: utf-8 -*-
# NVTweet - core.py: the global plugin. The hotkey opens the main window.

import json
import random
import time

import addonHandler
import globalPluginHandler
import gui
import ui
import wx
from logHandler import log
from scriptHandler import script

from . import attachments, keymap, soundpack, tabstate
from .autoread import AutoReader
from .compose import ComposeDialog
from .config import BG_TAB_TYPES, Config
from .mainwindow import MainWindow
from .service import FetchService
from .settings import AddAccountDialog, SettingsDialog
from .storage import open_store
from .tabs import build_tabs
from .tray import TrayIcon, category_labels, choose_overlay, fill_app_menu
from .utils import APP_NAME, TAG, account_label, announce_after_menu, announce_now, is_pinned

addonHandler.initTranslation()

BG_TICK_MS = 60000             # how often we check whether a background update is due
BG_JITTER = 0.25               # up to +25% of the interval is added at random
BG_KEYS = {  # the tabs every account has: the setting that holds the minutes between checks
	"home": "bg_interval", "notifications": "bg_interval_notifications", "bookmarks": "bg_interval_bookmarks",
	"trends": "bg_interval_trends",
}
STARTUP_DELAY_MS = 3000        # after NVDA starts: open the cache, show the tray icon
STARTUP_SYNC_DELAY_MS = 15000  # after NVDA starts: load new tweets once
CLOSED_TABS_KEPT = 20          # removed tabs (per account) that Ctrl+Shift+T can bring back


class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	scriptCategory = APP_NAME

	def __init__(self):
		super().__init__()
		self._store = None
		self._service = None
		self._config = None
		self._active = None  # active account dict, or None
		self._main = None
		self._settings = None
		self._tray = None
		self._closed_tabs = {}  # account id -> [(entry, tweets, position, focused tweet id)] of removed tabs
		self._nvda_menu = None  # the add-on's submenu of NVDA's menu, with the entries of the tray icon's menu
		self._nvda_item = None
		self._nvda_items = []
		self._bg_due = {}  # (account id, category) -> time of its next background check
		self._bg_last_error = {}  # account id -> the last background problem that was spoken
		self._bg_seen = {}  # item (home, notifications, bookmarks, an open tab) -> its interval when last scheduled (active account)
		self._bg_cfgs = {}  # account id -> settings of an account that is not the active one
		self._autoread = None
		self._unread_known = {}  # unread counts a tab (or the cache) already worked out, for the tray icon
		self._tabs_sig = ""  # the optional-tabs setting the open window was built with
		self._terminating = False
		attachments.cleanup_old_temp_files()
		try:
			# One submenu with the entries of the notification area icon's menu. It takes the place of the single
			# settings item this add-on had in the Preferences menu, so NVDA's menu has no duplicate.
			self._nvda_menu = wx.Menu()
			self._nvda_item = gui.mainFrame.sysTrayIcon.preferencesMenu.AppendSubMenu(self._nvda_menu, APP_NAME)
			self._fill_nvda_menu()
		except Exception:
			log.error(TAG + "could not add the add-on menu to NVDA's menu", exc_info=True)
		self._bg_timer = wx.Timer(gui.mainFrame)
		gui.mainFrame.Bind(wx.EVT_TIMER, self._on_bg_tick, self._bg_timer)
		self._bg_timer.Start(BG_TICK_MS)
		wx.CallLater(STARTUP_DELAY_MS, self._startup)

	def terminate(self):
		self._terminating = True
		try:
			self._bg_timer.Stop()
		except Exception:
			pass
		try:
			soundpack.reset_progress()
		except Exception:
			pass
		if self._autoread is not None:
			self._autoread.stop()
		if self._tray is not None:
			try:
				self._tray.cleanup()
			except Exception:
				log.debugWarning(TAG + "could not remove the tray icon", exc_info=True)
			self._tray = None
		try:
			if self._main is not None and not self._main.closed:
				self._main.flush_state()
				self._main.Destroy()
		except Exception:
			log.debugWarning(TAG + "could not close the window", exc_info=True)
		self._main = None
		try:
			if self._settings is not None and not self._settings.closed:
				self._settings.Destroy()
		except Exception:
			log.debugWarning(TAG + "could not close the settings", exc_info=True)
		self._settings = None
		try:
			if self._nvda_item is not None:
				gui.mainFrame.sysTrayIcon.preferencesMenu.Remove(self._nvda_item)
		except Exception:
			pass
		if self._service is not None:
			self._service.stop()
		if self._store is not None:
			self._store.close()
		super().terminate()

	# ------------------------------------------------------------ backend

	@property
	def store(self):
		return self._store

	@property
	def config(self):
		return self._config

	@property
	def busy(self):
		return self._service is not None and self._service.busy

	def _ensure_backend(self):
		"""Open the cache and start the worker thread, once, on first use."""
		if self._store is not None:
			return
		store, note = open_store()
		self._store = store
		keymap.configure(lambda: store.get_meta("keymap"), lambda text: store.set_meta("keymap", text))  # shared by every account
		self._active = store.active_account()
		self._config = Config(store, self._active["id"] if self._active else None)  # settings are per account
		soundpack.init(self._config)
		self._tabs_sig = self._config.get("tabs_off")
		self._service = FetchService(store, notify=lambda cb, res: wx.CallAfter(cb, res))
		self._service.start()
		if note:
			ui.message(note)

	def start_job(self, kind, callback, options=None, progress=True, background=False):
		"""Returns False if a job is already running. The active account is added to the options.
		progress: loop the progress sound until the result comes back.
		background: a check nobody asked for (it never waits, and a reader's request goes before it)."""
		options = dict(options or {})
		if "account_id" not in options and self._active is not None:
			options["account_id"] = self._active["id"]
		if kind == "reload":
			self._schedule_bg("home")
		if progress:
			inner = callback

			def callback(result):
				soundpack.stop_progress()
				inner(result)
		if not self._service.submit(kind, callback, options, background=background):
			return False
		if progress:
			soundpack.start_progress()
		return True

	# ------------------------------------------------------------ windows

	def _live_main(self):
		"""The main window if it really is usable. A hidden or dead leftover is dropped."""
		main = self._main
		if main is None:
			return None
		try:
			if not main.closed and main.IsShown():
				return main
			log.info(TAG + "main window is %s: discarding it" % ("closed" if main.closed else "hidden"))
			if not main.closed:
				main.closed = True
				main.Destroy()
		except RuntimeError:
			log.info(TAG + "main window object is gone: discarding it")
		self._main = None
		gui.mainFrame.postPopup()  # balances the prePopup of _open_main
		return None

	def _live_settings(self):
		settings = self._settings
		if settings is None:
			return None
		try:
			if not settings.closed and settings.IsShown():
				return settings
			log.info(TAG + "settings window is %s: discarding it" % ("closed" if settings.closed else "hidden"))
			if not settings.closed:
				settings.closed = True
				settings.Destroy()
		except RuntimeError:
			log.info(TAG + "settings window object is gone: discarding it")
		self._settings = None
		gui.mainFrame.postPopup()  # balances the prePopup of open_settings
		return None

	def _bring_front(self, win):
		try:
			from .utils import force_foreground
			gui.mainFrame.prePopup()
			try:
				if win.IsIconized():
					win.Iconize(False)
				if not win.IsShown():
					win.Show()
				win.Raise()
				forced = force_foreground(win.GetHandle())
			finally:
				gui.mainFrame.postPopup()
			wx.CallLater(150, self._focus_window, win)  # after Windows has finished activating it
			return bool(forced or win.IsActive())
		except RuntimeError:
			log.info(TAG + "could not bring a window to the front")
			return False

	def _focus_window(self, win):
		try:
			if win.closed:
				return
			focus = getattr(win, "focus_current", None)
			if callable(focus):
				focus()
			else:
				win.SetFocus()
		except RuntimeError:
			pass

	# ------------------------------------------------------------ start of NVDA

	def _startup(self):
		"""Shortly after NVDA starts: the tray icon shows the unread count from the cache,
		and one background check loads what arrived meanwhile."""
		if self._terminating:
			return
		try:
			self._ensure_backend()
		except Exception:
			log.error(TAG + "could not start", exc_info=True)
			return
		self._apply_tray()
		self._fill_nvda_menu()
		if self._active is not None and self.bg_enabled() and self._config.get("bg_interval") > 0:
			wx.CallLater(STARTUP_SYNC_DELAY_MS, self._startup_sync)

	def _startup_sync(self):
		if self._terminating or self._active is None or self._service.busy or not self.bg_enabled():
			return
		self._schedule_bg("home")
		self._service.submit("reload", self._on_bg_result, {"account_id": self._active["id"]}, background=True)

	# ------------------------------------------------------------ tray icon

	def _unread_now(self):
		if self._store is None or self._active is None:
			return 0
		return self._store.unread_count(self._active["id"])

	def _tray_picked(self, cfg=None):
		"""Ids of the tabs, besides Home and Notifications, that the tray icon counts (cfg: settings of an account)."""
		try:
			return set(json.loads((cfg or self._config).get("tray_tabs") or "[]"))
		except ValueError:
			return set()

	def _cached_unread(self, ident, aid=None):
		"""Unread count of a tab kept in its cache (the tab may not be open). ident: "notifications",
		"bookmarks", or "<type>:<key>" of a saved tab. aid: another account than the active one."""
		own = aid is None or aid == self.account_id()
		if own:
			aid = self.account_id()
		if aid is None:
			return 0
		count = self._unread_known.get(ident) if own else None  # only the active account's counts are remembered
		if count is None:
			tab_type, _sep, tab_key = ident.partition(":")
			cached = tabstate.get_cache(self._store, aid, tab_type, tab_key or tab_type) or []
			count = sum(1 for p in cached if not p.is_read)
			if own:
				self._unread_known[ident] = count
		return count

	def _tray_counts(self, home=None, aid=None):
		"""(short name, unread) of every tab the icon counts, only those with something unread.
		home: the Home count an open tab just worked out; otherwise it is counted in the cache.
		aid: count for another account than the active one, with the tabs its own settings pick."""
		own = aid is None or aid == self.account_id()
		if own:
			aid, cfg = self.account_id(), self._config
		else:
			cfg = self._cfg_for(aid)
		if cfg is None or aid is None:
			return []
		off = {k for k in cfg.get("tray_disabled").split(",") if k}
		labels = category_labels()
		counts = []
		if "home" not in off:
			unread = home if (home is not None and own) else self._store.unread_count(aid)
			if unread:
				counts.append((labels["home"], unread))
		if "notifications" not in off:
			unread = self._cached_unread("notifications", aid)
			if unread:
				counts.append((labels["notifications"], unread))
		picked = self._tray_picked(cfg)
		if "bookmarks" in picked:
			unread = self._cached_unread("bookmarks", aid)
			if unread:
				counts.append((labels["bookmarks"], unread))
		if any(":" in ident for ident in picked):
			for entry in tabstate.get_open_tabs(self._store, aid):
				ident = "%s:%s" % (entry["type"], entry["key"])
				if ident not in picked:
					continue
				unread = self._cached_unread(ident, aid)
				if unread:
					name = entry.get("name") or entry["key"]
					counts.append((name if len(name) <= 16 else name[:15].rstrip() + "...", unread))
		return counts

	def _apply_tray(self):
		"""Show or hide the notification area icon as the setting says."""
		wanted = self._config is not None and self._config.get("show_tray")
		if wanted and self._tray is None:
			self._tray = TrayIcon(
				self.open_main_window, self.open_settings, self._tray_counts(),
				self._config.get("tray_per_tab"),
				accounts=self.account_choices, on_account=lambda acc: self.choose_account(acc),
			)
		elif not wanted and self._tray is not None:
			self._tray.cleanup()
			self._tray = None

	def tray_refresh(self, key=None, unread=None):
		"""Update the icon. key/unread: a count an open tab just worked out itself.
		Home is never remembered between calls: the background check changes its count while
		no tab is open, and a remembered value froze the icon.
		Replacing the icon makes Explorer move its own focus to another button; the focus event that
		follows is dropped before NVDA sees it (StrayTrayFocusGuard in tray.py), so the icon can be
		updated at any time."""
		if key is not None and key != "home":
			self._unread_known[key] = unread  # kept until a background merge writes the cache
		if self._tray is None:
			return
		t0 = time.perf_counter()  # TEMP
		self._tray.set_counts(self._tray_counts(), self._config.get("tray_per_tab"))
		if time.perf_counter() - t0 > 0.05:  # TEMP
			log.info(TAG + "TEMP tray_refresh took %.2fs" % (time.perf_counter() - t0))  # TEMP

	# ------------------------------------------------------------ main window

	def open_main_window(self):
		"""Used by the hotkey and by the tray icon."""
		try:
			self._ensure_backend()
		except Exception:
			log.error(TAG + "could not start", exc_info=True)
			ui.message(_("{name} could not start. See the NVDA log.").format(name=APP_NAME))
			return
		main = self._live_main()
		if main is not None:
			self._bring_front(main)
			return
		if self._active is None:
			wx.CallAfter(self._first_login)  # a modal window must not open straight from a gesture
			return
		self._open_main()

	def _open_main(self):
		t0 = time.perf_counter()  # TEMP
		gui.mainFrame.prePopup()
		try:
			main = MainWindow(gui.mainFrame, self)
			t1 = time.perf_counter()  # TEMP
			self._main = main
			build_tabs(main, self)
			t2 = time.perf_counter()  # TEMP
		except Exception:
			self._main = None
			gui.mainFrame.postPopup()
			raise
		main.activate_initial()
		t3 = time.perf_counter()  # TEMP
		main.Show()
		main.Raise()
		soundpack.play("main_open")
		log.info(
			TAG + "TEMP open main: window %.2fs, tabs %.2fs, first tab %.2fs, show %.2fs"
			% (t1 - t0, t2 - t1, t3 - t2, time.perf_counter() - t3)
		)  # TEMP

	def main_closed(self, main):
		if self._main is main:
			self._main = None
		gui.mainFrame.postPopup()
		if not self._terminating:
			soundpack.play("main_close")

	def _first_login(self):
		gui.mainFrame.prePopup()
		try:
			wx.MessageBox(
				# Translators: Message shown when no account is logged in.
				_("You are not logged in. The settings open now so you can add an account."),
				# Translators: Title of the not-logged-in message.
				_("Not logged in"),
				wx.OK | wx.ICON_INFORMATION,
			)
		finally:
			gui.mainFrame.postPopup()
		self._prompt_login()

	def _prompt_login(self):
		"""First login: the Add account window on its own, no settings window involved."""
		gui.mainFrame.prePopup()
		dlg = AddAccountDialog(gui.mainFrame, self)
		try:
			confirmed = dlg.ShowModal() == wx.ID_OK
			result = dlg.result
		finally:
			dlg.Destroy()
			gui.mainFrame.postPopup()
		if not confirmed or not result:
			return
		identity = result["identity"]
		account_id = self.add_account(identity, result["cookies"])
		acc = next(a for a in self._store.list_accounts() if a["id"] == account_id)
		text = _("Log in success! Logged in as {account}.").format(account=account_label(acc))
		if not (identity["name"] or identity["handle"]):
			text += "\n" + _("The account name could not be found. You can set one with Rename.")
		gui.mainFrame.prePopup()
		try:
			wx.MessageBox(text, _("Log in"), wx.OK | wx.ICON_INFORMATION)
		finally:
			gui.mainFrame.postPopup()
		self.after_login()

	# ------------------------------------------------------------ removed tabs (Ctrl+Shift+T)

	def remember_closed_tab(self, panel, position):
		"""A removable tab is about to go: keep what is needed to bring it back."""
		aid = self.account_id()
		kind, key = getattr(panel, "TAB_TEMP_TYPE", None), getattr(panel, "TAB_TEMP_KEY", None)
		snapshot = getattr(panel, "snapshot", None)
		if aid is None or not kind or key is None or not callable(snapshot):
			return
		entry = next(
			(e for e in tabstate.get_open_tabs(self._store, aid) if e["type"] == kind and e["key"] == key), None
		)
		if entry is None:
			return
		entry = dict(entry, name=panel.TAB_NAME)  # a renamed tab keeps its new name
		stack = self._closed_tabs.setdefault(aid, [])
		stack.append((entry, snapshot(), position, panel._current_post_id()))
		del stack[:-CLOSED_TABS_KEPT]

	def pop_closed_tab(self):
		"""The tab removed last in the active account: (entry, tweets, position, focused tweet id) or None."""
		stack = self._closed_tabs.get(self.account_id())
		return stack.pop() if stack else None

	# ------------------------------------------------------------ accounts

	def account_id(self):
		return self._active["id"] if self._active else None

	def account_label(self):
		return account_label(self._active)

	def add_account(self, identity, cookies):
		"""Store a checked account and make it the active one. Returns its id."""
		account_id = self._store.add_account(identity["uid"], identity["handle"], identity["name"], cookies)
		self._store.set_active(account_id)
		self._service.reset_client()
		self.accounts_changed()
		return account_id

	def switch_account(self, account_id, focus=False, after_menu=False):
		"""The one way to change the active account. focus: the reader asked from the main window,
		so the list there takes focus; from the settings window nothing takes focus.
		after_menu: a menu has just closed, so the announcement must not be masked by the focus
		that returns to where it was. Returns False if nothing changed."""
		current = self._active["id"] if self._active else None
		if account_id == current or not any(a["id"] == account_id for a in self._store.list_accounts()):
			return False
		main = self._live_main()
		self._store.set_active(account_id)
		self.accounts_changed(focus=focus and main is not None)
		text = _("{account} is now the active account.").format(account=self.account_label())
		if focus and main is not None:
			wx.CallLater(250, self._settle_switch, text)  # after Windows has moved the focus around
		elif after_menu:
			announce_after_menu(text)
		else:
			ui.message(text)
		if not self._store.load_recent(account_id, 1):
			self.sync_now()
		return True

	def _settle_switch(self, text):
		"""Switched from the main window's menu: the list takes the focus last, then the account is announced."""
		main = self._live_main()
		if main is not None and main.IsActive():
			main.focus_current()
		announce_now(text)

	def account_choices(self, counts=True):
		"""[(account, menu text)] for the account menus: number, name, unread Home tweets, active mark.
		counts False: no unread count (a menu that is built once cannot keep the number up to date)."""
		choices = []
		for number, acc in enumerate(self._store.list_accounts(), start=1):
			name = account_label(acc).replace("&", "&&")  # an ampersand in a name is not a mnemonic
			unread = 0
			if counts:
				unread = sum(n for _name, n in self._tray_counts(aid=acc["id"]))  # every tab that account's tray counts
				if acc["active"]:
					# Translators: Entry of the account menu for the active account. {account} is its name, {count} its unread Home tweets.
					text = _("{account}, {count} unread (active)")
				else:
					# Translators: Entry of the account menu. {account} is its name, {count} its unread Home tweets.
					text = _("{account}, {count} unread")
			elif acc["active"]:
				# Translators: Entry of the account menu for the active account, with no unread count. {account} is its name.
				text = _("{account} (active)")
			else:
				text = "{account}"
			prefix = "&%d " % number if number < 10 else ""
			choices.append((acc, prefix + text.format(account=name, count=unread)))
		return choices

	def _fill_nvda_menu(self):
		"""The entries of the add-on's submenu in NVDA's menu, the same as the notification area icon's menu.
		NVDA's menu is built once and can be opened by keyboard with no event of ours, so the entries are made
		again whenever the accounts change, and the account entries carry no unread count."""
		menu, sysicon = self._nvda_menu, gui.mainFrame.sysTrayIcon
		if menu is None:
			return
		for item in self._nvda_items:
			sysicon.Unbind(wx.EVT_MENU, source=item)
			menu.Delete(item)
		choices = self.account_choices(counts=False) if self._store is not None else []
		self._nvda_items = fill_app_menu(
			menu, lambda item, handler: sysicon.Bind(wx.EVT_MENU, lambda e: handler(), item),
			self.open_main_window, self.open_settings, choices,
			lambda acc: wx.CallAfter(self.choose_account, acc),  # after the menu has closed
		)

	def choose_account(self, acc, focus=False):
		"""An account picked in a menu: Alt+W in the main window (focus), or the tray icon's menu."""
		if not self.switch_account(acc["id"], focus=focus, after_menu=True):
			# Translators: Announced when the account picked in a menu is already the active one. {account} is its name.
			announce_after_menu(_("{account} is already the active account.").format(account=account_label(acc)))

	def bg_enabled(self, account_id=None):
		"""Whether an account (default: the active one) is checked in the background. Kept with the
		account list, so the Accounts page and the background checks can read every account's flag."""
		aid = self.account_id() if account_id is None else account_id
		return aid is not None and self._store.get_meta("bg_enabled:%d" % aid) != "0"

	def set_bg_enabled(self, account_id, enabled):
		self._store.set_meta("bg_enabled:%d" % account_id, "1" if enabled else "0")
		if enabled:
			self._schedule_bg(aid=account_id)
		else:
			self._forget_bg(account_id)

	def rename_account(self, account_id, name):
		self._store.rename_account(account_id, name)
		self._active = self._store.active_account()
		self._fill_nvda_menu()
		main = self._live_main()
		if main is not None:
			main.update_title()

	def remove_account(self, account_id):
		main = self._live_main()
		if main is not None:
			main.flush_state()  # before the data goes, so nothing is written for the removed account later
		self._store.remove_account(account_id)
		self._closed_tabs.pop(account_id, None)
		self._forget_bg(account_id)
		self._service.reset_client()
		self.accounts_changed()

	def accounts_changed(self, focus=False):
		"""Every change of the account list or of the active account ends here (the only rebuild).
		focus: the main window asked for it, so its list takes focus."""
		if self._autoread is not None:
			self._autoread.stop()  # it follows account_id(): it must not go on with another account
		self._active = self._store.active_account()
		self._config.load(self._active["id"] if self._active else None)  # every account has its own settings
		soundpack.reload()
		self._tabs_sig = self._config.get("tabs_off")
		self._bg_cfgs = {}  # copies of other accounts' settings may be older than what was just saved
		self._unread_known = {}
		aid = self.account_id()
		self._bg_seen = {}
		if aid is not None:
			self._bg_last_error.pop(aid, None)
			items = self._bg_items(aid, self._config)
			self._bg_seen = dict(items)
			for name, _minutes in items:
				if (aid, name) not in self._bg_due:  # the other accounts keep their own timers
					self._schedule_bg(name, aid)
		self._apply_tray()  # showing the icon is a setting of the account too
		self._fill_nvda_menu()
		self.tray_refresh()
		main = self._live_main()
		if main is None:
			return
		# Without focus the main window never takes it from the settings window that caused the change.
		main.rebuild(lambda: build_tabs(main, self), focus=focus)

	def after_login(self):
		"""A successful Add account: load the timeline, open the main window if it is not open."""
		self.sync_now()
		if self._live_main() is None and self._active is not None:
			self._open_main()

	def sync_now(self):
		"""Load the active account's timeline right away (after a login or a switch)."""
		if self._active is None:
			return
		if self.start_job("reload", self._on_sync_result):
			ui.message(_("Loading your timeline, please wait."))

	def _on_sync_result(self, result):
		self._report_new(result, always=True)

	# ------------------------------------------------------------ settings

	def open_settings(self, evt=None):
		try:
			self._ensure_backend()
		except Exception:
			log.error(TAG + "could not start", exc_info=True)
			ui.message(_("{name} could not start. See the NVDA log.").format(name=APP_NAME))
			return
		settings = self._live_settings()
		if settings is not None:
			if self._bring_front(settings):
				return
			# It exists but Windows will not bring it up: close it and open a fresh one.
			settings.Close()
		gui.mainFrame.prePopup()  # balanced in settings_closed, like NVSky's dialogs
		try:
			dlg = SettingsDialog(gui.mainFrame, self)
			dlg.Show()
			dlg.Raise()
		except Exception:
			gui.mainFrame.postPopup()
			raise
		self._settings = dlg

	def settings_changed(self):
		self._reschedule_changed()
		self._apply_tray()
		self.tray_refresh()
		tabs = self._config.get("tabs_off")
		tabs_changed, self._tabs_sig = tabs != self._tabs_sig, tabs
		main = self._live_main()
		if main is None:
			return
		main.refresh_labels()
		if tabs_changed:
			main.rebuild(lambda: build_tabs(main, self), focus=False)
		else:
			main.reload_display()

	def settings_closed(self, dialog):
		if self._settings is dialog:
			self._settings = None
			gui.mainFrame.postPopup()
		if self._main is not None and not self._main.closed:
			wx.CallAfter(self._focus_main)

	def _focus_main(self):
		try:
			if self._main is not None and not self._main.closed:
				self._main.Raise()
				self._main.focus_current()
		except RuntimeError:
			pass

	# ------------------------------------------------------------ background updates

	# Every account with "Enable this account in background" ticked is checked, each with the intervals of
	# its own settings. One check runs at a time, the most overdue first, so the accounts take turns.

	def _cfg_for(self, aid):
		"""Settings of an account: the live ones for the active account, a separate copy for the others."""
		if aid == self.account_id():
			return self._config
		cfg = self._bg_cfgs.get(aid)
		if cfg is None:
			cfg = self._bg_cfgs[aid] = Config(self._store, aid)
		return cfg

	def _account_exists(self, aid):
		return aid is not None and any(a["id"] == aid for a in self._store.list_accounts())

	def _forget_bg(self, aid):
		"""An account is gone or its background checks are off: drop what was kept for it."""
		for key in [k for k in self._bg_due if k[0] == aid]:
			del self._bg_due[key]
		self._bg_cfgs.pop(aid, None)
		self._bg_last_error.pop(aid, None)

	def _tag(self, aid, text):
		"""A message about another account than the active one starts with its @user."""
		if aid is None or aid == self.account_id():
			return text
		acc = next((a for a in self._store.list_accounts() if a["id"] == aid), None)
		if acc is None:
			return text
		name = "@" + acc["handle"] if acc["handle"] else account_label(acc)
		# Translators: A background message about an account that is not the active one. {account} is its @user, {text} the message.
		return _("{account}: {text}").format(account=name, text=text)

	def _bg_failed(self, aid, message):
		"""Say a background problem once for each account."""
		if self._bg_last_error.get(aid) == message:
			return
		self._bg_last_error[aid] = message
		ui.message(self._tag(aid, _("Background update failed: {message}").format(message=message)))

	def _bg_items(self, aid, cfg):
		"""[(item, minutes between checks)] of everything the background check of an account covers: Home,
		Notifications, Bookmarks and every open user / search / list / thread / quotes tab (item "<type>:<key>")."""
		items = [(item, cfg.get(key)) for item, key in BG_KEYS.items()]
		for entry in tabstate.get_open_tabs(self._store, aid):
			if entry["type"] in BG_TAB_TYPES:
				ident = "%s:%s" % (entry["type"], entry["key"])
				items.append((ident, cfg.tab_setting(ident)[0]))
		return items

	def _schedule_bg(self, item=None, aid=None):
		"""Next background check of one item (or of all) of one account (default: the active one):
		the interval plus a random extra."""
		aid = self.account_id() if aid is None else aid
		if aid is None:
			return
		minutes_of = dict(self._bg_items(aid, self._cfg_for(aid)))
		for name in ((item,) if item else minutes_of):
			minutes = minutes_of.get(name, 0)
			if minutes <= 0:
				self._bg_due.pop((aid, name), None)
				continue
			seconds = minutes * 60
			self._bg_due[(aid, name)] = time.time() + seconds + random.uniform(0, BG_JITTER * seconds)

	def _collect_due(self, now):
		"""[(due time, account id, item)] of the background checks that are due now, most overdue first,
		for every account that has them on. Also forgets the timers of closed tabs and sets the first
		timer of what has none."""
		due_items = []
		for acc in self._store.list_accounts():
			aid = acc["id"]
			if not self.bg_enabled(aid):
				continue
			items = self._bg_items(aid, self._cfg_for(aid))
			live = {name for name, _minutes in items}
			for key in [k for k in self._bg_due if k[0] == aid and k[1] not in live]:
				del self._bg_due[key]  # a tab that was closed
			for name, minutes in items:
				if minutes <= 0:
					self._bg_due.pop((aid, name), None)
					continue
				due = self._bg_due.get((aid, name))
				if due is None:
					self._schedule_bg(name, aid)
				elif now >= due:
					due_items.append((due, aid, name))
		return sorted(due_items)

	def _on_bg_tick(self, evt):
		"""Every tick starts at most one due check (account and category), the most overdue first, so a
		check never asks X for everything at once and every account gets its turn."""
		tick_start = time.perf_counter()  # TEMP
		try:
			self._ensure_backend()
			if self._service.busy:
				return
			now = time.time()
			for _due, aid, name in self._collect_due(now):
				self._schedule_bg(name, aid)
				if self._run_bg(aid, name):
					return
		except Exception:
			log.error(TAG + "background update failed to start", exc_info=True)
		finally:  # TEMP
			if time.perf_counter() - tick_start > 0.2:  # TEMP
				log.info(TAG + "TEMP background tick took %.2fs" % (time.perf_counter() - tick_start))  # TEMP

	def _run_bg(self, aid, cat):
		"""Start the background check of one item (Home, Notifications, Bookmarks or an open tab, "<type>:<key>")
		of one account. Returns False if nothing was started."""
		if cat == "home":
			return self._service.submit("reload", self._on_bg_result, {"account_id": aid}, background=True)
		if cat in ("notifications", "bookmarks"):
			options = {"account_id": aid, "source": cat, "params": {}, "chain": cat}
			if cat == "notifications":
				options["known"] = self._known_ids(aid, cat)  # read pages until they overlap what we have
			return self._service.submit(
				"feed", lambda result, c=cat: self._on_bg_feed(c, result), options, background=True,
			)
		if cat == "trends":
			return self._run_bg_trends(aid)
		main = self._live_main() if aid == self.account_id() else None  # an open tab: refresh the tab itself, else its saved copy
		if main is not None:
			for panel in main.getOpenTabs():
				if (
					not getattr(panel, "TAB_KEY", None) and getattr(panel, "TAB_TEMP_TYPE", None)
					and callable(getattr(panel, "refresh", None))
					and "%s:%s" % (panel.TAB_TEMP_TYPE, panel.TAB_TEMP_KEY) == cat
				):
					return panel.refresh(
						lambda changed, p=panel: self._bg_news("tabs", p.TAB_NAME, aid, cat) if changed else None,
						quiet=True,
					)
		entry = next(
			(e for e in tabstate.get_open_tabs(self._store, aid) if "%s:%s" % (e["type"], e["key"]) == cat), None
		)
		return self._bg_saved_tab(aid, entry) if entry is not None else False

	def _run_bg_trends(self, aid):
		"""Load again the trends that the Search and explore tab showed last, into their cache. Nothing is said:
		the list is simply up to date when the reader looks."""
		try:
			state = json.loads(self._store.get_account_meta(aid, "explore_state") or "{}")
			kind, value = (state.get("source") or ["category", "trending"])[:2]
		except (ValueError, TypeError):
			kind, value = "category", "trending"
		options = {"account_id": aid}
		options["woeid" if kind == "place" else "category"] = value
		key = "%s:%s" % (kind, value)
		return self._service.submit(
			"trends", lambda result, k=key: self._on_bg_trends(k, result), options, background=True,
		)

	def _on_bg_trends(self, key, result):
		aid = result.get("account_id")
		if not self._account_exists(aid):
			return  # the account was removed meanwhile
		if not result["ok"]:
			self._bg_failed(aid, result["message"])
			return
		self._bg_last_error.pop(aid, None)
		self._store.set_account_meta(aid, "trends:" + key, json.dumps(result["trends"]))
		main = self._live_main() if aid == self.account_id() else None
		if main is not None:
			for panel in main.getOpenTabs():
				hook = getattr(panel, "bg_trends", None)
				if callable(hook):
					hook(key, result["trends"])

	def _bg_saved_tab(self, aid, entry):
		"""The tab is not open in the main window (it is closed, or the account is not the active one):
		refresh the saved copy of this user / search / list / thread / quotes tab into its cache."""
		if entry["type"] in ("thread", "quotes"):
			options = {
				"account_id": aid, "id": entry.get("target") or entry["key"], "handle": entry.get("handle") or "",
				"pages": self._cfg_for(aid).get("feed_pages"),
			}
			return self._service.submit(
				entry["type"], lambda result, e=entry: self._on_bg_tab(e, result), options, background=True,
			)
		cached = tabstate.get_cache(self._store, aid, entry["type"], entry["key"]) or []
		options = {
			"account_id": aid, "source": entry["type"], "params": entry.get("params") or {},
			"chain": entry["key"], "known": [p.id for p in cached], "pinned": [p.id for p in cached if is_pinned(p)],
		}
		return self._service.submit(
			"feed", lambda result, e=entry: self._on_bg_tab(e, result), options, background=True,
		)

	def _on_bg_tab(self, entry, result):
		aid = result.get("account_id")
		if not self._account_exists(aid):
			return  # the account was removed meanwhile
		if not result["ok"]:
			self._bg_failed(aid, result["message"])
			return
		self._bg_last_error.pop(aid, None)
		own = aid == self.account_id()
		cached = tabstate.get_cache(self._store, aid, entry["type"], entry["key"]) or []
		known = {p.id for p in cached}
		ident = "%s:%s" % (entry["type"], entry["key"])
		if entry["type"] in ("thread", "quotes"):
			# the tab shows the whole conversation / quote list: replace it, keeping what was read
			posts = result["posts"]
			if not posts or [p.id for p in posts] == [p.id for p in cached]:
				return
			was_read = {p.id: p.is_read for p in cached}
			for p in posts:
				if p.id in was_read:
					p.is_read = was_read[p.id]
			tabstate.set_cache(self._store, aid, entry["type"], entry["key"], posts)
			if own:
				self._unread_known.pop(ident, None)
				self.tray_refresh()
			if any(p.id not in known for p in posts):
				self._bg_news("tabs", entry.get("name") or entry["key"], aid, ident)
			return
		fresh = [p for p in result["posts"] if p.id not in known]
		if not fresh:
			return
		merged = sorted(fresh + cached, key=lambda p: p.id, reverse=True)
		tabstate.set_cache(self._store, aid, entry["type"], entry["key"], merged[:500])
		if own:
			self._unread_known.pop(ident, None)
			self.tray_refresh()
		self._bg_news("tabs", entry.get("name") or entry["key"], aid, ident)

	def _known_ids(self, aid, cat):
		"""Ids of the tweets a tab of this account already has: from the open tab, else from its cache."""
		main = self._live_main() if aid == self.account_id() else None
		if main is not None:
			for panel in main.getOpenTabs():
				if getattr(panel, "TAB_KEY", None) == cat:
					return [p.id for p in panel._posts.rows]
		cached = tabstate.get_cache(self._store, aid, cat, cat) or []
		return [p.id for p in cached]

	def _reschedule_changed(self):
		"""Settings were applied: only an item whose interval changed gets a new schedule."""
		aid = self.account_id()
		if aid is None:
			return
		seen = {}
		for name, minutes in self._bg_items(aid, self._config):
			seen[name] = minutes
			if self._bg_seen.get(name) != minutes:
				self._schedule_bg(name, aid)
		self._bg_seen = seen

	def live_main(self):
		return self._live_main()

	def unread_invalidate(self, ident):
		"""A tab's unread count changed in its cache (auto read): forget the remembered value, redraw the icon."""
		self._unread_known.pop(ident, None)
		self.tray_refresh()

	def _bg_announces(self, cat, aid=None, tab=None):
		"""Whether this account's own settings let a background check speak about this category, or about
		this open tab (tab: its "<type>:<key>")."""
		cfg = self._cfg_for(self.account_id() if aid is None else aid)
		if tab is not None:
			return cfg.tab_setting(tab)[1]
		return cat not in {k for k in cfg.get("bg_announce_off").split(",") if k}

	def _bg_news(self, cat, info, aid=None, tab=None):
		"""A background check found news: always the sound, speech only for what speaks.
		aid: the account it is about (default: the active one); another account's news starts with its @user.
		tab: the open tab it is about ("<type>:<key>"), whose own setting says whether to speak."""
		soundpack.play("notification")
		if not self._bg_announces(cat, aid, tab):
			return
		if cat == "notifications":
			text = _("{count} new notifications").format(count=info)
		else:
			# Translators: Announced when a background check refreshed an open tab and found changes. {name} is the tab name.
			text = _("New in {name}").format(name=info)
		ui.message(self._tag(aid, text))

	def _on_bg_feed(self, cat, result):
		aid = result.get("account_id")
		if not self._account_exists(aid):
			return  # the account was removed meanwhile
		if not result["ok"]:
			self._bg_failed(aid, result["message"])
			return
		self._bg_last_error.pop(aid, None)
		added = self._merge_bg_feed(cat, result["posts"], aid)
		if aid == self.account_id():
			self.tray_refresh()
		if added and cat != "bookmarks":  # bookmarks are the user's own: updated quietly
			self._bg_news(cat, added, aid)

	def _merge_bg_feed(self, cat, posts, aid):
		"""Fold a fetched first page into the open tab, or into the account's cache when it is not open
		(or not the active account). Returns how many are new."""
		own = aid == self.account_id()
		main = self._live_main() if own else None
		if main is not None:
			for panel in main.getOpenTabs():
				if getattr(panel, "TAB_KEY", None) == cat:
					return panel.merge_bg(posts)
		cached = tabstate.get_cache(self._store, aid, cat, cat) or []
		known = {p.id for p in cached}
		fresh = [p for p in posts if p.id not in known]
		if fresh:
			merged = fresh + cached
			if cat == "notifications":
				merged.sort(key=lambda p: p.id, reverse=True)
			tabstate.set_cache(self._store, aid, cat, cat, merged[:500])
			if own:
				self._unread_known.pop(cat, None)
		return len(fresh)

	def _on_bg_result(self, result):
		self._report_new(result, always=False)

	def _report_new(self, result, always):
		"""Show the tweets of a finished reload. Background runs only speak when there is news, and
		a background run for another account than the active one starts with its @user."""
		aid = result.get("account_id")
		if not result["ok"]:
			message = result["message"]
			if always:
				soundpack.play("error")
				ui.message(message)
			elif self._account_exists(aid):
				self._bg_failed(aid, message)
			return
		active = aid == self.account_id()
		if (always and not active) or not self._account_exists(aid):
			return  # the reader switched accounts (or the account was removed) while it loaded
		self._bg_last_error.pop(aid, None)
		main = self._live_main() if active else None
		if main is not None:
			added = main.merge_result(result)
		else:
			added = result.get("new", 0)
		if active:
			self.tray_refresh()
		if not added:
			if always:
				ui.message(_("No new tweets"))
			return
		soundpack.play("ready" if always else "notification")
		if always or self._bg_announces("home", aid):
			if added == 1:
				text = _("1 new tweet")
			else:
				text = _("{count} new tweets").format(count=added)
			ui.message(text if always else self._tag(aid, text))

	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		"""The notification area buttons get an overlay that drops the focus event Explorer sends
		when our icon goes away. NVDA calls this for every object it creates."""
		try:
			choose_overlay(obj, clsList)
		except Exception:
			log.debugWarning(TAG + "could not choose the overlay of an object", exc_info=True)

	# ------------------------------------------------------------ commands

	@script(
		# Translators: Input help description for the command that opens the main window. {name} is the add-on name.
		description=_("{name}: open the main window").format(name=APP_NAME),
		category=APP_NAME,
		gesture="kb:nvda+i",
	)
	def script_openTimeline(self, gesture):
		self.open_main_window()

	def open_compose(self, reply_to=None, quote_of=None):
		"""The compose window (new tweet, reply or quote). Non-modal, owned by NVDA's main frame."""
		try:
			self._ensure_backend()
		except Exception:
			log.error(TAG + "could not start", exc_info=True)
			ui.message(_("{name} could not start. See the NVDA log.").format(name=APP_NAME))
			return
		if self._active is None:
			ui.message(_("Add an account first."))
			return
		gui.mainFrame.prePopup()
		try:
			dlg = ComposeDialog(
				gui.mainFrame, self, onClosed=self._compose_closed, reply_to=reply_to, quote_of=quote_of,
			)
			dlg.Show()
			dlg.Raise()
		except Exception:
			gui.mainFrame.postPopup()
			raise

	def _compose_closed(self, posted):
		gui.mainFrame.postPopup()
		if self._main is not None and not self._main.closed:
			wx.CallAfter(self._focus_main)

	def after_post(self, post):
		"""A tweet was posted: put it in the Home cache (already read) and in an open Home tab."""
		if post is None or self._active is None:
			return
		aid = self._active["id"]
		try:
			self._store.add_posts(aid, [post])
			self._store.mark_read(aid, [post.id], True)
		except Exception:
			log.error(TAG + "could not keep the tweet that was just posted", exc_info=True)
			return
		main = self._live_main()
		if main is not None:
			main.merge_result({"posts": [post], "account_id": aid})
		self.tray_refresh()

	def _unread_summary(self):
		counts = []
		unread = self._unread_now()
		if unread:
			counts.append((_("Home"), unread))
		unread = self._cached_unread("notifications")
		if unread:
			counts.append((_("Notifications"), unread))
		if not counts:
			# Translators: Spoken by the read-unread command when nothing is unread. {name} is the add-on name.
			return _("{name}: no unread").format(name=APP_NAME)
		# Translators: Spoken by the read-unread command. {name} is the add-on name, {counts} a list such as "Home 12, Notifications 3".
		return _("{name} unread: {counts}").format(
			name=APP_NAME, counts=", ".join("%s %d" % (label, n) for label, n in counts)
		)

	@script(
		# Translators: Input help description for the command that reads the unread counts. {name} is the add-on name.
		description=_("{name}: read the unread count").format(name=APP_NAME),
		category=APP_NAME,
	)
	def script_readUnread(self, gesture):
		try:
			self._ensure_backend()
		except Exception:
			log.error(TAG + "could not start", exc_info=True)
			return
		ui.message(self._unread_summary())

	@script(
		# Translators: Input help description for the command that opens the compose window. {name} is the add-on name.
		description=_("{name}: write a new tweet").format(name=APP_NAME),
		category=APP_NAME,
	)
	def script_quickNewTweet(self, gesture):
		self.open_compose()

	@script(
		# Translators: Input help description for the command that starts and stops auto read. {name} is the add-on name.
		description=_("{name}: start or stop auto read of the unread tweets").format(name=APP_NAME),
		category=APP_NAME,
	)
	def script_toggleAutoRead(self, gesture):
		try:
			self._ensure_backend()
		except Exception:
			log.error(TAG + "could not start", exc_info=True)
			return
		if self._active is None:
			ui.message(_("Add an account first."))
			return
		if self._autoread is None:
			self._autoread = AutoReader(self)
		self._autoread.toggle()

	@script(
		# Translators: Input help description for the command that loads new tweets. {name} is the add-on name.
		description=_("{name}: update Home (the same as F5 in the main window)").format(name=APP_NAME),
		category=APP_NAME,
		gesture="kb:nvda+shift+f5",
	)
	def script_updateHome(self, gesture):
		try:
			self._ensure_backend()
		except Exception:
			log.error(TAG + "could not start", exc_info=True)
			return
		if self._active is None:
			ui.message(_("Add an account first."))
			return
		if not self.start_job("reload", self._on_sync_result):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading new tweets"))