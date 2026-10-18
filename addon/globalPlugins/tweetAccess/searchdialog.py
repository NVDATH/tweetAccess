# -*- coding: utf-8 -*-
# tweetAccess - searchdialog.py: the search window. Keywords (comma separated: any matches), then a
# collapsible advanced part like NVSky's Explore: words to leave out, include / leave out filters,
# user names, language, dates, minimum counts. The query that is sent to X is built by
# utils.build_search_query and shown as the title of the search tab.

import datetime
import re

import addonHandler
import ui
import wx

import gui.nvdaControls

from . import soundpack
from .utils import build_search_query

addonHandler.initTranslation()

FILTER_ORDER_KEY = "search_filter_order"  # config key: order of the Include list
OMIT_ORDER_KEY = "search_omit_order"      # config key: order of the Leave out list

SEARCH_FILTERS = [
	# Translators: Search filter (X operator filter:images).
	("images", _("Images")),
	# Translators: Search filter (X operator filter:videos).
	("videos", _("Videos")),
	# Translators: Search filter (X operator filter:native_video).
	("native_video", _("Videos uploaded to X")),
	# Translators: Search filter (X operator filter:media).
	("media", _("Any media")),
	# Translators: Search filter (X operator filter:links).
	("links", _("Links")),
	# Translators: Search filter (X operator filter:replies).
	("replies", _("Replies")),
	# Translators: Search filter (X operator filter:retweets).
	("retweets", _("Retweets")),
	# Translators: Search filter (X operator filter:quote).
	("quote", _("Quote tweets")),
	# Translators: Search filter (X operator filter:self_threads).
	("self_threads", _("Threads by the same author")),
	# Translators: Search filter (X operator filter:follows).
	("follows", _("From people you follow")),
	# Translators: Search filter (X operator filter:verified).
	("verified", _("From verified accounts")),
	# Translators: Search filter (X operator filter:hashtags).
	("hashtags", _("With hashtags")),
	# Translators: Search filter (X operator filter:mentions).
	("mentions", _("With mentions")),
]


def ordered_filters(saved):
	"""SEARCH_FILTERS in the order the user chose (saved: keys, comma separated). Keys not saved go last."""
	by_key = dict(SEARCH_FILTERS)
	order = []
	for key in (k for k in (saved or "").split(",") if k):
		if key in by_key and key not in order:
			order.append(key)
	order += [key for key, _text in SEARCH_FILTERS if key not in order]
	return [(key, by_key[key]) for key in order]


