# -*- coding: utf-8 -*-
# tweetAccess - newchat.py: the window that starts a chat, after NVSky's. Search for users (only when the reader asks:
# Enter or Find), check the ones to write to, write the first message and start: one user opens a conversation,
# several create a group. The first message is needed: X does not list a conversation that has nothing in it.

import addonHandler
import ui
import wx
from logHandler import log

import gui.nvdaControls

from . import soundpack
from .utils import TAG, announce_now, safe_ui

addonHandler.initTranslation()

DM_MAX_LENGTH = 10000  # characters of a direct message (X's limit, not confirmed here)


def user_label(user):
	"""'Name (@user)' of a user of the lists of this window."""
	name, handle = user.get("name") or "", user.get("handle") or ""
	if name and handle:
		return _("{name} (@{user})").format(name=name, user=handle)
	return "@" + handle if handle else name


class NewChatDialog(wx.Dialog):
	"""self.recipients (dicts with id, handle, name) and self.text after OK."""

	def __init__(self, parent, plugin, recipients=None):
		# Translators: Title of the window that starts a chat.
		super().__init__(parent, title=_("New chat"), size=(480, 620))
		self._plugin = plugin
		self._dead = False
		self._found = []
		self.recipients = [dict(r) for r in (recipients or [])]
		self.text = ""

		sizer = wx.BoxSizer(wx.VERTICAL)
		row = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label of the box that searches for a user in the New chat window.
		row.Add(wx.StaticText(self, label=_("Search for a &user:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
		self.query = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
		row.Add(self.query, 1)
		# Translators: Button of the New chat window that searches for the user typed.
		self.findButton = wx.Button(self, label=_("&Find"))
		row.Add(self.findButton, 0, wx.LEFT, 5)
		sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)

		# Translators: Label of the list of the users found, in the New chat window.
		self.foundLabel = wx.StaticText(self, label=_("Search &results (check the people to write to):"))
		self.found = gui.nvdaControls.CustomCheckListBox(self, choices=[], size=(-1, 110))
		# Translators: Button of the New chat window that adds the checked users to the recipients.
		self.addButton = wx.Button(self, label=_("&Add checked"))
		sizer.Add(self.foundLabel, 0, wx.LEFT | wx.RIGHT, 8)
		sizer.Add(self.found, 0, wx.EXPAND | wx.ALL, 8)
		sizer.Add(self.addButton, 0, wx.LEFT | wx.BOTTOM, 8)

		# Translators: Label of the list of the people the chat is with, in the New chat window.
		self.peopleLabel = wx.StaticText(self, label=_("Rec&ipients:"))
		self.people = gui.nvdaControls.CustomCheckListBox(self, choices=[], size=(-1, 90))
		# Translators: Button of the New chat window that takes the checked recipients out.
		self.removeButton = wx.Button(self, label=_("Remove chec&ked"))
		sizer.Add(self.peopleLabel, 0, wx.LEFT | wx.RIGHT, 8)
		sizer.Add(self.people, 0, wx.EXPAND | wx.ALL, 8)
		sizer.Add(self.removeButton, 0, wx.LEFT | wx.BOTTOM, 8)

		# Translators: Label of the box of the first message, in the New chat window.
		sizer.Add(wx.StaticText(self, label=_("&Message:")), 0, wx.LEFT | wx.RIGHT, 8)
		self.message = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 80))
		sizer.Add(self.message, 1, wx.EXPAND | wx.ALL, 8)

		buttons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button of the New chat window that starts the chat (a group when there are several recipients).
		self.startButton = wx.Button(self, label=_("St&art chat"))
		self.cancelButton = wx.Button(self, wx.ID_CANCEL, _("&Cancel"))
		buttons.Add(self.startButton, 0, wx.RIGHT, 8)
		buttons.Add(self.cancelButton, 0)
		sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.BOTTOM, 8)
		self.SetSizer(sizer)
		self.SetEscapeId(wx.ID_CANCEL)
		self.CentreOnParent()

		self.query.Bind(wx.EVT_TEXT_ENTER, self.on_find)
		self.findButton.Bind(wx.EVT_BUTTON, self.on_find)
		self.addButton.Bind(wx.EVT_BUTTON, self.on_add)
		self.removeButton.Bind(wx.EVT_BUTTON, self.on_remove)
		self.startButton.Bind(wx.EVT_BUTTON, self.on_start)

		self._show_found(False)
		self._refresh_people()
		(self.message if self.recipients else self.query).SetFocus()

	def Destroy(self):
		self._dead = True  # a result that arrives later is thrown away (see safe_ui)
		return super().Destroy()

	# ------------------------------------------------------------ the lists

	def _show_found(self, shown):
		for control in (self.foundLabel, self.found, self.addButton):
			control.Show(shown)
		self.Layout()

	def _refresh_people(self):
		self.people.Set([user_label(r) for r in self.recipients])
		self.people.CheckedItems = []
		shown = bool(self.recipients)
		for control in (self.peopleLabel, self.people, self.removeButton):
			control.Show(shown)
		if shown:
			self.people.SetSelection(0)
		self.Layout()

	# ------------------------------------------------------------ searching

	def on_find(self, event=None):
		query = self.query.GetValue().strip()
		if not query:
			ui.message(_("Type something to search for."))
			self.query.SetFocus()
			return
		options = {
			"kind": "search", "params": {"handle": query, "query": query}, "chain": "newchat:%s" % query, "pages": 1,
		}
		if not self._plugin.start_job("users", self.on_found, options):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading, please wait."))

	@safe_ui
	def on_found(self, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		taken = {r["id"] for r in self.recipients}
		self._found = [u for u in result["users"] if u["id"] not in taken]
		if not self._found:
			self._show_found(False)
			ui.message(_("No users found."))
			self.query.SetFocus()
			return
		self.found.Set([user_label(u) for u in self._found])
		self.found.CheckedItems = []
		self._show_found(True)
		self.found.SetSelection(0)
		self.found.SetFocus()  # the first of them, so the arrow keys and Space work at once

	def on_add(self, event=None):
		if not self.addButton.IsShown():
			return
		picked = list(self.found.CheckedItems)
		if not picked and self.found.GetSelection() != wx.NOT_FOUND:
			picked = [self.found.GetSelection()]  # nothing checked: the highlighted one
		users = [self._found[i] for i in picked if 0 <= i < len(self._found)]
		if not users:
			return
		self.recipients.extend({"id": u["id"], "handle": u["handle"], "name": u["name"]} for u in users)
		self._found = []
		self.query.SetValue("")
		self._show_found(False)
		self._refresh_people()
		self.message.SetFocus()
		# Translators: Announced after adding recipients in the New chat window. {count} is how many recipients there are now.
		announce_now(_("Added. {count} recipients.").format(count=len(self.recipients)))

	def on_remove(self, event=None):
		if not self.removeButton.IsShown():
			return
		picked = set(self.people.CheckedItems)
		if not picked:
			# Translators: Announced when Remove checked is pressed with nothing checked in the New chat window.
			announce_now(_("No recipients checked."))
			return
		self.recipients = [r for i, r in enumerate(self.recipients) if i not in picked]
		self._refresh_people()
		(self.people if self.recipients else self.query).SetFocus()
		# Translators: Announced after removing recipients in the New chat window. {count} is how many are left.
		announce_now(_("Removed. {count} recipients left.").format(count=len(self.recipients)))

	# ------------------------------------------------------------ starting

	def on_start(self, event=None):
		if not self.recipients:
			# Translators: Announced when Start chat is pressed with nobody to write to.
			announce_now(_("Add at least one recipient first."))
			self.query.SetFocus()
			return
		text = self.message.GetValue().strip()
		if not text:
			# Translators: Announced when Start chat is pressed with no first message.
			announce_now(_("Type a first message before starting the chat."))
			self.message.SetFocus()
			return
		if len(text) > DM_MAX_LENGTH:
			soundpack.play("max_length")
			announce_now(_("Message is too long ({length}/{max}).").format(length=len(text), max=DM_MAX_LENGTH))
			self.message.SetFocus()
			return
		self.text = text
		self.EndModal(wx.ID_OK)
