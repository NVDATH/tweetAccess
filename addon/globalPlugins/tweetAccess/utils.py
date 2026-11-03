# -*- coding: utf-8 -*-
# NVTweet - utils.py: paths, constants, Post, formatting, cookie parsing, module isolation.

import contextlib
import functools
import hashlib
import importlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import zlib
from datetime import datetime

import addonHandler
import globalVars
import speech
import ui
import wx

from . import keymap

addonHandler.initTranslation()

APP_NAME = "tweetAccess"  # the one place that names the add-on
TAG = APP_NAME + ": "

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(ADDON_DIR, "lib")

CONFIG_ROOT = globalVars.appArgs.configPath
DATA_NAME = APP_NAME.lower()
DATA_DIR = os.path.join(CONFIG_ROOT, APP_NAME)            # database, key live here
DB_FILE = os.path.join(DATA_DIR, DATA_NAME + ".db")
KEY_FILE = os.path.join(DATA_DIR, DATA_NAME + ".key")     # DPAPI-protected DB key
COOKIE_VAULT = os.path.join(DATA_DIR, DATA_NAME + "_cookies.dat")  # old single-account vault (migration only)

# Page with browser extensions that export cookies.
COOKIE_EXTENSION_URL = "https://cookie-editor.com/"

USE_IMPERSONATE = True  # try curl_cffi; silently fall back to the plain client
IMPERSONATE = "chrome124"
FETCH_COUNT = 40        # X returns about 100 per request whatever we ask
POST_MAX_LENGTH = 280   # X's limit, counted as in tweet_length()

# per-account meta keys (stored as "<key>:<account id>")
FOCUS_KEY = "last_focus_id"        # tweet the reader was on when the window closed
CURSOR_KEY = "older_cursor"        # position for "load older"

ABSOLUTE_FORMAT = "%Y-%m-%d %H:%M"
FULL_FORMAT = "%Y-%m-%d %H:%M:%S"

CELL_LIMIT = 450  # a ListCtrl cell shows about 511 characters; astral characters count twice


class Post:
	"""One timeline row. id is the timeline entry (for a retweet: the retweet itself).
	The constructor order matches the column order in storage.py."""

	__slots__ = (
		"id", "author", "message", "ts", "url", "kind", "orig_author", "quote_author",
		"quote_text", "source", "embeds", "liked", "bookmarked", "is_read", "reply_to", "stats",
		"depth",
	)

	def __init__(
		self, id, author, message, ts, url, kind="", orig_author="", quote_author="",
		quote_text="", source="", embeds="", liked=False, bookmarked=False, is_read=False,
		reply_to="", stats="",
	):
		self.id = int(id)
		self.author = author
		self.message = message
		self.ts = ts
		self.url = url
		self.kind = kind or ""            # "" or "retweet"
		self.orig_author = orig_author or ""  # retweet: author of the original tweet
		self.quote_author = quote_author or ""
		self.quote_text = quote_text or ""
		self.source = source or ""        # client used; X does not send it, kept for later
		self.embeds = embeds or ""        # JSON list, see service._embeds_of
		self.liked = bool(liked)
		self.bookmarked = bool(bookmarked)
		self.is_read = bool(is_read)
		self.reply_to = reply_to or ""    # user name (no @) this tweet replies to
		self.stats = stats or ""          # JSON: counts, language, ids (see service._stats_of)
		self.depth = 0                    # reply level in a thread tab (not stored in the cache)

	def to_dict(self):
		return {name: getattr(self, name) for name in self.__slots__}

	@classmethod
	def from_dict(cls, data):
		post = cls(data["id"], data.get("author", ""), data.get("message", ""), data.get("ts", 0), data.get("url", ""))
		for name in cls.__slots__:
			if name != "id" and name in data:
				setattr(post, name, data[name])
		return post


def safe_ui(func):
	"""For callbacks that may run after their tab or window is gone."""
	@functools.wraps(func)
	def wrapper(self, *args, **kwargs):
		if getattr(self, "_dead", False):
			return None
		try:
			return func(self, *args, **kwargs)
		except RuntimeError as e:
			if "deleted" in str(e):
				return None
			raise
	return wrapper


