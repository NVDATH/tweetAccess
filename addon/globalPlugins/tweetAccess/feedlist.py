# -*- coding: utf-8 -*-
# NVTweet - feedlist.py: the list of tweets shared by every tab (Home, Thread, Quotes).
#
# Keys in a tab:  F5 refresh, Shift+F5 older (Home), Space next unread (Home), Alt+A actions,
# Alt+1..9 read the Nth newest (Home), Ctrl+A select all, Ctrl+E links, Ctrl+J jump to a row,
# Ctrl+Enter open in the browser, Enter full tweet, Ctrl+C copy the row, Ctrl+Delete clear cache (Home).

import bisect
import contextlib
import os
import threading
import time  # TEMP
import webbrowser

import addonHandler
import ui
import wx
from logHandler import log

import gui.nvdaControls

from . import attachments, keymap, soundpack
from .dialogs import FullTextDialog
from .utils import (
	TAG, announce_now, author_handle, cell_texts, copy_text, format_ts, handle_of, hashtags_of,
	is_pinned, is_relative_mode, is_retweeted, links_of, load_embeds, message_text, post_involves,
	quoted_post, row_sound_events, row_speech, safe_ui, set_stat, target_id,
)

addonHandler.initTranslation()

_ALLOWED_LINK_PREFIXES = ("https://x.com/", "https://twitter.com/")
CLOCK_REFRESH_MS = 60000  # refresh relative times


class OrderedPosts:
	"""Posts sorted by id (newest first or oldest first), without duplicates. The tweet a user pinned
	(see utils.is_pinned) sits beside the newest ones: first when the newest come first, last when the
	oldest come first, so the list is the web page turned the way the reader chose."""

	def __init__(self, newest_first=True):
		self.newest_first = newest_first
		self._sign = -1 if newest_first else 1
		self.rows = []
		self._keys = []  # sort keys (pinned beside the newest, then the signed id), ascending, parallel to rows
		self._ids = set()
		self._pinned = set()

	def __len__(self):
		return len(self.rows)

	@property
	def pinned(self):
		"""Ids of the pinned tweets among the rows."""
		return self._pinned

	def _key(self, post_id):
		# a pinned tweet counts as newer than the newest: it goes to the end of the list where the newest are
		return (self._sign if post_id in self._pinned else 0, self._sign * post_id)

	def add(self, post, older=False):
		"""Insert a post. Returns its index, or None if it is already there."""
		if post.id in self._ids:
			return None
		if is_pinned(post):
			self._pinned.add(post.id)
		key = self._key(post.id)
		idx = bisect.bisect_left(self._keys, key)
		self._keys.insert(idx, key)
		self.rows.insert(idx, post)
		self._ids.add(post.id)
		return idx

	def index_of(self, post_id):
		if post_id not in self._ids:
			return -1
		return bisect.bisect_left(self._keys, self._key(post_id))


class SequencePosts:
	"""Posts in the order they were given (thread, quotes)."""

	def __init__(self, newest_first=True, flipped=False):
		self.newest_first = newest_first  # how the rows are shown now: Space and Alt+number read it
		self.flipped = flipped  # shown the other way round than the order the tab was given
		self.rows = []
		self._ids = set()

	def __len__(self):
		return len(self.rows)

	def add(self, post, older=False):
		"""older: this tweet is older than every row (a "load older" batch): it goes to the far end."""
		if post.id in self._ids:
			return None
		self._ids.add(post.id)
		if older and self.flipped:
			self.rows.insert(0, post)
			return 0
		self.rows.append(post)
		return len(self.rows) - 1

	def index_of(self, post_id):
		for i, p in enumerate(self.rows):
			if p.id == post_id:
				return i
		return -1


class HashtagDialog(wx.Dialog):
	"""Check the hashtags to search for. Nothing checked: the highlighted one is used."""

	def __init__(self, parent, tags):
		# Translators: Title of the window that lists a tweet's hashtags.
		super().__init__(parent, title=_("Hashtags"))
		self._tags = list(tags)
		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label of the hashtag checklist; the checked ones are searched together.
		label = _("&Hashtags to search for (check several to search them together):")
		sizer.Add(wx.StaticText(self, label=label), 0, wx.ALL, 10)
		self.list = gui.nvdaControls.CustomCheckListBox(self, choices=["#" + t for t in self._tags], size=(-1, 150))
		self.list.SetSelection(0)
		sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		sizer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_CENTER | wx.ALL, 10)
		self.SetSizerAndFit(sizer)
		self.CentreOnParent()
		self.list.SetFocus()

	def picked(self):
		checked = list(self.list.CheckedItems)
		if not checked:
			index = self.list.GetSelection()
			checked = [index] if index != wx.NOT_FOUND else []
		return [self._tags[i] for i in checked]


