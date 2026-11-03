# -*- coding: utf-8 -*-
# tweetAccess - chat.py: the Chat tab (direct messages), after NVSky's Chat window. A tree of the conversations,
# the messages of the one selected and a box to answer. Everything shown comes from the account's cache
# (store.dm_*); X is asked only when the reader says so: Enter on a conversation, F5, Shift+F5. X has no way to
# mark a conversation read, so what is read is kept on this computer only.

import json
import webbrowser

import addonHandler
import ui
import wx
from logHandler import log

from . import keymap, soundpack
from .newchat import NewChatDialog
from .utils import (
	TAG, announce_now, format_author, format_ts, is_relative_mode, safe_ui, truncate_cell, user_id_from_twid,
)

addonHandler.initTranslation()

DM_MAX_LENGTH = 10000  # characters of a direct message (X's limit, not confirmed here)
FOCUS_SAVE_DELAY_MS = 800
CLOCK_REFRESH_MS = 60000
STATE_KEY = "chat_state"  # account meta: the conversation shown last
OWN_KEY = "dm_own"        # account meta: the id of the account, as the inbox told it
NOTICE_KEY = "chat_notice"  # account meta: the notice about the limits of the Chat tab was shown


def member_label(member):
	"""'Name (@user)' of a conversation member, the form format_author reads; '' when X told nothing."""
	name, handle = member.get("name") or "", member.get("handle") or ""
	if name and handle:
		return "%s (@%s)" % (name, handle)
	return name or ("@" + handle if handle else "")


def convo_name(convo, author_mode):
	"""What a conversation is called: the name of the group, or the other user, as the author setting says."""
	if convo.get("name"):
		return convo["name"]
	members = convo.get("members") or []
	if convo.get("is_group"):
		short = "name" if author_mode == "both" else author_mode
		names = [format_author(member_label(m), short) for m in members[:3] if member_label(m)]
		if not names:
			# Translators: Name of a group chat that has no name and whose members X did not describe.
			return _("Group")
		return ", ".join(names) + ("..." if len(members) > 3 else "")
	label = member_label(members[0]) if members else ""
	if label:
		return format_author(label, author_mode)
	# Translators: Name of a conversation whose other user X did not describe. {id} is the user id.
	return _("User {id}").format(id=convo.get("partner_id") or "?")


def has_new_messages(convo, newest_known):
	"""True when X says the conversation has an event newer than what was read (by X's pointer or here) and
	newer than the messages kept here: there is something to load that was not read."""
	last = convo.get("last_id") or 0
	return last > max(convo.get("read_id") or 0, convo.get("seen_id") or 0, newest_known or 0)


def filter_choices():
	"""(key, text) of the filter of the Chat tab, after the filters of X's web page."""
	return [
		# Translators: Choice of the filter of the Chat tab: every conversation that is not a request or spam.
		("all", _("All")),
		# Translators: Choice of the filter of the Chat tab: conversations with something unread.
		("unread", _("Unread")),
		# Translators: Choice of the filter of the Chat tab: conversations with one user.
		("direct", _("Direct")),
		# Translators: Choice of the filter of the Chat tab: group conversations.
		("groups", _("Groups")),
		# Translators: Choice of the filter of the Chat tab: message requests.
		("requests", _("Requests")),
		# Translators: Choice of the filter of the Chat tab: conversations X filtered out as spam.
		("spam", _("Spam")),
	]


def convo_visible(convo, mode, unread):
	"""Whether a conversation is in the list for this filter. unread: it has something unread."""
	spam = bool(convo.get("low_quality"))
	if mode == "spam":
		return spam
	if spam:
		return False
	trusted = bool(convo.get("trusted"))
	if mode == "requests":
		return not trusted
	if mode == "unread":
		return bool(unread)
	if mode == "direct":
		return trusted and not convo.get("is_group")
	if mode == "groups":
		return trusted and bool(convo.get("is_group"))
	return trusted


def split_cell(text):
	"""The message in two cells as NVSky does it: the start, and what does not fit after it."""
	head, cut = truncate_cell(text)
	if not cut:
		return head, ""
	return head, truncate_cell(text[len(head):].lstrip())[0]


def describe_reactions(message):
	"""The Reactions cell: each emoji once, with how many people used it."""
	counts = {}
	for reaction in message.get("reactions") or []:
		emoji = reaction.get("emoji") or ""
		if emoji:
			counts[emoji] = counts.get(emoji, 0) + 1
	return " ".join("%sx%d" % (e, n) if n > 1 else e for e, n in counts.items())


def convo_label(convo, unread, author_mode, state=""):
	"""The label of a conversation in the tree: what is unread and the tags first (a long name must not hide
	them), then the name. state: "new" (messages not loaded yet that were not read) or "hidden" (X has newer
	activity than the messages it gave: encrypted messages, reactions...)."""
	tags = []
	if unread:
		# Translators: Tag of a conversation with unread messages, first in its label in the list. {count} is the number.
		tags.append(_("{count} unread").format(count=unread))
	elif state == "hidden":
		# Translators: Tag of a conversation that has newer activity than X gave to this add-on (encrypted messages, reactions...).
		tags.append(_("newer activity not available here"))
	elif state == "new":
		# Translators: Tag of a conversation that has messages not loaded yet that were not read.
		tags.append(_("new messages"))
	if not convo.get("trusted"):
		# Translators: Tag of a conversation that is a message request, in the list of conversations.
		tags.append(_("request"))
	if convo.get("muted"):
		# Translators: Tag of a muted conversation, in the list of conversations.
		tags.append(_("muted"))
	if convo.get("is_group"):
		# Translators: Tag of a group conversation, in the list of conversations.
		tags.append(_("group"))
	# Translators: Tags before the name of a conversation. {tags} is a comma separated list such as "2 unread, group".
	prefix = _("({tags}) ").format(tags=", ".join(tags)) if tags else ""
	return prefix + convo_name(convo, author_mode)


