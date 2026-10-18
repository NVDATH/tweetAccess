# -*- coding: utf-8 -*-
# NVTweet - compose.py: the window that writes a new tweet, a reply or a quote.
#
# Shown non-modally (Show, never ShowModal: ShowModal straight from an NVDA gesture can crash NVDA).
# Escape does NOT close it, so a half-written tweet is not lost by accident; Ctrl+W, Alt+F4 or
# Cancel close it. Ctrl+Enter posts. Completion is reported through onClosed(posted).

import addonHandler
import ui
import wx

from . import keymap, soundpack
from .utils import POST_MAX_LENGTH, author_handle, safe_ui, target_id, tweet_length

addonHandler.initTranslation()


class ComposeDialog(wx.Dialog):
	def __init__(self, parent, plugin, onClosed=None, reply_to=None, quote_of=None):
		# reply_to / quote_of: the Post being answered or quoted (only one of them).
		if reply_to:
			# Translators: Compose window title when replying. {user} is the user name replied to.
			title = _("Reply to @{user}").format(user=author_handle(reply_to))
		elif quote_of:
			# Translators: Compose window title when quoting. {user} is the user name quoted.
			title = _("Quote @{user}").format(user=author_handle(quote_of))
		else:
			# Translators: Compose window title for a plain new tweet.
			title = _("New tweet")
		super().__init__(parent, title=title)
		self._plugin = plugin
		self._onClosed = onClosed
		self._replyTo = reply_to
		self._quoteOf = quote_of
		self.posted = False
		self._posting = False
		self._dead = False

		sizer = wx.BoxSizer(wx.VERTICAL)
		target = reply_to or quote_of
		# Translators: Label of the compose box for a plain new tweet.
		text_label = _("Tweet t&ext:")
		if target:
			preview = " ".join(target.message.split())
			if len(preview) > 100:
				preview = preview[:100] + "..."
			user = author_handle(target)
			if reply_to:
				# Translators: Context line above the compose box when replying. {user} is the user name, {text} a preview of the tweet.
				context = _("Replying to @{user}: {text}").format(user=user, text=preview)
				# Translators: Compose box label when replying (folds the context in so Tab announces both).
				text_label = _("Replying to @{user}. Tweet t&ext:").format(user=user)
			else:
				# Translators: Context line above the compose box when quoting. {user} is the user name, {text} a preview of the tweet.
				context = _("Quoting @{user}: {text}").format(user=user, text=preview)
				# Translators: Compose box label when quoting (folds the context in so Tab announces both).
				text_label = _("Quoting @{user}. Tweet t&ext:").format(user=user)
			sizer.Add(wx.StaticText(self, label=context), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

		sizer.Add(wx.StaticText(self, label=text_label), 0, wx.LEFT | wx.TOP, 10)
		self.textCtrl = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(400, 150))
		sizer.Add(self.textCtrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

		# Only shown once there is text: an empty gauge is just clutter to navigate past.
		self.charGauge = wx.Gauge(self, range=POST_MAX_LENGTH, size=(400, 20))
		sizer.Add(self.charGauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
		self.charGauge.Hide()
		self.charCountLabel = wx.StaticText(self, label="")
		sizer.Add(self.charCountLabel, 0, wx.LEFT | wx.TOP, 5)
		self.statusLabel = wx.StaticText(self, label="")
		sizer.Add(self.statusLabel, 0, wx.LEFT | wx.TOP, 10)

		# Translators: Button that posts the tweet. The shortcut is added after the label.
		self.postButton = wx.Button(self, label=keymap.with_key(_("&Post"), "send_tweet"))
		self.cancelButton = wx.Button(self, label=_("&Cancel"))
		row = wx.BoxSizer(wx.HORIZONTAL)
		row.Add(self.postButton, 0, wx.RIGHT, 5)
		row.Add(self.cancelButton, 0)
		sizer.Add(row, 0, wx.ALL | wx.ALIGN_CENTER, 10)
		self.SetSizerAndFit(sizer)

		self.textCtrl.Bind(wx.EVT_TEXT, self.onTextChanged)
		self.postButton.Bind(wx.EVT_BUTTON, self.onPost)
		self.cancelButton.Bind(wx.EVT_BUTTON, lambda e: self.Close())
		self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
		self.Bind(wx.EVT_CLOSE, self.onCloseEvent)
		self._update_title()
		self.textCtrl.SetFocus()

	def Destroy(self):
		self._dead = True  # a result that arrives later is thrown away (see safe_ui)
		return super().Destroy()

	def _base_title(self):
		if self._replyTo:
			return _("Reply to @{user}").format(user=author_handle(self._replyTo))
		if self._quoteOf:
			return _("Quote @{user}").format(user=author_handle(self._quoteOf))
		return _("New tweet")

	def _update_title(self):
		length = tweet_length(self.textCtrl.GetValue())
		# Translators: Compose window title. {base} is New tweet / Reply to... / Quote..., then the length, the limit and the account.
		self.SetTitle(_("{base} {length}/{max} - ({account})").format(
			base=self._base_title(), length=length, max=POST_MAX_LENGTH, account=self._plugin.account_label(),
		))

	def onTextChanged(self, evt):
		length = tweet_length(self.textCtrl.GetValue())
		if length > POST_MAX_LENGTH:
			soundpack.play("max_length")  # on every change while over the limit, not only the first
		self.charGauge.Show(length > 0)
		if length > 0:
			self.charGauge.SetValue(min(length, POST_MAX_LENGTH))
			self.charCountLabel.SetLabel("%d / %d" % (length, POST_MAX_LENGTH))
		else:
			self.charCountLabel.SetLabel("")
		self._update_title()
		self.Layout()
		evt.Skip()

	def onCharHook(self, evt):
		key = evt.GetKeyCode()
		if key == wx.WXK_ESCAPE:
			return  # swallowed on purpose: a half-written tweet must not be dismissed by accident
		if keymap.matches(evt, "close_compose"):
			self.Close()
			return
		if keymap.matches(evt, "send_tweet"):
			self.onPost(evt)  # works from any control, the multiline box included
			return
		evt.Skip()

	def onPost(self, evt):
		if self._posting:
			return
		text = self.textCtrl.GetValue().strip()
		if not text:
			# Translators: Shown when trying to post with nothing typed.
			self.statusLabel.SetLabel(_("Tweet text can't be empty."))
			ui.message(_("Tweet text can't be empty."))
			return
		length = tweet_length(text)
		if length > POST_MAX_LENGTH:
			soundpack.play("max_length")
			# Translators: Shown when the tweet is too long. {length} is the current length, {max} the limit.
			too_long = _("Tweet is too long ({length}/{max}).").format(length=length, max=POST_MAX_LENGTH)
			self.statusLabel.SetLabel(too_long)
			ui.message(too_long)
			return
		options = {"text": text}
		if self._replyTo:
			options["reply_to"] = target_id(self._replyTo)
		if self._quoteOf:
			options["quote_url"] = self._quoteOf.url
		self._posting = True  # a flag, not Disable(): disabling the focused button makes NVDA say "unavailable"
		if not self._plugin.start_job("post", self.on_posted, options):
			self._posting = False
			# Translators: Shown when another request is still running; the text is kept.
			self.statusLabel.SetLabel(_("Still loading, please wait a moment and post again."))
			ui.message(_("Still loading, please wait a moment and post again."))
			return
		# Translators: Shown and spoken while a tweet is being posted.
		posting = _("Posting...")
		self.statusLabel.SetLabel(posting)
		ui.message(posting)

	@safe_ui
	def on_posted(self, result):
		self._posting = False
		if not result["ok"]:
			soundpack.play("error")
			# Translators: Shown and spoken when posting fails. {error} is the reason.
			failed = _("Failed to post: {error}").format(error=result["message"])
			self.statusLabel.SetLabel(failed)
			ui.message(failed)
			return
		self.posted = True
		posts = result["posts"]
		self._plugin.after_post(posts[0] if posts else None)
		soundpack.play("send_post")
		# Translators: Spoken after a tweet was posted.
		ui.message(_("Posted successfully."))
		# Delay Close(): it returns focus to the parent and NVDA would cut off the message above.
		wx.CallLater(1000, self._close_later)

	def _close_later(self):
		if not self._dead:
			self.Close()

	def onCloseEvent(self, evt):
		if self._posting and evt.CanVeto():
			evt.Veto()
			ui.message(_("Posting, please wait."))
			return
		if self._onClosed:
			self._onClosed(self.posted)
		self.Destroy()