class TweetPanel(wx.Panel):
	TRACKS_UNREAD = True   # unread count, Space, mark read/unread
	SUPPORTS_NTH = True    # Alt+1..9
	SORTED = True          # rows follow the sort setting (otherwise: the order given)
	CAN_OLDER = False      # Shift+F5
	TAB_NAME = ""
	# Shortcut action (see keymap.py) -> (method, needs a tweet: the quote-tweet setting applies)
	_KEY_ACTIONS = {
		"thread": ("_do_thread", True), "user_menu": ("_do_user_menu", False),
		"user_timeline": ("_do_user", False), "user_info": ("_do_user_info", False),
		"user_pinned": ("_do_user_pinned", False), "user_copy_url": ("_do_user_copy_url", False),
		"user_open_web": ("_do_user_open_web", False), "user_followers": ("_do_user_followers", False),
		"user_following": ("_do_user_following", False), "user_known_followers": ("_do_user_known_followers", False),
		"user_follow": ("_do_user_follow", False), "user_mute": ("_do_user_mute", False),
		"user_block": ("_do_user_block", False), "quotes": ("_do_quotes", True),
		"reply": ("_do_reply", True), "retweet": ("_do_retweet", True), "like": ("_do_like", True),
		"bookmark": ("_do_bookmark", True), "hashtags": ("_do_hashtags", False), "quote_tweet": ("_do_quote", True),
		"open_browser": ("_do_browser", True), "links": ("_do_links", False), "full_tweet": ("_show_full", False),
		"copy_row": ("_do_copy_all", False), "copy_text": ("_do_copy_text", True), "copy_link": ("_do_copy_link", True),
		"mark_read": ("_do_mark_read", False), "mark_unread": ("_do_mark_unread", False),
		"mark_all_read": ("_do_mark_all", False), "open_image": ("_do_image", False),
		"bemyeyes": ("_do_bemyeyes", False), "copy_image": ("_do_copy_image", False),
		"open_video": ("_do_video", False), "download": ("_do_download", False),
	}
	_LIST_ONLY = ("copy_row", "full_tweet")  # these keys only count while the list has focus
	# Enter key action (Settings > General) -> (method, needs a tweet: the quote-tweet setting applies)
	ENTER_HANDLERS = {
		"view_thread": ("_do_thread", True), "quotes": ("_do_quotes", True), "user": ("_do_user", False),
		"user_info": ("_do_user_info", False), "user_pinned": ("_do_user_pinned", False),
		"user_followers": ("_do_user_followers", False), "user_following": ("_do_user_following", False),
		"user_known_followers": ("_do_user_known_followers", False),
		"hashtags": ("_do_hashtags", False), "full": ("_show_full", False), "reply": ("_do_reply", True),
		"quote": ("_do_quote", True), "retweet": ("_do_retweet", True), "like": ("_do_like", True),
		"bookmark": ("_do_bookmark", True), "browser": ("_do_browser", True), "links": ("_do_links", False),
		"image": ("_do_image", False), "bemyeyes": ("_do_bemyeyes", False), "copy_image": ("_do_copy_image", False),
		"video": ("_do_video", False), "download": ("_do_download", False), "copy_text": ("_do_copy_text", True),
		"copy_link": ("_do_copy_link", True), "copy_all": ("_do_copy_all", False), "menu": ("_open_menu", False),
	}

	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._plugin = plugin
		self._cfg = plugin.config
		self._dead = False
		self._jump_handle = None  # user the Left/Right jump follows (locked on the first press)
		self._parked = False  # focus is waiting on the toolbar button because the list is empty
		self._quiet_depth = 0  # >0: focus events come from our own code, not the reader
		self._expanded = set()  # ids of rows shown in full (auto read, main window)
		self._posts = self._new_posts()

		self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
		self._cols = None
		self._build_columns()

		# Translators: Button that opens the tweet action menu (Alt+A).
		self.action_btn = wx.Button(self, label=_("Tweet &action"))
		# Translators: Button that opens the user menu (Alt+U), the actions about a user of the tweet.
		self.user_btn = wx.Button(self, label=_("&User action"))
		self.status = wx.StatusBar(self)
		# Translators: Accessible name of the status bar of a tweet tab.
		self.status.SetName(_("Tweets status"))

		sizer = wx.BoxSizer(wx.VERTICAL)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		buttons.Add(self.action_btn, 0, wx.RIGHT, 8)
		buttons.Add(self.user_btn, 0)
		sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
		sizer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		sizer.Add(self.status, 0, wx.EXPAND)
		self.SetSizer(sizer)

		self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_activate)
		self.list.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.on_item_focused)
		self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_action)
		self.action_btn.Bind(wx.EVT_BUTTON, self.on_action)
		self.user_btn.Bind(wx.EVT_BUTTON, self.on_user_action)
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

		self._clock_timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self.on_clock_timer, self._clock_timer)
		self._update_clock()
		self._update_status(notify=False)  # the first real count comes with the tweets

	def _new_posts(self):
		if self.SORTED:
			return OrderedPosts(self._cfg.get("sort_newest_first"))
		# NEWEST_FIRST: the order the tab is given in (a thread: oldest first). The reader's Display
		# setting says which way to read; when they differ the rows are shown the other way round.
		wanted = self._cfg.get("sort_newest_first")
		return SequencePosts(wanted, flipped=wanted != getattr(self, "NEWEST_FIRST", True))

	def _aid(self):
		return self._plugin.account_id()

	# ------------------------------------------------------------ display

	def _opts(self):
		cfg = self._cfg
		return cfg.get("author_mode"), cfg.get("time_mode"), cfg.get("time_pattern")

	def _row_text(self, post):
		"""The row as NVDA reads it when arrowing: the columns in their order."""
		return row_speech(self._cell_texts(post, self._opts()), self._cols)

	def _build_columns(self):
		"""Embed first or last, as the setting says. Does nothing when the layout is unchanged."""
		cols = ["embed", "author", "message", "time"] if self._cfg.get("embed_first") \
			else ["author", "message", "time", "embed"]
		if cols == self._cols:
			return
		self._cols = cols
		self.list.DeleteAllColumns()
		# Translators: Column headers of the tweet list. "created at" is read
		# aloud before the time, e.g. "created at 5 minutes ago".
		titles = {"embed": _("Embed"), "author": _("Author"), "message": _("Message"), "time": _("created at")}
		widths = {"embed": 110, "author": 200, "message": 560, "time": 150}
		for i, name in enumerate(cols):
			self.list.InsertColumn(i, titles[name], width=widths[name])

	def _cell_texts(self, post, opts):
		return cell_texts(post, *opts, full=post.id in self._expanded)

	def _set_row(self, idx, post, opts, insert):
		t0 = time.perf_counter()  # TEMP
		texts = self._cell_texts(post, opts)
		t1 = time.perf_counter()  # TEMP
		for col, name in enumerate(self._cols):
			if col == 0 and insert:
				self.list.InsertItem(idx, texts[name])
			else:
				self.list.SetItem(idx, col, texts[name])
		self._t_texts = getattr(self, "_t_texts", 0.0) + t1 - t0  # TEMP
		self._t_wx = getattr(self, "_t_wx", 0.0) + time.perf_counter() - t1  # TEMP

	def _update_status(self, notify=True):
		"""notify False: the list is only being emptied to be filled again, so the tray must not see a 0."""
		total = len(self._posts)
		if self.TRACKS_UNREAD:
			unread = sum(1 for p in self._posts.rows if not p.is_read)
			# Translators: Status bar of the Home tab.
			text = _("{unread} unread {total} total").format(unread=unread, total=total)
			if notify:
				self._unread_changed(unread)
		else:
			# Translators: Status bar of a thread or quotes tab.
			text = _("{total} total").format(total=total)
		self.status.SetStatusText(text)
		for button in (self.action_btn, self.user_btn):
			button.Show(total > 0)
			button.Enable(total > 0)
		self.Layout()

	def _update_clock(self):
		if is_relative_mode(self._cfg.get("time_mode")):
			self._clock_timer.Start(CLOCK_REFRESH_MS)
		else:
			self._clock_timer.Stop()

	def on_clock_timer(self, event):
		if self._dead or not self.IsShownOnScreen():
			return
		started = time.perf_counter()  # TEMP
		_a, mode, pattern = self._opts()
		time_col = self._cols.index("time")
		for i, p in enumerate(self._posts.rows):
			self.list.SetItem(i, time_col, format_ts(p.ts, mode, pattern))
		if time.perf_counter() - started > 0.2:  # TEMP
			log.info(TAG + "TEMP clock refresh of %s: %d rows took %.2fs" % (self.TAB_NAME, len(self._posts), time.perf_counter() - started))  # TEMP

	def reload_display(self):
		"""Settings changed: re-sort and re-render every row."""
		keep_id = self._current_post_id()
		rows = list(self._posts.rows)
		if getattr(self._posts, "flipped", False):
			rows.reverse()  # back to the order the tab was given
		self._reset_list()
		self._build_columns()
		self._add_posts(rows, fresh=False)
		self._restore_place(keep_id)
		self._update_clock()

	# ------------------------------------------------------------ list content

	@contextlib.contextmanager
	def _quiet(self):
		self._quiet_depth += 1
		try:
			yield
		finally:
			self._quiet_depth -= 1

	def _reset_list(self):
		with self._quiet():
			self.list.DeleteAllItems()
		self._posts = self._new_posts()
		self._expanded = set()
		self._update_status(notify=False)  # callers add rows next, or call _update_status() themselves

	def _add_posts(self, posts, fresh, older=False):
		"""Merge tweets into the list. Returns how many were new.
		fresh: tweets just fetched; their read state is looked up in the cache.
		older: a batch older than the rows (only matters for a list shown the other way round)."""
		started = time.perf_counter()  # TEMP
		self._t_texts = self._t_wx = 0.0  # TEMP
		added = []
		opts = self._opts()
		ordered = sorted(posts, key=lambda p: p.id, reverse=True) if self.SORTED else list(posts)
		if not self.SORTED and not older and getattr(self._posts, "flipped", False):
			ordered.reverse()
		self.list.Freeze()
		try:
			with self._quiet():
				for p in ordered:
					idx = self._posts.add(p, older)
					if idx is None:
						continue
					self._set_row(idx, p, opts, insert=True)
					added.append(p)
		finally:
			self.list.Thaw()
		if fresh and added and self.TRACKS_UNREAD:
			unread = self._plugin.store.unread_among(self._aid(), [p.id for p in added])
			for p in added:
				p.is_read = p.id not in unread
		t_status = time.perf_counter()  # TEMP
		self._update_status()
		if len(added) >= 100:  # TEMP
			log.info(
				TAG + "TEMP add_posts %s: %d rows, %.2fs in all, texts %.2fs, list %.2fs, status %.2fs" % (
					self.TAB_NAME, len(added), time.perf_counter() - started, self._t_texts, self._t_wx,
					time.perf_counter() - t_status,
				)
			)  # TEMP
		return len(added)

	def _top_index(self):
		"""Index of the newest tweet."""
		return 0 if self._posts.newest_first else max(len(self._posts) - 1, 0)

	def _clear_selection(self, keep):
		i = self.list.GetFirstSelected()
		while i != -1:
			following = self.list.GetNextSelected(i)
			if i != keep:
				self.list.SetItemState(i, 0, wx.LIST_STATE_SELECTED)
			i = following

	def _move_focus(self, idx):
		"""Move focus without marking read. True if the caller must announce the row."""
		had_focus = self.list.HasFocus()
		previous = self.list.GetFocusedItem()
		with self._quiet():
			self._clear_selection(idx)
			self.list.Focus(idx)
			self.list.Select(idx)
			self.list.EnsureVisible(idx)
		return (not had_focus) or previous == idx

	def _select(self, idx):
		self._move_focus(idx)
		self._note_focus(idx)

	def _current_index(self):
		idx = self.list.GetFocusedItem()
		if idx < 0:
			idx = self.list.GetFirstSelected()
		return idx if 0 <= idx < len(self._posts) else -1

	def _current_post(self):
		idx = self._current_index()
		return self._posts.rows[idx] if idx >= 0 else None

	def _current_post_id(self):
		post = self._current_post()
		return post.id if post is not None else None

	def _selected_posts(self):
		rows = self._posts.rows
		out = []
		i = self.list.GetFirstSelected()
		while i != -1:
			if 0 <= i < len(rows):
				out.append(rows[i])
			i = self.list.GetNextSelected(i)
		return out

	def _restore_place(self, keep_id):
		"""New tweets are inserted above the one being read; keep the reader's place."""
		if not len(self._posts):
			return
		if keep_id is None:
			want = self._top_index()
		else:
			want = self._posts.index_of(keep_id)
			if want < 0:
				want = self._top_index()
		if keep_id is None or self.list.GetFocusedItem() != want:
			self._select(want)
		self._leave_toolbar_button()

	def _unread_changed(self, unread):
		"""Hook: the Home tab tells the tray icon."""

	def _note_focus(self, idx):
		"""Hook: the Home tab remembers where the reader is."""

	def flush_state(self):
		"""Hook: write anything not saved yet."""

	def _restoreFocusPosition(self, moveFocus=True):
		if not moveFocus:
			return
		if len(self._posts):
			self._parked = False
			self.list.SetFocus()
		else:
			self._parked = True
			button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
			if button is not None:
				button.SetFocus()

	def _leave_toolbar_button(self):
		"""The list was empty, so focus waited on the toolbar button: now it has items, move it in.
		Only when focus is still on that button: a reader who is elsewhere is not disturbed."""
		if not self._parked or not len(self._posts):
			return
		button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
		try:
			if button is not None and self.IsShownOnScreen() and self.FindFocus() is button:
				self._parked = False
				self.list.SetFocus()
		except RuntimeError:
			pass

	def onTabActivated(self):
		ui.message(_("{name} tab").format(name=self.TAB_NAME))
		self._restoreFocusPosition()

	# ------------------------------------------------------------ read state (Home)

	def _set_read(self, post, read):
		if not self.TRACKS_UNREAD or post.is_read == read:
			return
		post.is_read = read
		self._save_read([post.id], read)
		self._update_status()

	def _save_read(self, ids, read):
		"""Persist the read state of these rows. Home: the cache; the other tabs override this."""
		try:
			self._plugin.store.mark_read(self._aid(), ids, read)
		except Exception:
			log.error(TAG + "could not save the read state", exc_info=True)

	def _post_changed(self, post):
		"""A like / bookmark / retweet changed this row. Home: the cache; the other tabs override this."""
		try:
			self._plugin.store.update_details(self._aid(), post.id, post.stats, post.liked, post.bookmarked)
		except Exception:
			log.error(TAG + "could not save the tweet", exc_info=True)

	def _mark_many(self, posts, read):
		for p in posts:
			p.is_read = read
		self._save_read([p.id for p in posts], read)
		self._update_status()
		if read:
			# Translators: Announced after marking several tweets read. {count} is the number.
			announce_now(_("{count} tweets marked as read.").format(count=len(posts)))
		else:
			# Translators: Announced after marking several tweets unread. {count} is the number.
			announce_now(_("{count} tweets marked as unread.").format(count=len(posts)))

	def _mark(self, post, read):
		self._set_read(post, read)
		announce_now(_("Marked as read.") if read else _("Marked as unread."))

	def _mark_all(self):
		self._save_read([p.id for p in self._posts.rows], True)
		for p in self._posts.rows:
			p.is_read = True
		self._update_status()
		announce_now(_("All tweets marked as read."))

	def _goto(self, idx, now=False):
		"""Focus a row, play its sounds, say it, and count it as read."""
		post = self._posts.rows[idx]
		announce = self._move_focus(idx)
		self._play_row_sounds(post)
		if announce or now:
			(announce_now if now else ui.message)(self._row_text(post))
		self._set_read(post, True)
		self._note_focus(idx)

	def _next_unread_index(self):
		"""Index of the oldest unread tweet, whatever the sort order, or None."""
		rows = self._posts.rows
		order = range(len(rows) - 1, -1, -1) if self._posts.newest_first else range(len(rows))
		for i in order:
			if not rows[i].is_read:
				return i
		return None

	def expand_row(self, idx):
		"""Auto read: show this row's whole message in its cell, so NVDA reads it all."""
		if not 0 <= idx < len(self._posts):
			return
		post = self._posts.rows[idx]
		self._expanded.add(post.id)
		self._set_row(idx, post, self._opts(), insert=False)

	def collapse_rows(self):
		"""Put every expanded row back to the cut cell."""
		ids, self._expanded = self._expanded, set()
		opts = self._opts()
		for post_id in ids:
			idx = self._posts.index_of(post_id)
			if 0 <= idx < len(self._posts):
				self._set_row(idx, self._posts.rows[idx], opts, insert=False)

	def _focus_next_unread(self):
		"""Oldest unread tweet first, whatever the sort order."""
		idx = self._next_unread_index()
		if idx is not None:
			self._goto(idx)
			return
		soundpack.play("boundary")
		ui.message(_("No unread tweets."))

	def auto_next(self):
		"""Auto read: the oldest unread tweet, as Space picks it. The focus moves onto its row, so the
		reader finds the place again, and the place is remembered; the tweet is marked read.
		Returns (text to speak, sound events, whether the list had focus and NVDA announces the row by
		itself) or None."""
		rows = self._posts.rows
		order = range(len(rows) - 1, -1, -1) if self._posts.newest_first else range(len(rows))
		full = bool(self._cfg.get("auto_full"))
		for i in order:
			post = rows[i]
			if post.is_read:
				continue
			native = self.list.HasFocus()
			self._move_focus(i)  # quiet: the focus event does not mark anything
			self._note_focus(i)
			self._set_read(post, True)
			texts = cell_texts(post, *self._opts(), full=full)
			return row_speech(texts, self._cols), row_sound_events(post, full=full), native
		return None

	def _announce_nth(self, n):
		rows = self._posts.rows
		newest_first = self._posts.newest_first
		# a pinned tweet is beside the newest ones (first, or last when the oldest come first) but is not one of them
		skip = 1 if rows and is_pinned(rows[0] if newest_first else rows[-1]) else 0
		if not 1 <= n <= len(rows) - skip:
			# Translators: Announced when Alt+number is pressed and that tweet does not exist. {number} is the number.
			ui.message(_("No tweet {number}.").format(number=n))
			return
		self._goto(skip + n - 1 if newest_first else len(rows) - skip - n)

	def _jump_to_row(self):
		total = len(self._posts)
		if not total:
			ui.message(_("Nothing to jump to."))
			return
		current = self._current_index()
		dlg = wx.TextEntryDialog(
			# Translators: Prompt of the jump-to-row window. {total} is the highest row number.
			self, _("Row number (1 to {total}):").format(total=total),
			# Translators: Title of the jump-to-row window.
			_("Jump to row"), value=str(current + 1 if current >= 0 else 1),
		)
		confirmed = dlg.ShowModal() == wx.ID_OK
		value = dlg.GetValue().strip()
		dlg.Destroy()
		if not confirmed or not value.isdigit():
			return
		idx = max(0, min(int(value) - 1, total - 1))
		wx.CallAfter(self._goto, idx, True)

	def _play_row_sounds(self, post):
		"""A sound for every embed kind the tweet has, a quote sound, then read more; before NVDA speaks the row."""
		for event in row_sound_events(post, full=post.id in self._expanded):
			soundpack.play_now(event)

	def _check_boundary(self, key):
		count = len(self._posts)
		idx = self.list.GetFocusedItem()
		if not count or idx == -1:
			return
		if (key == wx.WXK_UP and idx == 0) or (key == wx.WXK_DOWN and idx == count - 1):
			soundpack.play("boundary")

	def _jump_to_user(self, direction):
		"""Left/Right: the previous/next tweet (up/down the list) by, replying to, quoting or
		mentioning the same user. The user is locked on the first press and stays locked
		until the reader moves some other way (see on_item_focused)."""
		current = self._current_post()
		if current is None:
			return
		if self._jump_handle is None:
			# Whose row it is in the feed (the Author column): the one who wrote, retweeted or quoted it,
			# not the author of the original: that is who the reader follows.
			self._jump_handle = handle_of(current.author)
		handle = self._jump_handle
		rows = self._posts.rows
		i = self._current_index()
		while True:
			i += direction
			if not 0 <= i < len(rows):
				break
			if post_involves(rows[i], handle):
				self._goto(i)  # quiet focus: does not reset the locked user
				return
		soundpack.play("boundary")
		# Translators: Announced when Left/Right finds no more tweets about the same user. {user} is the user name.
		ui.message(_("No more tweets involving @{user} in that direction.").format(user=handle))

	def on_item_focused(self, event):
		if self._quiet_depth == 0:
			self._jump_handle = None
			idx = event.GetIndex()
			self._note_focus(idx)
			if 0 <= idx < len(self._posts):
				post = self._posts.rows[idx]
				self._play_row_sounds(post)
				if self.TRACKS_UNREAD:
					self._set_read(post, True)
		event.Skip()

	# ------------------------------------------------------------ commands

	def on_activate(self, event):
		idx = event.GetIndex()
		if not 0 <= idx < len(self._posts):
			return
		post = self._posts.rows[idx]
		action = self._cfg.get("enter_action")  # Settings > General > Enter key action
		if action == "mark_read":
			self._mark(post, not post.is_read)
			return
		method, needs_tweet = self.ENTER_HANDLERS.get(action, ("_do_thread", True))
		if needs_tweet:
			if self._check_post(post):
				self._call(getattr(self, method), post)
		else:
			getattr(self, method)(post)

	def _check_post(self, post):
		"""Hook: False (after saying why) when this row has no tweet to act on."""
		return True

	def _act(self, method, needs_tweet=True):
		post = self._current_post()
		if post is None:
			ui.message(_("No tweet selected."))
			return
		if needs_tweet and not self._check_post(post):
			return
		if needs_tweet:
			self._call(getattr(self, method), post)
		else:
			getattr(self, method)(post)

	def _show_full(self, post):
		dlg = FullTextDialog(self, post, self._cfg.get("author_mode"), self._plugin)
		try:
			dlg.ShowModal()
		finally:
			dlg.Destroy()

	def _target(self, post):
		"""The tweet a shortcut applies to. A quote tweet holds two: the setting says which, or asks."""
		quoted = quoted_post(post)
		if quoted is None:
			return post
		mode = self._cfg.get("target_mode")
		if mode == "quoted":
			return quoted
		if mode != "ask":
			return post
		own = " ".join(post.message.split())[:50]
		quoted_text = " ".join(quoted.message.split())[:50]
		dlg = wx.SingleChoiceDialog(
			# Translators: Prompt and title of the window that asks which of two tweets an action is for.
			self, _("Which tweet?"), _("Quote tweet"),
			[
				# Translators: Entry of the "which tweet?" window. {user} is the user name, {text} the start of the tweet.
				_("@{user}: {text}").format(user=author_handle(post), text=own),
				_("@{user} (quoted): {text}").format(user=author_handle(quoted), text=quoted_text),
			],
		)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return None
			return quoted if dlg.GetSelection() == 1 else post
		finally:
			dlg.Destroy()

	def _call(self, handler, post):
		"""Run an action on the tweet the setting picks; row is the row it came from when that is the quoted one."""
		target = self._target(post)
		if target is None:
			return
		if target is post:
			handler(post)
		else:
			handler(target, row=post)

	def _do_thread(self, post, row=None):
		self.GetTopLevelParent().open_related("thread", post)

	def _do_quotes(self, post, row=None):
		self.GetTopLevelParent().open_related("quotes", post)

	def _do_user(self, post, row=None):
		self.GetTopLevelParent().open_user(post)

	def _do_user_menu(self, post, row=None):
		self.GetTopLevelParent().open_user_menu(post)

	def _do_user_info(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_info", post)

	def _do_user_pinned(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_pinned", post)

	def _do_user_copy_url(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_copy_url", post)

	def _do_user_open_web(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_open_web", post)

	def _do_user_followers(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_followers", post)

	def _do_user_following(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_following", post)

	def _do_user_known_followers(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_known_followers", post)

	def _do_user_follow(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_follow", post)

	def _do_user_mute(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_mute", post)

	def _do_user_block(self, post, row=None):
		self.GetTopLevelParent().run_user_action("user_block", post)

	def _do_reply(self, post, row=None):
		self._plugin.open_compose(reply_to=post)

	def _do_quote(self, post, row=None):
		self._plugin.open_compose(quote_of=post)

	def _do_retweet(self, post, row=None):
		self._run_action("unretweet" if is_retweeted(post) else "retweet", post, row)

	def _do_like(self, post, row=None):
		self._run_action("unlike" if post.liked else "like", post, row)

	def _do_bookmark(self, post, row=None):
		self._run_action("unbookmark" if post.bookmarked else "bookmark", post, row)

	def _do_hashtags(self, post, row=None):
		"""Search a hashtag of the tweet; several: check which ones, they are searched together."""
		tags = hashtags_of(post)
		if not tags:
			announce_now(_("This tweet has no hashtags."))
			return
		picked = tags
		if len(tags) > 1:
			dlg = HashtagDialog(self, tags)
			try:
				if dlg.ShowModal() != wx.ID_OK:
					return
				picked = dlg.picked()
			finally:
				dlg.Destroy()
			if not picked:
				return
		query = "#" + picked[0] if len(picked) == 1 else "(" + " OR ".join("#" + t for t in picked) + ")"
		self.GetTopLevelParent().open_search(query)

	def _pick_media(self, post, kinds, what):
		"""(url, kind, number, total) of the media to act on; asks when there are several; None after saying why."""
		media = [e for e in load_embeds(post.embeds) if e["t"] in kinds]
		if not media:
			# Translators: Announced when a quick action needs media the tweet does not have. {what} is "image", "video or GIF"...
			announce_now(_("This tweet has no {what}.").format(what=what))
			return None
		pick = 0
		if len(media) > 1:
			names = {"image": _("Image"), "video": _("Video"), "gif": _("GIF")}
			labels = []
			for i, e in enumerate(media, start=1):
				label = "%s %d" % (names.get(e["t"], e["t"]), i)
				if e.get("src") == "quote":
					label += " " + _("(quoted tweet)")
				labels.append(label)
			dlg = wx.SingleChoiceDialog(
				# Translators: Prompt and title of the window that asks which of several images or videos to use.
				self, _("Which one?"), _("Media"), labels,
			)
			try:
				if dlg.ShowModal() != wx.ID_OK:
					return None
				pick = dlg.GetSelection()
			finally:
				dlg.Destroy()
		e = media[pick]
		return e["url"], e["t"], pick + 1, len(media)

	def _do_image(self, post, row=None):
		item = self._pick_media(post, ("image",), _("image"))
		if item:
			self._image_open(item[0], post, item[2], item[3])

	def _do_bemyeyes(self, post, row=None):
		item = self._pick_media(post, ("image",), _("image"))
		if item:
			self._image_bemyeyes(item[0], post, item[2], item[3])

	def _do_copy_image(self, post, row=None):
		item = self._pick_media(post, ("image",), _("image"))
		if item:
			self._image_copy(item[0], post, item[2], item[3])

	def _do_video(self, post, row=None):
		item = self._pick_media(post, ("video", "gif"), _("video or GIF"))
		if item:
			self._video_open(item[0], post, item[2], item[3])

	def _do_download(self, post, row=None):
		item = self._pick_media(post, ("image", "video", "gif"), _("media"))
		if item:
			self._download(item[0], "image" if item[1] == "image" else "video", post, item[2], item[3])

	def _do_browser(self, post, row=None):
		self._open_post_url(post)

	def _do_links(self, post, row=None):
		self.on_links(None)  # the activated row is the focused one

	def _do_copy_text(self, post, row=None):
		self._copy_text(message_text(post, oneline=False), _("Tweet text copied."))

	def _do_copy_link(self, post, row=None):
		self._copy_text(post.url, _("URL copied."))

	def _do_copy_all(self, post, row=None):
		self._copy_text(copy_text(post, self._cfg.get("author_mode")), _("Copied."))

	def _do_mark_read(self, post, row=None):
		if self.TRACKS_UNREAD:
			self._mark(post, True)

	def _do_mark_unread(self, post, row=None):
		if self.TRACKS_UNREAD:
			self._mark(post, False)

	def _do_mark_all(self, post, row=None):
		if self.TRACKS_UNREAD:
			self._mark_all()

	def _open_menu(self, post, row=None):
		self.on_action()

	def on_open_link(self, event):
		post = self._current_post()
		if post is not None:
			post = self._target(post)
		if post is not None:
			self._open_post_url(post)

	def _open_post_url(self, post):
		if not post.url.lower().startswith(_ALLOWED_LINK_PREFIXES):
			log.warning(TAG + "refusing to open an unexpected link")
			return
		try:
			webbrowser.open(post.url)
			ui.message(_("Opening in the browser"))
		except Exception:
			log.error(TAG + "could not open the browser", exc_info=True)
			ui.message(_("Could not open the browser."))

	def on_links(self, event):
		post = self._current_post()
		if post is None:
			return
		links = links_of(post)
		if not links:
			ui.message(_("This tweet has no links."))
			return
		url = links[0]["url"]
		if len(links) > 1:
			dlg = wx.SingleChoiceDialog(
				# Translators: Prompt and title of the window that lists the links of a tweet.
				self, _("Choose a link:"), _("Open link"), [l.get("label") or l["url"] for l in links],
			)
			try:
				if dlg.ShowModal() != wx.ID_OK:
					return
				url = links[dlg.GetSelection()]["url"]
			finally:
				dlg.Destroy()
		if not url.lower().startswith(("http://", "https://")):
			announce_now(_("Only http and https links can be opened."))
			return
		webbrowser.open(url)

	def on_char_hook(self, event):
		key = event.GetKeyCode()
		ctrl, shift, alt = event.ControlDown(), event.ShiftDown(), event.AltDown()
		in_list = self.FindFocus() is self.list
		plain = not (ctrl or shift or alt)
		if in_list and key in (wx.WXK_UP, wx.WXK_DOWN) and plain:
			self._check_boundary(key)
		if in_list and key in (wx.WXK_LEFT, wx.WXK_RIGHT) and plain:
			self._jump_to_user(-1 if key == wx.WXK_LEFT else 1)
			return
		if keymap.matches(event, "check_updates"):
			self.onCheckForUpdates(None)
			return
		if self.CAN_OLDER and keymap.matches(event, "load_older"):
			self.onFetchOlder()
			return
		if keymap.matches(event, "action_menu"):
			self.on_action()  # the Action button has this key too; only one of them ever fires
			return
		for action_id, (method, needs_tweet) in self._KEY_ACTIONS.items():
			if keymap.matches(event, action_id) and (in_list or action_id not in self._LIST_ONLY):
				self._act(method, needs_tweet)
				return
		if keymap.matches(event, "clear_cache") and hasattr(self, "onClearCache"):
			self.onClearCache()
			return
		if in_list and keymap.matches(event, "select_all"):
			with self._quiet():
				for i in range(len(self._posts)):
					self.list.SetItemState(i, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED)
			# Translators: Announced after selecting all tweets. {count} is the number of selected tweets.
			ui.message(_("{count} tweets selected.").format(count=len(self._posts)))
			return
		if in_list and keymap.matches(event, "jump_row"):
			self._jump_to_row()
			return
		if self.SUPPORTS_NTH and alt and not ctrl and not shift and ord("1") <= key <= ord("9"):
			self._announce_nth(key - ord("0"))
			return
		if key == wx.WXK_SPACE and in_list and plain and self.TRACKS_UNREAD:
			self._focus_next_unread()
			return
		event.Skip()

	# ------------------------------------------------------------ tweet action menu

	def _item(self, menu, label, callback):
		item = menu.Append(wx.ID_ANY, label)
		self.Bind(wx.EVT_MENU, lambda evt: callback(), item)

	def on_user_action(self, event=None):
		post = self._current_post()
		if post is None:
			ui.message(_("No tweet selected."))
			return
		self._do_user_menu(post)

	def on_action(self, event=None):
		if self.TRACKS_UNREAD:
			selected = self._selected_posts()
			if len(selected) > 1:
				menu = wx.Menu()
				self._item(menu, _("Mark selected as &read"), lambda: self._mark_many(selected, True))
				self._item(menu, _("Mark selected as &unread"), lambda: self._mark_many(selected, False))
				self.PopupMenu(menu)
				menu.Destroy()
				return
		post = self._current_post()
		if post is None:
			ui.message(_("No tweet selected."))
			return
		if not self._check_post(post):
			return
		menu = wx.Menu()
		self._item(
			menu, _("Undo re&tweet") if is_retweeted(post) else _("Re&tweet"),
			lambda: self._do_retweet(post),
		)
		self._item(menu, _("Un&like") if post.liked else _("&Like"), lambda: self._do_like(post))
		self._item(menu, _("Repl&y..."), lambda: self._do_reply(post))
		self._item(menu, _("&Quote tweet..."), lambda: self._do_quote(post))
		self._item(
			menu, _("Remove boo&kmark") if post.bookmarked else _("&Bookmark"),
			lambda: self._do_bookmark(post),
		)
		quoted = quoted_post(post)
		if quoted is not None:
			sub = wx.Menu()
			self._item(
				sub, _("Undo re&tweet") if is_retweeted(quoted) else _("Re&tweet"),
				lambda: self._do_retweet(quoted, row=post),
			)
			self._item(sub, _("Un&like") if quoted.liked else _("&Like"), lambda: self._do_like(quoted, row=post))
			self._item(sub, _("Repl&y..."), lambda: self._do_reply(quoted))
			self._item(sub, _("&Quote tweet..."), lambda: self._do_quote(quoted))
			self._item(
				sub, _("Remove boo&kmark") if quoted.bookmarked else _("&Bookmark"),
				lambda: self._do_bookmark(quoted, row=post),
			)
			self._item(sub, _("T&hread..."), lambda: self._do_thread(quoted))
			self._item(sub, _("Quote&s..."), lambda: self._do_quotes(quoted))
			self._item(sub, _("&Open in browser"), lambda: self._open_post_url(quoted))
			# Translators: Submenu with the actions for the tweet that a quote tweet quotes.
			menu.AppendSubMenu(sub, _("Quote&d tweet..."))
		menu.AppendSeparator()
		view = wx.Menu()
		self._item(view, _("&Thread..."), lambda: self._do_thread(post))
		self._item(view, _("&Quotes..."), lambda: self._do_quotes(post))
		self._item(view, _("&Hashtags..."), lambda: self._do_hashtags(post))
		menu.AppendSubMenu(view, _("&View..."))
		self._item(menu, keymap.with_key(_("&Full tweet..."), "full_tweet"), lambda: self._show_full(post))
		menu.AppendSubMenu(self._copy_menu(post), _("&Copy..."))
		embed_menu = self._embed_menu(post)
		if embed_menu is not None:
			menu.AppendSubMenu(embed_menu, _("&Embed..."))
		if links_of(post):
			self._item(menu, keymap.with_key(_("Open links"), "links"), lambda: self.on_links(None))
		self._item(menu, keymap.with_key(_("&Open in browser"), "open_browser"), lambda: self.on_open_link(None))
		if self.TRACKS_UNREAD:
			menu.AppendSeparator()
			mark = wx.Menu()
			# Translators: Entries of the Mark as submenu of the tweet action menu.
			self._item(mark, _("&Read"), lambda: self._mark(post, True))
			self._item(mark, _("&Unread"), lambda: self._mark(post, False))
			self._item(mark, _("&All as read"), self._mark_all)
			# Translators: Submenu of the tweet action menu to mark tweets as read or unread.
			menu.AppendSubMenu(mark, _("Mar&k as..."))
		self.PopupMenu(menu)
		menu.Destroy()

	def _copy_text(self, text, done_text):
		if wx.TheClipboard.Open():
			try:
				wx.TheClipboard.SetData(wx.TextDataObject(text))
			finally:
				wx.TheClipboard.Close()
			announce_now(done_text)
		else:
			announce_now(_("Could not copy to the clipboard."))

	def _copy_menu(self, post):
		"""Text and URLs only; copying an image is in the Embed menu."""
		menu = wx.Menu()
		self._item(menu, _("&Author, text and link"), lambda: self._copy_text(
			copy_text(post, self._cfg.get("author_mode")), _("Copied."),
		))
		self._item(menu, _("Tweet &text"), lambda: self._copy_text(
			message_text(post, oneline=False), _("Tweet text copied."),
		))
		self._item(menu, _("Tweet &URL"), lambda: self._copy_text(post.url, _("URL copied.")))

		def url_item(label, url):
			self._item(menu, label, lambda: self._copy_text(url, _("URL copied.")))

		embeds = load_embeds(post.embeds)
		for kind, single, several in (
			# Translators: Menu items that copy the URL of an image, video or animated GIF. {number} is the position when there are several.
			("image", _("Image URL"), _("Image {number} URL")),
			("video", _("Video URL"), _("Video {number} URL")),
			("gif", _("GIF URL"), _("GIF {number} URL")),
		):
			urls = [e["url"] for e in embeds if e["t"] == kind]
			for i, url in enumerate(urls, start=1):
				url_item(several.format(number=i) if len(urls) > 1 else single, url)
		for e in embeds:
			if e["t"] == "link":
				# Translators: Menu item that copies a link of the tweet. {label} is the link as shown in the tweet.
				url_item(_("Link {label}").format(label=e.get("label") or e["url"]), e["url"])
		return menu

	# ---- like / bookmark

	def _run_action(self, action, post, row=None):
		options = {"action": action, "id": target_id(post), "row_id": post.id}
		if not self._plugin.start_job(
			"action", lambda result: self.on_action_result(result, post, row), options, progress=False,
		):
			announce_now(_("Still loading, please wait."))

	@safe_ui
	def on_action_result(self, result, post, row=None):
		if not result["ok"]:
			soundpack.play("error")
			announce_now(result["message"])
			return
		action = result["action"]
		soundpack.play(action)  # like / unlike / bookmark / unbookmark / retweet / unretweet
		if row is not None:
			# the action was on the quoted tweet: its state lives in the stats of the row that shows it
			stat, value = {
				"like": ("quote_liked", True), "unlike": ("quote_liked", False),
				"bookmark": ("quote_bookmarked", True), "unbookmark": ("quote_bookmarked", False),
				"retweet": ("quote_retweeted", True), "unretweet": ("quote_retweeted", False),
			}[action]
			row.stats = set_stat(row.stats, stat, value)
			self._post_changed(row)
		else:
			if action in ("like", "unlike"):
				post.liked = action == "like"
			elif action in ("retweet", "unretweet"):
				post.stats = set_stat(post.stats, "retweeted", action == "retweet")
			else:
				post.bookmarked = action == "bookmark"
			self._post_changed(post)
		announce_now({
			# Translators: Announced after liking a tweet.
			"like": _("Liked."),
			# Translators: Announced after removing a like.
			"unlike": _("Unliked."),
			# Translators: Announced after bookmarking a tweet.
			"bookmark": _("Bookmarked."),
			# Translators: Announced after removing a bookmark.
			"unbookmark": _("Bookmark removed."),
			# Translators: Announced after retweeting.
			"retweet": _("Retweeted."),
			# Translators: Announced after undoing a retweet.
			"unretweet": _("Retweet undone."),
		}[action])

	# ---- embeds

	def _embed_menu(self, post):
		media = [e for e in load_embeds(post.embeds) if e["t"] != "link"]
		if not media:
			return None
		menu = wx.Menu()

		def tag(e):
			# Translators: Added to an Embed menu entry whose media belongs to the quoted tweet.
			return " " + _("(quoted tweet)") if e.get("src") == "quote" else ""

		images = [e for e in media if e["t"] == "image"]
		total = len(images)

		def image_items(target, e, i, single):
			url = e["url"]
			suffix = tag(e) if single else ""
			labels = (
				(_("&Open image"), _("&Send image to Be My Eyes"), _("&Download image..."), _("&Copy image to clipboard"))
				if single else
				(_("&Open"), _("&Send to Be My Eyes"), _("&Download..."), _("&Copy to clipboard"))
			)
			self._item(target, labels[0] + suffix, lambda: self._image_open(url, post, i, total))
			self._item(target, labels[1] + suffix, lambda: self._image_bemyeyes(url, post, i, total))
			self._item(target, labels[2] + suffix, lambda: self._download(url, "image", post, i, total))
			self._item(target, labels[3] + suffix, lambda: self._image_copy(url, post, i, total))

		if total == 1:
			image_items(menu, images[0], 1, True)
		else:
			for i, e in enumerate(images, start=1):
				sub = wx.Menu()
				image_items(sub, e, i, False)
				# Translators: Submenu for one of several images. {number} is its position.
				menu.AppendSubMenu(sub, _("Image &{number}").format(number=i) + tag(e))

		def clip_items(kind):
			clips = [e for e in media if e["t"] == kind]
			count = len(clips)
			is_gif = kind == "gif"
			for i, e in enumerate(clips, start=1):
				url = e["url"]
				if count == 1:
					target, suffix = menu, tag(e)
					open_label = _("&Open animated GIF") if is_gif else _("&Open video")
					save_label = _("&Download animated GIF...") if is_gif else _("&Download video...")
				else:
					target, suffix = wx.Menu(), ""
					open_label, save_label = _("&Open"), _("&Download...")
					# Translators: Submenu for one of several videos or animated GIFs. {number} is its position.
					label = (_("GIF &{number}") if is_gif else _("Video &{number}")).format(number=i)
					menu.AppendSubMenu(target, label + tag(e))
				self._item(target, open_label + suffix, lambda url=url, i=i: self._video_open(url, post, i, count))
				self._item(target, save_label + suffix, lambda url=url, i=i: self._download(url, "video", post, i, count))

		clip_items("video")
		clip_items("gif")
		return menu

	def _media_stem(self, post, index, total, url=None):
		"""File name stem: <user name> <first 50 characters of the text> (n). Both are those of the
		tweet that owns the media: the quoted tweet for its media, otherwise the author of the tweet itself."""
		quoted = any(e["url"] == url and e.get("src") == "quote" for e in load_embeds(post.embeds))
		author = (handle_of(post.quote_author) if quoted else author_handle(post)) or "tweet"
		text = " ".join((post.quote_text if quoted else post.message).split())[:50]
		stem = ("%s %s" % (author, text)).strip()
		return stem + (" (%d)" % index if total > 1 else "")

	def _background(self, work, done_text, finish=None, progress=False):
		"""Run work() off the main thread; finish(result) then runs on the main thread
		and returns an error text or None. done_text may be a function.
		progress: loop the progress sound while work() runs (long downloads)."""
		if progress:
			soundpack.start_progress()

		def runner():
			try:
				result, error = work(), None
			except Exception as e:
				log.error(TAG + "embed action failed", exc_info=True)
				result, error = None, str(e)
			finally:
				if progress:
					soundpack.stop_progress()
			wx.CallAfter(self._background_done, result, error, done_text, finish)

		threading.Thread(target=runner, daemon=True).start()

	@safe_ui
	def _background_done(self, result, error, done_text, finish):
		if not error and finish is not None:
			try:
				error = finish(result)
			except Exception as e:
				log.error(TAG + "embed action failed", exc_info=True)
				error = str(e)
		if error:
			soundpack.play("error")
			announce_now(_("Action failed: {error}").format(error=error))
		else:
			announce_now(done_text() if callable(done_text) else done_text)

	def _fetch(self, url, kind, post, index, total):
		stem = self._media_stem(post, index, total, url)
		return lambda: attachments.fetch_media(url, kind, stem)

	def _image_open(self, url, post, index, total):
		self._background(
			self._fetch(url, "image", post, index, total), _("Image opened."),
			lambda path: attachments.open_with_default_app(path),
		)

	def _image_bemyeyes(self, url, post, index, total):
		self._background(
			self._fetch(url, "image", post, index, total), _("Sent to Be My Eyes."),
			lambda path: None if attachments.send_to_bemyeyes(path)
			else _("Could not launch Be My Eyes. Is it installed?"),
		)

	def _image_copy(self, url, post, index, total):
		self._background(
			self._fetch(url, "image", post, index, total), _("Image copied to clipboard."),
			lambda path: None if attachments.copy_image_to_clipboard(path)
			else _("Could not read the downloaded image."),
		)

	def _video_open(self, url, post, index, total):
		announce_now(_("Downloading video, please wait..."))
		self._background(
			self._fetch(url, "video", post, index, total), _("Video opened."),
			lambda path: attachments.open_with_default_app(path),
			progress=True,
		)

	def _download(self, url, kind, post, index, total):
		folder = self._cfg.get("download_dir")
		try:
			os.makedirs(folder, exist_ok=True)
		except OSError:
			announce_now(_("Could not create the download folder. Choose another one in the settings."))
			return
		if kind != "image":  # images are quick: nothing is said or played
			announce_now(_("Downloading, please wait..."))
		saved = {}

		def finish(path):
			saved["path"] = attachments.move_to_folder(path, folder)
			return None

		self._background(
			self._fetch(url, kind, post, index, total),
			# Translators: Announced after a download. {name} is the saved file name.
			lambda: _("Saved {name}").format(name=os.path.basename(saved.get("path", ""))),
			finish,
			progress=kind != "image",
		)