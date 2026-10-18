# -*- coding: utf-8 -*-
# NVTweet - service.py: talks to X (via twifork/twikit) on one background thread.
#
# One long-lived worker thread owns an asyncio loop and the twikit client of the
# active account, so the client (and its expensive start-up) is reused between jobs.
# Each job runs inside isolated_modules() so our bundled libraries never mix with
# other add-ons'.
#
# Jobs: reload, older (timeline), thread, quotes, tweet (one tweet's details), login
# (test cookies, find the account), action (like/bookmark), probe (optional diagnostics.py).
# Jobs that need an account get options["account_id"]; results carry it back.

import asyncio
import html
import inspect
import json
import queue
import random
import re
import threading
from types import SimpleNamespace

import addonHandler
from logHandler import log

from .utils import (
	CURSOR_KEY, FETCH_COUNT, IMPERSONATE, TAG, USE_IMPERSONATE,
	Post, hashed_uid, isolated_modules, login_needed_text, notification_id, set_stat, user_id_from_twid,
)

addonHandler.initTranslation()


class CookieError(Exception):
	pass


# ------------------------------------------------------------------ attribute helpers

class _AttrError:
	def __init__(self, name):
		self.name = name


def _safe_attr(obj, name):
	try:
		return getattr(obj, name)
	except Exception as e:
		return _AttrError(type(e).__name__)


def _val(obj, name):
	v = _safe_attr(obj, name)
	return None if isinstance(v, _AttrError) else v


def _int(v):
	try:
		return int(v)
	except (TypeError, ValueError):
		return None


def _short_error(e):
	return "%s: %s" % (type(e).__name__, " ".join(str(e).split())[:110])


def _flag(obj, name):
	"""A true / false flag of something X sent, None when X did not send it."""
	value = _val(obj, name)
	return None if value is None else bool(value)


def _legacy(tw):
	"""The raw 'legacy' part of a tweet (X's own field names), or {}."""
	data = _val(tw, "_data")
	legacy = data.get("legacy") if isinstance(data, dict) else None
	return legacy if isinstance(legacy, dict) else {}


def _shown(t):
	return _val(t, "retweeted_tweet") or t


# ------------------------------------------------------------------ tweet conversion

def _author_label(user):
	if user is None:
		return "?"
	name = str(_val(user, "name") or "").strip()
	screen = str(_val(user, "screen_name") or "").strip()
	if name and screen:
		return "%s (@%s)" % (name, screen)
	return name or ("@" + screen if screen else "?")


def _clean_text(tw, hide_ids=()):
	"""Tweet text with links expanded, media links and the link to a quoted tweet removed."""
	text = _val(tw, "full_text") or _val(tw, "text") or ""
	try:
		for u in _val(tw, "urls") or []:
			if isinstance(u, dict) and u.get("url") and u.get("expanded_url"):
				text = text.replace(u["url"], u["expanded_url"])
		media = _val(tw, "media")
		if isinstance(media, (list, tuple)):
			for m in media:
				link = _val(m, "url")
				if isinstance(link, str) and link:
					text = text.replace(link, "")
		for qid in hide_ids:
			if qid:
				text = re.sub(
					r"https?://(?:www\.)?(?:x|twitter)\.com/\S+/status/%s\S*" % re.escape(str(qid)), "", text
				)
	except Exception:
		log.debugWarning(TAG + "could not clean the text", exc_info=True)
	text = html.unescape(text)
	text = re.sub(r"[ \t]{2,}", " ", text)
	return text.strip()


def _raw_quote(shown):
	"""Fallback when twifork leaves .quote empty: read the quoted tweet from the raw data."""
	data = _val(shown, "_data")
	if not isinstance(data, dict):
		return None
	res = (data.get("quoted_status_result") or {}).get("result") or {}
	if isinstance(res.get("tweet"), dict):
		res = res["tweet"]
	legacy = res.get("legacy") or {}
	text = legacy.get("full_text") or ""
	if not text:
		return None
	entities = legacy.get("entities") or {}
	for u in entities.get("urls") or []:
		if u.get("url") and u.get("expanded_url"):
			text = text.replace(u["url"], u["expanded_url"])
	for m in entities.get("media") or []:
		if m.get("url"):
			text = text.replace(m["url"], "")
	user = ((res.get("core") or {}).get("user_results") or {}).get("result") or {}
	ulegacy = user.get("legacy") or {}
	ucore = user.get("core") or {}
	name = (ulegacy.get("name") or ucore.get("name") or "").strip()
	screen = (ulegacy.get("screen_name") or ucore.get("screen_name") or "").strip()
	author = "%s (@%s)" % (name, screen) if name and screen else (name or ("@" + screen if screen else ""))
	return SimpleNamespace(
		author=author, text=re.sub(r"[ \t]{2,}", " ", html.unescape(text)).strip(),
		id=res.get("rest_id"), tweet=None,
	)


def _quote_of(shown):
	q = _val(shown, "quote")
	if q is not None:
		return SimpleNamespace(
			author=_author_label(_val(q, "user")), text=_clean_text(q), id=_val(q, "id"), tweet=q
		)
	if not _val(shown, "is_quote_status"):
		return None
	return _raw_quote(shown)


def _safe_quote(shown):
	try:
		return _quote_of(shown)
	except Exception:
		log.debugWarning(TAG + "could not read a quoted tweet", exc_info=True)
		return None


def _best_stream_url(m):
	"""Highest-bitrate mp4 of a video / GIF, or None."""
	best = None
	streams = _val(m, "streams")
	if isinstance(streams, (list, tuple)):
		for s in streams:
			url = _val(s, "url")
			if not isinstance(url, str) or not url:
				continue
			ctype = str(_val(s, "content_type") or "")
			if "mpegurl" in ctype or ".m3u8" in url:
				continue
			bitrate = _int(_val(s, "bitrate")) or 0
			if best is None or bitrate > best[0]:
				best = (bitrate, url)
	if best:
		return best[1]
	for name in ("video_url", "media_url", "url"):
		v = _val(m, name)
		if isinstance(v, str) and ".mp4" in v:
			return v
	return None


