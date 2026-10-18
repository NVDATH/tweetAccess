# -*- coding: utf-8 -*-
# NVTweet - attachments.py: download media, then open it, send it to Be My Eyes or save it.
#
# Files get readable names ("<author> <text>") and are kept in a temp folder; asking
# for the same URL again reuses the file instead of fetching it from X again.

import ctypes
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request

import addonHandler
import wx

from .utils import APP_NAME

addonHandler.initTranslation()

BEMYEYES_AUMID = "BeMyEyes.BeMyEyes_7yeb8xxw19svt!App"

_HEADERS = {
	"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
	"Accept": "*/*",
	"Referer": "https://x.com/",
}
_IMAGE_SUFFIX = {
	"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
}
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_cache = {}  # media URL -> file in the temp folder
_cache_lock = threading.Lock()


def _temp_dir():
	path = os.path.join(tempfile.gettempdir(), APP_NAME)
	os.makedirs(path, exist_ok=True)
	return path


def cleanup_old_temp_files(max_age_seconds=86400):
	"""Best effort; files still open in another app just fail to delete."""
	try:
		folder = _temp_dir()
		cutoff = time.time() - max_age_seconds
		for name in os.listdir(folder):
			full = os.path.join(folder, name)
			try:
				if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
					os.remove(full)
			except OSError:
				pass
	except OSError:
		pass


def safe_name(text, limit=120):
	"""A file name stem that Windows accepts."""
	text = _INVALID_CHARS.sub(" ", text)
	text = " ".join(text.split()).strip(" .")
	return (text[:limit].strip(" .")) or "tweet"


def _unique_path(folder, stem, suffix):
	path = os.path.join(folder, stem + suffix)
	n = 2
	while os.path.exists(path):
		path = os.path.join(folder, "%s (%d)%s" % (stem, n, suffix))
		n += 1
	return path


def _download(url, suffix_for, timeout, stem):
	"""Streams url into the temp folder as <stem><suffix>. suffix_for(content_type) -> suffix or None."""
	request = urllib.request.Request(url, headers=_HEADERS)
	folder = _temp_dir()
	with urllib.request.urlopen(request, timeout=timeout) as response:
		content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
		suffix = suffix_for(content_type)
		if suffix is None:
			raise OSError(_("Unexpected content type: {type}").format(type=content_type or "?"))
		fd, part = tempfile.mkstemp(suffix=".part", dir=folder)
		try:
			with os.fdopen(fd, "wb") as f:
				while True:
					chunk = response.read(256 * 1024)
					if not chunk:
						break
					f.write(chunk)
			final = _unique_path(folder, safe_name(stem), suffix)
			os.replace(part, final)
		except Exception:
			try:
				os.remove(part)
			except OSError:
				pass
			raise
	return final


def _webp_to_png(path):
	"""Be My Eyes and wx cannot read WebP: convert with the Pillow bundled with NVDA."""
	try:
		from PIL import Image
		target = os.path.splitext(path)[0] + ".png"
		with Image.open(path) as img:
			img.save(target, "PNG")
	except Exception:
		return path
	try:
		os.remove(path)
	except OSError:
		pass
	return target


def fetch_media(url, kind, stem):
	"""kind is image or video. Returns the temp file path; reuses a file fetched before."""
	with _cache_lock:
		path = _cache.get(url)
		if path and os.path.isfile(path):
			return path
	if kind == "image":
		last_error = None
		path = None
		for candidate in (url + ("&" if "?" in url else "?") + "name=orig", url):
			try:
				path = _download(candidate, lambda ct: _IMAGE_SUFFIX.get(ct), 60, stem)
				break
			except Exception as e:
				last_error = e
		if path is None:
			raise last_error
		if path.lower().endswith(".webp"):
			path = _webp_to_png(path)
	else:
		path = _download(
			url, lambda ct: ".mp4" if ct.startswith("video/") or ct == "application/octet-stream" else None,
			180, stem,
		)
	with _cache_lock:
		_cache[url] = path
	return path


def move_to_folder(path, folder):
	"""Move the temp file to the download folder (copy it if it is in use). Returns the new path."""
	os.makedirs(folder, exist_ok=True)
	stem, suffix = os.path.splitext(os.path.basename(path))
	target = _unique_path(folder, stem, suffix)
	try:
		shutil.move(path, target)
	except OSError:
		shutil.copy2(path, target)
	return target


def open_with_default_app(path):
	os.startfile(path)


def copy_image_to_clipboard(path):
	"""Main thread only."""
	image = wx.Image(path)
	if not image.IsOk():
		return False
	if wx.TheClipboard.Open():
		try:
			wx.TheClipboard.SetData(wx.BitmapDataObject(wx.Bitmap(image)))
		finally:
			wx.TheClipboard.Close()
		return True
	return False


def send_to_bemyeyes(path):
	"""ShellExecute against shell:appsFolder\\<AUMID>; returns False if it did not start.
	The file goes under a plain random name like NVSky does: a name with spaces
	is split into several arguments and Be My Eyes then only opens its start page."""
	target = "shell:appsFolder\\" + BEMYEYES_AUMID
	fd, plain = tempfile.mkstemp(suffix=os.path.splitext(path)[1], dir=_temp_dir())
	os.close(fd)
	shutil.copyfile(path, plain)
	param = '"%s"' % plain if " " in plain else plain  # only if the temp folder itself has spaces
	return ctypes.windll.shell32.ShellExecuteW(None, "open", target, param, None, 1) > 32