def force_foreground(hwnd):
	"""Bring a window of ours to the foreground even when another program has it (Raise alone is
	refused then). Attaches to the input of the foreground thread for the moment. True if it worked."""
	import ctypes
	user32 = ctypes.windll.user32
	kernel32 = ctypes.windll.kernel32
	try:
		foreground = user32.GetForegroundWindow()
		other = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
		mine = kernel32.GetCurrentThreadId()
		attached = bool(other and other != mine and user32.AttachThreadInput(other, mine, True))
		try:
			user32.BringWindowToTop(hwnd)
			ok = bool(user32.SetForegroundWindow(hwnd))
		finally:
			if attached:
				user32.AttachThreadInput(other, mine, False)
		if not ok:
			# A guess: an input event of our own can lift the foreground lock. F24 is used by nothing.
			user32.keybd_event(0x87, 0, 0, 0)
			user32.keybd_event(0x87, 0, 2, 0)
			ok = bool(user32.SetForegroundWindow(hwnd))
		return ok
	except Exception:
		return False


def announce_now(text):
	"""Speak text after a menu closes, cutting off the focus announcement."""
	def speak():
		speech.cancelSpeech()
		ui.message(text, speechPriority=speech.priorities.Spri.NOW)

	wx.CallLater(100, speak)


def announce_after_menu(text, tries=20):
	"""Speak text once NVDA's focus has left the menu that was just closed. Right after a menu choice
	NVDA still sees the menu; the focus event that follows would cut the message off. After
	tries * 100 ms the text is spoken anyway."""
	import api
	import controlTypes

	menu_roles = (controlTypes.Role.POPUPMENU, controlTypes.Role.MENUITEM, controlTypes.Role.MENUBAR)

	def check(left):
		try:
			focus = api.getFocusObject()
			in_menu = focus.role in menu_roles or getattr(focus, "windowClassName", "") == "#32768"
		except Exception:
			in_menu = False
		if in_menu and left > 0:
			wx.CallLater(100, check, left - 1)
			return
		announce_now(text)

	wx.CallLater(100, check, tries)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def hashed_uid(auth_token):
	"""Stand-in account id when the cookies carry no twid."""
	return "h" + hashlib.sha256(auth_token.encode("utf-8")).hexdigest()[:12]


def user_id_from_twid(value):
	"""The twid cookie looks like u%3D1234567890."""
	m = re.search(r"u=(\d+)", urllib.parse.unquote(str(value or "")))
	return m.group(1) if m else ""


def account_label(acc):
	"""'Name (@user)' for an account dict from the store, or ''."""
	if not acc:
		return ""
	handle, name = acc.get("handle") or "", acc.get("name") or ""
	if name and handle:
		return "%s (@%s)" % (name, handle)
	if handle:
		return "@" + handle
	if name:
		return name
	# Translators: Name of an account whose name is unknown. {number} is its number.
	return _("Account {number}").format(number=acc["id"])


def main_title(tab, label):
	"""Title of the main window: the tab, the account, then the add-on name."""
	return " - ".join(part for part in (tab, label, APP_NAME) if part)


def login_needed_text():
	"""Tells the reader to add an account in the settings, naming the shortcut that opens them."""
	key = keymap.display("settings")
	if key:
		# Translators: Shown when no account is logged in. {key} is the shortcut that opens the settings.
		return _("You are not logged in. Open the settings ({key}) and add an account.").format(key=key)
	# Translators: Shown when no account is logged in.
	return _("You are not logged in. Open the settings and add an account.")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def is_relative_mode(mode):
	return mode in ("relative_24h", "relative_always")