class SearchDialog(wx.Dialog):
	PRODUCTS = ("Latest", "Top")
	MAX_QUERY = 500  # X's limit for a search
	_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

	def __init__(self, parent, suggestions=None, config=None):
		# Translators: Title of the search window.
		super().__init__(parent, title=_("Search"))
		self._suggestions = list(suggestions or [])
		self._result = ""
		self._config = config
		self._incl_filters = ordered_filters(config.get(FILTER_ORDER_KEY) if config is not None else "")
		self._omit_filters = ordered_filters(config.get(OMIT_ORDER_KEY) if config is not None else "")
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Label of the keywords field. A comma separates keywords; a space belongs to a keyword.
		sizer.Add(wx.StaticText(self, label=_("&Keywords (separate several with commas, any of them matches):")), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
		self.query = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER, size=(440, -1))
		sizer.Add(self.query, 0, wx.EXPAND | wx.ALL, 10)

		if self._suggestions:
			# Translators: Label of the checklist of hashtags and @users taken from the tweet in focus.
			sizer.Add(wx.StaticText(self, label=_("From this t&weet:")), 0, wx.LEFT | wx.RIGHT, 10)
			self.suggest = gui.nvdaControls.CustomCheckListBox(self, choices=self._suggestions, size=(-1, 90))
			self.suggest.SetSelection(0)
			sizer.Add(self.suggest, 0, wx.EXPAND | wx.ALL, 10)
			# Translators: Button that adds the checked hashtags and users to the keywords.
			self.suggestBtn = wx.Button(self, label=_("&Add checked"))
			sizer.Add(self.suggestBtn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
			self.suggestBtn.Bind(wx.EVT_BUTTON, self.on_add_suggestion)

		# Translators: Label of the search order choice.
		sizer.Add(wx.StaticText(self, label=_("O&rder:")), 0, wx.LEFT | wx.RIGHT, 10)
		# Translators: Search order choices.
		self.order = wx.Choice(self, choices=[_("Latest tweets first"), _("Top tweets")])
		self.order.SetSelection(0)
		sizer.Add(self.order, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

		# Like NVSky's Explore: a plain button plus a panel, not wx.CollapsiblePane (its children
		# make NVDA read the label twice). The state is written into the button label by hand.
		self._advExpanded = False
		self.advBtn = wx.Button(self, label="")
		sizer.Add(self.advBtn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		self.advPanel = wx.Panel(self)
		adv = wx.BoxSizer(wx.VERTICAL)

		def field(label_text, key, store):
			row = wx.BoxSizer(wx.HORIZONTAL)
			row.Add(wx.StaticText(self.advPanel, label=label_text), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
			ctrl = wx.TextCtrl(self.advPanel)
			row.Add(ctrl, 1)
			adv.Add(row, 0, wx.EXPAND | wx.BOTTOM, 5)
			store[key] = ctrl

		self.adv = {}
		# Translators: Label of the words to leave out of a search (commas separate them).
		adv.Add(wx.StaticText(self.advPanel, label=_("E&xclude words (separate several with commas):")), 0, wx.BOTTOM, 3)
		self.exclude = wx.TextCtrl(self.advPanel)
		adv.Add(self.exclude, 0, wx.EXPAND | wx.BOTTOM, 5)
		# Translators: Label of the checklist of filters a tweet must match. Shift+Up and Shift+Down move the highlighted filter.
		adv.Add(wx.StaticText(self.advPanel, label=_("&Include only tweets that have (Shift+Up or Down moves an item):")), 0, wx.BOTTOM, 3)
		self.incl = gui.nvdaControls.CustomCheckListBox(
			self.advPanel, choices=[text for _key, text in self._incl_filters], size=(-1, 110)
		)
		self.incl.SetSelection(0)
		adv.Add(self.incl, 0, wx.EXPAND | wx.BOTTOM, 5)
		# Translators: Label of the checklist of filters that make a tweet be left out. Shift+Up and Shift+Down move the highlighted filter.
		adv.Add(wx.StaticText(self.advPanel, label=_("&Leave out tweets that have (Shift+Up or Down moves an item):")), 0, wx.BOTTOM, 3)
		self.omit = gui.nvdaControls.CustomCheckListBox(
			self.advPanel, choices=[text for _key, text in self._omit_filters], size=(-1, 110)
		)
		self.omit.SetSelection(0)
		adv.Add(self.omit, 0, wx.EXPAND | wx.BOTTOM, 5)
		# Translators: Labels of the advanced search fields. The date format is fixed.
		field(_("&From user name:"), "from", self.adv)
		field(_("&To user name (replies to):"), "to", self.adv)
		field(_("&Mentioning user name:"), "mention", self.adv)
		field(_("Lan&guage (such as en):"), "lang", self.adv)
		field(_("&Since (YYYY-MM-DD):"), "since", self.adv)
		field(_("&Until (YYYY-MM-DD):"), "until", self.adv)
		field(_("Minimum li&kes:"), "likes", self.adv)
		field(_("Minimum retwee&ts:"), "retweets", self.adv)
		field(_("Minimum rep&lies:"), "replies", self.adv)
		self.advPanel.SetSizer(adv)
		sizer.Add(self.advPanel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

		sizer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_CENTER | wx.BOTTOM | wx.TOP, 10)
		self.Bind(wx.EVT_BUTTON, self.on_ok, id=wx.ID_OK)
		self.advBtn.Bind(wx.EVT_BUTTON, lambda e: self._toggle_adv())
		self.query.Bind(wx.EVT_TEXT_ENTER, self.on_ok)
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
		self._set_adv(False, fit=False)
		self.SetSizerAndFit(sizer)
		self.CentreOnParent()
		self.query.SetFocus()

	# ---- advanced part

	def _set_adv(self, expanded, fit=True):
		self._advExpanded = expanded
		self.advPanel.Show(expanded)
		self.advPanel.Enable(expanded)  # a hidden panel's children can stay in a dialog's tab order
		# Translators: State word in the "Advanced search ({state})" button label (panel open / closed).
		state = _("expanded") if expanded else _("collapsed")
		# Translators: Button that shows or hides the advanced search fields. {state} is "expanded" or "collapsed".
		self.advBtn.SetLabel(_("Ad&vanced search ({state})").format(state=state))
		if fit:
			self.Layout()
			self.Fit()

	def _toggle_adv(self):
		self._set_adv(not self._advExpanded)
		if self._advExpanded:
			self.exclude.SetFocus()

	def _save_order(self):
		if self._config is not None:
			self._config.set(FILTER_ORDER_KEY, ",".join(key for key, _text in self._incl_filters))
			self._config.set(OMIT_ORDER_KEY, ",".join(key for key, _text in self._omit_filters))

	def _move_filter(self, box, step):
		"""Shift+Up/Down: move the highlighted filter in this list only. Each list has its own order."""
		filters = self._incl_filters if box is self.incl else self._omit_filters
		i = box.GetSelection()
		if i == wx.NOT_FOUND:
			return
		j = i + step
		if not 0 <= j < len(filters):
			soundpack.play("boundary")
			return
		first, second = box.IsChecked(i), box.IsChecked(j)
		text_i, text_j = box.GetString(i), box.GetString(j)
		box.SetString(i, text_j)
		box.SetString(j, text_i)
		box.Check(i, second)
		box.Check(j, first)
		box.SetSelection(j)
		filters[i], filters[j] = filters[j], filters[i]
		self._save_order()
		# Translators: Announced after Shift+Up or Down moves a filter in the advanced search. {number} is its new position, {total} how many filters there are.
		ui.message(_("{number} of {total}").format(number=j + 1, total=len(filters)))

	def on_char_hook(self, event):
		key = event.GetKeyCode()
		if (
			key in (wx.WXK_UP, wx.WXK_DOWN) and event.ShiftDown() and not (event.ControlDown() or event.AltDown())
			and self.FindFocus() in (self.incl, self.omit)
		):
			self._move_filter(self.FindFocus(), -1 if key == wx.WXK_UP else 1)
			return
		event.Skip()

	def _problem(self, control, message):
		ui.message(message)
		self._set_adv(True)
		control.SetFocus()

	def on_add_suggestion(self, event):
		indexes = list(self.suggest.CheckedItems)
		if not indexes and self.suggest.GetSelection() != wx.NOT_FOUND:
			indexes = [self.suggest.GetSelection()]  # nothing checked: the highlighted one
		items = [self._suggestions[i] for i in indexes]
		if not items:
			return
		current = self.query.GetValue().strip()
		added = ", ".join(items)
		self.query.SetValue(current + ", " + added if current else added)
		self.query.SetInsertionPointEnd()
		self.query.SetFocus()
		ui.message(_("Added {item}.").format(item=added))

	def _fields(self):
		return {key: ctrl.GetValue().strip() for key, ctrl in self.adv.items()}

	def on_ok(self, event):
		fields = self._fields()
		dates = {}
		for key in ("since", "until"):
			if not fields[key]:
				continue
			try:
				if not self._DATE.match(fields[key]):
					raise ValueError
				dates[key] = datetime.date.fromisoformat(fields[key])
			except ValueError:
				self._problem(self.adv[key], _("Dates must look like 2026-10-06."))
				return
		if len(dates) == 2 and dates["since"] >= dates["until"]:
			self._problem(self.adv["since"], _("Since must be earlier than Until."))
			return
		for key in ("likes", "retweets", "replies"):
			if fields[key] and not fields[key].isdigit():
				self._problem(self.adv[key], _("Minimum counts must be whole numbers."))
				return
		include = [self._incl_filters[i][0] for i in self.incl.CheckedItems]
		omit = [self._omit_filters[i][0] for i in self.omit.CheckedItems]
		both = set(include) & set(omit)
		if both:
			name = next(text for key, text in SEARCH_FILTERS if key in both)
			# Translators: Shown when a filter is checked in both lists. {name} is the filter.
			self._problem(self.omit, _("{name} is both included and left out.").format(name=name))
			return
		query = build_search_query(self.query.GetValue(), self.exclude.GetValue(), include, omit, fields)
		if not query:
			ui.message(_("Type something to search for."))
			self.query.SetFocus()
			return
		if len(query) > self.MAX_QUERY:
			# Translators: Shown when the search is longer than X accepts. {length} is its length, {max} the limit.
			self._problem(self.query, _("The search is too long for X ({length} of {max} characters).").format(
				length=len(query), max=self.MAX_QUERY,
			))
			return
		self._result = query
		self.EndModal(wx.ID_OK)

	def values(self):
		"""(the query exactly as sent to X, Latest or Top)."""
		return self._result, self.PRODUCTS[self.order.GetSelection()]