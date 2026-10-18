# -*- coding: utf-8 -*-
# autoread.py: auto read. Reads the unread tweets of the last tab one after another (oldest unread
# first, as Space picks them), waiting a few seconds after each one. No spoken messages of its own:
# start and stop are sounds (auto_start, auto_stop), so there is nothing to translate.
#
# Main window open: it does what the reader does with Space (the tab's _focus_next_unread), and
# paces itself by the speech: pre_speech says the row started, synthDoneSpeaking says it ended.
# It never cancels or speaks by itself there. Main window closed: the tweets come from the cache,
# the read state and the place go back into it, and the text is spoken by us.
#
# It stops when the speech of a row is cut, when it is toggled again, or when nothing is left.
# Host hooks it needs: plugin.account_id(), .config, .store, .live_main(), .tray_refresh(),
# .unread_invalidate(ident); a tab's _focus_next_unread(), _posts, _current_post_id().

import time

import config as nvdaConfig
import speech
import wx
from logHandler import log
from speech.commands import CallbackCommand, ConfigProfileTriggerCommand
from synthDriverHandler import synthDoneSpeaking

from . import soundpack, tabstate
from .utils import FOCUS_KEY, TAG, cell_texts, is_pinned, row_sound_events, row_speech

try:
	from speech.extensions import pre_speech, pre_speechCanceled
except ImportError:  # an NVDA without these extension points: only the detection of a cut speech is lost
	pre_speech = pre_speechCanceled = None

SORTED_KINDS = ("notifications", "user", "search", "list")  # ordered by the time of the tweet
SIGNAL_GUARD = 1.0       # seconds: the toggle key itself may cut the speech and stop us just before it runs
SETTLE_SECONDS = 0.6     # cancels right after we moved the list focus belong to NVDA, not to the reader
START_TIMEOUT_MS = 2500  # main window: no speech began after the step, go on anyway
PROFILE_TRIGGER_SPEC = "tweetAccessAutoRead"  # key of our private profile trigger

# phases: idle, waiting (a row was picked, speech not begun), speaking, pause (waiting for the next step)


def _active_profile_names():
	return [getattr(p, "name", None) or "base" for p in nvdaConfig.conf.profiles]


