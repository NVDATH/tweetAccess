# -*- coding: utf-8 -*-
# NVTweet - tabs.py: the Home tab and the removable Thread and Quotes tabs.

import json
import time  # TEMP

import addonHandler
import ui
import wx
from logHandler import log

from . import keymap, soundpack, tabstate
from .chat import ChatTab
from .explore import ExploreTab
from .feedlist import TweetPanel
from .userstab import PeopleTab, UsersTab
from .utils import (
	FOCUS_KEY, TAG, announce_now, is_pinned, login_needed_text, quotes_tab_name, safe_ui, thread_tab_name,
)

addonHandler.initTranslation()

FOCUS_SAVE_DELAY_MS = 800


def _gap_text():
	"""Said after a refresh that may have left a hole; names the key that loads older tweets."""
	key = keymap.display("load_older")
	if key:
		# Translators: Announced after a refresh that may have left a gap. {key} is the shortcut that loads older tweets.
		return _("There may be a gap. Press {key} to load older tweets.").format(key=key)
	# Translators: Announced after a refresh that may have left a gap.
	return _("There may be a gap in the list.")


class HomePanel(TweetPanel):
	"""The timeline of the active account: cached, unread tracking, remembers the place."""

	TAB_KEY = "home"
	CAN_OLDER = True
	VIRTUAL = True  # the list holds no rows (feedlist.VirtualList); False goes back to the normal list

	def __init__(self, parent, plugin):
		super().__init__(parent, plugin)
		# Translators: Name of the Home tab.
		self.TAB_NAME = _("Home")
		self._pending_focus = None  # (account id, tweet id)
		self._focus_timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self.on_focus_timer, self._focus_timer)
		self.load_cached()

	# ---- cache and remembered place

	def load_cached(self):
		t0 = time.perf_counter()  # TEMP
		posts = self._plugin.store.load_all(self._aid())
		t1 = time.perf_counter()  # TEMP
		self._add_posts(posts, fresh=False)
		t2 = time.perf_counter()  # TEMP
		self._select_remembered()
		log.info(
			TAG + "TEMP home: %d rows, database %.2fs, list %.2fs, remembered place %.2fs"
			% (len(posts), t1 - t0, t2 - t1, time.perf_counter() - t2)
		)  # TEMP

	def _select_remembered(self):
		if not len(self._posts):
			return
		try:
			anchor = int(self._plugin.store.get_account_meta(self._aid(), FOCUS_KEY) or 0) or None
		except ValueError:
			anchor = None
		idx = self._posts.index_of(anchor) if anchor else -1
		self._select(idx if idx >= 0 else self._top_index())

	def initial_announce(self):
		if not len(self._posts) and not self._plugin.busy:
			key = keymap.display("check_updates")
			if key:
				# Translators: Announced when the Home tab has nothing saved. {key} is the shortcut that checks for updates.
				ui.message(_("No saved tweets yet. Press {key} to load your timeline.").format(key=key))
			else:
				# Translators: Announced when the Home tab has nothing saved.
				ui.message(_("No saved tweets yet. Use Check for updates to load your timeline."))

	def _unread_changed(self, unread):
		self._plugin.tray_refresh("home", unread)

	def _note_focus(self, idx):
		if 0 <= idx < len(self._posts) and self._aid() is not None:
			self._pending_focus = (self._aid(), self._posts.rows[idx].id)
			self._focus_timer.StartOnce(FOCUS_SAVE_DELAY_MS)

	def on_focus_timer(self, event):
		self.flush_state()

	def flush_state(self):
		self._focus_timer.Stop()
		if self._pending_focus is None:
			return
		(aid, post_id), self._pending_focus = self._pending_focus, None
		try:
			self._plugin.store.set_account_meta(aid, FOCUS_KEY, str(post_id))
		except Exception:
			log.error(TAG + "could not save the reading position", exc_info=True)

	# ---- loading

	def merge_result(self, result):
		"""Add the tweets of a finished reload/older job. Returns how many were new."""
		if result.get("account_id") not in (None, self._aid()):
			return 0  # the account was switched meanwhile
		keep_id = self._current_post_id()
		added = self._add_posts(result["posts"], fresh=True)
		self._restore_place(keep_id)  # also moves focus into a list that was empty
		return added

	def _login_needed(self):
		gui_parent = self.GetTopLevelParent()
		wx.MessageBox(
			login_needed_text(),
			# Translators: Title of the not-logged-in message.
			_("Not logged in"),
			wx.OK | wx.ICON_INFORMATION, gui_parent,
		)

	def refresh(self, done=None):
		"""Load new tweets. With done, stay silent and call done(changed) at the end."""
		if not self._plugin.start_job("reload", lambda result: self.on_reload_result(result, done)):
			if done is None:
				ui.message(_("Still loading, please wait."))
			return False
		if done is None:
			ui.message(_("Loading new tweets"))
		return True

	def onCheckForUpdates(self, evt=None):
		self.refresh()

	@safe_ui
	def on_reload_result(self, result, done):
		if not result["ok"]:
			soundpack.play("error")
			if result.get("login_required"):
				self._login_needed()
			else:
				ui.message(result["message"])
			if done:
				done(False)
			return
		added = self.merge_result(result)
		if done:
			done(bool(added))
			return
		text = _("{count} new tweets").format(count=added) if added else _("No new tweets")
		if result.get("gap"):
			text += ". " + _gap_text()
		if result.get("limited"):
			text += ". " + _("X limited the requests, so only part of the new tweets was loaded. Try again later.")
		ui.message(text)

	def onFetchOlder(self):
		if not len(self._posts):
			ui.message(_("Nothing loaded yet."))
			return
		if not self._plugin.start_job("older", self.on_older_result):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading older tweets"))

	@safe_ui
	def on_older_result(self, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		added = self.merge_result(result)
		text = _("{count} older tweets loaded").format(count=added) if added else _("No older tweets")
		if result.get("connected"):
			text += ". " + _("The list is connected again.")
		ui.message(text)

	def onClearCache(self):
		aid = self._aid()
		if aid is None:
			return
		confirm = wx.MessageDialog(
			self,
			# Translators: Confirmation before clearing the tweet cache.
			_("Clear all cached tweets of this account? This can't be undone."),
			# Translators: Title of the clear-cache confirmation.
			_("Clear cache"),
			wx.YES_NO | wx.NO_DEFAULT,
		)
		confirmed = confirm.ShowModal() == wx.ID_YES
		confirm.Destroy()
		if not confirmed:
			return
		self._focus_timer.Stop()
		self._pending_focus = None
		try:
			self._plugin.store.clear_posts(aid)
		except Exception:
			log.error(TAG + "could not clear the cache", exc_info=True)
			ui.message(_("Could not clear the cache. See the NVDA log."))
			return
		self._reset_list()
		self._update_status()  # the tray now sees 0 unread
		self._restoreFocusPosition()  # an emptied list that keeps focus confuses NVDA
		ui.message(_("Cache cleared."))


class TempTab:
	"""Shared by removable tabs: remembered across sessions, jump back to where it was opened from."""

	TAB_TEMP_TYPE = ""

	def _setup_temp(self, key, origin_key, name):
		self.TAB_TEMP_KEY = key
		self._originTabKey = origin_key
		self._tab_aid = self._plugin.account_id()
		self.TAB_NAME = name
		self._focus_pending = None
		self._read_dirty = False  # read state or flags changed since the tab's cache was written
		self._focus_timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._on_focus_timer, self._focus_timer)

	def _focus_key(self):
		return "tabfocus:%s:%s" % (self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY)

	def _remembered_id(self):
		try:
			return int(self._plugin.store.get_account_meta(self._tab_aid, self._focus_key()) or 0) or None
		except ValueError:
			return None

	def _note_focus(self, idx):
		if 0 <= idx < len(self._posts):
			self._focus_pending = self._posts.rows[idx].id
			self._focus_timer.StartOnce(FOCUS_SAVE_DELAY_MS)

	def _on_focus_timer(self, event):
		self.flush_state()

	def flush_state(self):
		self._focus_timer.Stop()
		if self._read_dirty:
			self._read_dirty = False
			try:
				self._persist_posts()
			except Exception:
				log.error(TAG + "could not save the tweets of a tab", exc_info=True)
		if self._focus_pending is None:
			return
		post_id, self._focus_pending = self._focus_pending, None
		try:
			self._plugin.store.set_account_meta(self._tab_aid, self._focus_key(), str(post_id))
		except Exception:
			log.error(TAG + "could not save the reading position of a tab", exc_info=True)

	def _save_read(self, ids, read):
		# these tweets are not in the Home cache: the state lives in the tab's own cache
		self._read_dirty = True
		self._focus_timer.StartOnce(FOCUS_SAVE_DELAY_MS)

	def _post_changed(self, post):
		self._read_dirty = True
		self._focus_timer.StartOnce(FOCUS_SAVE_DELAY_MS)

	def _tray_id(self):
		return getattr(self, "TAB_KEY", None) or "%s:%s" % (self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY)

	def _unread_changed(self, unread):
		if getattr(self, "TAB_TEMP_KEY", None) is None:
			return  # still being built
		self._plugin.tray_refresh(self._tray_id(), unread)

	def _carry_read(self, posts):
		"""A refreshed copy of a tweet the tab already shows keeps its read state."""
		old = {p.id: p.is_read for p in self._posts.rows}
		for p in posts:
			if p.id in old:
				p.is_read = old[p.id]

	def _persist_posts(self):
		rows = list(self._posts.rows)
		if getattr(self._posts, "flipped", False):
			rows.reverse()  # the cache keeps the order the tab was given, not the one the reader chose
		tabstate.set_cache(self._plugin.store, self._tab_aid, self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY, rows)

	def onTabRemoved(self):
		self._dead = True
		for value in vars(self).values():
			if isinstance(value, wx.Timer):
				value.Stop()
		store = self._plugin.store
		tabstate.remove_open_tab(store, self._tab_aid, self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY)
		tabstate.delete_cache(store, self._tab_aid, self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY)
		store.set_account_meta(self._tab_aid, self._focus_key(), "")
		self.GetTopLevelParent().jump_to_permanent(self._originTabKey)

	def onTabRenamed(self, new_name):
		self.TAB_NAME = new_name
		tabstate.set_tab_name(self._plugin.store, self._tab_aid, self.TAB_TEMP_TYPE, self.TAB_TEMP_KEY, new_name)

	def _refresh_job(self, kind, options, loading_text, done, quiet=False):
		"""quiet: a background check: no progress sound and no spoken errors."""
		callback = lambda result: self.on_refreshed(result, done, quiet)
		if not self._plugin.start_job(kind, callback, options, progress=not quiet, background=quiet):
			if done is None:
				ui.message(_("Still loading, please wait."))
			return False
		if done is None:
			ui.message(loading_text)
		return True

	@safe_ui
	def on_refreshed(self, result, done, quiet=False):
		if not result["ok"]:
			if not quiet:
				soundpack.play("error")
				ui.message(result["message"])
			if done:
				done(False)
			return
		posts = result["posts"]
		changed = [p.id for p in posts] != [p.id for p in self._posts.rows]
		keep_id = self._current_post_id()
		self._show(posts, keep_id)
		self._persist_posts()
		if done:
			done(changed)
			return
		text = self._count_text(len(posts))
		if result.get("limited"):
			text += ". " + _("X limited the requests, so only part of the new tweets was loaded. Try again later.")
		ui.message(text)

	def snapshot(self):
		"""The tweets in the order the tab was given (what its cache holds), as they are now."""
		rows = list(self._posts.rows)
		if getattr(self._posts, "flipped", False):
			rows.reverse()
		return rows

	def _load_more(self, kind, options, loading_text):
		"""Shift+F5: the next page after what the tab shows; the new tweets go to the far end."""
		if not len(self._posts):
			ui.message(_("Nothing loaded yet."))
			return
		options = dict(options, more=True, have=[p.id for p in self._posts.rows])
		if not self._plugin.start_job(kind, self.on_more_result, options):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(loading_text)

	@safe_ui
	def on_more_result(self, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		keep_id = self._current_post_id()
		added = self._add_posts(result["posts"], fresh=False, older=True)
		self._restore_place(keep_id)
		self._persist_posts()
		text = self._more_text(added)
		if result.get("limited"):
			text += " " + _("X limited the requests, so only part of the new tweets was loaded. Try again later.")
		ui.message(text)


class ThreadPanel(TempTab, TweetPanel):
	"""Ancestors, the tweet and its replies."""

	TRACKS_UNREAD = True
	SUPPORTS_NTH = True
	SORTED = False
	NEWEST_FIRST = False  # reading order: the tweet that started it, then the replies
	TAB_TEMP_TYPE = "thread"
	CAN_OLDER = True  # Shift+F5: the next page of replies

	def __init__(self, parent, plugin, posts, target, key, origin_key=None, name=None):
		super().__init__(parent, plugin)
		self._tweet_id = str(target)  # not _target: TweetPanel._target() is a method
		self._setup_temp(key, origin_key, name or (thread_tab_name(posts[0]) if posts else _("Thread")))
		self._show(posts, None)

	def _show(self, posts, keep_id):
		self._carry_read(posts)
		self._reset_list()
		self._add_posts(posts, fresh=False)
		try:
			target = int(self._tweet_id)
		except ValueError:
			target = None
		if keep_id is None:
			remembered = self._remembered_id()
			keep_id = remembered if remembered and self._posts.index_of(remembered) >= 0 else target
		self._restore_place(keep_id)

	def _count_text(self, count):
		return _("{count} tweets in thread.").format(count=count)

	def _job_options(self):
		return {"id": self._tweet_id, "pages": self._cfg.get("feed_pages")}

	def refresh(self, done=None, quiet=False):
		return self._refresh_job("thread", self._job_options(), _("Loading thread, please wait."), done, quiet)

	def onFetchOlder(self):
		self._load_more("thread", {"id": self._tweet_id}, _("Loading more replies, please wait."))

	def _more_text(self, count):
		if not count:
			return _("No more replies.")
		# Translators: Announced after Shift+F5 in a thread tab. {count} is the number of replies added.
		return _("{count} more replies loaded.").format(count=count)

	def onCheckForUpdates(self, evt=None):
		self.refresh()


class QuotesPanel(TempTab, TweetPanel):
	"""Tweets that quote one tweet."""

	TRACKS_UNREAD = True
	SUPPORTS_NTH = True
	SORTED = False
	TAB_TEMP_TYPE = "quotes"
	CAN_OLDER = True  # Shift+F5: the next page of quotes

	def __init__(self, parent, plugin, posts, target, name, origin_key=None, handle=""):
		super().__init__(parent, plugin)
		self._tweet_id = str(target)  # not _target: TweetPanel._target() is a method
		self._handle = handle
		self._setup_temp(self._tweet_id, origin_key, name)
		self._show(posts, None)

	def _show(self, posts, keep_id):
		self._carry_read(posts)
		self._reset_list()
		self._add_posts(posts, fresh=False)
		if keep_id is None:
			remembered = self._remembered_id()
			if remembered and self._posts.index_of(remembered) >= 0:
				keep_id = remembered
		self._restore_place(keep_id)

	def _count_text(self, count):
		return _("{count} quotes.").format(count=count)

	def refresh(self, done=None, quiet=False):
		options = {"id": self._tweet_id, "handle": self._handle, "pages": self._cfg.get("feed_pages")}
		return self._refresh_job("quotes", options, _("Loading quotes, please wait."), done, quiet)

	def onFetchOlder(self):
		self._load_more("quotes", {"id": self._tweet_id, "handle": self._handle}, _("Loading more quotes, please wait."))

	def _more_text(self, count):
		if not count:
			return _("No more quotes.")
		# Translators: Announced after Shift+F5 in a quotes tab. {count} is the number of quotes added.
		return _("{count} more quotes loaded.").format(count=count)

	def onCheckForUpdates(self, evt=None):
		self.refresh()


class FeedTab(TempTab, TweetPanel):
	"""A remote feed in a tab (bookmarks, notifications, a user's tweets, a search, a list).
	Cached like Thread/Quotes. F5 refreshes, Shift+F5 loads older."""

	SOURCE = ""
	TRACKS_UNREAD = True
	SUPPORTS_NTH = True
	CAN_OLDER = True

	def __init__(self, parent, plugin, params, posts, key, name, origin_key=None):
		super().__init__(parent, plugin)
		self.TAB_TEMP_TYPE = self.SOURCE
		self._params = dict(params or {})
		self._setup_temp(key, origin_key, name)
		self._tried = bool(posts)  # a tab with nothing cached loads itself the first time it is shown
		self._show(posts, None)

	def _show(self, posts, keep_id):
		self._carry_read(posts)
		if not self.SORTED:
			self._reset_list()  # server order: the new first page replaces the list
		self._add_posts(posts, fresh=False)
		if keep_id is None:
			remembered = self._remembered_id()
			if remembered and self._posts.index_of(remembered) >= 0:
				keep_id = remembered
		self._restore_place(keep_id)

	def _options(self, older=False):
		options = {
			"source": self.SOURCE, "params": self._params, "chain": self.TAB_TEMP_KEY,
			"pages": self._cfg.get("feed_pages"),
		}
		if older:
			options["have"] = [p.id for p in self._posts.rows]
		elif self.SORTED and len(self._posts):
			options["known"] = [p.id for p in self._posts.rows]  # the refresh reads pages until it meets these
			options["pinned"] = [p.id for p in self._posts.rows if is_pinned(p)]  # not an overlap, see service._feed
		return options

	def _summary(self, new, total):
		if self.SORTED:
			if not new:
				return _("No new tweets")
			return _("{count} new tweets").format(count=new)
		return _("{count} tweets.").format(count=total)

	def onTabActivated(self):
		super().onTabActivated()
		if not self._tried and not len(self._posts):
			self._tried = True
			self.refresh()

	def refresh(self, done=None, quiet=False):
		return self._refresh_job("feed", self._options(), _("Loading, please wait."), done, quiet)

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
		old = [p.id for p in self._posts.rows]
		posts = result["posts"]
		keep_id = self._current_post_id()
		self._show(posts, keep_id)
		self._persist_posts()
		if self.SORTED:
			known = set(old)
			new = sum(1 for p in posts if p.id not in known)
			changed = new > 0
		else:
			new = 0
			changed = [p.id for p in self._posts.rows] != old
		if done:
			done(changed)
			return
		text = self._summary(new, len(self._posts))
		if result.get("gap"):
			text += ". " + _gap_text()
		if result.get("limited"):
			text += ". " + _("X limited the requests, so only part of the new tweets was loaded. Try again later.")
		ui.message(text)

	def onClearCache(self):
		if not len(self._posts):
			ui.message(_("Nothing to clear."))
			return
		confirm = wx.MessageDialog(
			self,
			# Translators: Confirmation before clearing a tab's cached tweets. {name} is the tab name.
			_("Clear the cached tweets of {name}? This can't be undone.").format(name=self.TAB_NAME),
			# Translators: Title of the clear-cache confirmation.
			_("Clear cache"),
			wx.YES_NO | wx.NO_DEFAULT,
		)
		confirmed = confirm.ShowModal() == wx.ID_YES
		confirm.Destroy()
		if not confirmed:
			return
		self._reset_list()
		self._update_status()  # the tray now sees 0 unread
		self._persist_posts()
		self._restoreFocusPosition()  # an emptied list that keeps focus confuses NVDA
		ui.message(_("Cache cleared."))

	def merge_bg(self, posts):
		"""A background check found these tweets: add the ones this tab does not have. Returns how many."""
		known = {p.id for p in self._posts.rows}
		fresh = [p for p in posts if p.id not in known]
		if not fresh:
			return 0
		keep_id = self._current_post_id()
		if self.SORTED:
			self._add_posts(fresh, fresh=False)
		else:
			current = list(self._posts.rows)
			if getattr(self._posts, "flipped", False):
				current.reverse()
			rows = fresh + current  # the order the tab was given: the new ones on top
			self._reset_list()
			self._add_posts(rows, fresh=False)
		self._restore_place(keep_id)
		self._persist_posts()
		return len(fresh)

	def onFetchOlder(self):
		if not len(self._posts):
			ui.message(_("Nothing loaded yet."))
			return
		if not self._plugin.start_job("feed", self.on_older_result, self._options(older=True)):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading older tweets"))

	@safe_ui
	def on_older_result(self, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		keep_id = self._current_post_id()
		added = self._add_posts(result["posts"], fresh=False, older=True)
		self._restore_place(keep_id)
		self._persist_posts()
		ui.message(_("{count} older tweets loaded").format(count=added) if added else _("No older tweets"))


class BookmarksTab(FeedTab):
	SOURCE = "bookmarks"
	TAB_KEY = "bookmarks"
	SORTED = False

	def on_action_result(self, result, post, row=None):
		super().on_action_result(result, post, row)
		if row is None and result["ok"] and result["action"] == "unbookmark":
			self._drop(post)

	def _drop(self, post):
		"""An unbookmarked tweet leaves this list."""
		idx = self._posts.index_of(post.id)
		rows = [p for p in self._posts.rows if p.id != post.id]
		self._reset_list()
		self._add_posts(rows, fresh=False)
		self._persist_posts()
		if rows:
			self._select(min(max(idx, 0), len(rows) - 1))
		else:
			self._restoreFocusPosition()


class NotificationsTab(FeedTab):
	SOURCE = "notifications"
	TAB_KEY = "notifications"
	SORTED = True  # by the time of the notification: the server's own order interleaves groups

	def _summary(self, new, total):
		if not new:
			# Translators: Announced after refreshing the Notifications tab and finding nothing new.
			return _("No new notifications")
		# Translators: Announced after refreshing the Notifications tab. {count} is the number.
		return _("{count} new notifications").format(count=new)

	def _check_post(self, post):
		if "/status/" not in post.url:
			announce_now(_("This notification has no tweet."))
			return False
		return True


class UserTab(FeedTab):
	SOURCE = "user"


class SearchTab(FeedTab):
	SOURCE = "search"

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		# Translators: Button of a search tab that opens the search form with this search, to change it. The shortcut is added after the label.
		self.edit_btn = wx.Button(self, label=keymap.with_key(_("Edit &search..."), "edit_search"))
		self._buttons.Add(self.edit_btn, 0, wx.LEFT, 8)
		self.edit_btn.Bind(wx.EVT_BUTTON, lambda e: self.edit_search())
		self.Layout()

	def edit_search(self):
		self.GetTopLevelParent().edit_search(self._params)


class ListTab(FeedTab):
	SOURCE = "list"


FEED_TABS = {"user": UserTab, "search": SearchTab, "list": ListTab}


def create_feed_tab(parent, plugin, source, key, params, posts, name, origin_key=None):
	return FEED_TABS[source](parent, plugin, params, posts, key, name, origin_key=origin_key)


def make_temp_tab(parent, plugin, entry, posts):
	"""A removable tab from its saved entry and tweets (a closed tab brought back), or None."""
	kind, key = entry["type"], entry["key"]
	if kind == "thread":
		return ThreadPanel(
			parent, plugin, posts, entry.get("target", key), key,
			origin_key=entry.get("origin"), name=entry.get("name"),
		)
	if kind == "quotes":
		return QuotesPanel(
			parent, plugin, posts, entry.get("target", key), entry.get("name") or quotes_tab_name(posts[0]),
			origin_key=entry.get("origin"), handle=entry.get("handle", ""),
		)
	if kind in FEED_TABS:
		return create_feed_tab(
			parent, plugin, kind, key, entry.get("params"), posts, entry.get("name") or key,
			origin_key=entry.get("origin"),
		)
	if kind == "users" and (entry.get("params") or {}).get("kind") and (entry.get("params") or {}).get("handle"):
		return UsersTab(parent, plugin, entry["params"], posts, entry.get("name") or key, origin_key=entry.get("origin"))
	return None


class ListsPanel(wx.Panel):
	"""The lists of the account. Enter opens one as a tab."""

	TAB_KEY = "lists"

	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._plugin = plugin
		self._dead = False
		# Translators: Name of the Lists tab.
		self.TAB_NAME = _("Lists")
		self._lists = self._load_cache()
		self._tried = bool(self._lists)
		self._parked = False  # focus is waiting on the toolbar button because the list is empty

		self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
		# Translators: Column header of the Lists tab.
		self.list.InsertColumn(0, _("List"), width=500)
		# Translators: Button of the Lists tab that opens the list in a tab of its own.
		self.open_btn = wx.Button(self, label=_("&Open in new tab"))
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
		sizer.Add(self.open_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		self.SetSizer(sizer)
		self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._open_selected())
		self.open_btn.Bind(wx.EVT_BUTTON, lambda e: self._open_selected())
		self._render()

	def _aid(self):
		return self._plugin.account_id()

	def _load_cache(self):
		raw = self._plugin.store.get_account_meta(self._aid(), "lists_cache")
		try:
			data = json.loads(raw) if raw else []
		except ValueError:
			data = []
		return [d for d in data if isinstance(d, dict) and d.get("id")] if isinstance(data, list) else []

	def _render(self):
		self.list.DeleteAllItems()
		for i, item in enumerate(self._lists):
			self.list.InsertItem(i, item["name"])
		if self._lists:
			self.list.Focus(0)
			self.list.Select(0)
		self.open_btn.Show(bool(self._lists))
		self.open_btn.Enable(bool(self._lists))
		self.Layout()

	def _open_selected(self):
		idx = self.list.GetFocusedItem()
		if not 0 <= idx < len(self._lists):
			ui.message(_("No list selected."))
			return
		item = self._lists[idx]
		self.GetTopLevelParent().open_feed("list", item["id"], {"list_id": item["id"]}, item["name"])

	def refresh(self, done=None):
		if not self._plugin.start_job("lists", lambda result: self.on_lists(result, done)):
			if done is None:
				ui.message(_("Still loading, please wait."))
			return False
		if done is None:
			ui.message(_("Loading lists, please wait."))
		return True

	@safe_ui
	def on_lists(self, result, done):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			if done:
				done(False)
			return
		items = result["items"]
		changed = items != self._lists
		self._lists = items
		try:
			self._plugin.store.set_account_meta(self._aid(), "lists_cache", json.dumps(items))
		except Exception:
			log.error(TAG + "could not save the lists", exc_info=True)
		self._render()
		self._leave_toolbar_button()
		if done:
			done(changed)
			return
		# Translators: Announced after loading the lists. {count} is the number.
		ui.message(_("{count} lists.").format(count=len(items)))

	def onCheckForUpdates(self, evt=None):
		self.refresh()

	def onTabActivated(self):
		ui.message(_("{name} tab").format(name=self.TAB_NAME))
		self._restoreFocusPosition()
		if not self._tried and not self._lists:
			self._tried = True
			self.refresh()

	def _leave_toolbar_button(self):
		"""The list was empty, so focus waited on the toolbar button: now it has items, move it in."""
		button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
		try:
			if self._parked and self._lists and button is not None and self.IsShownOnScreen() \
					and self.FindFocus() is button:
				self._parked = False
				self.list.SetFocus()
		except RuntimeError:
			pass

	def _restoreFocusPosition(self, moveFocus=True):
		if not moveFocus:
			return
		if self._lists:
			self._parked = False
			self.list.SetFocus()
		else:
			self._parked = True
			button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
			if button is not None:
				button.SetFocus()

	def flush_state(self):
		pass

	def reload_display(self):
		pass


def _cached(plugin, key):
	return tabstate.get_cache(plugin.store, plugin.account_id(), key, key) or []


# key -> maker; shown in this order after Home, unless switched off in Settings > Display
OPTIONAL_TABS = (
	("notifications", lambda parent, plugin: NotificationsTab(
		parent, plugin, {}, _cached(plugin, "notifications"), "notifications", _("Notifications"))),
	("explore", lambda parent, plugin: ExploreTab(parent, plugin)),  # not in the Settings list: always shown
	("chat", lambda parent, plugin: ChatTab(parent, plugin)),
	("people", lambda parent, plugin: PeopleTab(parent, plugin)),
	("bookmarks", lambda parent, plugin: BookmarksTab(
		parent, plugin, {}, _cached(plugin, "bookmarks"), "bookmarks", _("Bookmarks"))),
	("lists", lambda parent, plugin: ListsPanel(parent, plugin)),
)


def build_tabs(main, plugin):
	"""Home plus the removable tabs left open last time, in the order they were left."""
	t_start = time.perf_counter()  # TEMP
	store, aid = plugin.store, plugin.account_id()
	home = HomePanel(main.notebook, plugin)
	main.home = home
	tabs = [("permanent", "home", home, home.TAB_NAME, False)]
	off = {k for k in plugin.config.get("tabs_off").split(",") if k}
	if aid is not None:
		for key, make in OPTIONAL_TABS:
			if key not in off:
				panel = make(main.notebook, plugin)
				tabs.append(("permanent", key, panel, panel.TAB_NAME, False))
	for entry in (tabstate.get_open_tabs(store, aid) if aid is not None else []):
		kind, key = entry["type"], entry["key"]
		posts = tabstate.get_cache(store, aid, kind, key)
		if posts is None or (kind in ("thread", "quotes") and not posts):
			tabstate.remove_open_tab(store, aid, kind, key)
			continue
		if kind == "thread":
			panel = ThreadPanel(
				main.notebook, plugin, posts, entry.get("target", key), key,
				origin_key=entry.get("origin"), name=entry.get("name"),
			)
		elif kind == "quotes":
			panel = QuotesPanel(
				main.notebook, plugin, posts, entry.get("target", key), entry.get("name") or quotes_tab_name(posts[0]),
				origin_key=entry.get("origin"), handle=entry.get("handle", ""),
			)
		elif kind in FEED_TABS:
			panel = create_feed_tab(
				main.notebook, plugin, kind, key, entry.get("params"), posts,
				entry.get("name") or key, origin_key=entry.get("origin"),
			)
		elif kind == "users" and (entry.get("params") or {}).get("kind") and (entry.get("params") or {}).get("handle"):
			panel = UsersTab(
				main.notebook, plugin, entry["params"], posts, entry.get("name") or key,
				origin_key=entry.get("origin"),
			)
		else:
			tabstate.remove_open_tab(store, aid, kind, key)
			continue
		tabs.append((kind, key, panel, panel.TAB_NAME, True))
	saved = tabstate.get_order(store, aid) if aid is not None else []
	rank = {(k, v): i for i, (k, v) in enumerate(saved)}
	tabs.sort(key=lambda t: rank.get((t[0], t[1]), len(saved)))
	t_add = time.perf_counter()  # TEMP
	for i, (kind, key, panel, label, removable) in enumerate(tabs):
		main.addTab(panel, label, select=(i == 0), removable=removable, play_sound=False)
	log.info(
		TAG + "TEMP build_tabs: %d tabs, making the panels %.2fs, adding them to the window %.2fs"
		% (len(tabs), t_add - t_start, time.perf_counter() - t_add)
	)  # TEMP