def _embeds_of(tw, hide_ids=()):
	items = []
	media = _val(tw, "media")
	if isinstance(media, (list, tuple)):
		for m in media:
			type_name = type(m).__name__
			kind = str(_val(m, "type") or "").lower()
			if type_name == "Photo" or kind == "photo":
				url = _val(m, "media_url")
				if isinstance(url, str) and url:
					items.append({"t": "image", "url": url})
			else:
				url = _best_stream_url(m)
				if url:
					is_gif = "gif" in kind or type_name == "AnimatedGif"
					item = {"t": "gif" if is_gif else "video", "url": url}
					length = _int(_val(m, "duration_millis"))
					if length:
						item["ms"] = length
					items.append(item)
	seen = set()
	for u in _val(tw, "urls") or []:
		if not isinstance(u, dict):
			continue
		expanded = u.get("expanded_url") or ""
		if not expanded or expanded in seen:
			continue
		if any(q and ("/status/%s" % q) in expanded for q in hide_ids):
			continue
		seen.add(expanded)
		items.append({"t": "link", "url": expanded, "label": u.get("display_url") or expanded})
	return items


def _stats_of(shown, quote):
	"""Counts and ids kept for the full-tweet window and for later features (JSON)."""
	legacy = _legacy(shown)
	stats = {
		"replies": _int(_val(shown, "reply_count")),
		"likes": _int(_val(shown, "favorite_count")),
		"retweets": _int(_val(shown, "retweet_count")),
		"quotes": _int(_val(shown, "quote_count")),
		"bookmarks": _int(_val(shown, "bookmark_count")),
		"views": _int(_val(shown, "view_count")),
		"lang": _val(shown, "lang") or "",
		"sensitive": bool(_val(shown, "possibly_sensitive")),
		"conversation_id": legacy.get("conversation_id_str") or "",
		"reply_to_id": legacy.get("in_reply_to_status_id_str") or "",
		"quote_id": str(quote.id) if quote is not None and quote.id else "",
		"author_id": legacy.get("user_id_str") or "",
		"hashtags": [
			h["text"] for h in ((legacy.get("entities") or {}).get("hashtags") or [])
			if isinstance(h, dict) and h.get("text")
		],
		"retweeted": bool(legacy.get("retweeted")),
		"quote_liked": bool(_val(quote.tweet, "favorited")) if quote is not None and quote.tweet is not None else None,
		"quote_bookmarked": bool(_val(quote.tweet, "bookmarked")) if quote is not None and quote.tweet is not None else None,
		"quote_retweeted": bool(_legacy(quote.tweet).get("retweeted")) if quote is not None and quote.tweet is not None else None,
	}
	return json.dumps({k: v for k, v in stats.items() if v is not None and v != ""})


_LEADING_MENTIONS = re.compile(r"^(?:@\w+\s+)+")


def tweet_to_post(tw):
	"""Convert a twikit Tweet into a Post. Raises if the tweet is unusable."""
	rt = tw.retweeted_tweet
	shown = rt if rt is not None else tw
	quote = _safe_quote(shown)
	hide = [quote.id] if quote is not None and quote.id else []

	embeds = _embeds_of(shown, hide)
	if quote is not None and quote.tweet is not None:
		# src marks media that belongs to the quoted tweet (the Embed menu names it)
		embeds += [dict(e, src="quote") for e in _embeds_of(quote.tweet) if e["t"] != "link"]

	message = _clean_text(shown, hide)
	reply_to = str(_legacy(shown).get("in_reply_to_screen_name") or "")
	if reply_to:
		message = _LEADING_MENTIONS.sub("", message).strip()  # X adds the replied-to users in front

	try:
		ts = int(tw.created_at_datetime.timestamp())
	except Exception:
		ts = 0
	return Post(
		int(tw.id),
		_author_label(tw.user),
		message,
		ts,
		shown.url,
		kind="retweet" if rt is not None else "",
		orig_author=_author_label(shown.user) if rt is not None else "",
		quote_author=quote.author if quote is not None else "",
		quote_text=quote.text if quote is not None else "",
		embeds=json.dumps(embeds) if embeds else "",
		liked=bool(_val(shown, "favorited")),
		bookmarked=bool(_val(shown, "bookmarked")),
		reply_to=reply_to,
		stats=_stats_of(shown, quote),
	)


_PRIVATE_USE = re.compile("[\ue000-\uf8ff]")  # icon glyphs X puts in names (verified badge...)


def notification_to_post(n, index=0):
	"""A notification as a row: the author is who did it, the message says what and on which tweet."""
	user, tw = _val(n, "from_user"), _val(n, "tweet")
	text = _PRIVATE_USE.sub("", str(_val(n, "message") or "")).strip()
	name = _PRIVATE_USE.sub("", str(_val(user, "name") or "")).strip()
	if name and text.startswith(name):
		text = text[len(name):].strip()  # the author column already says who
	body = _clean_text(tw) if tw is not None else ""
	message = ": ".join(p for p in (text, body) if p)
	stamp = _int(_val(n, "timestamp_ms")) or 0
	screen = str(_val(user, "screen_name") or "")
	url = (_val(tw, "url") if tw is not None else "") or ("https://x.com/" + screen if screen else "")
	try:
		embeds = _embeds_of(tw) if tw is not None else []
	except Exception:
		embeds = []
	return Post(
		notification_id(stamp, screen, url),
		_author_label(user), message, stamp // 1000, url,
		embeds=json.dumps(embeds) if embeds else "",
		liked=bool(_val(tw, "favorited")) if tw is not None else False,
		bookmarked=bool(_val(tw, "bookmarked")) if tw is not None else False,
	)