class AutoReader:
	def __init__(self, plugin):
		self._plugin = plugin
		self.active = False
		self._mode = None
		self._phase = "idle"
		self._own = 0  # our own speech calls in progress: not an interruption
		self._timer = None
		self._ignore_until = 0.0
		self._interrupted = 0.0
		self._last_id = None
		self._panel = None  # main window: the tab whose row is shown in full
		self._trigger = None  # private profile trigger while a voice profile is in use
		self._profile_name = None
		self._previous_spec = None  # what the trigger spec was mapped to before we took it

	# ---- start / stop

	def toggle(self):
		if self.active:
			self.stop()
		elif time.time() - self._interrupted > SIGNAL_GUARD:
			self.start()

	def start(self):
		self.active = True
		self._phase = "idle"
		self._last_id = None
		synthDoneSpeaking.register(self._on_synth_done)
		if pre_speech is not None:
			pre_speech.register(self._on_pre_speech)
		if pre_speechCanceled is not None:
			pre_speechCanceled.register(self._on_canceled)
		self._arm_profile()
		soundpack.play("auto_start")
		self._timer = wx.CallLater(400, self._step)

	def stop(self):
		if not self.active:
			return
		self.active = False
		self._phase = "idle"
		self._cancel_timer()
		self._collapse()
		synthDoneSpeaking.unregister(self._on_synth_done)
		if pre_speech is not None:
			pre_speech.unregister(self._on_pre_speech)
		if pre_speechCanceled is not None:
			pre_speechCanceled.unregister(self._on_canceled)
		soundpack.play("auto_stop")
		self._release_profile()

	# ---- voice profile (Settings > General): its voice is used while auto read runs

	def _arm_profile(self):
		self._trigger = None
		name = self._plugin.config.get("auto_profile")
		if not name:
			return
		try:
			if name not in [n for n in nvdaConfig.conf.listProfiles() if n] or name in _active_profile_names():
				return  # deleted since, or its voice applies anyway
			mapping = nvdaConfig.conf.triggersToProfiles

			class AutoReadTrigger(nvdaConfig.ProfileTrigger):
				spec = PROFILE_TRIGGER_SPEC

			self._previous_spec = mapping.get(PROFILE_TRIGGER_SPEC)
			mapping[PROFILE_TRIGGER_SPEC] = name
			self._profile_name = name
			self._trigger = AutoReadTrigger()
		except Exception:
			log.error(TAG + "auto read voice profile unavailable", exc_info=True)
			self._trigger = None

	def _profile_active(self):
		return self._trigger is not None and self._profile_name in _active_profile_names()

	def _voice(self, on):
		"""Main window open: NVDA reads the rows itself, so the profile is switched on for the whole
		run. Main window closed: it is wrapped around our own speech row by row (see _say), so it is
		switched off here. Switching it can cut speech, which must not count as an interruption."""
		trigger = self._trigger
		if trigger is None or on == self._profile_active():
			return
		self._own += 1
		try:
			if on:
				trigger.enter()
			else:
				trigger.exit()
		except Exception:
			log.error(TAG + "could not switch the auto read voice profile", exc_info=True)
		finally:
			self._own -= 1
			self._ignore_until = max(self._ignore_until, time.time() + SETTLE_SECONDS)

	def _release_profile(self):
		"""Leave the profile (speech cut before its exit command ran leaves it on) and give the trigger back."""
		self._voice(False)
		if self._trigger is None:
			return
		self._trigger = None
		try:
			mapping = nvdaConfig.conf.triggersToProfiles
			if self._previous_spec is None:
				mapping.pop(PROFILE_TRIGGER_SPEC, None)
			else:
				mapping[PROFILE_TRIGGER_SPEC] = self._previous_spec
		except Exception:
			log.error(TAG + "could not restore the profile triggers", exc_info=True)

	def _cancel_timer(self):
		if self._timer is not None:
			self._timer.Stop()
			self._timer = None

	def _collapse(self):
		panel, self._panel = self._panel, None
		if panel is None:
			return
		try:
			panel.collapse_rows()
		except RuntimeError:
			pass  # the tab is gone already

	def _interrupt(self):
		"""The speech of a row was cut: the reader is doing something else."""
		self._interrupted = time.time()
		wx.CallAfter(self.stop)

	# ---- listening to the speech

	def _quiet_time(self):
		return time.time() > self._ignore_until

	def _on_pre_speech(self, *args, **kwargs):
		if not self.active:
			return
		if self._mode == "main":
			if self._phase == "waiting":
				self._phase = "speaking"
				self._cancel_timer()
			return
		if not self._own and self._quiet_time():
			self._interrupt()  # cache mode: something else speaks

	def _on_canceled(self, *args, **kwargs):
		if not self.active or self._own or not self._quiet_time():
			return
		if self._phase in ("waiting", "speaking") and (self._mode != "main" or self._phase == "speaking"):
			self._interrupt()

	def _on_synth_done(self, *args, **kwargs):
		if self.active and self._phase == "speaking":
			wx.CallAfter(self._after_spoken)

	def _on_spoken(self):
		wx.CallAfter(self._after_spoken)  # callback of our own speech (cache mode); may run on another thread

	def _after_spoken(self):
		if not self.active or self._phase != "speaking":
			return
		self._phase = "pause"
		seconds = self._plugin.config.get("auto_pause")
		self._timer = wx.CallLater(int(seconds * 1000), self._step)

	def _no_speech(self):
		"""Main window: the row made no speech at all (empty text, silent synthesizer)."""
		self._timer = None
		if self.active and self._phase == "waiting":
			self._phase = "speaking"
			self._after_spoken()

	# ---- one step

	def _step(self):
		self._timer = None
		if not self.active:
			return
		try:
			panel = self._main_panel()
			more = self._step_main(panel) if panel is not None else self._step_cache()
		except Exception:
			log.error(TAG + "auto read failed", exc_info=True)
			self.stop()
			return
		if not more:
			soundpack.play("boundary")
			self.stop()

	def _identity(self, aid):
		return tabstate.get_last(self._plugin.store, aid) or {"kind": "permanent", "key": "home"}

	# ---- main window open: as if the reader pressed Space

	def _main_panel(self):
		aid = self._plugin.account_id()
		main = self._plugin.live_main()
		if aid is None or main is None:
			return None
		identity = self._identity(aid)
		for panel in main.getOpenTabs():
			if main._getTabIdentity(panel) == identity and callable(getattr(panel, "_focus_next_unread", None)):
				return panel
		return None

	def _step_main(self, panel):
		self._mode = "main"
		if not any(not p.is_read for p in panel._posts.rows):
			return False
		self._voice(True)
		self._collapse()  # the row read before goes back to the cut cell
		if self._plugin.config.get("auto_full"):
			idx = panel._next_unread_index()
			if idx is not None:
				panel.expand_row(idx)  # before the focus moves: NVDA reads the cell as it is then
				self._panel = panel
		self._phase = "waiting"
		self._ignore_until = time.time() + SETTLE_SECONDS
		panel._focus_next_unread()
		current = panel._current_post_id()
		if current is not None and current == self._last_id:
			self._phase = "idle"  # the same row again: nothing moves on in this tab
			return False
		self._last_id = current
		self._timer = wx.CallLater(START_TIMEOUT_MS, self._no_speech)
		return True

	# ---- main window closed: the cache

	def _say(self, text):
		trigger = self._trigger
		if trigger is None:
			sequence = [text, CallbackCommand(self._on_spoken)]
		else:
			sequence = [
				ConfigProfileTriggerCommand(trigger, True),
				text,
				ConfigProfileTriggerCommand(trigger, False),
				CallbackCommand(self._on_spoken),
			]
		self._own += 1
		try:
			speech.speak(sequence)
		finally:
			self._own -= 1

	def _step_cache(self):
		self._mode = "cache"
		self._voice(False)  # the main window was closed meanwhile: from here the profile wraps each row
		aid = self._plugin.account_id()
		if aid is None:
			return False
		item = self._next_from_cache(aid, self._identity(aid))
		if item is None:
			return False
		text, events, _moved = item
		for event in events:
			soundpack.play_now(event)
		self._phase = "speaking"
		self._say(text)
		return True

	def _compose(self, post):
		cfg = self._plugin.config
		full = bool(cfg.get("auto_full"))
		columns = ["embed", "author", "message", "time"] if cfg.get("embed_first") else ["author", "message", "time", "embed"]
		texts = cell_texts(post, cfg.get("author_mode"), cfg.get("time_mode"), cfg.get("time_pattern"), full=full)
		return row_speech(texts, columns), row_sound_events(post, full=full), False

	def _next_home(self, aid):
		store = self._plugin.store
		post = store.oldest_unread(aid)
		if post is None:
			return None
		store.mark_read(aid, [post.id], True)
		store.set_account_meta(aid, FOCUS_KEY, str(post.id))  # where the reader is when the window opens again
		self._plugin.tray_refresh()
		return self._compose(post)

	def _next_from_cache(self, aid, identity):
		plugin = self._plugin
		store = plugin.store
		kind, key = identity["kind"], identity["key"]
		if kind == "users":
			return None  # a list of users has no tweets
		if kind == "permanent":
			if key == "home":
				return self._next_home(aid)
			if key not in ("notifications", "bookmarks"):
				return None  # the Lists tab has no tweets
			kind = cache_type = cache_key = ident = key
		else:
			cache_type, cache_key = kind, key
			ident = "%s:%s" % (kind, key)
		posts = tabstate.get_cache(store, aid, cache_type, cache_key) or []
		if kind in SORTED_KINDS:
			# oldest first; a pinned tweet counts as the newest, the way the list shows it
			scan = sorted(posts, key=lambda p: (is_pinned(p), p.id))
		elif kind == "thread":
			scan = posts  # top to bottom: the tweet that started it, then the replies
		else:
			scan = list(reversed(posts))  # quotes, bookmarks: given newest first
		pick = next((p for p in scan if not p.is_read), None)
		if pick is None:
			return None
		pick.is_read = True
		tabstate.set_cache(store, aid, cache_type, cache_key, posts)
		store.set_account_meta(aid, "tabfocus:%s:%s" % (cache_type, cache_key), str(pick.id))
		plugin.unread_invalidate(ident)
		return self._compose(pick)