def attachment_text(attachment):
	"""The Embed cell of a message: what is attached."""
	kind = (attachment or {}).get("kind")
	if not kind:
		return ""
	names = {
		# Translators: Embed cell of a direct message with a picture.
		"image": _("Image"),
		# Translators: Embed cell of a direct message with a video.
		"video": _("Video"),
		# Translators: Embed cell of a direct message with an animated GIF.
		"gif": _("Animated GIF"),
		# Translators: Embed cell of a direct message that shares a tweet.
		"tweet": _("Shared tweet"),
	}
	return names.get(kind) or str(kind)


class MessageDialog(wx.Dialog):
	"""One message in full, to read with the cursor."""

	def __init__(self, parent, title, text):
		super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		# Translators: Label of the text box of the full message window.
		label = wx.StaticText(self, label=_("&Message:"))
		box = wx.TextCtrl(self, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
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


class ChatTab(wx.Panel):
	TAB_KEY = "chat"

	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._plugin = plugin
		self._cfg = plugin.config
		self._aid = plugin.account_id()
		self._dead = False
		# Translators: Name of the Chat tab.
		self.TAB_NAME = _("Chat")
		# Translators: What the New button of the main window says in the Chat tab.
		self.NEW_LABEL = _("New chat")
		self._convos = []       # every conversation, newest first (dicts of the cache)
		self._visible = []      # those the filter lets through, in the same order
		self._unread = {}       # conversation id -> unread messages
		self._newest = {}       # conversation id -> id of the newest message kept
		self._filter = "all"
		self._current = None    # the conversation shown, or None
		self._messages = []     # its messages, oldest first
		self._rows = []         # the same, in the order the list shows them
		self._fetched = set()   # conversations whose history was asked for since NVDA started
		self._reply_to = None   # the message the next one answers
		self._sending = False
		self._tried = False
		self._parked = False    # focus is waiting on the toolbar button because there is nothing to show
		self._quiet = 0         # >0: events come from our own code, not from the reader
		self._cols = None
		self._focus_pending = False
		store = plugin.store
		self._own = (
			user_id_from_twid((store.get_cookies(self._aid) or {}).get("twid"))
			or store.get_account_meta(self._aid, OWN_KEY) or ""
		)

		sizer = wx.BoxSizer(wx.VERTICAL)
		top = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label of the choice that filters the conversations of the Chat tab.
		top.Add(wx.StaticText(self, label=_("&Filter:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
		self._filters = filter_choices()
		self.filterChoice = wx.Choice(self, choices=[text for _key, text in self._filters])
		top.Add(self.filterChoice, 0)
		sizer.Add(top, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
		split = wx.BoxSizer(wx.HORIZONTAL)
		self.tree = wx.TreeCtrl(self, style=wx.TR_HAS_BUTTONS | wx.TR_HIDE_ROOT | wx.TR_SINGLE | wx.TR_LINES_AT_ROOT)
		# Translators: Accessible name of the list of conversations of the Chat tab.
		self.tree.SetName(_("Conversations"))
		self._root = self.tree.AddRoot(_("Conversations"))
		split.Add(self.tree, 1, wx.EXPAND | wx.RIGHT, 5)
		self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
		# Translators: Accessible name of the list of messages of the Chat tab.
		self.list.SetName(_("Messages"))
		self._build_columns()
		split.Add(self.list, 2, wx.EXPAND)
		sizer.Add(split, 1, wx.EXPAND | wx.ALL, 8)

		# Translators: Shown instead of the box to answer, for a message request: X lets a request be answered only after it is accepted.
		self.requestNotice = wx.StaticText(self, label=_("This is a message request. Accept it on x.com to answer."))
		sizer.Add(self.requestNotice, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		self.requestNotice.Hide()

		row = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label of the box where a direct message is written.
		self.composeLabel = wx.StaticText(self, label=_("&Message:"))
		self.compose = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 60))
		# Translators: Button that sends the direct message. The shortcut is added after the label.
		self.sendButton = wx.Button(self, label=keymap.with_key(_("&Send"), "send_tweet"))
		row.Add(self.composeLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
		row.Add(self.compose, 1, wx.EXPAND | wx.RIGHT, 5)
		row.Add(self.sendButton, 0)
		sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

		reply = wx.BoxSizer(wx.HORIZONTAL)
		self.replyLabel = wx.StaticText(self, label="")
		# Translators: Button that cancels the reply that is being written in the Chat tab.
		self.cancelReply = wx.Button(self, label=_("&Cancel reply"))
		reply.Add(self.replyLabel, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
		reply.Add(self.cancelReply, 0)
		sizer.Add(reply, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		self.replyLabel.Hide()
		self.cancelReply.Hide()

		self.status = wx.StatusBar(self)
		# Translators: Accessible name of the status bar of the Chat tab.
		self.status.SetName(_("Chat status"))
		sizer.Add(self.status, 0, wx.EXPAND)
		self.SetSizer(sizer)

		self.filterChoice.Bind(wx.EVT_CHOICE, self.on_filter)
		self.tree.Bind(wx.EVT_TREE_ITEM_MENU, self.on_convo_menu)
		self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_convo_selected)
		self.tree.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self.on_convo_activated)
		self.list.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.on_item_focused)
		self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_action)
		self.sendButton.Bind(wx.EVT_BUTTON, self.on_send)
		self.cancelReply.Bind(wx.EVT_BUTTON, self.on_cancel_reply)
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
		self._focus_timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._on_focus_timer, self._focus_timer)
		self._clock_timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self.on_clock_timer, self._clock_timer)

		saved = self._saved_state()
		if saved.get("filter") in [key for key, _text in self._filters]:
			self._filter = saved["filter"]
		self.filterChoice.SetSelection([key for key, _text in self._filters].index(self._filter))
		self._update_area()
		self._load_convos(saved.get("convo"))
		self._update_clock()

	# ------------------------------------------------------------ small helpers

	def _say(self, text):
		ui.message(text)

	def _writable(self):
		return bool(self._current and self._current.get("trusted"))

	def _saved_state(self):
		try:
			state = json.loads(self._plugin.store.get_account_meta(self._aid, STATE_KEY) or "{}")
		except ValueError:
			return {}
		return state if isinstance(state, dict) else {}

	def _remember_soon(self):
		self._focus_pending = True
		self._focus_timer.StartOnce(FOCUS_SAVE_DELAY_MS)

	@safe_ui
	def _on_focus_timer(self, event):
		self.flush_state()

	def flush_state(self):
		self._focus_timer.Stop()
		if not self._focus_pending:
			return
		self._focus_pending = False
		try:
			state = {"convo": self._current["convo_id"] if self._current else None, "filter": self._filter}
			self._plugin.store.set_account_meta(self._aid, STATE_KEY, json.dumps(state))
		except Exception:
			log.error(TAG + "could not save the place in the Chat tab", exc_info=True)

	def _sender_label(self, message):
		sender = message.get("sender_id")
		if sender and sender == self._own:
			# Translators: Author of your own messages in the Chat tab.
			return _("You")
		members = (self._current or {}).get("members") or []
		member = next((m for m in members if m.get("id") == sender), None)
		label = member_label(member) if member else ""
		if label:
			return format_author(label, self._cfg.get("author_mode"))
		# Translators: Author of a message whose sender X did not describe, in the Chat tab.
		return _("Them")

	# ------------------------------------------------------------ the list of messages

	def _build_columns(self):
		"""The columns of NVSky's chat list: Reactions, From, Message, Message (more), Sent."""
		if self._cols:
			return
		self._cols = True
		self.list.DeleteAllColumns()
		# Translators: Column headers of the list of messages of the Chat tab. "Message (more)" holds what does not fit in the Message cell.
		for i, (title, width) in enumerate((
			(_("Reactions"), 100), (_("From"), 140), (_("Message"), 350), (_("Message (more)"), 250), (_("Sent"), 140),
		)):
			self.list.InsertColumn(i, title, width=width)

	def _display_text(self, message):
		"""The whole message on one line: what it answers, its text, what is attached."""
		text = message.get("text") or ""
		reply = message.get("reply")
		if reply and reply.get("text"):
			# Translators: Start of a message that answers another one. {preview} is the start of that message, {text} the answer.
			text = _("(Reply to: {preview}) {text}").format(preview=" ".join(reply["text"].split())[:30], text=text)
		attached = attachment_text(message.get("attachment"))
		if attached:
			text = "%s %s" % (text, attached) if text else attached
		return " ".join(text.split())

	def _time_text(self, message):
		return format_ts(message.get("ts"), self._cfg.get("time_mode"), self._cfg.get("time_pattern"))

	def _cells(self, message):
		main, more = split_cell(self._display_text(message))
		return [
			describe_reactions(message), self._sender_label(message), main, more, self._time_text(message),
		]

	def _row_text(self, message):
		parts = [
			describe_reactions(message), self._sender_label(message), self._display_text(message),
			self._time_text(message),
		]
		return ", ".join(p for p in parts if p)

	def _focused_index(self):
		idx = self.list.GetFocusedItem()
		if idx < 0:
			idx = self.list.GetFirstSelected()
		return idx if 0 <= idx < len(self._rows) else -1

	def _focused_message(self):
		idx = self._focused_index()
		return self._rows[idx] if idx >= 0 else None

	def _render_messages(self, keep_id=None):
		"""Fill the list from self._messages in the order the Display setting says; the focus goes to keep_id
		(default: the message that has it now), else to the newest message."""
		if keep_id is None:
			current = self._focused_message()
			keep_id = current["id"] if current else None
		newest_first = bool(self._cfg.get("sort_newest_first"))
		self._rows = list(reversed(self._messages)) if newest_first else list(self._messages)
		self._build_columns()
		self._quiet += 1
		self.list.Freeze()
		try:
			self.list.DeleteAllItems()
			for i, message in enumerate(self._rows):
				cells = self._cells(message)
				self.list.InsertItem(i, cells[0])
				for col in range(1, len(cells)):
					self.list.SetItem(i, col, cells[col])
			if self._rows:
				idx = next((i for i, m in enumerate(self._rows) if m["id"] == keep_id), 0 if newest_first else len(self._rows) - 1)
				self.list.Focus(idx)
				self.list.Select(idx)
				self.list.EnsureVisible(idx)
		finally:
			self.list.Thaw()
			self._quiet -= 1

	def _goto_index(self, idx):
		"""Focus a message, say it and count it as read."""
		message = self._rows[idx]
		self._quiet += 1
		try:
			self.list.Focus(idx)
			self.list.Select(idx)
			self.list.EnsureVisible(idx)
		finally:
			self._quiet -= 1
		ui.message(self._row_text(message))
		self._mark(message)

	def _announce_nth(self, n):
		total = len(self._rows)
		if not 1 <= n <= total:
			# Translators: Announced when Alt+number is pressed and that message does not exist. {number} is the number.
			ui.message(_("No message {number}.").format(number=n))
			return
		self._goto_index(n - 1 if self._cfg.get("sort_newest_first") else total - n)

	def _focus_next_unread(self):
		"""The oldest message not read yet, whatever the order of the list."""
		target = next((m for m in self._messages if not m.get("is_read")), None)
		if target is None:
			soundpack.play("boundary")
			# Translators: Announced when Space is pressed in the Chat tab and no message is unread.
			ui.message(_("No unread messages."))
			return
		self._goto_index(next(i for i, m in enumerate(self._rows) if m["id"] == target["id"]))

	def _check_boundary(self, key):
		count = len(self._rows)
		idx = self.list.GetFocusedItem()
		if not count or idx == -1:
			return
		if (key == wx.WXK_UP and idx == 0) or (key == wx.WXK_DOWN and idx == count - 1):
			soundpack.play("boundary")

	@safe_ui
	def on_item_focused(self, event):
		if not self._quiet:
			idx = event.GetIndex()
			if 0 <= idx < len(self._rows):
				self._mark(self._rows[idx])
		event.Skip()

	def _mark(self, message, read=True):
		if not self._current or bool(message.get("is_read")) == read:
			return
		message["is_read"] = read
		convo_id = self._current["convo_id"]
		try:
			self._plugin.store.dm_mark_read(self._aid, convo_id, [message["id"]], read)
		except Exception:
			log.error(TAG + "could not save the read state of a message", exc_info=True)
		self._refresh_counts()
		self._refresh_label(convo_id)
		self._update_status()

	# ------------------------------------------------------------ the tree of conversations

	def _child_items(self):
		items = []
		item, cookie = self.tree.GetFirstChild(self._root)
		while item.IsOk():
			items.append(item)
			item, cookie = self.tree.GetNextChild(self._root, cookie)
		return items

	def _by_id(self, convo_id):
		return next((c for c in self._convos if c["convo_id"] == convo_id), None)

	def _refresh_counts(self):
		store = self._plugin.store
		self._unread = store.dm_unread_counts(self._aid)
		self._newest = store.dm_newest_ids(self._aid)

	def _state(self, convo):
		""""" (nothing to say), "new" or "hidden": see convo_label."""
		convo_id = convo["convo_id"]
		if self._unread.get(convo_id) or not has_new_messages(convo, self._newest.get(convo_id, 0)):
			return ""
		return "hidden" if convo_id in self._fetched else "new"

	def _flag(self, convo):
		"""Whether the conversation has something unread, or something new to load."""
		return bool(self._unread.get(convo["convo_id"])) or bool(self._state(convo))

	def _label(self, convo):
		return convo_label(convo, self._unread.get(convo["convo_id"], 0), self._cfg.get("author_mode"), self._state(convo))

	def _render_convos(self):
		items = self._child_items()
		if [self.tree.GetItemData(i) for i in items] == [c["convo_id"] for c in self._visible]:
			for item, convo in zip(items, self._visible):  # the same conversations: only the labels change
				if self.tree.GetItemText(item) != self._label(convo):
					self.tree.SetItemText(item, self._label(convo))
			return
		self._quiet += 1
		self.tree.Freeze()
		try:
			self.tree.DeleteAllItems()
			# Translators: Hidden root of the list of conversations in the Chat tab (read as its name).
			self._root = self.tree.AddRoot(_("Conversations"))
			for convo in self._visible:
				item = self.tree.AppendItem(self._root, self._label(convo))
				self.tree.SetItemData(item, convo["convo_id"])
		finally:
			self.tree.Thaw()
			self._quiet -= 1

	def _select_item(self, convo_id):
		for item in self._child_items():
			if self.tree.GetItemData(item) == convo_id:
				self._quiet += 1
				try:
					self.tree.SelectItem(item)
				finally:
					self._quiet -= 1
				return

	def _refresh_label(self, convo_id):
		convo = self._by_id(convo_id)
		if convo is None:
			return
		for item in self._child_items():
			if self.tree.GetItemData(item) == convo_id:
				self.tree.SetItemText(item, self._label(convo))
				return

	def _load_convos(self, keep_id=None):
		self._convos = self._plugin.store.dm_load_convos(self._aid)
		self._refresh_counts()
		self._visible = [c for c in self._convos if convo_visible(c, self._filter, self._flag(c))]
		ids = [c["convo_id"] for c in self._visible]
		wanted = keep_id or (self._current or {}).get("convo_id")
		if wanted not in ids:
			wanted = ids[0] if ids else None
		self._render_convos()
		if wanted is None:
			self._current, self._messages, self._reply_to = None, [], None
			self._render_messages()
			self._update_area()
		else:
			self._select_item(wanted)
			self._show(wanted)
		self._update_status()
		self._leave_toolbar_button()

	@safe_ui
	def on_filter(self, event):
		self._filter = self._filters[max(0, self.filterChoice.GetSelection())][0]
		self._remember_soon()
		self._load_convos()
		ui.message(_("{count} conversations.").format(count=len(self._visible)))

	def _focus_next_unread_convo(self):
		"""Space in the list of conversations: the next one (wrapping round) with something unread or new."""
		flagged = [i for i, c in enumerate(self._visible) if self._flag(c)]
		if not flagged:
			soundpack.play("boundary")
			# Translators: Announced when Space is pressed in the list of conversations and none has anything unread.
			ui.message(_("No unread conversations."))
			return
		here = next(
			(i for i, c in enumerate(self._visible) if self._current and c["convo_id"] == self._current["convo_id"]), -1,
		)
		convo = self._visible[next((i for i in flagged if i > here), flagged[0])]
		self._select_item(convo["convo_id"])
		self._show(convo["convo_id"])
		ui.message(self._label(convo))

	# ---- the menu of a conversation

	@safe_ui
	def on_convo_menu(self, event):
		item = event.GetItem()
		if not item.IsOk() or item == self._root:
			return
		convo_id = self.tree.GetItemData(item)
		if self._current is None or self._current["convo_id"] != convo_id:
			self._select_item(convo_id)
			self._show(convo_id)
		self._popup_convo_menu(self._current)

	def _popup_convo_menu(self, convo):
		menu = wx.Menu()
		# Translators: Entries of the menu of a conversation in the Chat tab. Mark read only changes what this add-on shows.
		self._item(menu, _("&Mark read"), lambda: self._mark_convo_read(convo))
		self._item(menu, _("Mark &all read"), self._mark_all_read)
		self._item(menu, keymap.with_key(_("Re&fresh"), "check_updates"), lambda: self._fetch_history(convo, "refresh"))
		# Translators: Entry of the menu of a conversation in the Chat tab: deletes it for you only.
		self._item(menu, _("&Delete conversation..."), lambda: self._delete_convo(convo))
		# Translators: Entry of the menu of a conversation in the Chat tab: what this add-on cannot show or do is done on the web page.
		self._item(menu, _("Open on &x.com"), lambda: self._open_web(convo))
		self.PopupMenu(menu)
		menu.Destroy()

	def _mark_convo_read(self, convo, announce=True):
		"""Everything of the conversation counts as read here (X is not told: it has no way to be)."""
		store = self._plugin.store
		convo_id = convo["convo_id"]
		unread = [m["id"] for m in store.dm_load_messages(self._aid, convo_id) if not m.get("is_read")]
		if unread:
			store.dm_mark_read(self._aid, convo_id, unread)
		convo["seen_id"] = max(convo.get("last_id") or 0, convo.get("seen_id") or 0)
		store.dm_save_convos(self._aid, [convo])
		self._refresh_counts()
		if self._current and self._current["convo_id"] == convo_id:
			self._messages = store.dm_load_messages(self._aid, convo_id)
			self._render_messages()
		self._refresh_label(convo_id)
		self._update_status()
		if announce:
			announce_now(_("Marked as read."))

	def _mark_all_read(self):
		for convo in self._convos:
			self._mark_convo_read(convo, announce=False)
		self._load_convos()
		# Translators: Announced after every conversation of the Chat tab was marked read.
		announce_now(_("All conversations marked read."))

	def _delete_convo(self, convo):
		dlg = wx.MessageDialog(
			self,
			# Translators: Question before deleting a conversation. X only removes it for you: the other side keeps it.
			_("Delete this conversation for you? The other side keeps it. This can't be undone."),
			# Translators: Title of the question before deleting a conversation.
			_("Delete conversation"), wx.YES_NO | wx.NO_DEFAULT,
		)
		try:
			if dlg.ShowModal() != wx.ID_YES:
				return
		finally:
			dlg.Destroy()
		convo_id = convo["convo_id"]
		callback = lambda result: self.on_convo_deleted(result, convo_id)
		if not self._plugin.start_job("dm_delete_convo", callback, {"convo_id": convo_id}, progress=False):
			ui.message(_("Still loading, please wait."))

	@safe_ui
	def on_convo_deleted(self, result, convo_id):
		if not result["ok"]:
			soundpack.play("error")
			# Translators: Announced when a conversation could not be deleted. {error} is the reason.
			ui.message(_("Failed to delete: {error}").format(error=result["message"]))
			return
		if result.get("account_id") != self._aid:
			return
		self._plugin.store.dm_delete_convo(self._aid, convo_id)
		self._fetched.discard(convo_id)
		self._load_convos()
		# Translators: Announced after a conversation was deleted.
		announce_now(_("Conversation deleted."))

	def _open_web(self, convo):
		"""What this add-on cannot do (accept a request, see an encrypted message) is done on x.com."""
		convo_id = convo["convo_id"]
		webbrowser.open("https://x.com/messages/" + ("requests" if not convo.get("trusted") else convo_id))
		announce_now(_("Opening in the browser"))

	def _show(self, convo_id, announce=False):
		convo = next((c for c in self._convos if c["convo_id"] == convo_id), None)
		if convo is None:
			return
		changed = self._current is None or self._current["convo_id"] != convo_id
		self._current = convo
		self._messages = self._plugin.store.dm_load_messages(self._aid, convo_id)
		if changed:
			self._reply_to = None
		self._render_messages(0 if changed else None)  # 0: a conversation just opened starts at its newest message
		self._update_area()
		self._update_status()
		if changed:
			self._remember_soon()
		if announce and changed and not convo.get("trusted"):
			ui.message(self.requestNotice.GetLabel())

	@safe_ui
	def on_convo_selected(self, event):
		if not self._quiet:
			item = event.GetItem()
			if item.IsOk() and item != self._root:
				self._show(self.tree.GetItemData(item), announce=True)
		event.Skip()

	@safe_ui
	def on_convo_activated(self, event):
		"""Enter on a conversation: its messages are loaded from X the first time, then the list takes focus."""
		item = event.GetItem()
		if not item.IsOk() or item == self._root:
			return
		convo_id = self.tree.GetItemData(item)
		if self._current is None or self._current["convo_id"] != convo_id:
			self._show(convo_id)
		if convo_id not in self._fetched and not self._messages:
			self._fetch_history(self._current, "first")
		else:
			self._focus_messages()

	def _focus_messages(self):
		if self._rows:
			self.list.SetFocus()
		elif self._writable():
			self.compose.SetFocus()

	# ------------------------------------------------------------ the area under the lists

	def _update_area(self):
		writable = self._writable()
		for control in (self.composeLabel, self.compose, self.sendButton):
			control.Show(writable)
		self.requestNotice.Show(bool(self._current) and not writable)
		replying = writable and self._reply_to is not None
		self.replyLabel.Show(replying)
		self.cancelReply.Show(replying)
		self.Layout()

	def _update_status(self):
		# Translators: Status bar of the Chat tab. {unread} is the number of unread messages, {total} the number of conversations.
		unread = sum(n for convo_id, n in self._unread.items() if not (self._by_id(convo_id) or {}).get("low_quality"))
		self.status.SetStatusText(_("Chat {unread} unread {total} conversations").format(
			unread=unread, total=len(self._visible),
		))

	# ------------------------------------------------------------ loading from X

	def _history_options(self, convo, **extra):
		options = {
			"convo_id": convo["convo_id"], "is_group": convo["is_group"], "partner_id": convo["partner_id"],
			"read_id": convo.get("read_id") or 0, "pages": 1,
		}
		options.update(extra)
		return options

	def _fetch_history(self, convo, mode, **extra):
		"""mode: first (the conversation was never loaded), refresh (the newest page again) or older."""
		if convo is None:
			return
		convo_id = convo["convo_id"]
		callback = lambda result: self.on_history(result, convo_id, mode)
		if not self._plugin.start_job("dm_history", callback, self._history_options(convo, **extra)):
			ui.message(_("Still loading, please wait."))
			return
		if mode != "first":
			ui.message(_("Loading, please wait."))

	@safe_ui
	def on_history(self, result, convo_id, mode):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		if result.get("account_id") != self._aid:
			return  # the account was switched meanwhile
		store = self._plugin.store
		known = {m["id"] for m in store.dm_load_messages(self._aid, convo_id)}
		fresh = [m for m in result["messages"] if m["id"] not in known]
		store.dm_add_messages(self._aid, convo_id, result["messages"])
		self._fetched.add(convo_id)
		self._refresh_counts()
		showing = bool(self._current) and self._current["convo_id"] == convo_id
		if showing:
			self._messages = store.dm_load_messages(self._aid, convo_id)
			self._render_messages()
		self._refresh_label(convo_id)
		self._update_status()
		convo = self._by_id(convo_id)
		hidden = convo is not None and has_new_messages(convo, self._newest.get(convo_id, 0))
		# Translators: Announced after a conversation was loaded when X has newer activity than the messages it gave (the web page shows it after the PIN of encrypted chats is entered).
		hidden_text = _("Newer activity exists that X did not give to this add-on. It may be encrypted messages or reactions. Open the conversation on x.com to see it.")
		if mode == "first":
			if showing:
				self._focus_messages()
			if hidden:
				ui.message(hidden_text)
			return
		if mode == "older":
			# Translators: Announced after Shift+F5 in the Chat tab. {count} is the number of older messages loaded.
			ui.message(_("{count} older messages loaded").format(count=len(fresh)) if fresh else _("No older messages"))
		else:
			# Translators: Announced after F5 in a conversation. {count} is the number of new messages.
			ui.message(_("{count} new messages").format(count=len(fresh)) if fresh else _("No new messages"))
			if hidden:
				ui.message(hidden_text)

	def refresh(self, done=None, quiet=False, full=False):
		"""Read the list of conversations again: the first page, where the newest are, or with full every page (the
		first time too). With done, stay silent and call done(changed) at the end."""
		partial = not full and bool(self._convos)
		callback = lambda result: self.on_inbox(result, done, partial)
		options = {"pages": 1} if partial else {}
		if not self._plugin.start_job("dm_inbox", callback, options, progress=not quiet, background=quiet):
			if done is None:
				ui.message(_("Still loading, please wait."))
			return False
		if done is None:
			# Translators: Announced while the list of conversations is loaded.
			ui.message(_("Loading conversations, please wait."))
		return True

	@safe_ui
	def on_inbox(self, result, done, partial=False):
		if not result["ok"]:
			soundpack.play("error")
			if done:
				done(False)
			else:
				ui.message(result["message"])
			return
		if result.get("account_id") != self._aid:
			return
		store = self._plugin.store
		old = {c["convo_id"]: c for c in self._convos}
		rows = result["convos"]
		for row in rows:
			row["seen_id"] = (old.get(row["convo_id"]) or {}).get("seen_id", 0)  # what was marked read here stays so
		store.dm_save_convos(self._aid, rows)
		if result.get("complete") and rows:
			# the whole inbox was read: a conversation X no longer lists (deleted on the web) goes from the cache too
			kept = {r["convo_id"] for r in rows}
			for convo_id in old:
				if convo_id not in kept:
					store.dm_delete_convo(self._aid, convo_id)
					self._fetched.discard(convo_id)
		if result.get("own"):
			self._own = result["own"]
			store.set_account_meta(self._aid, OWN_KEY, self._own)
		self._load_convos()
		changed = any((old.get(c["convo_id"]) or {}).get("last_id") != c.get("last_id") for c in rows)
		if done:
			done(changed)
			return
		if result.get("complete") or partial:
			# Translators: Announced after the list of conversations was loaded. {count} is the number.
			ui.message(_("{count} conversations.").format(count=len(self._visible)))
		else:
			# Translators: Announced after the list of conversations was loaded when X has older ones that were not loaded. {count} is the number.
			ui.message(_("{count} conversations. Older ones were not loaded.").format(count=len(self._visible)))

	def onCheckForUpdates(self, evt=None):
		"""F5: the open conversation when the focus is on its messages, otherwise the list of conversations."""
		if self._current is not None and self.FindFocus() is not self.tree:
			self._fetch_history(self._current, "refresh")
		else:
			self.refresh()

	def onFetchOlder(self):
		"""Shift+F5: the messages before the oldest one the list holds."""
		if self._current is None or not self._messages:
			ui.message(_("Nothing loaded yet."))
			return
		self._fetch_history(self._current, "older", max_id=min(m["id"] for m in self._messages))

	# ------------------------------------------------------------ starting a chat

	@safe_ui
	def on_new(self, prefill=None):
		"""The New chat button, its key and Start chat of the user menu: the window that picks the people and
		writes the first message. prefill: users already picked."""
		dlg = NewChatDialog(self.GetTopLevelParent(), self._plugin, prefill)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			recipients, text = dlg.recipients, dlg.text
		finally:
			dlg.Destroy()
		self._start_chat(recipients, text)

	def _open_convo(self, convo_id):
		"""Show a conversation whatever the filter is, with the cursor in the box that writes to it."""
		convo = self._by_id(convo_id)
		if convo is None:
			return False
		if not convo_visible(convo, self._filter, self._flag(convo)):
			self._filter = "all" if convo.get("trusted") else "requests"
			self.filterChoice.SetSelection([key for key, _text in self._filters].index(self._filter))
		self._load_convos(convo_id)
		self._remember_soon()
		return True

	def _start_chat(self, recipients, text):
		if len(recipients) == 1:
			partner = recipients[0]["id"]
			known = next((c for c in self._convos if not c["is_group"] and c["partner_id"] == partner), None)
			if known is not None and known.get("trusted"):
				# the conversation exists: the message goes into it
				self._open_convo(known["convo_id"])
				self.compose.SetValue(text)
				self.on_send()
				return
		options = {"user_ids": [r["id"] for r in recipients], "text": text}
		callback = lambda result: self.on_started(result)
		if not self._plugin.start_job("dm_start", callback, options, progress=False):
			ui.message(_("Still loading, please wait."))
			return
		# Translators: Announced while a chat is being started.
		ui.message(_("Starting the chat, please wait."))

	@safe_ui
	def on_started(self, result):
		if not result["ok"]:
			soundpack.play("error")
			# Translators: Announced when a chat could not be started. {error} is the reason.
			ui.message(_("Could not start the chat: {error}").format(error=result["message"]))
			return
		if result.get("account_id") != self._aid:
			return
		soundpack.play("send_post")
		partner, group_id = result.get("partner_id", ""), result.get("group_id", "")
		if not self.refresh(lambda changed: self._after_started(partner, group_id)):
			# Translators: Announced after the first message of a chat was sent.
			ui.message(_("Message sent."))

	@safe_ui
	def _after_started(self, partner, group_id):
		convo = next((
			c for c in self._convos
			if (group_id and c["convo_id"] == group_id) or (partner and not c["is_group"] and c["partner_id"] == partner)
		), None)
		if convo is None:
			# Translators: Announced after the first message of a chat was sent, when X does not list the conversation yet.
			ui.message(_("Message sent. The conversation shows in the list when X lists it."))
			return
		self._open_convo(convo["convo_id"])
		self._fetch_history(convo, "first")
		# Translators: Announced after a chat was started.
		ui.message(_("Chat started."))

	# ------------------------------------------------------------ sending, answering, deleting

	@safe_ui
	def on_send(self, event=None):
		if self._sending or not self._writable():
			return
		text = self.compose.GetValue().strip()
		if not text:
			# Translators: Announced when sending a direct message with nothing typed.
			self._say(_("Message text can't be empty."))
			return
		if len(text) > DM_MAX_LENGTH:
			soundpack.play("max_length")
			# Translators: Announced when the direct message is too long. {length} is its length, {max} the limit.
			self._say(_("Message is too long ({length}/{max}).").format(length=len(text), max=DM_MAX_LENGTH))
			return
		convo = self._current
		options = {
			"convo_id": convo["convo_id"], "is_group": convo["is_group"], "partner_id": convo["partner_id"],
			"text": text, "reply_to": self._reply_to["id"] if self._reply_to else None,
		}
		callback = lambda result: self.on_sent(result, convo["convo_id"], text)
		self._sending = True
		if not self._plugin.start_job("dm_send", callback, options, progress=False):
			self._sending = False
			# Translators: Announced when a request is still running; the text is kept.
			self._say(_("Still loading, please wait a moment and send again."))
			return
		# Translators: Announced while a direct message is being sent.
		ui.message(_("Sending..."))

	@safe_ui
	def on_sent(self, result, convo_id, text):
		self._sending = False
		if not result["ok"]:
			soundpack.play("error")
			# Translators: Announced when a direct message could not be sent. {error} is the reason.
			ui.message(_("Failed to send: {error}").format(error=result["message"]))
			return
		if result.get("account_id") != self._aid:
			return
		store = self._plugin.store
		store.dm_add_messages(self._aid, convo_id, [result["message"]])
		if self.compose.GetValue().strip() == text:
			self.compose.SetValue("")
		self._reply_to = None
		if self._current and self._current["convo_id"] == convo_id:
			self._messages = store.dm_load_messages(self._aid, convo_id)
			self._render_messages(result["message"]["id"])
			self._update_area()
		soundpack.play("send_post")
		# Translators: Announced after a direct message was sent.
		ui.message(_("Message sent."))

	def _start_reply(self, message):
		self._reply_to = message
		preview = " ".join((message.get("text") or "").split())[:40]
		# Translators: Shown above the box of the Chat tab while answering a message. {text} is the start of that message.
		label = _("Replying to: {text}").format(text=preview)
		self._update_area()
		self.replyLabel.SetLabel(label)
		self.Layout()
		self.compose.SetFocus()
		ui.message(label)

	@safe_ui
	def on_cancel_reply(self, event=None):
		if self._reply_to is None:
			return
		self._reply_to = None
		self._update_area()
		# Translators: Announced after the reply that was being written was cancelled.
		ui.message(_("Reply canceled."))
		self.compose.SetFocus()

	def _delete(self, message):
		dlg = wx.MessageDialog(
			self,
			# Translators: Question before deleting a direct message. X only removes it for you: the other side keeps it.
			_("Delete this message for you? The other side keeps it. This can't be undone."),
			# Translators: Title of the question before deleting a direct message.
			_("Delete message"), wx.YES_NO | wx.NO_DEFAULT,
		)
		try:
			if dlg.ShowModal() != wx.ID_YES:
				return
		finally:
			dlg.Destroy()
		convo_id = self._current["convo_id"]
		callback = lambda result: self.on_deleted(result, convo_id, message["id"])
		if not self._plugin.start_job("dm_delete", callback, {"id": message["id"]}, progress=False):
			ui.message(_("Still loading, please wait."))

	@safe_ui
	def on_deleted(self, result, convo_id, message_id):
		if not result["ok"]:
			soundpack.play("error")
			# Translators: Announced when a message could not be deleted. {error} is the reason.
			ui.message(_("Failed to delete: {error}").format(error=result["message"]))
			return
		store = self._plugin.store
		store.dm_delete_message(self._aid, convo_id, message_id)
		self._refresh_counts()
		if self._current and self._current["convo_id"] == convo_id:
			self._messages = store.dm_load_messages(self._aid, convo_id)
			self._render_messages()
		self._refresh_label(convo_id)
		self._update_status()
		# Translators: Announced after a direct message was deleted.
		announce_now(_("Message deleted."))

	# ------------------------------------------------------------ commands on the focused message

	def _copy(self, text, done_text):
		if wx.TheClipboard.Open():
			try:
				wx.TheClipboard.SetData(wx.TextDataObject(text))
			finally:
				wx.TheClipboard.Close()
			announce_now(done_text)
		else:
			announce_now(_("Could not copy to the clipboard."))

	def _show_full(self, message):
		# Translators: Title of the window that shows one direct message in full. {user} is who wrote it.
		title = _("Message from {user}").format(user=self._sender_label(message))
		dlg = MessageDialog(self, title, message.get("text") or "")
		try:
			dlg.ShowModal()
		finally:
			dlg.Destroy()

	def _item(self, menu, label, callback):
		item = menu.Append(wx.ID_ANY, label)
		self.Bind(wx.EVT_MENU, lambda evt: callback(), item)

	@safe_ui
	def on_action(self, event=None):
		if self.FindFocus() is self.tree:  # Alt+A in the list of conversations: the menu of the conversation
			if self._current is not None:
				self._popup_convo_menu(self._current)
			return
		message = self._focused_message()
		if message is None:
			# Translators: Announced when the message menu is opened with no message focused.
			ui.message(_("No message selected."))
			return
		menu = wx.Menu()
		if self._writable():
			# Translators: Entry of the menu of a message in the Chat tab. The shortcut is added after the label.
			self._item(menu, keymap.with_key(_("&Reply"), "reply"), lambda: self._start_reply(message))
		# Translators: Entry of the menu of a message in the Chat tab, then what is said after it was copied.
		self._item(menu, _("&Copy text"), lambda: self._copy(message.get("text") or "", _("Message text copied.")))
		# Translators: Entry of the menu of a message in the Chat tab. The shortcut is added after the label.
		self._item(menu, keymap.with_key(_("Show &full message..."), "full_tweet"), lambda: self._show_full(message))
		# Translators: Entry of the menu of a message in the Chat tab: deletes it for you only.
		self._item(menu, _("&Delete for me..."), lambda: self._delete(message))
		self.PopupMenu(menu)
		menu.Destroy()

	# ------------------------------------------------------------ keyboard

	@safe_ui
	def on_escape(self):
		"""Escape in the box that writes a message leaves it, so a half written message is not lost with the
		window. Returns True when it handled the key."""
		if self.FindFocus() is self.compose:
			(self.list if self._rows else self.tree).SetFocus()
			return True
		return False

	@safe_ui
	def on_char_hook(self, event):
		key = event.GetKeyCode()
		focus = self.FindFocus()
		in_list = focus is self.list
		plain = not event.HasAnyModifiers()
		if key == wx.WXK_ESCAPE and plain and self.on_escape():
			return
		if in_list and key in (wx.WXK_UP, wx.WXK_DOWN) and plain:
			self._check_boundary(key)
		if keymap.matches(event, "send_tweet") and self._writable():
			self.on_send()
			return
		if keymap.matches(event, "check_updates"):
			self.onCheckForUpdates(None)
			return
		if in_list and keymap.matches(event, "load_older"):
			self.onFetchOlder()
			return
		if focus is self.tree and keymap.matches(event, "load_older"):
			self.refresh(full=True)  # every page of the inbox: the older conversations, and what X dropped
			return
		if keymap.matches(event, "action_menu"):
			self.on_action()
			return
		message = self._focused_message() if in_list else None
		if message is not None:
			if keymap.matches(event, "reply") and self._writable():
				self._start_reply(message)
				return
			if keymap.matches(event, "full_tweet"):
				self._show_full(message)
				return
			if keymap.matches(event, "copy_row"):
				self._copy(self._row_text(message), _("Copied."))
				return
			if key == wx.WXK_DELETE and plain:
				self._delete(message)
				return
		if event.AltDown() and not event.ControlDown() and not event.ShiftDown() and ord("1") <= key <= ord("9"):
			self._announce_nth(key - ord("0"))
			return
		if key == wx.WXK_SPACE and plain and in_list:
			self._focus_next_unread()
			return
		if key == wx.WXK_SPACE and plain and focus is self.tree:
			self._focus_next_unread_convo()
			return
		event.Skip()

	# ------------------------------------------------------------ what the main window asks of a tab

	def onTabActivated(self):
		ui.message(_("{name} tab").format(name=self.TAB_NAME))
		self._restoreFocusPosition()
		store = self._plugin.store
		if not store.get_account_meta(self._aid, NOTICE_KEY):
			store.set_account_meta(self._aid, NOTICE_KEY, "1")
			wx.CallAfter(self._show_notice)
		if not self._tried and not self._convos:
			self._tried = True
			self.refresh()

	@safe_ui
	def _show_notice(self):
		wx.MessageBox(
			# Translators: Shown once, the first time the Chat tab is opened.
			_("The Chat tab is experimental. X's encrypted chats cannot be read by this add-on, so some conversations "
			  "and messages may be missing, and reactions are not shown. Sending works. What cannot be done here can "
			  "be done on x.com: press Alt+A on a conversation and choose Open on x.com."),
			# Translators: Title of the notice shown the first time the Chat tab is opened.
			_("Chat is experimental"), wx.OK | wx.ICON_INFORMATION, self,
		)

	def _restoreFocusPosition(self, moveFocus=True):
		if not moveFocus:
			return
		if self._convos:
			self._parked = False
			self.tree.SetFocus()
		else:
			self._parked = True
			button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
			if button is not None:
				button.SetFocus()

	def _leave_toolbar_button(self):
		"""The tab was empty, so focus waited on the toolbar button: now it has conversations, move it in."""
		if not self._parked or not self._convos:
			return
		button = getattr(self.GetTopLevelParent(), "checkUpdatesButton", None)
		try:
			if button is not None and self.IsShownOnScreen() and self.FindFocus() is button:
				self._parked = False
				self.tree.SetFocus()
		except RuntimeError:
			pass

	def _update_clock(self):
		if is_relative_mode(self._cfg.get("time_mode")):
			self._clock_timer.Start(CLOCK_REFRESH_MS)
		else:
			self._clock_timer.Stop()

	@safe_ui
	def on_clock_timer(self, event):
		if self._dead or not self.IsShownOnScreen():
			return
		for i, message in enumerate(self._rows):
			self.list.SetItem(i, 4, self._time_text(message))

	def reload_display(self):
		"""Settings changed: the labels and the cells are made again."""
		self._cols = None
		self._load_convos()
		self._update_clock()
