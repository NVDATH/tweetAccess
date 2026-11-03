# -*- coding: utf-8 -*-
# tweetAccess - explore.py: the Search and explore tab, after NVSky's Explore window. One search field and the
# kind of result: tweets (the search opens in a tab of its own; the advanced part is the old search window's),
# people (a tab with the users found) or trends (those of X's Explore page or of a place, in a list here).
# Nothing is searched while typing: every request counts, so a search starts with Enter or its button.

import datetime
import json
import re

import addonHandler
import ui
import wx
from logHandler import log

import gui.nvdaControls

from . import soundpack
from .utils import TAG, build_search_query, safe_ui

addonHandler.initTranslation()

FILTER_ORDER_KEY = "search_filter_order"  # config key: order of the Include list
OMIT_ORDER_KEY = "search_omit_order"      # config key: order of the Leave out list
STATE_KEY = "explore_state"               # account meta: the kind of result, the trends and the place shown last
PLACES_KEY = "places_cache"               # account meta: the places X has trends for
CATEGORIES = ("trending", "for-you", "news", "sports", "entertainment")
WORLDWIDE = 1

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


class PlaceDialog(wx.Dialog):
	"""Choose the place whose trends are shown: a filter box and the list of the places that match it."""

	def __init__(self, parent, places):
		# Translators: Title of the window that picks the place of the trends.
		super().__init__(parent, title=_("Choose a place"))
		self._places = places  # [{"name", "woeid", "country"}]
		self._shown = []
		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label of the filter box of the place window.
		sizer.Add(wx.StaticText(self, label=_("&Filter:")), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
		self.filter = wx.TextCtrl(self)
		sizer.Add(self.filter, 0, wx.EXPAND | wx.ALL, 10)
		# Translators: Label of the list of places of the place window.
		sizer.Add(wx.StaticText(self, label=_("&Places:")), 0, wx.LEFT | wx.RIGHT, 10)
		self.list = wx.ListBox(self, size=(380, 240))
		sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)
		sizer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)
		self.SetSizerAndFit(sizer)
		self.CentreOnParent()
		self.filter.Bind(wx.EVT_TEXT, self._refill)
		self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self.EndModal(wx.ID_OK))
		self._refill(None)
		self.filter.SetFocus()

	def _refill(self, event):
		text = self.filter.GetValue().strip().lower()
		self._shown = [p for p in self._places if text in ("%s %s" % (p["name"], p.get("country") or "")).lower()]
		self.list.Set([p["name"] + (", " + p["country"] if p.get("country") else "") for p in self._shown])
		if self._shown:
			self.list.SetSelection(0)

	def picked(self):
		index = self.list.GetSelection()
		return self._shown[index] if 0 <= index < len(self._shown) else None


