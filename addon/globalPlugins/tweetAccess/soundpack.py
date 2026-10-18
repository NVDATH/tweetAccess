# -*- coding: utf-8 -*-
# NVTweet - soundpack.py: event sounds and a looped progress sound from SoundPack\<pack>\<event>.wav.
# A pack need not be complete: a missing file stays silent (the progress sound falls back to a beep).

import os
import threading
import time

import nvwave
import tones
from logHandler import log

from .utils import ADDON_DIR, TAG

SOUND_PACK_DIR = os.path.join(ADDON_DIR, "SoundPack")
SILENT_PACK = "-"      # stored when the user picks "Silent" (an empty value reads as "never set")
DEFAULT_PACK = "default"
PROGRESS_KEY = "progress"
PROGRESS_MAX_SECONDS = 180  # safety net for a start_progress nobody stopped

# File names inside a pack: <key>.wav
EVENT_KEYS = [
	"like", "unlike", "bookmark", "unbookmark", "retweet", "unretweet", "send_post", "max_length",
	"auto_start", "auto_stop",
	"open_tab", "close_tab", "boundary", "error", "ready", "notification",
	"embed_image", "embed_video", "embed_gif", "embed_link", "embed_quote", "read_more",
	"main_open", "main_close",
]


def list_packs():
	try:
		return sorted(
			name for name in os.listdir(SOUND_PACK_DIR)
			if os.path.isdir(os.path.join(SOUND_PACK_DIR, name))
		)
	except OSError:
		return []


def _sound_path(pack, key):
	path = os.path.join(SOUND_PACK_DIR, pack, key + ".wav")
	return path if os.path.isfile(path) else None


class SoundEngine:
	def __init__(self, config):
		self._cfg = config
		self._silent = True
		self._enabled = set()
		self._paths = {}
		self._progress_path = None
		self._lock = threading.Lock()
		self._count = 0
		self._stop = threading.Event()
		self.reload()

	def reload(self):
		pack = self._cfg.get("sound_pack")
		self._pack = pack
		disabled = {k for k in self._cfg.get("sound_disabled").split(",") if k}
		self._enabled = set(EVENT_KEYS) - disabled
		self._paths = {}
		self._progress_path = None
		self._silent = not pack or pack == SILENT_PACK or not os.path.isdir(os.path.join(SOUND_PACK_DIR, pack))
		if self._silent:
			return
		for key in EVENT_KEYS:
			path = _sound_path(pack, key)
			if path:
				self._paths[key] = path
		self._progress_path = _sound_path(pack, PROGRESS_KEY)

	def _path_for(self, key):
		"""Path of a sound. A file added to the pack after start-up is picked up the first time it is needed."""
		path = self._paths.get(key)
		if path is None and not self._silent:
			path = _sound_path(self._pack, key)
			if path:
				self._paths[key] = path
		return path

	def _lazy_progress(self):
		if self._silent:
			return None
		self._progress_path = _sound_path(self._pack, PROGRESS_KEY)
		return self._progress_path

	def play(self, key, asynchronous=True):
		if key not in self._enabled:
			return
		path = self._path_for(key)
		if not path:
			return
		try:
			nvwave.playWaveFile(path, asynchronous=asynchronous)
		except Exception:
			log.error(TAG + "could not play " + path, exc_info=True)

	# ---- looped progress sound; counted: every start needs one stop

	def start_progress(self):
		if self._silent:
			return
		with self._lock:
			self._count += 1
			if self._count > 1:
				return
			stop = threading.Event()
			self._stop = stop
		threading.Thread(
			target=self._progress_worker, args=(self._progress_path or self._lazy_progress(), stop), daemon=True,
		).start()

	def stop_progress(self):
		with self._lock:
			if self._count == 0:
				return
			self._count -= 1
			if self._count == 0:
				self._stop.set()

	def reset_progress(self):
		with self._lock:
			self._count = 0
			self._stop.set()

	def _progress_worker(self, path, stop):
		beep = not path
		deadline = time.time() + PROGRESS_MAX_SECONDS
		while not stop.is_set():
			if time.time() > deadline:
				log.info(TAG + "progress sound ended by the safety timeout")
				with self._lock:
					if self._stop is stop:
						self._count = 0
				stop.set()
				return
			if beep:
				tones.beep(500, 50)
			else:
				try:
					nvwave.playWaveFile(path)
				except Exception:
					log.error(TAG + "could not play the progress sound", exc_info=True)
					beep = True
					continue
			stop.wait(1.0)


_engine = None


def init(config):
	global _engine
	_engine = SoundEngine(config)


def reload():
	if _engine is not None:
		_engine.reload()


def play(key):
	if _engine is not None:
		_engine.play(key)


def play_now(key):
	"""Blocks until the sound ends: used on focus, so it lands before NVDA speaks the row."""
	if _engine is not None:
		_engine.play(key, asynchronous=False)


def start_progress():
	if _engine is not None:
		_engine.start_progress()


def stop_progress():
	if _engine is not None:
		_engine.stop_progress()


def reset_progress():
	if _engine is not None:
		_engine.reset_progress()