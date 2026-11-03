# -*- coding: utf-8 -*-
# NVTweet - compose.py: the window that writes a new tweet, a reply or a quote.
#
# Shown non-modally (Show, never ShowModal: ShowModal straight from an NVDA gesture can crash NVDA).
# Escape does NOT close it, so a half-written tweet is not lost by accident; Ctrl+W, Alt+F4 or
# Cancel close it. Ctrl+Enter posts. Completion is reported through onClosed(posted).

import os

import addonHandler
import ui
import wx

from . import keymap, soundpack
from .utils import POST_MAX_LENGTH, author_handle, safe_ui, target_id, tweet_length

addonHandler.initTranslation()

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
GIF_EXTENSION = ".gif"
VIDEO_EXTENSIONS = (".mp4", ".mov")
MAX_IMAGES = 4
MAX_ALT_LENGTH = 1000
MAX_BYTES = {"image": 5 * 1024 * 1024, "gif": 15 * 1024 * 1024, "video": 512 * 1024 * 1024}


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

		self._attachments = []  # [{"path", "alt", "kind"}]
		# Translators: Button that attaches images, a GIF or a video.
		self.attachButton = wx.Button(self, label=_("Attach &media..."))
		sizer.Add(self.attachButton, 0, wx.LEFT | wx.TOP, 10)
		# Translators: Label of the list of attached files in the compose window.
		self.attachLabel = wx.StaticText(self, label=_("Attached &files (Delete removes one):"))
		sizer.Add(self.attachLabel, 0, wx.LEFT | wx.TOP, 10)
		self.attachList = wx.ListBox(self, size=(400, 70))
		sizer.Add(self.attachList, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		# Translators: Button that removes the selected attached file.
		self.removeButton = wx.Button(self, label=_("&Remove attachment"))
		sizer.Add(self.removeButton, 0, wx.LEFT | wx.TOP, 10)
		for control in (self.attachLabel, self.attachList, self.removeButton):
			control.Hide()

		# Translators: Button that posts the tweet. The shortcut is added after the label.
		self.postButton = wx.Button(self, label=keymap.with_key(_("&Post"), "send_tweet"))
		self.cancelButton = wx.Button(self, label=_("&Cancel"))
		row = wx.BoxSizer(wx.HORIZONTAL)
		row.Add(self.postButton, 0, wx.RIGHT, 5)
		row.Add(self.cancelButton, 0)
		sizer.Add(row, 0, wx.ALL | wx.ALIGN_CENTER, 10)
		self.SetSizerAndFit(sizer)

		self.textCtrl.Bind(wx.EVT_TEXT, self.onTextChanged)
		self.attachButton.Bind(wx.EVT_BUTTON, self.onAttach)
		self.removeButton.Bind(wx.EVT_BUTTON, self.onRemoveAttachment)
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
		kinds = [a["kind"] for a in self._attachments]
		if not kinds:
			suffix = ""
		elif kinds[0] == "video":
			# Translators: Compose window title suffix when a video is attached.
			suffix = _(" (1 video attachment)")
		elif kinds[0] == "gif":
			# Translators: Compose window title suffix when an animated GIF is attached.
			suffix = _(" (1 GIF attachment)")
		elif len(kinds) == 1:
			# Translators: Compose window title suffix when one picture is attached.
			suffix = _(" (1 picture attachment)")
		else:
			# Translators: Compose window title suffix when several pictures are attached. {count} is the number.
			suffix = _(" ({count} picture attachments)").format(count=len(kinds))
		# Translators: Compose window title. {base} is New tweet / Reply to... / Quote..., then the length, the limit, the attachments and the account.
		self.SetTitle(_("{base} {length}/{max}{suffix} - ({account})").format(
			base=self._base_title(), length=length, max=POST_MAX_LENGTH, suffix=suffix,
			account=self._plugin.account_label(),
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
		if key == wx.WXK_DELETE and not evt.HasAnyModifiers() and self.FindFocus() is self.attachList:
			self.onRemoveAttachment()
			return
		evt.Skip()

	def onPost(self, evt):
		if self._posting:
			return
		text = self.textCtrl.GetValue().strip()
		if not text and not self._attachments:
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
		if self._attachments:
			options["media"] = [
				{"path": a["path"], "alt": a["alt"], "kind": a["kind"], "shrink": a["shrink"]}
				for a in self._attachments
			]
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
		if self._attachments:
			# Translators: Shown and spoken while a tweet with attached files is being uploaded and posted.
			posting = _("Uploading and posting...")
		else:
			# Translators: Shown and spoken while a tweet is being posted.
			posting = _("Posting...")
		self.statusLabel.SetLabel(posting)
		ui.message(posting)

	def _say(self, text):
		self.statusLabel.SetLabel(text)
		ui.message(text)

	@staticmethod
	def _media_kind(path):
		ext = os.path.splitext(path)[1].lower()
		if ext in IMAGE_EXTENSIONS:
			return "image"
		if ext == GIF_EXTENSION:
			return "gif"
		if ext in VIDEO_EXTENSIONS:
			return "video"
		return None

	def onAttach(self, evt=None):
		if self._posting:
			return
		if any(a["kind"] != "image" for a in self._attachments):
			# Translators: Shown when attaching to a tweet that already has a video or GIF. {max} is the number of images allowed.
			self._say(_("A tweet can have one video or GIF, or up to {max} images. Remove the attached file first.").format(max=MAX_IMAGES))
			return
		if len(self._attachments) >= MAX_IMAGES:
			# Translators: Shown when the tweet already has the most images. {max} is the number.
			self._say(_("You can attach up to {max} images to a tweet.").format(max=MAX_IMAGES))
			return
		patterns = "*.jpg;*.jpeg;*.png;*.webp;*.gif;*.mp4;*.mov"
		with wx.FileDialog(
			# Translators: Title of the window that picks the media to attach.
			self, _("Attach images, a GIF or a video"),
			wildcard=_("Media files") + " (%s)|%s" % (patterns, patterns),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
		) as dlg:
			if dlg.ShowModal() != wx.ID_OK:
				return
			paths = dlg.GetPaths()
		wx.CallLater(300, self._add_media, paths)  # the file window closing makes NVDA read this window again

	def _add_media(self, paths):
		if self._dead:
			return
		kinds = [self._media_kind(p) for p in paths]
		if None in kinds:
			# Translators: Shown when a chosen file is not an image, GIF or video X accepts.
			self._say(_("Only JPG, PNG, WebP, GIF, MP4 and MOV files can be attached."))
			return
		if any(k != "image" for k in kinds) and (len(paths) > 1 or self._attachments):
			# Translators: Shown when a video or GIF is chosen together with other files.
			self._say(_("A video or GIF can't be combined with other files."))
			return
		room = MAX_IMAGES - len(self._attachments)
		if kinds[0] == "image" and len(paths) > room:
			# Translators: Shown when more images were chosen than the tweet has room for. {count} is how many were attached.
			ui.message(_("Only the first {count} were attached.").format(count=room))
			paths, kinds = paths[:room], kinds[:room]
		for path, kind in zip(paths, kinds):
			name = os.path.basename(path)
			try:
				size = os.path.getsize(path)
			except OSError:
				self._say(_("Could not read that file."))
				continue
			shrink = False
			if size > MAX_BYTES[kind]:
				limit = MAX_BYTES[kind] // (1024 * 1024)
				if kind != "image":
					# Translators: Shown when a GIF or video is larger than X accepts. {name} is the file name, {size} the limit in MB.
					self._say(_("{name} is too large for X (the limit is {size} MB).").format(name=name, size=limit))
					continue
				answer = wx.MessageBox(
					# Translators: Asks whether to shrink an image over X's size limit. {name} is the file name, {size} its size in MB, {limit} the limit in MB.
					_("{name} is {size:.1f} MB, over the {limit} MB limit of X. Shrink it automatically so it can be attached?").format(
						name=name, size=size / (1024 * 1024), limit=limit,
					),
					# Translators: Title of the question about an image that is too large.
					_("Image too large"), wx.YES_NO | wx.ICON_QUESTION, self,
				)
				if answer != wx.YES:
					continue
				shrink = True
			dlg = wx.TextEntryDialog(
				# Translators: Prompt for the description of an attached file. {name} is the file name.
				self, _("Description (alt text) for {name}, optional:").format(name=name),
				# Translators: Title of the description window of an attached file.
				_("Description"),
			)
			alt = dlg.GetValue().strip()[:MAX_ALT_LENGTH] if dlg.ShowModal() == wx.ID_OK else ""
			dlg.Destroy()
			self._attachments.append({"path": path, "alt": alt, "kind": kind, "shrink": shrink})
		self._refresh_attachments()
		# Translators: Announced after attaching files. {count} is the number attached now.
		self._say(_("{count} attached.").format(count=len(self._attachments)))

	def _refresh_attachments(self, select=0):
		labels = []
		for a in self._attachments:
			name = os.path.basename(a["path"])
			if a["alt"]:
				# Translators: Entry of the attached files list. {name} is the file name, {text} the start of its description.
				labels.append(_("{name}, description: {text}").format(name=name, text=a["alt"][:40]))
			else:
				# Translators: Entry of the attached files list for a file with no description. {name} is the file name.
				labels.append(_("{name}, no description").format(name=name))
		self.attachList.Set(labels)
		shown = bool(labels)
		for control in (self.attachLabel, self.attachList, self.removeButton):
			control.Show(shown)
		if shown:
			self.attachList.SetSelection(max(0, min(select, len(labels) - 1)))
		self.Layout()
		best, size = self.GetBestSize(), self.GetSize()
		self.SetSize((max(size[0], best[0]), max(size[1], best[1])))
		self._update_title()

	def onRemoveAttachment(self, evt=None):
		index = self.attachList.GetSelection()
		if self._posting or not 0 <= index < len(self._attachments):
			return
		name = os.path.basename(self._attachments.pop(index)["path"])
		self._refresh_attachments(index)
		if not self._attachments:
			self.attachButton.SetFocus()
		# Translators: Announced after removing an attached file. {name} is the file name.
		self._say(_("Removed {name}.").format(name=name))

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