class ExploreTab(wx.Panel):
	TAB_KEY = "explore"
	PRODUCTS = ("Latest", "Top")
	MAX_QUERY = 500  # X's limit for a search
	_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._plugin = plugin
		self._cfg = plugin.config
		self._aid = plugin.account_id()
		self._dead = False
		# Translators: Name of the Search and explore tab.
		self.TAB_NAME = _("Search and explore")
		self._types = ("tweets", "people", "trends")
		self._adv_open = False
		self._incl = ordered_filters(self._cfg.get(FILTER_ORDER_KEY))
		self._omit = ordered_filters(self._cfg.get(OMIT_ORDER_KEY))
		self._rows = []  # the trends shown: name, query, note
		self._sources = []  # (kind, value, label) of the trends choice
		self._previous_source = 0
		state = self._state()
		self._place = state.get("place") if isinstance(state.get("place"), dict) else None

		sizer = wx.BoxSizer(wx.VERTICAL)
		self._sizer = sizer
		row = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label of the search field of the Search and explore tab.
		row.Add(wx.StaticText(self, label=_("&Search:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
		self.query = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
		row.Add(self.query, 1)
		self._queryRow = row
		sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)
		self.typeRadio = wx.RadioBox(
			# Translators: Label of the radio group of the kind of result in the Search and explore tab.
			self, label=_("Result t&ype"),
			# Translators: Choices of the kind of result in the Search and explore tab.
			choices=[_("Tweets"), _("People"), _("Trends")], majorDimension=1, style=wx.RA_SPECIFY_ROWS,
		)
		sizer.Add(self.typeRadio, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		self._build_tweets()
		self._build_trends()
		# Translators: Button that starts the search of the Search and explore tab.
		self.goBtn = wx.Button(self, label=_("&Go"))
		sizer.Add(self.goBtn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
		self.status = wx.StatusBar(self)
		# Translators: Accessible name of the status bar of the Search and explore tab.
		self.status.SetName(_("Search and explore status"))
		sizer.Add(self.status, 0, wx.EXPAND)
		self.SetSizer(sizer)

		kind = state.get("type")
		self.typeRadio.SetSelection(self._types.index(kind) if kind in self._types else 0)
		self._fill_sources(tuple(state["source"]) if isinstance(state.get("source"), list) else None)
		self._rows = self._cache_get(self._source_key()) or []
		self._render_trends()
		self.prepare([])
		self._show_type()

		self.query.Bind(wx.EVT_TEXT_ENTER, self.on_search)
		self.typeRadio.Bind(wx.EVT_RADIOBOX, self.on_type)
		self.goBtn.Bind(wx.EVT_BUTTON, self.on_search)
		self.advBtn.Bind(wx.EVT_BUTTON, lambda e: self._toggle_adv())
		self.suggestBtn.Bind(wx.EVT_BUTTON, self.on_add_suggestion)
		self.source.Bind(wx.EVT_CHOICE, self.on_source)
		self.trendList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_trend_activated)
		self.openBtn.Bind(wx.EVT_BUTTON, self.on_trend_activated)
		self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

	# ------------------------------------------------------------ building

	def _build_tweets(self):
		"""What goes with a search for tweets: suggestions from a tweet, the order and the advanced part."""
		panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
		panel.SetScrollRate(0, 20)
		self.tweetsPanel = panel
		box = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label of the checklist of hashtags and @users taken from the tweet in focus.
		self.suggestLabel = wx.StaticText(panel, label=_("From this t&weet:"))
		self.suggest = gui.nvdaControls.CustomCheckListBox(panel, choices=[], size=(-1, 90))
		# Translators: Button that adds the checked hashtags and users to the search.
		self.suggestBtn = wx.Button(panel, label=_("&Add checked"))
		box.Add(self.suggestLabel, 0, wx.TOP, 4)
		box.Add(self.suggest, 0, wx.EXPAND | wx.BOTTOM, 5)
		box.Add(self.suggestBtn, 0, wx.BOTTOM, 8)
		# Translators: Label of the search order choice.
		box.Add(wx.StaticText(panel, label=_("O&rder:")), 0)
		# Translators: Search order choices.
		self.order = wx.Choice(panel, choices=[_("Latest tweets first"), _("Top tweets")])
		self.order.SetSelection(0)
		box.Add(self.order, 0, wx.BOTTOM, 8)
		# A plain button plus a panel, not wx.CollapsiblePane (its children make NVDA read the label twice).
		self.advBtn = wx.Button(panel, label="")
		box.Add(self.advBtn, 0, wx.BOTTOM, 8)
		self.advPanel = wx.Panel(panel)
		adv = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label of the words to leave out of a search (commas separate them).
		adv.Add(wx.StaticText(self.advPanel, label=_("E&xclude words (separate several with commas):")), 0, wx.BOTTOM, 3)
		self.exclude = wx.TextCtrl(self.advPanel)
		adv.Add(self.exclude, 0, wx.EXPAND | wx.BOTTOM, 5)
		# Translators: Label of the checklist of filters a tweet must match. Shift+Up and Shift+Down move the highlighted filter.
		adv.Add(wx.StaticText(self.advPanel, label=_("&Include only tweets that have (Shift+Up or Down moves an item):")), 0, wx.BOTTOM, 3)
		self.incl = gui.nvdaControls.CustomCheckListBox(
			self.advPanel, choices=[text for _key, text in self._incl], size=(-1, 110)
		)
		self.incl.SetSelection(0)
		adv.Add(self.incl, 0, wx.EXPAND | wx.BOTTOM, 5)
		# Translators: Label of the checklist of filters that make a tweet be left out. Shift+Up and Shift+Down move the highlighted filter.
		adv.Add(wx.StaticText(self.advPanel, label=_("E&xclude tweets that have (Shift+Up or Down moves an item):")), 0, wx.BOTTOM, 3)
		self.omit = gui.nvdaControls.CustomCheckListBox(
			self.advPanel, choices=[text for _key, text in self._omit], size=(-1, 110)
		)
		self.omit.SetSelection(0)
		adv.Add(self.omit, 0, wx.EXPAND | wx.BOTTOM, 5)
		self.adv = {}

		def field(label_text, key):
			line = wx.BoxSizer(wx.HORIZONTAL)
			line.Add(wx.StaticText(self.advPanel, label=label_text), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
			ctrl = wx.TextCtrl(self.advPanel)
			line.Add(ctrl, 1)
			adv.Add(line, 0, wx.EXPAND | wx.BOTTOM, 5)
			self.adv[key] = ctrl

		# Translators: Labels of the advanced search fields. The date format is fixed.
		field(_("&From user name:"), "from")
		field(_("&To user name (replies to):"), "to")
		field(_("&Mentioning user name:"), "mention")
		field(_("Language (such as en):"), "lang")
		field(_("Since (YYYY-MM-DD):"), "since")
		field(_("Until (YYYY-MM-DD):"), "until")
		field(_("Minimum li&kes:"), "likes")
		field(_("Minimum retwee&ts:"), "retweets")
		field(_("Minimum rep&lies:"), "replies")
		self.advPanel.SetSizer(adv)
		box.Add(self.advPanel, 0, wx.EXPAND)
		panel.SetSizer(box)
		self._set_adv(False)
		self._sizer.Add(panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

	def _build_trends(self):
		"""What goes with trends: where they come from, and the list."""
		panel = wx.Panel(self)
		self.trendsPanel = panel
		box = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label of the choice of where the trends come from.
		box.Add(wx.StaticText(panel, label=_("Tre&nds of:")), 0)
		self.source = wx.Choice(panel, choices=[])
		box.Add(self.source, 0, wx.BOTTOM, 8)
		self.trendList = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
		# Translators: Column headers of the list of trends.
		self.trendList.InsertColumn(0, _("Trend"), width=360)
		self.trendList.InsertColumn(1, _("About"), width=320)
		box.Add(self.trendList, 1, wx.EXPAND | wx.BOTTOM, 8)
		# Translators: Button that searches for the trend in focus.
		self.openBtn = wx.Button(panel, label=_("&Open search"))
		box.Add(self.openBtn, 0)
		panel.SetSizer(box)
		self._sizer.Add(panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

	# ------------------------------------------------------------ the kind of result

	def _type(self):
		return self._types[max(0, self.typeRadio.GetSelection())]

	def _show_type(self):
		kind = self._type()
		trends = kind == "trends"
		self._sizer.Show(self._queryRow, not trends, True)
		self.query.Enable(not trends)
		for panel, shown in ((self.tweetsPanel, kind == "tweets"), (self.trendsPanel, trends)):
			panel.Show(shown)
			panel.Enable(shown)
		self.goBtn.Show(not trends)
		self.goBtn.Enable(not trends)
		self._update_status()
		self.Layout()

	def on_type(self, event):
		self._show_type()
		self._save_state()
		if self._type() == "trends" and not self._rows:
			self._load_trends()

	def _update_status(self):
		if self._type() == "trends":
			# Translators: Status bar of the trends of the Search and explore tab. {count} is the number of trends.
			self.status.SetStatusText(_("{count} trends").format(count=len(self._rows)))
		else:
			self.status.SetStatusText("")

	# ------------------------------------------------------------ state kept between runs

	def _state(self):
		try:
			data = json.loads(self._plugin.store.get_account_meta(self._aid, STATE_KEY) or "{}")
		except ValueError:
			return {}
		return data if isinstance(data, dict) else {}

	def _save_state(self):
		kind, value, _label = self._sources[self.source.GetSelection()] if self._sources else ("category", "trending", "")
		state = {"type": self._type(), "source": [kind, value], "place": self._place}
		try:
			self._plugin.store.set_account_meta(self._aid, STATE_KEY, json.dumps(state))
		except Exception:
			log.error(TAG + "could not save the state of the search and explore tab", exc_info=True)

	def flush_state(self):
		self._save_state()

	# ------------------------------------------------------------ search for tweets and people

	def prepare(self, suggestions):
		"""Ctrl+F: the hashtags and users of the tweet in focus can be added to the search."""
		items = list(suggestions)
		self.suggest.Set(items)
		if items:
			self.suggest.SetSelection(0)
		for ctrl in (self.suggestLabel, self.suggest, self.suggestBtn):
			ctrl.Show(bool(items))
			ctrl.Enable(bool(items))
		self.tweetsPanel.Layout()
		self.tweetsPanel.FitInside()
		self._suggestions = items

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

	def _set_adv(self, expanded):
		self._adv_open = expanded
		self.advPanel.Show(expanded)
		self.advPanel.Enable(expanded)  # a hidden panel's children can stay in the tab order
		# Translators: State word in the "Advanced search ({state})" button label (panel open / closed).
		state = _("expanded") if expanded else _("collapsed")
		# Translators: Button that shows or hides the advanced search fields. {state} is "expanded" or "collapsed".
		self.advBtn.SetLabel(_("Ad&vanced search ({state})").format(state=state))
		self.tweetsPanel.Layout()
		self.tweetsPanel.FitInside()

	def _toggle_adv(self):
		self._set_adv(not self._adv_open)
		if self._adv_open:
			self.exclude.SetFocus()

	def _save_order(self):
		self._cfg.set(FILTER_ORDER_KEY, ",".join(key for key, _text in self._incl))
		self._cfg.set(OMIT_ORDER_KEY, ",".join(key for key, _text in self._omit))

	def _move_filter(self, box, step):
		"""Shift+Up/Down: move the highlighted filter in this list only. Each list has its own order."""
		filters = self._incl if box is self.incl else self._omit
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
		focus = self.FindFocus()
		if (
			key in (wx.WXK_UP, wx.WXK_DOWN) and event.ShiftDown() and not (event.ControlDown() or event.AltDown())
			and focus in (self.incl, self.omit)
		):
			self._move_filter(focus, -1 if key == wx.WXK_UP else 1)
			return
		event.Skip()

	def _problem(self, control, message):
		ui.message(message)
		self._set_adv(True)
		control.SetFocus()

	def _tweets_query(self):
		"""The text for X from the keywords and the advanced part, or None after saying what is wrong."""
		fields = {key: ctrl.GetValue().strip() for key, ctrl in self.adv.items()}
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
				return None
		if len(dates) == 2 and dates["since"] >= dates["until"]:
			self._problem(self.adv["since"], _("Since must be earlier than Until."))
			return None
		for key in ("likes", "retweets", "replies"):
			if fields[key] and not fields[key].isdigit():
				self._problem(self.adv[key], _("Minimum counts must be whole numbers."))
				return None
		include = [self._incl[i][0] for i in self.incl.CheckedItems]
		omit = [self._omit[i][0] for i in self.omit.CheckedItems]
		both = set(include) & set(omit)
		if both:
			name = next(text for key, text in SEARCH_FILTERS if key in both)
			# Translators: Shown when a filter is checked in both lists. {name} is the filter.
			self._problem(self.omit, _("{name} is both included and left out.").format(name=name))
			return None
		query = build_search_query(self.query.GetValue(), self.exclude.GetValue(), include, omit, fields)
		if not query:
			ui.message(_("Type something to search for."))
			self.query.SetFocus()
			return None
		if len(query) > self.MAX_QUERY:
			# Translators: Shown when the search is longer than X accepts. {length} is its length, {max} the limit.
			self._problem(self.query, _("The search is too long for X ({length} of {max} characters).").format(
				length=len(query), max=self.MAX_QUERY,
			))
			return None
		form = {
			"keywords": self.query.GetValue(), "exclude": self.exclude.GetValue(), "include": include, "omit": omit,
			"fields": fields, "order": self.order.GetSelection(),
		}
		return query, form

	def on_search(self, event=None):
		kind = self._type()
		if kind == "trends":
			self._load_trends(refresh=True)
			return
		if self._plugin.account_id() is None:
			ui.message(_("Add an account first."))
			return
		main = self.GetTopLevelParent()
		if kind == "people":
			query = self.query.GetValue().strip()
			if not query:
				ui.message(_("Type something to search for."))
				self.query.SetFocus()
				return
			main.open_people_search(query)
			return
		built = self._tweets_query()
		if built:
			query, form = built
			main.open_search(query, self.PRODUCTS[self.order.GetSelection()], form)

	def load_search(self, params):
		"""Edit a search that was run: its text and filters go into the form, to change and run again. A search
		that was not made here (a hashtag, a trend) has no form: its text becomes the keywords."""
		form = params.get("form") or {
			"keywords": str(params.get("query") or "").replace(",", " "),  # a comma would split the keywords
			"order": 1 if params.get("product") == "Top" else 0,
		}
		self.typeRadio.SetSelection(self._types.index("tweets"))
		self._show_type()
		self.query.SetValue(form.get("keywords") or "")
		self.exclude.SetValue(form.get("exclude") or "")
		include, omit = set(form.get("include") or ()), set(form.get("omit") or ())
		self.incl.CheckedItems = [i for i, (key, _text) in enumerate(self._incl) if key in include]
		self.omit.CheckedItems = [i for i, (key, _text) in enumerate(self._omit) if key in omit]
		fields = form.get("fields") or {}
		for key, ctrl in self.adv.items():
			ctrl.SetValue(fields.get(key) or "")
		self.order.SetSelection(int(form.get("order") or 0))
		self._set_adv(bool(form.get("exclude") or include or omit or any(fields.values())))
		self._save_state()

	# ------------------------------------------------------------ trends

	def _fill_sources(self, select=None):
		"""The choices of where the trends come from: X's Explore categories, Worldwide, the place chosen last."""
		names = {
			# Translators: Choices of where the trends come from: the categories of X's Explore page.
			"trending": _("Trending"), "for-you": _("For you"), "news": _("News"), "sports": _("Sports"),
			"entertainment": _("Entertainment"),
		}
		sources = [("category", c, names[c]) for c in CATEGORIES]
		# Translators: Choice of where the trends come from: the trends of the whole world.
		sources.append(("place", WORLDWIDE, _("Worldwide")))
		if self._place and self._place.get("woeid") != WORLDWIDE:
			# Translators: Choice of where the trends come from: the place the reader chose. {name} is its name.
			sources.append(("place", self._place["woeid"], _("Place: {name}").format(name=self._place["name"])))
		# Translators: Choice that opens the window to pick another place for the trends.
		sources.append(("choose", None, _("Choose a place...")))
		self._sources = sources
		self.source.Set([label for _kind, _value, label in sources])
		keys = [(kind, value) for kind, value, _label in sources]
		index = keys.index(select) if select in keys else 0
		self.source.SetSelection(index)
		self._previous_source = index

	def _source_key(self):
		kind, value, _label = self._sources[self.source.GetSelection()]
		return "%s:%s" % (kind, value)

	def _cache_get(self, key):
		try:
			data = json.loads(self._plugin.store.get_account_meta(self._aid, "trends:" + key) or "null")
		except ValueError:
			return None
		return data if isinstance(data, list) else None

	def _load_trends(self, refresh=False):
		kind, value, _label = self._sources[self.source.GetSelection()]
		key = self._source_key()
		if not refresh:
			cached = self._cache_get(key)
			if cached is not None:
				self._rows = cached
				self._render_trends()
				return
		options = {"woeid": value} if kind == "place" else {"category": value}
		if not self._plugin.start_job("trends", lambda result: self._trends_loaded(key, result), options):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading, please wait."))

	@safe_ui
	def _trends_loaded(self, key, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		rows = result["trends"]
		try:
			self._plugin.store.set_account_meta(self._aid, "trends:" + key, json.dumps(rows))
		except Exception:
			log.error(TAG + "could not save the trends", exc_info=True)
		if key != self._source_key():
			return  # the reader chose another source meanwhile
		self._rows = rows
		self._render_trends()
		# Translators: Announced after the trends were loaded. {count} is the number of trends.
		ui.message(_("{count} trends.").format(count=len(rows)))

	def _render_trends(self):
		self.trendList.Freeze()
		try:
			self.trendList.DeleteAllItems()
			for i, row in enumerate(self._rows):
				self.trendList.InsertItem(i, row["name"])
				self.trendList.SetItem(i, 1, row["note"])
		finally:
			self.trendList.Thaw()
		if self._rows:
			self.trendList.Focus(0)
			self.trendList.Select(0)
		self._update_status()

	def bg_trends(self, key, rows):
		"""A background check loaded the trends of a source: the list changes if it shows that source and the
		reader is not in it (the cache is fresh either way)."""
		if self._type() == "trends" and key == self._source_key() and rows != self._rows and not self.trendList.HasFocus():
			self._rows = rows
			self._render_trends()

	def on_source(self, event):
		kind = self._sources[self.source.GetSelection()][0]
		if kind == "choose":
			self.source.SetSelection(self._previous_source)
			self._pick_place()
			return
		self._previous_source = self.source.GetSelection()
		self._save_state()
		self._load_trends()

	def _pick_place(self):
		try:
			places = json.loads(self._plugin.store.get_account_meta(self._aid, PLACES_KEY) or "[]")
		except ValueError:
			places = []
		if places:
			self._ask_place(places)
			return
		if not self._plugin.start_job("places", self._places_loaded):
			ui.message(_("Still loading, please wait."))
			return
		ui.message(_("Loading, please wait."))

	@safe_ui
	def _places_loaded(self, result):
		if not result["ok"]:
			soundpack.play("error")
			ui.message(result["message"])
			return
		self._plugin.store.set_account_meta(self._aid, PLACES_KEY, json.dumps(result["places"]))
		self._ask_place(result["places"])

	def _ask_place(self, places):
		dlg = PlaceDialog(self, places)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			place = dlg.picked()
		finally:
			dlg.Destroy()
		if place is None:
			return
		self._place = {"name": place["name"], "woeid": int(place["woeid"])}
		self._fill_sources(("place", self._place["woeid"]))
		self._save_state()
		self._load_trends()
		self.source.SetFocus()

	def on_trend_activated(self, event=None):
		index = self.trendList.GetFocusedItem()
		if not 0 <= index < len(self._rows):
			ui.message(_("No trend selected."))
			return
		self.GetTopLevelParent().open_search(self._rows[index]["query"], "Top")

	# ------------------------------------------------------------ what the main window asks of a tab

	def focus_query(self):
		kind = self._type()
		if kind == "trends":
			(self.trendList if self._rows else self.source).SetFocus()
		else:
			self.query.SetFocus()

	def onTabActivated(self):
		ui.message(_("{name} tab").format(name=self.TAB_NAME))
		self.focus_query()
		if self._type() == "trends" and not self._rows:
			self._load_trends()

	def _restoreFocusPosition(self, moveFocus=True):
		if moveFocus:
			self.focus_query()

	def onCheckForUpdates(self, evt=None):
		self.on_search()

	def refresh(self, done=None, quiet=False):
		if done is not None:  # Ctrl+F5 and the background checks: a search costs requests, only F5 starts one
			done(False)
			return True
		self.on_search()
		return True

	def reload_display(self):
		pass