def describe_error(exc):
	"""A short sentence for the user. Details go to the log."""
	name = type(exc).__name__
	if isinstance(exc, CookieError):
		return login_needed_text()
	if name == "TooManyRequests":
		return _("X is rate limiting the requests. Wait a while before trying again.")
	if name in ("Unauthorized", "Forbidden", "InvalidSession"):
		return _("X rejected the request. Your cookies may have expired.")
	if name in ("AccountLocked", "AccountSuspended"):
		return _("X reports a problem with the account.")
	if name in ("RequestTimeout", "ConnectTimeout", "ReadTimeout", "ConnectError"):
		return _("The connection to X timed out.")
	if name == "NotFound":
		return _("X says this address does not exist. X may have changed or removed this feature.")
	return _("The request failed (%s). See the NVDA log.") % name


def _ok(kind, posts, gap=False, **extra):
	result = {"kind": kind, "ok": True, "posts": list(posts), "message": "", "gap": gap}
	result.update(extra)
	return result


def _fail(kind, message):
	return {"kind": kind, "ok": False, "posts": [], "message": message, "gap": False}


# ------------------------------------------------------------------ service

class FetchService:
	"""submit(kind, callback, options). callback(result) runs via notify()."""

	def __init__(self, store, notify):
		self.store = store
		self._notify = notify  # notify(callback, result), called from the worker thread
		self._queue = queue.Queue()
		self._lock = threading.Lock()
		self._pending = 0  # jobs submitted and not finished: the one running, and at most one that waits
		self._bg_running = False  # the running job is a background check: a reader's request may queue behind it
		self._clients = {}  # account id -> its twikit client, so accounts that take turns keep theirs
		self._chains = {}  # (account, tab key) -> last result page of a remote feed, for "older"
		self._job_account = None  # account of the job that runs: what its pages tell about their authors is kept for it
		self._worker_thread = threading.Thread(target=self._run, name="NVTweetWorker", daemon=True)

	def start(self):
		self._worker_thread.start()

	def stop(self):
		self._queue.put(None)

	def reset_client(self, account_id=None):
		"""Forget the client of one account (new cookies, or X rejected the session), or of all."""
		if account_id is None:
			self._clients.clear()
		else:
			self._clients.pop(account_id, None)

	@property
	def busy(self):
		return self._pending > 0

	def submit(self, kind, callback, options=None, background=False):
		"""False when a job is running already. A reader's request (background False) may wait for the
		background check that is running and goes next; a background check never waits."""
		with self._lock:
			if self._pending and (background or not self._bg_running or self._pending > 1):
				return False
			self._pending += 1
			if background:
				self._bg_running = True
		self._queue.put((kind, callback, options or {}, background))
		return True

	# -------------------------------------------------------------- worker

	def _run(self):
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		while True:
			job = self._queue.get()
			if job is None:
				break
			kind, callback, options, background = job
			try:
				with isolated_modules():
					result = loop.run_until_complete(self._do(kind, options))
			except Exception as e:
				log.error(TAG + "%s failed (%s)" % (kind, type(e).__name__), exc_info=True)
				if type(e).__name__ in ("Unauthorized", "Forbidden", "InvalidSession") or isinstance(e, CookieError):
					self.reset_client(options.get("account_id"))  # re-read the cookies of this account next time
				result = _fail(kind, describe_error(e))
				if isinstance(e, CookieError):
					result["login_required"] = True
			finally:
				with self._lock:
					self._pending -= 1
					if background:
						self._bg_running = False
			result["account_id"] = options.get("account_id")
			try:
				self._notify(callback, result)
			except Exception:
				log.error(TAG + "could not deliver the result", exc_info=True)
		try:
			loop.run_until_complete(loop.shutdown_asyncgens())
			loop.close()
		except Exception:
			pass

	@staticmethod
	def _make_client(cookies):
		"""Must run inside isolated_modules()."""
		import twikit
		client = None
		if USE_IMPERSONATE:
			try:
				client = twikit.Client("en-US", impersonate=IMPERSONATE)
			except Exception:
				client = None
		if client is None:
			client = twikit.Client("en-US")
		client.set_cookies(cookies)
		return client

	def _get_client(self, account_id):
		client = self._clients.get(account_id)
		if client is not None:
			return client
		cookies = self.store.get_cookies(account_id)
		if cookies is None:
			raise CookieError("no cookies for this account")
		client = self._make_client(cookies)
		self._clients[account_id] = client
		return client

	async def _do(self, kind, options):
		if kind == "login":
			return await self._login(options)
		account_id = options.get("account_id")
		if account_id is None:
			raise CookieError("no account")
		self._job_account = account_id
		client = self._get_client(account_id)
		if kind == "reload":
			return await self._reload(client, account_id)
		if kind == "older":
			return await self._older(client, account_id)
		if kind == "thread":
			return await self._thread(client, account_id, options)
		if kind == "quotes":
			return await self._quotes(client, account_id, options)
		if kind == "tweet":
			return await self._tweet(client, account_id, options)
		if kind == "user":
			return await self._user(client, account_id, options)
		if kind == "pinned":
			return await self._pinned(client, account_id, options)
		if kind == "users":
			return await self._users(client, account_id, options)
		if kind == "relation":
			return await self._relation(client, account_id, options)
		if kind == "feed":
			return await self._feed(client, account_id, options)
		if kind == "lists":
			return await self._lists(client, account_id, options)
		if kind == "post":
			return await self._post(client, account_id, options)
		if kind == "action":
			return await self._action(client, account_id, options)
		if kind == "probe":
			from . import diagnostics  # optional file: ImportError when it is not shipped
			return await diagnostics.run_probe(self, client, account_id, options)
		raise ValueError("unknown job: %r" % (kind,))

	# -------------------------------------------------------------- login

	@staticmethod
	async def _lookup_user(client, uid):
		"""(name, user name, error text). The account's own tweets first (that request works),
		then the user lookup, which X currently answers with 403."""
		errors = []
		try:
			page = await client.get_user_tweets(uid, "Tweets", count=5)
			for tw in page:
				user = _val(tw, "user")
				if user is not None and str(_val(user, "id")) == str(uid):
					name = str(_val(user, "name") or "").strip()
					handle = str(_val(user, "screen_name") or "").strip()
					if name or handle:
						return name, handle, ""
			errors.append("get_user_tweets found no tweet of this account")
		except Exception as e:
			errors.append("get_user_tweets " + _short_error(e))
		try:
			user = await client.get_user_by_id(uid)
			name = str(_val(user, "name") or "").strip()
			handle = str(_val(user, "screen_name") or "").strip()
			if name or handle:
				return name, handle, ""
			errors.append("get_user_by_id returned no name")
		except Exception as e:
			errors.append("get_user_by_id " + _short_error(e))
		return "", "", "; ".join(errors)

	async def _login(self, options):
		"""Check the cookies with a timeline request, then find out whose they are."""
		cookies = dict(options["cookies"])
		client = self._make_client(cookies)
		await client.get_latest_timeline(count=1)
		uid = user_id_from_twid(cookies.get("twid"))
		name = handle = error = ""
		if uid:
			name, handle, error = await self._lookup_user(client, uid)
		else:
			uid = hashed_uid(cookies["auth_token"])
			error = "the cookies have no twid"
		return _ok(
			"login", [], cookies=cookies, lookup_error=error,
			identity={"uid": uid, "handle": handle, "name": name},
		)

	# -------------------------------------------------------------- timeline jobs

	def _collect(self, page, keep_order=False):
		"""Posts of one result page, newest first (or in the server's order). Also salvages twifork's overflow
		buffer, because we persist the cursor and would otherwise lose those tweets. What the page tells about
		how the account stands to the authors (following...) goes to the table of relations, at no cost."""
		tweets = list(page)
		tweets.extend(list(getattr(page, "_Result__overflow", None) or []))
		posts = {}
		rels = {}
		for tw in tweets:
			try:
				p = tweet_to_post(tw)
			except Exception:
				log.error(TAG + "skipping a tweet that could not be read", exc_info=True)
				continue
			posts.setdefault(p.id, p)
			self._note_authors(rels, tw)
		if rels and self._job_account is not None:
			try:
				self.store.set_relations(self._job_account, list(rels.values()))
			except Exception:
				log.error(TAG + "could not keep the relations of the authors", exc_info=True)
		if keep_order:
			return list(posts.values())
		return sorted(posts.values(), key=lambda p: p.id, reverse=True)

	@staticmethod
	def _note_authors(rels, tw):
		"""The author of a tweet, and of the tweet it retweets, with the flags X sent (None: X did not send it)."""
		for tweet in (tw, _val(tw, "retweeted_tweet")):
			user = _val(tweet, "user") if tweet is not None else None
			handle = str(_val(user, "screen_name") or "").lower()
			flags = {key: _flag(user, key) for key in ("following", "followed_by", "muting", "blocking")}
			if handle and any(v is not None for v in flags.values()):
				rels[handle] = dict(flags, handle=handle, uid=str(_val(user, "id") or ""))

	async def _reload(self, client, account_id):
		page = await client.get_latest_timeline(count=FETCH_COUNT)
		posts = self._collect(page)
		if not posts:
			return _ok("reload", [], new=0)
		had_cached = bool(self.store.load_recent(account_id, 1))
		overlap = bool(self.store.known_ids(account_id, [p.id for p in posts]))
		gathered = {p.id: p for p in posts}
		walks = 1
		# More new tweets than one page holds (or we were away for a while): read older pages
		# until one meets what we already have, so the list stays in one piece without the
		# reader having to ask for it.
		limited = False
		while had_cached and not overlap and walks < self.MAX_WALK:
			cursor = getattr(page, "next_cursor", None)
			if not cursor:
				break
			await self._pause()
			try:
				nxt = await client.get_latest_timeline(count=FETCH_COUNT, cursor=cursor)
			except Exception as e:
				if type(e).__name__ != "TooManyRequests":
					raise
				limited = True  # keep what was already read
				break
			more = self._collect(nxt)
			if not more:
				break
			page = nxt
			for p in more:
				gathered.setdefault(p.id, p)
			overlap = bool(self.store.known_ids(account_id, [p.id for p in more]))
			walks += 1
		everything = sorted(gathered.values(), key=lambda p: p.id, reverse=True)
		known = self.store.known_ids(account_id, [p.id for p in everything])
		next_cursor = getattr(page, "next_cursor", None)
		# Keep the existing "older" position while this run connects to what we already
		# have; start a new chain if it does not (first run, or a hole we could not close).
		if next_cursor and (not self.store.get_account_meta(account_id, CURSOR_KEY) or not overlap):
			self.store.set_account_meta(account_id, CURSOR_KEY, next_cursor)
		self.store.add_posts(account_id, everything)
		return _ok(
			"reload", everything, gap=had_cached and not overlap, new=len(everything) - len(known),
			limited=limited,
		)

	async def _older(self, client, account_id):
		collected = []
		cursor = self.store.get_account_meta(account_id, CURSOR_KEY)
		if not cursor:
			page0 = await client.get_latest_timeline(count=FETCH_COUNT)
			first = self._collect(page0)
			self.store.add_posts(account_id, first)
			collected.extend(first)
			cursor = getattr(page0, "next_cursor", None)
			if not cursor:
				return _fail("older", _("Could not find an older position."))
		try:
			page = await client.get_latest_timeline(count=FETCH_COUNT, cursor=cursor)
		except Exception:
			# The saved position may be stale: start a fresh chain next time.
			self.store.set_account_meta(account_id, CURSOR_KEY, "")
			raise
		posts = self._collect(page)
		joined = bool(self.store.known_ids(account_id, [p.id for p in posts]))  # met what we had: a gap closed
		self.store.add_posts(account_id, posts)
		next_cursor = getattr(page, "next_cursor", None)
		if next_cursor:
			self.store.set_account_meta(account_id, CURSOR_KEY, next_cursor)
		collected.extend(posts)
		return _ok("older", collected, connected=joined)

	# -------------------------------------------------------------- thread, quotes, details

	async def _thread(self, client, account_id, options):
		"""Ancestors, the tweet and its replies (up to three levels), in reading order.
		options["pages"]: how many pages of replies to read (X gives about 20 conversations per page).
		options["more"] with options["have"] (ids the tab shows): only the next page that has something new."""
		tweet_id = str(options["id"])
		chain = (account_id, "thread:" + tweet_id)
		if options.get("more"):
			return await self._thread_more(client, chain, tweet_id, set(options.get("have") or []))
		full = await client.get_tweet_by_id(tweet_id)
		posts = []
		seen = set()

		def add(tw, depth):
			try:
				post = tweet_to_post(tw)
			except Exception:
				log.error(TAG + "skipping a tweet that could not be read", exc_info=True)
				return
			if post.id in seen:
				return
			seen.add(post.id)
			post.depth = depth
			posts.append(post)

		def walk(tw, depth):
			children = _val(tw, "replies")
			if not children:
				return
			for reply in list(children):
				add(reply, depth)
				if depth < 3:
					walk(reply, depth + 1)

		parents = _val(full, "reply_to")
		if isinstance(parents, (list, tuple)):
			for tw in sorted(parents, key=lambda t: _int(_val(t, "id")) or 0):
				add(tw, 0)
		add(full, 0)
		walk(full, 1)
		page, limited = _val(full, "replies"), False  # get_tweet_by_id gives the first page only
		for _n in range(max(1, int(options.get("pages") or 1)) - 1):
			nxt, limited = await self._next_page(page)
			if nxt is None:
				break
			page = nxt
			for reply in list(page):
				add(reply, 1)
				walk(reply, 2)
		if page is not None:
			self._chains[chain] = page  # where "more" goes on
		return _ok("thread", posts, target_id=tweet_id, limited=limited)

	@classmethod
	async def _next_page(cls, page):
		"""(the page after this one, True if X limited the requests). None when there is nothing more."""
		if page is None:
			return None, False
		await cls._pause()
		try:
			nxt = page.next()
			if inspect.isawaitable(nxt):
				nxt = await nxt
		except Exception as e:
			if type(e).__name__ != "TooManyRequests":
				raise
			return None, True
		if nxt is None or not len(nxt):
			return None, False
		return nxt, False

	async def _thread_more(self, client, chain, tweet_id, have):
		"""The next page of replies that brings something the tab does not show yet."""
		page = self._chains.get(chain)
		items = []
		if page is None:
			# no place remembered (the tab came from the cache): the first page again
			page = _val(await client.get_tweet_by_id(tweet_id), "replies")
			items = list(page) if page is not None else []
		news = [t for t in items if (_int(_val(t, "id")) or 0) not in have]
		walks, limited = 0, False
		while not news and walks < self.MAX_WALK:
			nxt, limited = await self._next_page(page)
			if nxt is None:
				break
			page, items = nxt, list(nxt)
			news = [t for t in items if (_int(_val(t, "id")) or 0) not in have]
			walks += 1
		if page is not None:
			self._chains[chain] = page
		posts = []
		for tw in news:
			try:
				post = tweet_to_post(tw)
			except Exception:
				log.error(TAG + "skipping a tweet that could not be read", exc_info=True)
				continue
			post.depth = 1
			posts.append(post)
		return _ok("thread", posts, target_id=tweet_id, limited=limited)

	@staticmethod
	def _quoting(tweets, tweet_id):
		"""The tweets of this result that quote tweet_id (the search also returns tweets that only mention it)."""
		return [
			t for t in tweets
			if str(_legacy(_shown(t)).get("quoted_status_id_str") or "") == str(tweet_id)
		]

	async def _search_quotes(self, client, tweet_id, handle, pages=1):
		"""Tweets that quote tweet_id, from the first search that finds any, read for up to pages pages.
		Returns (tweets, notes, page, limited); page is where the reading stopped, for "more"."""
		queries = ["quoted_tweet_id:%s" % tweet_id]
		if handle:
			queries.append('url:"x.com/%s/status/%s"' % (handle, tweet_id))
		notes = []
		for query in queries:
			for product in ("Top", "Latest"):
				try:
					page = await client.search_tweet(query, product, count=20)
				except Exception as e:
					notes.append("%s [%s]: FAIL %s" % (query, product, _short_error(e)))
					continue
				found = list(page)
				mine = self._quoting(found, tweet_id)
				read, limited = 1, False
				while read < pages:
					nxt, limited = await self._next_page(page)
					if nxt is None:
						break
					page = nxt
					found += list(page)
					mine += self._quoting(page, tweet_id)
					read += 1
				notes.append("%s [%s]: %d pages, %d found, %d quote it" % (query, product, read, len(found), len(mine)))
				if mine:
					return mine, notes, page, limited
		return [], notes, None, False

	@staticmethod
	def _quote_posts(found, skip=()):
		"""Posts of these tweets, newest first, without the ids in skip."""
		posts = {}
		for tw in found:
			try:
				post = tweet_to_post(tw)
			except Exception:
				log.error(TAG + "skipping a tweet that could not be read", exc_info=True)
				continue
			if post.id not in skip:
				posts.setdefault(post.id, post)
		return sorted(posts.values(), key=lambda p: p.id, reverse=True)

	async def _quotes(self, client, account_id, options):
		"""options["pages"]: pages of search results to read. options["more"] with options["have"]
		(ids the tab shows): only the next page that has a quote the tab does not show yet."""
		tweet_id = str(options["id"])
		chain = (account_id, "quotes:" + tweet_id)
		handle = options.get("handle") or ""
		have = set(options.get("have") or [])
		page = self._chains.get(chain) if options.get("more") else None
		limited = False
		if options.get("more") and page is not None:
			notes, found = [], []
			for _walk in range(self.MAX_WALK):
				nxt, limited = await self._next_page(page)
				if nxt is None:
					break
				page = nxt
				found = self._quoting(page, tweet_id)
				if any((_int(_val(t, "id")) or 0) not in have for t in found):
					break
		else:
			pages = max(1, int(options.get("pages") or 1))
			if options.get("more"):
				pages = min(self.MAX_WALK * 2, len(have) // 20 + 2)  # no place remembered: read as deep as the tab is
			found, notes, page, limited = await self._search_quotes(client, tweet_id, handle, pages)
		if page is not None:
			self._chains[chain] = page
		return _ok("quotes", self._quote_posts(found, have), notes=notes, limited=limited)

	async def _tweet(self, client, account_id, options):
		"""Fresh counts and like/bookmark state of one tweet (the full tweet window)."""
		full = await client.get_tweet_by_id(str(options["id"]))
		return _ok("tweet", [tweet_to_post(full)])

	async def _user(self, client, account_id, options):
		"""Profile of one user for the user information window. A value X does not give stays None."""
		user = await client.get_user_by_screen_name(str(options["handle"]))
		joined = _val(user, "created_at_datetime")
		try:
			joined = int(joined.timestamp()) if joined is not None else None
		except Exception:
			joined = None
		info = {
			"name": str(_val(user, "name") or ""), "handle": str(_val(user, "screen_name") or options["handle"]),
			"bio": str(_val(user, "description") or ""), "location": str(_val(user, "location") or ""),
			"joined": joined,
			"followers": _int(_val(user, "followers_count")), "following": _int(_val(user, "following_count")),
			"tweets": _int(_val(user, "statuses_count")), "likes": _int(_val(user, "favourites_count")),
			"you_follow": _val(user, "following"), "follows_you": _val(user, "followed_by"),
			"muted": _val(user, "muting"), "blocked": _val(user, "blocking"), "can_dm": _val(user, "can_dm"),
			"protected": _val(user, "protected"),
			"verified": bool(_val(user, "is_blue_verified") or _val(user, "verified")),
		}
		self.store.set_relations(account_id, [dict(
			{key: _flag(user, key) for key in ("following", "followed_by", "muting", "blocking")},
			handle=info["handle"].lower(), uid=str(_val(user, "id") or ""),
		)])
		return _ok("user", [], info=info)

	async def _pinned(self, client, account_id, options):
		"""The pinned tweet of a user, as a thread. pinned is None when there is none; unavailable is True
		when X still lists a pinned id but the tweet is gone."""
		user = await client.get_user_by_screen_name(str(options["handle"]))
		ids = [str(i) for i in (_val(user, "pinned_tweet_ids") or [])]
		if not ids:
			return _ok("pinned", [], pinned=None)
		try:
			result = await self._thread(client, account_id, {"id": ids[0], "pages": options.get("pages")})
		except Exception as e:
			if type(e).__name__ != "TweetNotAvailable":
				raise
			return _ok("pinned", [], pinned=ids[0], unavailable=True)
		result["pinned"] = ids[0]
		return result

	# -------------------------------------------------------------- remote feeds and lists

	MAX_WALK = 5  # pages looked at for one "older", or for a refresh that has to meet what the tab has
	INITIAL_PAGES = 6  # pages read when a tab has nothing yet (X gives about 20 per page)
	PAGE_DELAY = (0.8, 1.6)  # seconds waited (random) before every extra page: gentler on X

	@classmethod
	async def _pause(cls):
		await asyncio.sleep(random.uniform(*cls.PAGE_DELAY))

	@staticmethod
	async def _first_page(client, source, params):
		if source == "bookmarks":
			return await client.get_bookmarks(count=FETCH_COUNT)
		if source == "notifications":
			return await client.get_notifications("All", count=FETCH_COUNT)
		if source == "user":
			return await client.get_user_tweets(params["uid"], "Tweets", count=FETCH_COUNT)
		if source == "search":
			return await client.search_tweet(params["query"], params.get("product") or "Latest", count=FETCH_COUNT)
		if source == "list":
			return await client.get_list_tweets(str(params["list_id"]), count=FETCH_COUNT)
		raise ValueError("unknown feed: %r" % (source,))

	def _convert(self, source, page):
		if source == "notifications":
			posts = {}
			for i, n in enumerate(list(page)):
				try:
					p = notification_to_post(n, i)
				except Exception:
					log.error(TAG + "skipping a notification that could not be read", exc_info=True)
					continue
				raw = getattr(self, "_notif_raw", None)  # TEMP
				if raw is not None:  # TEMP
					raw.setdefault(p.id, (_val(n, "id"), _int(_val(n, "timestamp_ms"))))  # TEMP
				posts.setdefault(p.id, p)
			return list(posts.values())
		return self._collect(page, keep_order=source == "bookmarks")

	def _log_notification_diff(self, account_id, gathered, seen):  # TEMP
		"""Why does a refresh find notifications the tab does not know? Logs ids, never content."""
		try:
			fresh = [p for p in gathered if p.id not in seen]
			if not seen or not fresh:
				return
			from . import tabstate
			cached = tabstate.get_cache(self.store, account_id, "notifications", "notifications") or []
			raw = getattr(self, "_notif_raw", None) or {}
			log.info(TAG + "TEMP notif: %d fetched, %d unknown, %d cached" % (len(gathered), len(fresh), len(cached)))
			for p in fresh[:6]:
				twin = next((c for c in cached if c.author == p.author and c.message == p.message), None)
				twin_text = "none" if twin is None else "id=%d ts=%d" % (twin.id, twin.ts)
				log.info(
					TAG + "TEMP notif fresh id=%d raw=%s ts=%d len=%d twin=%s"
					% (p.id, raw.get(p.id), p.ts, len(p.message), twin_text)
				)
		except Exception:
			log.error(TAG + "TEMP notif log failed", exc_info=True)
	async def _feed(self, client, account_id, options):
		"""First page of a remote feed, or (options["have"] = ids the tab shows) the next page
		that brings something the tab does not have yet."""
		source, params = options["source"], dict(options.get("params") or {})
		chain = (account_id, options.get("chain"))
		have = options.get("have")
		self._notif_raw = {} if source == "notifications" else None  # TEMP
		pinned = [str(i) for i in (options.get("pinned") or [])]  # the pinned tweet, as the tab knows it
		if source == "user" and (not params.get("uid") or options.get("pinned_lookup")):
			# The user's own data: the id to ask for tweets, and the pinned tweet. The pinned tweet is only looked
			# up when a tab is opened (pinned_lookup), so a refresh costs no extra request.
			user = await client.get_user_by_screen_name(params["handle"])
			params["uid"] = params.get("uid") or str(_val(user, "id") or "")
			if not params["uid"]:
				raise ValueError("no user id")
			pinned = [str(i) for i in (_val(user, "pinned_tweet_ids") or [])]
		page = self._chains.get(chain) if have is not None else None
		if page is None:
			page = await self._first_page(client, source, params)
			items = self._convert(source, page)
			if have is None:
				# A refresh. X gives about 20 per page: with the ids the tab already has, keep
				# reading pages until one overlaps them, so a busy feed leaves no hole. The pinned tweet
				# is in every first page, so it does not count as an overlap.
				seen = {i for i in (options.get("known") or []) if str(i) not in pinned}
				gathered = list(items)
				walks = 1
				limit = self.MAX_WALK if seen else int(options.get("pages") or self.INITIAL_PAGES)  # nothing to meet: read a few pages
				limited = False
				while source != "bookmarks" and walks < limit and not (
					seen and any(p.id in seen for p in gathered)
				):
					await self._pause()
					try:
						nxt = page.next()
						if inspect.isawaitable(nxt):
							nxt = await nxt
					except Exception as e:
						if type(e).__name__ != "TooManyRequests":
							raise
						limited = True  # keep what was already read
						break
					more = self._convert(source, nxt) if nxt is not None else []
					if not more:
						break
					page = nxt
					gathered.extend(more)
					walks += 1
				self._chains[chain] = page
				if source == "notifications":  # TEMP
					self._log_notification_diff(account_id, gathered, seen)  # TEMP
				for p in gathered:
					if str(p.id) in pinned:
						p.stats = set_stat(p.stats, "pinned", True)
				gap = bool(seen) and not any(p.id in seen for p in gathered)
				return _ok("feed", gathered, uid=params.get("uid"), gap=gap, limited=limited)
			self._chains[chain] = page
			if any(p.id not in set(have) for p in items):
				return _ok("feed", items, uid=params.get("uid"))
		known = set(have or [])
		for _walk in range(self.MAX_WALK):
			if _walk:
				await self._pause()
			nxt = page.next()
			if inspect.isawaitable(nxt):
				nxt = await nxt
			items = self._convert(source, nxt) if nxt is not None else []
			if not items:
				break
			page = nxt
			self._chains[chain] = page
			if any(p.id not in known for p in items):
				return _ok("feed", items, uid=params.get("uid"))
		return _ok("feed", [], uid=params.get("uid"))

	_RELATION_CALLS = {
		"follow": "follow_user", "unfollow": "unfollow_user", "mute": "mute_user", "unmute": "unmute_user",
		"block": "block_user", "unblock": "unblock_user",
	}

	_RELATION_EFFECT = {  # what each change sets in the table of relations
		"follow": {"following": True}, "unfollow": {"following": False},
		"mute": {"muting": True}, "unmute": {"muting": False},
		"block": {"blocking": True, "following": False, "followed_by": False}, "unblock": {"blocking": False},
	}

	async def _relation(self, client, account_id, options):
		"""options["do"] (follow, unfollow, mute, unmute, block or unblock) changes how the account stands to the
		user options["handle"] (options["uid"] when it is known). Without it: how the user stands to the account
		now. Either way the table of relations is kept up to date."""
		handle = str(options["handle"]).lower()
		action = options.get("do")
		uid = str(options.get("uid") or "")
		if action:
			if not uid:
				uid = str(_val(await client.get_user_by_screen_name(handle), "id") or "")
			if not uid:
				raise ValueError("no user id")
			await getattr(client, self._RELATION_CALLS[action])(uid)
			self.store.set_relations(account_id, [dict(self._RELATION_EFFECT[action], handle=handle, uid=uid)])
			return _ok("relation", [], done=action)
		user = await client.get_user_by_screen_name(handle)
		uid = str(_val(user, "id") or uid)
		if not uid:
			raise ValueError("no user id")
		rel = {key: bool(_val(user, key)) for key in ("following", "followed_by", "muting", "blocking")}
		self.store.set_relations(account_id, [dict(rel, handle=handle, uid=uid)])
		return _ok("relation", [], state={key: rel[key] for key in ("following", "muting", "blocking")}, uid=uid)

	@staticmethod
	def _convert_users(page):
		"""The rows of one page of users, without repeats: id, user name, display name, the bio on one line, and
		"rel": what the user object tells about how the account stands to that user (_record_users keeps it)."""
		users = list(page) + list(getattr(page, "_Result__overflow", None) or [])
		rows = {}
		for u in users:
			uid, handle = str(_val(u, "id") or ""), str(_val(u, "screen_name") or "")
			if uid and handle:
				rows.setdefault(uid, {
					"id": uid, "handle": handle, "name": str(_val(u, "name") or ""),
					"bio": " ".join(str(_val(u, "description") or "").split()),
					"rel": {key: _flag(u, key) for key in ("following", "followed_by", "muting", "blocking")},
				})
		return list(rows.values())

	USERS_PAGE = 50  # users asked for in one request: X sends 50 at most (asking for 100 gave 50)
	MAX_ALL_PAGES = 100  # pages read at most when a whole list is needed (the account's own lists)
	_USER_LISTS = {  # kind -> (client method, whether it asks for the id of a user)
		"followers": ("get_user_followers", True), "following": ("get_user_following", True),
		"known_followers": ("get_user_followers_you_know", True),
		"muted": ("get_muted_users", False), "blocked": ("get_blocked_users", False),
	}
	_LIST_FLAG = {"following": "following", "muted": "muting", "blocked": "blocking"}  # lists that are a relation

	def _own_uid(self, account_id):
		return user_id_from_twid((self.store.get_cookies(account_id) or {}).get("twid"))

	def _record_users(self, account_id, rows, flag=None, complete=False):
		"""Keep what a list of users tells about how the account stands to them, and return the rows without it.
		flag: the list is one of the account's own (following, muting or blocking): everyone in it has the flag,
		and when the whole list was read (complete) nobody else has it."""
		rels = []
		for r in rows:
			rel = dict(r.get("rel") or {}, handle=r["handle"].lower(), uid=r["id"])
			if flag:
				rel[flag] = True
			rels.append(rel)
		if flag and complete:
			self.store.replace_relation_list(account_id, flag, rels)
		else:
			self.store.set_relations(account_id, rels)
		return [{k: v for k, v in r.items() if k != "rel"} for r in rows]

	async def _users(self, client, account_id, options):
		"""A list of users: the followers, the following or the followers you know of a user, or the users the
		account muted or blocked (options["kind"]). With options["have"] (ids the tab shows) it is the next page
		that has someone new, from the place kept in _chains; without it the first options["pages"] pages, or all
		of them with options["all"]. What the users tell about how the account stands to them is kept."""
		kind, params = options["kind"], dict(options.get("params") or {})
		method, needs_uid = self._USER_LISTS[kind]
		chain = (account_id, "users:%s" % options.get("chain"))
		have = options.get("have")
		own_uid = self._own_uid(account_id)
		if needs_uid and not params.get("uid"):
			params["uid"] = own_uid if params.get("own") else ""
			if not params["uid"]:
				params["uid"] = str(_val(await client.get_user_by_screen_name(params["handle"]), "id") or "")
			if not params["uid"]:
				raise ValueError("no user id")
		uid = params.get("uid")
		flag = self._LIST_FLAG.get(kind) if (not needs_uid or (own_uid and uid == own_uid)) else None
		fetch = getattr(client, method)
		page = self._chains.get(chain) if have is not None else None
		limited = False
		if page is None:
			page = await (fetch(uid, count=self.USERS_PAGE) if needs_uid else fetch())
			items = self._convert_users(page)
			if have is None:
				gathered, complete = list(items), False
				budget = self.MAX_ALL_PAGES if options.get("all") else max(1, int(options.get("pages") or 1))
				for _n in range(budget - 1):
					nxt, limited = await self._next_page(page)
					if nxt is None:
						complete = not limited
						break
					page = nxt
					gathered.extend(self._convert_users(page))
				self._chains[chain] = page
				rows, seen = [], set()
				for u in gathered:
					if u["id"] not in seen:
						seen.add(u["id"])
						rows.append(u)
				rows = self._record_users(account_id, rows, flag, complete and bool(options.get("all")))
				return _ok("users", [], users=rows, uid=uid, limited=limited, complete=complete)
			self._chains[chain] = page
			if any(u["id"] not in set(have) for u in items):
				return _ok("users", [], users=self._record_users(account_id, items), uid=uid)
		known = set(have or [])
		for _walk in range(self.MAX_WALK):
			nxt, limited = await self._next_page(page)
			if nxt is None:
				break
			page = nxt
			self._chains[chain] = page
			items = self._convert_users(page)
			log.info(TAG + "TEMP users page of %s: %d users, %d new" % (kind, len(items), sum(1 for u in items if u["id"] not in known)))  # TEMP
			if any(u["id"] not in known for u in items):
				return _ok("users", [], users=self._record_users(account_id, items), uid=uid)
		return _ok("users", [], users=[], uid=uid, limited=limited)

	async def _lists(self, client, account_id, options):
		lists = list(await client.get_lists(count=50))
		items = [{"id": str(_val(l, "id")), "name": str(_val(l, "name") or "")} for l in lists if _val(l, "id")]
		return _ok("lists", [], items=items)

	# -------------------------------------------------------------- posting

	async def _post(self, client, account_id, options):
		"""A new tweet, a reply (reply_to = tweet id) or a quote (quote_url = link of the quoted tweet)."""
		kwargs = {}
		if options.get("reply_to"):
			kwargs["reply_to"] = str(options["reply_to"])
		if options.get("quote_url"):
			kwargs["attachment_url"] = options["quote_url"]
		tweet = await client.create_tweet(text=options["text"], **kwargs)
		try:
			posts = [tweet_to_post(tweet)]
		except Exception:
			log.error(TAG + "could not read the tweet that was just posted", exc_info=True)
			posts = []
		return _ok("post", posts)

	# -------------------------------------------------------------- actions

	async def _action(self, client, account_id, options):
		action = options.get("action")
		tweet_id = str(options["id"])
		row_id = int(options["row_id"])
		if action == "like":
			await client.favorite_tweet(tweet_id)
			flag = ("liked", True)
		elif action == "unlike":
			await client.unfavorite_tweet(tweet_id)
			flag = ("liked", False)
		elif action == "bookmark":
			await client.bookmark_tweet(tweet_id)
			flag = ("bookmarked", True)
		elif action == "unbookmark":
			await client.delete_bookmark(tweet_id)
			flag = ("bookmarked", False)
		elif action == "retweet":
			await client.retweet(tweet_id)
			flag = None
		elif action == "unretweet":
			await client.delete_retweet(tweet_id)
			flag = None
		else:
			raise ValueError("unknown action: %r" % (action,))
		if flag is not None:
			self.store.set_flag(account_id, row_id, flag[0], flag[1])
		return _ok("action", [], action=action, row_id=row_id)