def _relative(seconds, always):
	"""Relative text, or None when older than a day and not in 'always' mode."""
	if seconds < 60:
		# Translators: Relative time, under a minute old.
		return _("just now")
	if seconds < 3600:
		minutes = int(seconds // 60)
		if minutes == 1:
			# Translators: Relative time, exactly one minute old.
			return _("1 minute ago")
		# Translators: Relative time. {} is the number of minutes.
		return _("{} minutes ago").format(minutes)
	if seconds < 86400:
		hours = int(seconds // 3600)
		if hours == 1:
			# Translators: Relative time, exactly one hour old.
			return _("1 hour ago")
		# Translators: Relative time. {} is the number of hours.
		return _("{} hours ago").format(hours)
	if not always:
		return None
	days = int(seconds // 86400)
	if days < 30:
		if days == 1:
			# Translators: Relative time, exactly one day old.
			return _("1 day ago")
		# Translators: Relative time. {} is the number of days.
		return _("{} days ago").format(days)
	months = days // 30
	if months < 12:
		if months == 1:
			# Translators: Relative time, exactly one month old.
			return _("1 month ago")
		# Translators: Relative time. {} is the number of months.
		return _("{} months ago").format(months)
	years = days // 365
	if years <= 1:
		# Translators: Relative time, exactly one year old.
		return _("1 year ago")
	# Translators: Relative time. {} is the number of years.
	return _("{} years ago").format(years)


def format_ts(ts, mode="absolute", pattern=None):
	if not ts:
		return ""
	try:
		dt = datetime.fromtimestamp(ts)
	except (OverflowError, OSError, ValueError):
		return ""
	if is_relative_mode(mode):
		text = _relative(max(time.time() - ts, 0), mode == "relative_always")
		if text:
			return text
		return dt.strftime(ABSOLUTE_FORMAT)
	if mode == "custom":
		try:
			return dt.strftime(pattern or FULL_FORMAT)
		except ValueError:
			return dt.strftime(FULL_FORMAT)
	return dt.strftime(ABSOLUTE_FORMAT)


_AUTHOR_RE = re.compile(r"^(.*?)\s*\(@([^()]+)\)$")


def format_author(author, mode):
	"""author is 'Name (@user)'; mode is both / name (display name) / handle (user name)."""
	if mode == "both" or not author:
		return author
	m = _AUTHOR_RE.match(author)
	if not m:
		return author
	name, handle = m.group(1).strip(), m.group(2)
	if mode == "name":
		return name or "@" + handle
	return "@" + handle


def handle_of(label):
	"""Lowercase user name (no @) from 'Name (@user)' or '@user'; '' if there is none."""
	m = _AUTHOR_RE.match(label or "")
	if m:
		return m.group(2).lower()
	label = (label or "").strip()
	return label[1:].lower() if label.startswith("@") else ""


def notification_id(stamp_ms, handle, url):
	"""Id of a notification row: its time in milliseconds, then three digits from who did it and what it is
	about. Not from the id X gives it: that one is not the same in two fetches, and every fetch looked new."""
	digits = zlib.crc32(("%s\n%s" % ((handle or "").lower(), url or "")).encode("utf-8")) % 1000
	return int(stamp_ms) * 1000 + digits


def author_handle(post):
	"""Who wrote the tweet itself; for a retweet, the original author."""
	return handle_of(post.orig_author if post.kind == "retweet" and post.orig_author else post.author)


def post_involves(post, handle):
	"""True when the tweet is by, a retweet by, a reply to, a quote of, or mentions @handle."""
	if not handle:
		return False
	related = (handle_of(post.author), handle_of(post.orig_author), handle_of(post.quote_author), (post.reply_to or "").lower())
	if handle in related:
		return True
	pattern = r"(?<![A-Za-z0-9_])@%s(?![A-Za-z0-9_])" % re.escape(handle)
	return bool(re.search(pattern, post.message, re.IGNORECASE))


def mentions_of(text):
	"""Lowercase user names mentioned in a text, in order, without repeats."""
	found = []
	for m in re.finditer(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})", text or ""):
		name = m.group(1).lower()
		if name not in found:
			found.append(name)
	return found


def relevant_users(post):
	"""[(label, user name)] of everyone a tweet is about: author, retweeter, replied-to, quoted author, mentions."""
	users = []

	def add(handle, label):
		if handle and all(handle != other for _role, other in users):
			users.append((label.format(user=handle), handle))

	# Translators: Role of a user in the "which user?" list. {user} is the user name.
	add(author_handle(post), _("@{user} (author)"))
	if post.kind == "retweet":
		add(handle_of(post.author), _("@{user} (retweeted by)"))
	add((post.reply_to or "").lower(), _("@{user} (replying to)"))
	add(handle_of(post.quote_author), _("@{user} (quoted author)"))
	for name in mentions_of(post.message):
		add(name, _("@{user} (mentioned)"))
	return users


def tweet_length(text):
	"""Length as X counts it: a link is 23, most scripts 1 per character, CJK and emoji 2."""
	text = unicodedata.normalize("NFC", text)

	def weight(chunk):
		total = 0
		for ch in chunk:
			cp = ord(ch)
			narrow = cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247
			total += 1 if narrow else 2
		return total

	total = pos = 0
	for m in re.finditer(r"https?://\S+", text):
		total += weight(text[pos:m.start()]) + 23
		pos = m.end()
	return total + weight(text[pos:])


def is_retweeted(post):
	return load_stats(post.stats).get("retweeted") is True


def is_pinned(post):
	"""True for the tweet a user pinned, in that user's timeline tab (service.py marks it in stats)."""
	return '"pinned"' in post.stats and load_stats(post.stats).get("pinned") is True


def set_stat(raw, key, value):
	stats = load_stats(raw)
	stats[key] = value
	return json.dumps(stats)


def quoted_post(post):
	"""The tweet that a quote tweet quotes, as a row of its own (to act on it), or None."""
	stats = load_stats(post.stats)
	qid = str(stats.get("quote_id") or "")
	if not qid.isdigit():
		return None
	handle = handle_of(post.quote_author) or "i"
	media = [
		{k: v for k, v in e.items() if k != "src"}
		for e in load_embeds(post.embeds) if e.get("src") == "quote"
	]
	return Post(
		int(qid), post.quote_author, post.quote_text, 0, "https://x.com/%s/status/%s" % (handle, qid),
		embeds=json.dumps(media) if media else "", liked=bool(stats.get("quote_liked")),
		bookmarked=bool(stats.get("quote_bookmarked")),
		stats=json.dumps({"retweeted": bool(stats.get("quote_retweeted"))}),
	)


_HASHTAG_RE = re.compile(r"#([^\s#@.,;:!?()\[\]{}\"'<>]+)")  # not \w: it cuts Thai vowels and tone marks


def hashtags_of(post):
	"""Hashtags of a tweet (and of the tweet it quotes), without #, in order, without repeats.
	X's own list when we have it; otherwise found in the text."""
	found = [str(t) for t in load_stats(post.stats).get("hashtags") or []]
	found += _HASHTAG_RE.findall(post.message) + _HASHTAG_RE.findall(post.quote_text)
	out, seen = [], set()
	for tag in found:
		if tag.lower() not in seen:
			seen.add(tag.lower())
			out.append(tag)
	return out


def _term(text):
	"""One search term: a phrase with spaces gets quotes, unless it is already quoted or grouped."""
	text = text.strip()
	if " " in text and text[0] not in '"(':
		return '"%s"' % text.replace('"', "")
	return text


def build_search_query(keywords="", exclude="", include=(), omit=(), fields=None):
	"""The text sent to X. keywords: comma separated, any of them matches ((a OR b)); a space is part of
	a keyword. exclude: comma separated words to leave out. include / omit: filter names (filter:x / -filter:x).
	fields: from, to, mention, lang, since, until, likes, retweets, replies."""
	parts = []
	words = [w.strip() for w in keywords.split(",") if w.strip()]
	if len(words) == 1:
		parts.append(_term(words[0]))
	elif words:
		parts.append("(" + " OR ".join(_term(w) for w in words) + ")")
	parts += ["-" + _term(w) for w in (x.strip() for x in exclude.split(",")) if w]
	parts += ["filter:" + op for op in include]
	parts += ["-filter:" + op for op in omit]
	f = fields or {}
	for key, template in (("from", "from:%s"), ("to", "to:%s"), ("mention", "@%s")):
		if f.get(key):
			parts.append(template % f[key].lstrip("@"))
	if f.get("lang"):
		parts.append("lang:" + f["lang"])
	if f.get("since"):
		parts.append("since:" + f["since"])
	if f.get("until"):
		parts.append("until:" + f["until"])
	for key, operator in (("likes", "min_faves"), ("retweets", "min_retweets"), ("replies", "min_replies")):
		if f.get(key):
			parts.append("%s:%s" % (operator, f[key]))
	return " ".join(parts)


def load_stats(raw):
	try:
		data = json.loads(raw) if raw else {}
	except (ValueError, TypeError):
		return {}
	return data if isinstance(data, dict) else {}


def target_id(post):
	"""Id of the tweet itself (for a retweet: the original), taken from its link."""
	tail = post.url.rstrip("/").rsplit("/", 1)[-1] if post.url else ""
	return tail if tail.isdigit() else str(post.id)


def handle_from_url(url):
	m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/", url or "")
	return m.group(1) if m else ""


def main_text(post):
	"""The tweet with its reply / retweet prefixes. Other people are always named @user
	here, whatever the author setting says."""
	text = post.message
	if post.reply_to:
		# Translators: Start of a reply. {user} is the user name replied to (without @), {text} the reply.
		text = _("reply @{user}: {text}").format(user=post.reply_to, text=text)
	if post.kind == "retweet":
		if post.orig_author:
			# Translators: Message of a retweet. {author} is @user of the original author, {text} the original tweet.
			text = _("retweet: {author}: {text}").format(
				author=format_author(post.orig_author, "handle"), text=text
			)
		else:
			# Translators: Message of a retweet whose original author is unknown. {text} is the original tweet.
			text = _("retweet: {text}").format(text=text)
	if is_pinned(post):
		# Translators: Start of the tweet a user pinned, in that user's timeline. {text} is the tweet.
		text = _("pinned: {text}").format(text=text)
	return text


def message_text(post, oneline=True):
	"""Body of a row: the tweet, with reply / retweet / quote parts spelled out."""
	text = main_text(post)
	if post.quote_text:
		if post.quote_author:
			# Translators: Message of a quote tweet. {text} is the quoting tweet, {author} (@user) and {quote} the quoted one.
			text = _("{text} quote: {author}: {quote}").format(
				text=text, author=format_author(post.quote_author, "handle"), quote=post.quote_text
			)
		else:
			# Translators: Message of a quote tweet whose quoted author is unknown.
			text = _("{text} quote: {quote}").format(text=text, quote=post.quote_text)
	if oneline:
		text = " ".join(text.split())
	return text


def _u16(text):
	return len(text.encode("utf-16-le")) // 2


def _grapheme_starts(text):
	"""Start index of every grapheme cluster (combining marks and ZWJ sequences stay together)."""
	starts = [0]
	joined = False
	for i, ch in enumerate(text):
		if i == 0:
			continue
		if unicodedata.category(ch) in ("Mn", "Mc", "Me"):
			joined = False
			continue
		if ch == "\u200d":
			joined = True
			continue
		if joined:
			joined = False
			continue
		starts.append(i)
	return starts


def truncate_cell(text, limit=CELL_LIMIT):
	"""Cut a long message so a list cell can show it. Returns (text, was_cut).
	Never cuts inside a grapheme; prefers a space or punctuation near the end."""
	if _u16(text) <= limit:
		return text, False
	budget = limit
	starts = _grapheme_starts(text)
	total = prev = best = 0
	for b in starts[1:] + [len(text)]:
		seg = _u16(text[prev:b])
		if total + seg > budget:
			break
		total += seg
		best = b
		prev = b
	cut = best
	window = max(0, best - 100)
	for b in starts:
		if b < window or b > best or b == 0:
			continue
		before = text[b - 1]
		if before.isspace() or unicodedata.category(before).startswith("P"):
			cut = b
	return text[:cut].rstrip(), True


def read_more_text():
	# Translators: Added at the end of the Embed column when the message is too long for its cell; the whole text is in the window opened with Enter.
	return _("(read more...)")


def message_cell(post):
	"""(text of the Message cell, whether it was cut)."""
	return truncate_cell("> " * post.depth + message_text(post))


def cell_texts(post, author_mode, time_mode, pattern, full=False):
	"""The four cells of a tweet row, as the list shows them. full: the whole message, no read more."""
	message, cut = message_cell(post)
	if full:
		message, cut = "> " * post.depth + message_text(post), False
	embed = describe_embeds(post.embeds)
	if cut:
		embed = ", ".join(p for p in (embed, read_more_text()) if p)  # always last in the column
	return {
		"embed": embed,
		"author": format_author(post.author, author_mode),
		"message": message,
		"time": format_ts(post.ts, time_mode, pattern),
	}


def row_speech(texts, columns):
	"""A row as NVDA reads it when arrowing: the cells in column order, the time with its header."""
	parts = []
	for name in columns:
		text = texts[name]
		if name == "time" and text:
			text = _("created at {time}").format(time=text)  # the column header NVDA says
		parts.append(text)
	return ", ".join(p for p in parts if p)


def row_sound_events(post, full=False):
	"""Sound events of a row, when it is focused or auto read: its embed kinds, a quote, read more
	(not when the whole message is read)."""
	kinds = {e["t"] for e in load_embeds(post.embeds)}
	events = ["embed_" + kind for kind in ("video", "gif", "image", "link") if kind in kinds]
	if post.quote_text:
		events.append("embed_quote")
	if not full and message_cell(post)[1]:
		events.append("read_more")
	return events


def copy_text(post, author_mode):
	"""What copying a tweet gives, like NVSky: <author>, <tweet> (<link>)."""
	parts = (format_author(post.author, author_mode), message_text(post))
	text = ", ".join(p for p in parts if p)
	return "%s (%s)" % (text, post.url) if post.url else text


def load_embeds(raw):
	if not raw:
		return []
	try:
		items = json.loads(raw)
	except (ValueError, TypeError):
		return []
	return [i for i in items if isinstance(i, dict) and i.get("url")] if isinstance(items, list) else []


def links_of(post):
	return [e for e in load_embeds(post.embeds) if e["t"] == "link"]


def _duration(ms):
	try:
		seconds = int(ms) // 1000
	except (TypeError, ValueError):
		return ""
	hours, rest = divmod(seconds, 3600)
	minutes, secs = divmod(rest, 60)
	return "%d:%02d:%02d" % (hours, minutes, secs) if hours else "%d:%02d" % (minutes, secs)


def describe_embeds(raw):
	items = load_embeds(raw)
	images = sum(1 for i in items if i.get("t") == "image")
	links = sum(1 for i in items if i.get("t") == "link")
	parts = []
	if images == 1:
		# Translators: Embed summary, one image.
		parts.append(_("Image"))
	elif images:
		# Translators: Embed summary, several images. {count} is the number.
		parts.append(_("Images ({count})").format(count=images))
	for item in items:
		if item.get("t") == "video":
			length = _duration(item.get("ms"))
			# Translators: Embed summary, a video. {length} is its duration, like 1:05.
			parts.append(_("Video ({length})").format(length=length) if length else _("Video"))
		elif item.get("t") == "gif":
			# Translators: Embed summary, an animated GIF.
			parts.append(_("Animated GIF"))
	if links == 1:
		# Translators: Embed summary, one link.
		parts.append(_("Link"))
	elif links:
		# Translators: Embed summary, several links. {count} is the number.
		parts.append(_("Links ({count})").format(count=links))
	return ", ".join(parts)


def _preview(text, limit):
	text = " ".join(text.split())
	return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _original(post):
	"""(author, text) of the tweet itself, also for a retweet."""
	return (post.orig_author if post.kind == "retweet" and post.orig_author else post.author), post.message


def thread_tab_name(post):
	author, text = _original(post)
	# Translators: Name of a thread tab. {author} is @user of the first tweet, {text} the start of its text.
	return _("Thread: {author}: {text}").format(author=format_author(author, "handle"), text=_preview(text, 40))


def quotes_tab_name(post):
	author, text = _original(post)
	# Translators: Name of a quotes tab. {text} is the start of the quoted tweet, {author} its @user.
	return _('Quotes: "{text}" by {author}').format(text=_preview(text, 40), author=format_author(author, "handle"))


def users_tab_name(kind, handle):
	"""Name of a tab with the followers, the following or the followers you know of a user."""
	if kind == "following":
		# Translators: Name of a tab with the users a user follows. {user} is the user name.
		return _("Following of @{user}").format(user=handle)
	if kind == "known_followers":
		# Translators: Name of a tab with the followers of a user that you follow too. {user} is the user name.
		return _("Followers you know of @{user}").format(user=handle)
	if kind == "search":
		# Translators: Name of a tab with the users a search found. {query} is the search text.
		return _("People: {query}").format(query=handle)
	# Translators: Name of a tab with the followers of a user. {user} is the user name.
	return _("Followers of @{user}").format(user=handle)


def full_title(post):
	"""Window title for one tweet: the start of what the list shows."""
	return _preview(message_text(post), 50) or post.author


def head_text(post, author_mode):
	"""Author, the tweet and, for a quote tweet, the quoted tweet in its own block."""
	lines = [format_author(post.author, author_mode), main_text(post)]
	if post.quote_text:
		lines.append("")
		if post.quote_author:
			# Translators: Line before the quoted tweet in the full tweet window. {author} is @user.
			lines.append(_("quote {author}").format(author=format_author(post.quote_author, "handle")))
		else:
			# Translators: Line before the quoted tweet in the full tweet window.
			lines.append(_("quote"))
		lines.append(post.quote_text)
	return "\n".join(lines)


def info_items(post):
	"""(text, url) lines for the list of the full tweet window; url is None for plain information."""
	items = []
	embeds = load_embeds(post.embeds)
	images = [e for e in embeds if e["t"] == "image"]
	for i, e in enumerate(images, start=1):
		if len(images) > 1:
			# Translators: Info line of the full tweet window. {number} is the image number.
			items.append((_("image {number}: {url}").format(number=i, url=e["url"]), e["url"]))
		else:
			# Translators: Info line of the full tweet window.
			items.append((_("image: {url}").format(url=e["url"]), e["url"]))
	for e in embeds:
		if e["t"] == "video":
			length = _duration(e.get("ms"))
			if length:
				# Translators: Info line of the full tweet window. {length} is the duration.
				items.append((_("video ({length}): {url}").format(length=length, url=e["url"]), e["url"]))
			else:
				# Translators: Info line of the full tweet window.
				items.append((_("video: {url}").format(url=e["url"]), e["url"]))
		elif e["t"] == "gif":
			# Translators: Info line of the full tweet window.
			items.append((_("animated GIF: {url}").format(url=e["url"]), e["url"]))
		elif e["t"] == "link":
			# Translators: Info line of the full tweet window.
			items.append((_("link: {url}").format(url=e["url"]), e["url"]))
	# Translators: Info line of the full tweet window.
	items.append((_("tweet url: {url}").format(url=post.url), post.url))
	stats = load_stats(post.stats)
	if post.quote_text and post.quote_author and stats.get("quote_id"):
		handle = format_author(post.quote_author, "handle").lstrip("@")
		url = "https://x.com/%s/status/%s" % (handle, stats["quote_id"])
		# Translators: Info line of the full tweet window.
		items.append((_("quote url: {url}").format(url=url), url))
	# Translators: Info line of the full tweet window. {time} is the date and time.
	items.append((_("created at {time}").format(time=format_ts(post.ts, "custom", FULL_FORMAT)), None))
	counts = (
		# Translators: Info lines of the full tweet window. {count} is a number.
		("replies", _("replies: {count}")), ("likes", _("likes: {count}")),
		("retweets", _("retweets: {count}")), ("quotes", _("quotes: {count}")),
		("bookmarks", _("bookmarks: {count}")), ("views", _("views: {count}")),
	)
	for key, text in counts:
		if isinstance(stats.get(key), int):
			items.append((text.format(count=stats[key]), None))
	return items


# ---------------------------------------------------------------------------
# Cookie parsing
# ---------------------------------------------------------------------------

_REQUIRED = ("auth_token", "ct0")
_OPTIONAL = ("twid",)


def _domain_rank(domain):
	d = (domain or "").lower().lstrip(".")
	if d == "x.com" or d.endswith(".x.com"):
		return 0
	if d == "twitter.com" or d.endswith(".twitter.com"):
		return 1
	return 2


def _offer(found, name, value, domain):
	value = str(value).strip()
	if not value:
		return
	rank = _domain_rank(domain)
	old = found.get(name)
	if old is None or rank < old[0]:
		found[name] = (rank, value)


def _result(found):
	"""Dict with auth_token, ct0 (and twid if present), or None."""
	if all(k in found for k in _REQUIRED):
		return {k: found[k][1] for k in _REQUIRED + _OPTIONAL if k in found}
	return None


def extract_cookies(text):
	"""Read auth_token, ct0 and (optional) twid from JSON, Netscape cookies.txt or plain text.
	Returns (dict or None, mode). Values of x.com are preferred over other domains."""
	names = _REQUIRED + _OPTIONAL
	text = text.lstrip("\ufeff")

	# 1) JSON values (tolerates extra objects/lines after the first one)
	decoder = json.JSONDecoder()
	pos, found = 0, {}
	while pos < len(text):
		while pos < len(text) and text[pos].isspace():
			pos += 1
		if pos >= len(text):
			break
		try:
			obj, pos = decoder.raw_decode(text, pos)
		except ValueError:
			break
		for item in (obj if isinstance(obj, list) else [obj]):
			if not isinstance(item, dict):
				continue
			if item.get("name") in names and "value" in item:
				_offer(found, item["name"], item["value"], item.get("domain"))
			for k in names:
				if k in item:
					_offer(found, k, item[k], "")
	result = _result(found)
	if result:
		return result, "json"

	# 2) Netscape cookies.txt (tab separated, 7 fields)
	found = {}
	for line in text.splitlines():
		if line.startswith("#HttpOnly_"):
			line = line[len("#HttpOnly_"):]
		elif line.startswith("#"):
			continue
		fields = line.split("\t")
		if len(fields) >= 7 and fields[5] in names:
			_offer(found, fields[5], fields[6], fields[0])
	result = _result(found)
	if result:
		return result, "netscape"

	# 3) plain text key=value / key: value / header string
	found = {}
	for k in names:
		pattern = (
			r"(?<![A-Za-z0-9_])" + k
			+ r"""["']?(?:\s*[=:]\s*|\t)["']?([A-Za-z0-9%_.\-]+)"""
		)
		m = re.search(pattern, text)
		if m:
			found[k] = (2, m.group(1).strip())
	result = _result(found)
	if result:
		return result, "text"
	return None, "none"


# ---------------------------------------------------------------------------
# Module isolation
#
# Several add-ons bundle their own copy of the same libraries (httpx, httpcore,
# anyio, certifi ...) in their own "lib" folder, but sys.modules is shared by
# the whole NVDA process: whoever imports a module first wins, and every other
# add-on silently receives that first copy, even if it is a different version.
# While NVTweet runs we therefore move any such foreign modules out of
# sys.modules, use our own copies, and put the originals back afterwards.
# ---------------------------------------------------------------------------

_swap_lock = threading.RLock()
_private_modules = {}  # our own copies, kept between runs


def _lib_top_level_names():
	names = set()
	try:
		entries = os.listdir(LIB_DIR)
	except OSError:
		return names
	for entry in entries:
		low = entry.lower()
		if low.endswith((".dist-info", ".egg-info")) or entry in ("bin", "__pycache__"):
			continue
		path = os.path.join(LIB_DIR, entry)
		if os.path.isdir(path):
			names.add(entry)
		elif low.endswith(".py"):
			names.add(entry[:-3])
		elif low.endswith(".pyd"):
			names.add(entry.split(".", 1)[0])
	return names


def _is_ours(module, lib_prefix):
	path = getattr(module, "__file__", None)
	return bool(path) and os.path.normcase(os.path.abspath(path)).startswith(lib_prefix)


@contextlib.contextmanager
def isolated_modules():
	with _swap_lock:
		names = _lib_top_level_names()
		lib_prefix = os.path.normcase(os.path.abspath(LIB_DIR)) + os.sep

		def belongs(modname):
			return modname.split(".", 1)[0] in names

		foreign = {}
		for key, mod in list(sys.modules.items()):
			if (
				mod is not None
				and belongs(key)
				and getattr(mod, "__file__", None)
				and not _is_ours(mod, lib_prefix)
			):
				foreign[key] = mod
		for key in foreign:
			del sys.modules[key]
		sys.modules.update(_private_modules)

		added_path = LIB_DIR not in sys.path
		if added_path:
			sys.path.insert(0, LIB_DIR)
		importlib.invalidate_caches()
		try:
			yield
		finally:
			if added_path and LIB_DIR in sys.path:
				sys.path.remove(LIB_DIR)
			for key, mod in list(sys.modules.items()):
				if mod is not None and belongs(key) and _is_ours(mod, lib_prefix):
					_private_modules[key] = mod
					del sys.modules[key]
			sys.modules.update(foreign)