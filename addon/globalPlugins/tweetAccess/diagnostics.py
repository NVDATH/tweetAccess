# -*- coding: utf-8 -*-
# NOT TRANSLATED ON PURPOSE. Developer-only file: the release build leaves it out, so none of its
# strings may be wrapped in _() and none may reach the translators. I18N reviews must skip this
# whole file, including the Diagnostics settings page and its tab title (TITLE below).
#
# The probe job (run_probe) and its settings page. Read-only steps always run; the write, posting
# and media tests are opt-in and delete what they create.

import asyncio
import os
import sys
import tempfile
import textwrap
import time

import ui
import wx
from logHandler import log

import gui.nvdaControls

from .service import IMAGE_SHRINK_TARGET, _legacy, _ok, _short_error, _shown, _val, shrink_image, tweet_to_post
from .utils import IMPERSONATE, TAG, user_id_from_twid

_PROBE_METHODS = (
	"get_user_by_id", "get_user_tweets", "get_tweet_by_id", "search_tweet", "user",
	"create_tweet", "delete_tweet", "retweet", "delete_retweet",
	"get_bookmarks", "get_user_by_screen_name", "get_notifications", "get_lists", "get_list_tweets",
	"get_trends", "get_place_trends", "get_available_locations", "search_user",
	"get_user_followers", "get_user_following", "get_user_followers_you_know",
	"upload_media", "create_media_metadata", "get_dm_history", "send_dm",
	"follow_user", "unfollow_user", "mute_user", "unmute_user", "block_user", "unblock_user",
)
# method name fragments worth listing in the report
_TOPICS = ("dm", "message", "follow", "mute", "block", "pin", "trend", "locat", "media", "list", "search", "user", "notif", "bookmark")


# Steps whose questions are answered: a normal run leaves them out, the "full run" checkbox brings them back.
CONFIRMED_STEPS = (
	"Client methods", "Timeline (get_latest_timeline)", "Search operators (about 23 requests)",
	"People search (search_user)", "Users and relations", "DM inbox, muted and blocked users (read only)",
	"Pinned tweets", "Direct messages (read only)", "Bookmarks", "Notifications", "Lists",
	"Relation flags of the timeline authors, page sizes of the account's own lists",
	"Direct message history (counts only)", "Trends (get_trends, places)",
	"Direct message history, raw structure (field names and counts only)",
	"Direct message history of the first 8 conversations (counts only)",
)


async def _tries(func, *args, **kwargs):
	"""X answers slowly now and then: a ReadTimeout is tried again, twice, before it counts as a failure."""
	for attempt in range(3):
		try:
			return await func(*args, **kwargs)
		except Exception as e:
			if type(e).__name__ != "ReadTimeout" or attempt == 2:
				raise
			await asyncio.sleep(2)


def _field(obj, name):
	"""A field of something X sent, whether it is a dict or an object."""
	return obj.get(name) if isinstance(obj, dict) else _val(obj, name)


def _fields(obj):
	"""The short fields of a dict or an object, as name=value."""
	names = list(obj) if isinstance(obj, dict) else [a for a in dir(obj) if not a.startswith("_")]
	out = []
	for name in names:
		value = _field(obj, name)
		if not callable(value):
			out.append("%s=%s" % (name, " ".join(str(value).split())[:40]))
	return ", ".join(out)


def _indent(text):
	lines = text.splitlines() if isinstance(text, str) else list(text)
	return "\n".join("    " + line for line in lines)


def _convertible(tweets):
	"""How many of these tweets our own converter can turn into rows."""
	good = bad = 0
	for tw in tweets:
		try:
			tweet_to_post(tw)
			good += 1
		except Exception:
			bad += 1
	return "%d convertible, %d not" % (good, bad)


def _paging(page):
	return "next_cursor: %s" % bool(getattr(page, "next_cursor", None))


def _relation(user):
	"""The flags X gives with a user: do the lists carry what Follow / Mute / Block need to know?"""
	return "@%s following=%s followed_by=%s muting=%s blocking=%s" % (
		_val(user, "screen_name"), _val(user, "following"), _val(user, "followed_by"), _val(user, "muting"),
		_val(user, "blocking"),
	)


async def _delete_with_retry(client, tweet_id, notes):
	"""X answers slowly now and then: try three times before asking the reader to delete a test tweet by hand."""
	error = ""
	for _attempt in range(3):
		try:
			await client.delete_tweet(tweet_id)
			notes.append("deleted test tweet %s" % tweet_id)
			return
		except Exception as e:
			error = _short_error(e)
			await asyncio.sleep(3)
	notes.append("COULD NOT DELETE test tweet %s after 3 tries - delete it by hand: %s" % (tweet_id, error))


async def run_probe(service, client, account_id, options):
	"""Looks at what X really sends and at which features could work."""
	import twikit
	lines = []
	ctx = {}
	started = time.time()
	write_tests = bool(options.get("write_tests"))
	post_tests = bool(options.get("post_tests"))
	media_tests = bool(options.get("media_tests"))
	mute_tests = bool(options.get("mute_tests"))
	block_tests = bool(options.get("block_tests"))
	dm_tests = bool(options.get("dm_tests"))
	full = bool(options.get("full"))

	def add(text=""):
		lines.append(text)

	async def step(title, func):
		if title in CONFIRMED_STEPS and not full:
			return  # answered before: only a full run looks at it again
		t0 = time.time()
		await asyncio.sleep(0.5)
		try:
			info = await func()
		except Exception as e:
			log.error(TAG + "probe step failed: %s (%s)" % (title, type(e).__name__), exc_info=True)
			add("[FAIL] %s (%.1fs): %s" % (title, time.time() - t0, _short_error(e)))
			return
		add("[ OK ] %s (%.1fs)%s" % (title, time.time() - t0, (": " + info) if info else ""))

	add("Probe report")
	add("Time: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
	add("Python: %s" % sys.version.split()[0])
	add("twikit/twifork: %s" % getattr(twikit, "__version__", "?"))
	add("Write tests: %s; posting tests: %s; media tests: %s; mute test: %s; block test: %s; direct message test: %s; full run: %s" % tuple(
		"yes" if v else "no" for v in (write_tests, post_tests, media_tests, mute_tests, block_tests, dm_tests, full)
	))
	missing = [n for n in _PROBE_METHODS if not callable(getattr(client, n, None))]
	add("Methods missing: " + (", ".join(missing) or "none"))
	add()

	def first_user():
		"""(user id, screen name) of the author of the first timeline tweet: an account other than ours."""
		for t in ctx.get("tweets") or []:
			user = _val(t, "user")
			if user is not None and _val(user, "id") and _val(user, "screen_name"):
				return str(_val(user, "id")), str(_val(user, "screen_name"))
		return "", ""

	def users_pool(limit=8):
		seen, out = set(), []
		for t in ctx.get("tweets") or []:
			user = _val(t, "user")
			uid = str(_val(user, "id") or "")
			if user is not None and uid and uid not in seen and _val(user, "screen_name"):
				seen.add(uid)
				out.append((uid, str(_val(user, "screen_name"))))
			if len(out) >= limit:
				break
		return out

	# ---------------------------------------------------------------- what is there at all

	async def methods_view():
		names = sorted(n for n in dir(client) if not n.startswith("_") and callable(getattr(client, n, None)))
		notes = ["%d public methods" % len(names)]
		for topic in _TOPICS:
			found = [n for n in names if topic in n]
			if found:
				notes.append("%s: %s" % (topic, ", ".join(found)))
		return "\n" + _indent(notes)

	async def timeline():
		tweets = list(await client.get_latest_timeline(count=20))
		ctx["tweets"] = tweets
		return "%d tweets, %s" % (len(tweets), _convertible(tweets))

	await step("Client methods", methods_view)
	await step("Timeline (get_latest_timeline)", timeline)

	# ---------------------------------------------------------------- search operators

	async def operators_view():
		uid, screen = first_user()
		base = "news lang:en"
		ops = [
			"", "-filter:retweets", "-filter:replies", "-filter:native_video", "filter:media", "filter:images",
			"filter:videos", "filter:links", "filter:quote", "-filter:quote", "filter:self_threads",
			"filter:follows", "filter:verified", "filter:hashtags", "filter:mentions",
			"min_faves:50", "min_retweets:10", "min_replies:5",
			"since:%s until:%s" % (
				time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400)),
				time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400)),
			),
			"-from:%s" % screen, "from:%s" % screen,
			"to:%s" % screen, "@%s" % screen,
		]
		notes = []
		for op in ops:
			query = ("%s %s" % (base, op)).strip()
			try:
				page = await client.search_tweet(query, "Latest", count=20)
				notes.append("%-48s %d tweets" % (op or "(no operator)", len(list(page))))
			except Exception as e:
				notes.append("%-48s FAIL %s" % (op, _short_error(e)))
			await asyncio.sleep(0.7)
		return "\n" + _indent(notes)

	await step("Search operators (about 23 requests)", operators_view)

	# ---------------------------------------------------------------- trends and people search

	async def trends_view():
		notes = []

		def peek(obj):
			"""Field=value of an object, short values only: what could a row show?"""
			out = []
			for name in dir(obj):
				if name.startswith("_"):
					continue
				value = _val(obj, name)
				if not callable(value):
					out.append("%s=%s" % (name, " ".join(str(value).split())[:40]))
			return ", ".join(out)

		for category in (("trending", "for-you", "news", "sports", "entertainment") if full else ()):
			try:
				items = list(await client.get_trends(category, count=20))
				first = ", ".join(str(_val(t, "name")) for t in items[:3])
				notes.append("get_trends %s: %d items; first: %s" % (category, len(items), first))
				if items:
					notes.append("  first item: " + peek(items[0]))
					data = _val(items[0], "_data")
					if isinstance(data, dict):
						notes.append("  raw keys: %s; trend_url: %s" % (
							", ".join(sorted(data)), " ".join(str(data.get("trend_url")).split())[:120],
						))
			except Exception as e:
				notes.append("get_trends %s: FAIL %s" % (category, _short_error(e)))
		try:
			places = list(await client.get_available_locations())
			notes.append("get_available_locations: %d places; first: %s" % (
				len(places), ", ".join(str(_val(p, "name")) for p in places[:5]),
			))
			if places:
				notes.append("  first place: " + peek(places[0]))
			for name in ("Worldwide", "Thailand", "Bangkok"):
				place = next((p for p in places if str(_val(p, "name")) == name), None)
				if place is None:
					notes.append("  %s: not in the list" % name)
					continue
				await asyncio.sleep(0.7)
				woeid = _val(place, "woeid")
				result = await _tries(client.get_place_trends, woeid)  # a dict: trends (PlaceTrend objects), as_of...
				trends = list(_field(result, "trends") or [])
				if not trends:
					notes.append("get_place_trends %s (woeid %s): %s without trends; fields: %s" % (
						name, woeid, type(result).__name__, _fields(result),
					))
					continue
				notes.append("get_place_trends %s (woeid %s): %d trends; first: %s" % (
					name, woeid, len(trends), ", ".join(str(_field(t, "name")) for t in trends[:3]),
				))
				notes.append("  first trend: " + _fields(trends[0]))
		except Exception as e:
			notes.append("places / place trends: FAIL " + _short_error(e))
		return "\n" + _indent(notes)

	async def people_view():
		notes = []
		for query in ("NV Access", "น้ำท่วม"):
			try:
				page = await client.search_user(query, count=10)
				users = list(page)
				notes.append("search_user %r: %d users, %s; first: %s" % (
					query, len(users), _paging(page),
					", ".join("@%s" % _val(u, "screen_name") for u in users[:3]),
				))
				notes.append("  flags of the first 3: " + "; ".join(_relation(u) for u in users[:3]))
			except Exception as e:
				notes.append("search_user %r: FAIL %s" % (query, _short_error(e)))
		return "\n" + _indent(notes)

	await step("Trends (get_trends, places)", trends_view)
	await step("People search (search_user)", people_view)

	# ---------------------------------------------------------------- users and relations

	async def relations_view():
		uid, screen = first_user()
		if not uid:
			raise RuntimeError("skipped: no timeline tweet to take a user from")
		notes = []
		user = await client.get_user_by_screen_name(screen)
		fields = (
			"following", "followed_by", "blocking", "muting", "can_dm", "protected", "verified",
			"is_blue_verified", "followers_count", "following_count", "pinned_tweet_ids",
		)
		notes.append("@%s: %s" % (screen, ", ".join("%s=%s" % (f, _val(user, f)) for f in fields)))
		for method in ("get_user_followers", "get_user_following", "get_user_followers_you_know"):
			fn = getattr(client, method, None)
			if fn is None:
				notes.append("%s: not available" % method)
				continue
			try:
				page = await fn(uid, count=20)
				items = list(page)
				notes.append("%s: %d users, %s; first: %s" % (
					method, len(items), _paging(page),
					", ".join("@%s" % _val(u, "screen_name") for u in items[:3]),
				))
				notes.append("  flags of the first 3: " + "; ".join(_relation(u) for u in items[:3]))
			except Exception as e:
				notes.append("%s: FAIL %s" % (method, _short_error(e)))
			await asyncio.sleep(0.7)
		own = user_id_from_twid((service.store.get_cookies(account_id) or {}).get("twid"))
		if own:
			try:
				page = await client.get_user_following(own, count=20)
				notes.append("own following: %d users, %s" % (len(list(page)), _paging(page)))
			except Exception as e:
				notes.append("own following: FAIL " + _short_error(e))
		return "\n" + _indent(notes)

	async def pinned_view():
		notes = []
		found = 0
		for uid, screen in users_pool(20):
			if found >= 3:
				break
			await asyncio.sleep(0.7)
			try:
				user = await client.get_user_by_screen_name(screen)
			except Exception as e:
				notes.append("@%s: lookup FAIL %s" % (screen, _short_error(e)))
				continue
			ids = [str(i) for i in (_val(user, "pinned_tweet_ids") or [])]
			if not ids:
				continue
			found += 1
			stamp = "?"
			if ids[0].isdigit():
				stamp = time.strftime("%Y-%m", time.gmtime(((int(ids[0]) >> 22) + 1288834974657) / 1000))
			notes.append("@%s: pinned ids %s (the first one is from %s)" % (screen, ids, stamp))
			try:
				page = list(await client.get_user_tweets(uid, "Tweets", count=20))
				where = [i for i, t in enumerate(page) if str(_val(t, "id")) in ids]
				notes.append("  get_user_tweets: %d tweets; the pinned one is in it at index %s" % (len(page), where or "none"))
			except Exception as e:
				notes.append("  get_user_tweets: FAIL " + _short_error(e))
			try:
				tweet = await client.get_tweet_by_id(ids[0])
				notes.append("  get_tweet_by_id on the pinned id: ok, %s" % _convertible([tweet]))
			except Exception as e:
				notes.append("  get_tweet_by_id on the pinned id: FAIL " + _short_error(e))
		notes.append("users with a pinned tweet among those looked at: %d" % found)
		return "\n" + _indent(notes)

	async def dm_view():
		"""Counts, kinds and field names only: no names and no message text reach the report."""
		try:
			inbox = list(await client.get_dm_inbox())
		except Exception as e:
			return "\n" + _indent(["get_dm_inbox: FAIL " + _short_error(e)])
		single = [c for c in inbox if not _val(c, "is_group")]
		groups = [c for c in inbox if _val(c, "is_group")]
		notes = ["inbox: %d conversations, %d one-to-one, %d groups" % (len(inbox), len(single), len(groups))]
		for label, conv in [("one-to-one", c) for c in single[:2]] + [("group", c) for c in groups[:1]]:
			await asyncio.sleep(0.7)
			notes.append("%s: trusted=%s, muted=%s, participants=%d" % (
				label, _val(conv, "trusted"), _val(conv, "muted"), len(_val(conv, "participant_ids") or []),
			))
			try:
				res = conv.get_history()
				if hasattr(res, "__await__"):
					res = await res
				items = list(res)
				notes.append("  get_history: %d messages, %s" % (len(items), _paging(res)))
				counts, page = [len(items)], res
				for _walk in range(3):
					nxt = page.next()
					if hasattr(nxt, "__await__"):
						nxt = await nxt
					if nxt is None or not len(nxt):
						break
					page = nxt
					counts.append(len(list(page)))
					await asyncio.sleep(0.7)
				notes.append("  messages on the first pages: %s" % counts)
				if items:
					notes.append("  message fields: " + ", ".join(a for a in dir(items[0]) if not a.startswith("_")))
					notes.append("  text lengths of the first 5: %s" % [len(str(_val(m, "text") or "")) for m in items[:5]])
			except Exception as e:
				notes.append("  get_history: FAIL " + _short_error(e))
		return "\n" + _indent(notes)

	async def people_lists_view():
		notes = []
		for name in ("get_dm_inbox", "get_muted_users", "get_blocked_users"):
			fn = getattr(client, name, None)
			if fn is None:
				notes.append("%s: not available" % name)
				continue
			try:
				page = await fn()
				items = list(page)
				notes.append("%s: %d items, %s" % (name, len(items), _paging(page)))
				if items:
					first = items[0]
					fields = [a for a in dir(first) if not a.startswith("_")][:20]
					notes.append("  first item: %s, fields: %s" % (type(first).__name__, ", ".join(fields)))
			except Exception as e:
				notes.append("%s: FAIL %s" % (name, _short_error(e)))
			await asyncio.sleep(0.7)
		return "\n" + _indent(notes)

	await step("Users and relations", relations_view)
	await step("DM inbox, muted and blocked users (read only)", people_lists_view)
	await step("Pinned tweets", pinned_view)
	await step("Direct messages (read only)", dm_view)

	# ---------------------------------------------------------------- regression checks

	async def bookmarks_view():
		page = await client.get_bookmarks(count=20)
		tweets = list(page)
		return "%d tweets, %s, %s" % (len(tweets), _convertible(tweets), _paging(page))

	async def notifications_view():
		page = await client.get_notifications("All", count=20)
		items = list(page)
		return "%d notifications, %s" % (len(items), _paging(page))

	async def lists_view():
		lists = list(await client.get_lists(count=20))
		return "%d lists" % len(lists)

	await step("Bookmarks", bookmarks_view)
	await step("Notifications", notifications_view)
	await step("Lists", lists_view)

	# ---------------------------------------------------------------- open questions

	async def flags_view():
		"""Do the tweets of the timeline say how the account stands to their authors? How many users does a page
		of the account's own lists hold when more is asked for?"""
		notes = []
		tweets = list(await _tries(client.get_latest_timeline, count=20))
		authors = {}
		for t in tweets:
			user = _val(t, "user")
			handle = str(_val(user, "screen_name") or "")
			if handle and handle not in authors:
				authors[handle] = user
		shown = [
			{True: "T", False: "F"}.get(_val(u, "following"), "none") for u in list(authors.values())[:12]
		]
		notes.append("timeline authors: %d; following flag of the first 12: %s" % (len(authors), ", ".join(shown)))
		own = user_id_from_twid((service.store.get_cookies(account_id) or {}).get("twid"))
		if not own:
			notes.append("no twid in the cookies: own lists skipped")
			return "\n" + _indent(notes)
		for count in (20, 100):
			try:
				page = await _tries(client.get_user_following, own, count=count)
				notes.append("get_user_following count=%d: %d users, %s" % (count, len(list(page)), _paging(page)))
			except Exception as e:
				notes.append("get_user_following count=%d: FAIL %s" % (count, _short_error(e)))
			await asyncio.sleep(0.7)
		for name in ("get_muted_users", "get_blocked_users"):
			try:
				page = await _tries(getattr(client, name), count=100)
				notes.append("%s count=100: %d users, %s" % (name, len(list(page)), _paging(page)))
			except Exception as e:
				notes.append("%s count=100: FAIL %s" % (name, _short_error(e)))
			await asyncio.sleep(0.7)
		return "\n" + _indent(notes)

	async def dm_history_view():
		"""Counts only: no names and no message text reach the report. The first report showed one message and no
		next page from conversation.get_history(): does the history of a user or a group give more?"""
		inbox = list(await _tries(client.get_dm_inbox))
		single = [c for c in inbox if not _val(c, "is_group")]
		groups = [c for c in inbox if _val(c, "is_group")]
		notes = ["inbox: %d conversations, %d one-to-one, %d groups" % (len(inbox), len(single), len(groups))]
		cases = [("get_dm_history", _val(c, "partner_id")) for c in single[:3]]
		cases += [("get_group_dm_history", _val(c, "id")) for c in groups[:1]]
		for method, target in cases:
			await asyncio.sleep(0.7)
			try:
				page = await _tries(getattr(client, method), target)
				counts = [len(list(page))]
				for _walk in range(3):
					nxt = page.next()
					if hasattr(nxt, "__await__"):
						nxt = await nxt
					if nxt is None or not len(nxt):
						break
					page = nxt
					counts.append(len(list(page)))
					await asyncio.sleep(0.7)
				notes.append("%s: messages on the first pages %s, %s" % (method, counts, _paging(page)))
			except Exception as e:
				notes.append("%s: FAIL %s" % (method, _short_error(e)))
		return "\n" + _indent(notes)

	await step("Relation flags of the timeline authors, page sizes of the account's own lists", flags_view)
	await step("Direct message history (counts only)", dm_history_view)

	async def dm_raw_view():
		"""What X really sends for the history of the first one-to-one conversation: names of fields, counts and
		a few flags, never a name or a text."""
		inbox = list(await _tries(client.get_dm_inbox))
		conv = next((c for c in inbox if not _val(c, "is_group")), None)
		if conv is None:
			raise RuntimeError("skipped: no one-to-one conversation")
		conversation_id = "%s-%s" % (_val(conv, "partner_id"), await client.user_id())
		response, _headers = await _tries(client.v11.dm_conversation, conversation_id, None)
		timeline = response.get("conversation_timeline") or {}
		entries = timeline.get("entries") or []
		kinds = {}
		for entry in entries:
			for key in entry:
				kinds[key] = kinds.get(key, 0) + 1
		notes = [
			"response keys: %s" % ", ".join(sorted(response)),
			"timeline keys: %s; status: %s; entries: %d (%s)" % (
				", ".join(sorted(timeline)), timeline.get("status"), len(entries),
				", ".join("%s %d" % kv for kv in sorted(kinds.items())),
			),
		]
		first = ((entries[0].get("message") or {}).get("message_data") or {}) if entries else {}
		notes.append("fields of the first message: %s" % ", ".join(sorted(first)))
		info = (response.get("conversations") or {}).get(conversation_id) or {}
		notes.append("conversation fields: %s" % ", ".join(
			"%s=%s" % (k, v) for k, v in sorted(info.items()) if isinstance(v, (bool, int)) or k in ("type", "status")
		))
		return "\n" + _indent(notes)

	await step("Direct message history, raw structure (field names and counts only)", dm_raw_view)

	async def dm_scan_view():
		"""The history of the first one-to-one conversations: counts, status and the trusted flag, never a name or
		a text. Looking for a conversation that has more than one message."""
		inbox = list(await _tries(client.get_dm_inbox))
		own = await client.user_id()
		notes = []
		for conv in [c for c in inbox if not _val(c, "is_group")][:8]:
			await asyncio.sleep(0.7)
			try:
				response, _headers = await _tries(client.v11.dm_conversation, "%s-%s" % (_val(conv, "partner_id"), own), None)
			except Exception as e:
				notes.append("conversation: FAIL %s" % _short_error(e))
				continue
			timeline = response.get("conversation_timeline") or {}
			notes.append("trusted=%s: %d entries, status %s" % (
				_val(conv, "trusted"), len(timeline.get("entries") or []), timeline.get("status"),
			))
		return "\n" + _indent(notes)

	await step("Direct message history of the first 8 conversations (counts only)", dm_scan_view)

	async def dm_survey_view():
		"""Names and counts only, never a name or a text: what conversations, entries, messages and attachments
		carry, for up to 4 one-to-one and 2 group conversations, three pages each."""
		inbox = list(await _tries(client.get_dm_inbox))
		own = str(await client.user_id())
		trusted = [c for c in inbox if c.trusted]
		notes = ["inbox: %d conversations, %d trusted" % (len(inbox), len(trusted))]
		for label, conv in (
			("one-to-one", next((c for c in inbox if not c.is_group), None)),
			("group", next((c for c in inbox if c.is_group), None)),
		):
			if conv is None:
				continue
			notes.append("%s conversation fields: %s" % (label, ", ".join(sorted(conv._data))))
			people = conv._data.get("participants") or []
			if people and isinstance(people[0], dict):
				notes.append("  participant fields: %s" % ", ".join(sorted(people[0])))

		def number(value):
			try:
				return int(value)
			except (TypeError, ValueError):
				return None

		newer = 0
		for c in inbox:
			last, read = number(c._data.get("sort_event_id")), number(c.last_read_event_id)
			if last is not None and read is not None and last > read:
				newer += 1
		notes.append("conversations with an event newer than last_read_event_id: %d" % newer)
		group_conv = next((c for c in inbox if c.is_group), None)
		if group_conv is not None:
			group = await _tries(client.get_group, group_conv.id)
			names = [a for a in dir(group) if not a.startswith("_") and not callable(_val(group, a))]
			notes.append("group attributes: %s" % ", ".join(names))
		chosen = [c for c in trusted if not c.is_group][:4] + [c for c in trusted if c.is_group][:2]
		kinds, other, fields, entry_fields = {}, {}, set(), set()
		attach_kinds, attach_fields, reaction_fields = {}, set(), set()
		total = mine = with_reply = with_reactions = with_attachment = 0
		for conv in chosen:
			cid = conv.id if conv.is_group else "%s-%s" % (conv.partner_id, own)
			max_id = None
			for _page in range(3):
				response, _headers = await _tries(client.v11.dm_conversation, cid, max_id)
				timeline = response.get("conversation_timeline") or {}
				for entry in timeline.get("entries") or []:
					for key, value in entry.items():
						kinds[key] = kinds.get(key, 0) + 1
						if key != "message":
							other.setdefault(key, set()).update(value if isinstance(value, dict) else [])
							continue
						entry_fields.update(value)
						data = value.get("message_data") or {}
						total += 1
						fields.update(data)
						mine += str(data.get("sender_id")) == own
						with_reply += "reply_data" in data
						if data.get("reactions"):
							with_reactions += 1
							for r in data["reactions"]:
								reaction_fields.update(r if isinstance(r, dict) else [])
						attachment = data.get("attachment")
						if isinstance(attachment, dict):
							with_attachment += 1
							for key2, inner in attachment.items():
								attach_kinds[key2] = attach_kinds.get(key2, 0) + 1
								attach_fields.update(inner if isinstance(inner, dict) else [])
				if timeline.get("status") != "HAS_MORE" or not timeline.get("min_entry_id"):
					break
				max_id = timeline["min_entry_id"]
				await asyncio.sleep(0.7)
			await asyncio.sleep(0.7)
		notes.append("scanned %d conversations: %d messages, %d sent by this account" % (len(chosen), total, mine))
		notes.append("entry kinds: %s" % ", ".join("%s %d" % kv for kv in sorted(kinds.items())))
		for key, names in sorted(other.items()):
			notes.append("  %s fields: %s" % (key, ", ".join(sorted(names)) or "none"))
		notes.append("message entry fields: %s" % ", ".join(sorted(entry_fields)))
		notes.append("message_data fields: %s" % ", ".join(sorted(fields)))
		notes.append("messages with reply_data %d, reactions %d, attachment %d" % (with_reply, with_reactions, with_attachment))
		notes.append("reaction fields: %s" % (", ".join(sorted(reaction_fields)) or "none seen"))
		notes.append("attachment kinds: %s" % (", ".join("%s %d" % kv for kv in sorted(attach_kinds.items())) or "none seen"))
		notes.append("attachment fields: %s" % (", ".join(sorted(attach_fields)) or "none seen"))
		return "\n" + _indent(notes)

	await step("DM survey: conversation, entry, message and attachment fields (names and counts only)", dm_survey_view)

	async def dm_backend_view():
		"""The add-on's own DM jobs against X: counts only, never a name or a text."""
		notes = []
		inbox = await service._dm_inbox(client, account_id, {})
		rows = inbox["convos"]
		single = [r for r in rows if not r["is_group"]]
		groups = [r for r in rows if r["is_group"]]
		named = sum(1 for r in single if any(m.get("handle") for m in r["members"]))
		notes.append("inbox: %d conversations (%d one-to-one, %d groups, %d requests); one-to-one with a user name: %d" % (
			len(rows), len(single), len(groups), sum(1 for r in rows if not r["trusted"]), named,
		))
		notes.append("group members with a user name: %d of %d" % (
			sum(1 for g in groups for m in g["members"] if m.get("handle")), sum(len(g["members"]) for g in groups),
		))
		for label, pool in (("one-to-one", single), ("group", groups)):
			row = next((r for r in pool if r["trusted"]), None)
			if row is None:
				continue
			options = {
				"convo_id": row["convo_id"], "is_group": row["is_group"], "partner_id": row["partner_id"],
				"read_id": row["read_id"], "pages": 2,
			}
			result = await service._dm_history(client, account_id, options)
			messages = result["messages"]
			years = sorted({time.gmtime(m["ts"]).tm_year for m in messages})
			notes.append(
				"%s history, 2 pages: %d messages, more: %s, unread by X's pointer: %d, years %s, with attachment %d, with reply %d" % (
					label, len(messages), result["more"], sum(1 for m in messages if not m["is_read"]), years,
					sum(1 for m in messages if m["attachment"]), sum(1 for m in messages if m["reply"]),
				)
			)
			if messages:
				oldest = messages[0]["id"]
				older = await service._dm_history(client, account_id, dict(options, pages=1, max_id=oldest))
				ids = [m["id"] for m in older["messages"]]
				notes.append("  older than the oldest of those: %d messages, all older: %s, none unread: %s" % (
					len(ids), all(i < oldest for i in ids), all(m["is_read"] for m in older["messages"]),
				))
		return "\n" + _indent(notes)

	await step("DM backend: the add-on's own inbox and history jobs (counts only)", dm_backend_view)

	async def dm_complete_view():
		"""Does X give every message? For up to 8 conversations the newest message of the first page is compared
		with the newest event of the inbox, with the request twifork sends and with ones like the web page's
		(reactions, media, encrypted chats). Counts, field names and ids only, never a name or a text."""
		from twikit.client.v11 import Endpoint
		inbox = await service._dm_inbox(client, account_id, {})
		own = inbox["own"]
		rows = [r for r in inbox["convos"] if r["trusted"] and not r["low_quality"]][:8]
		base = {"context": "FETCH_DM_CONVERSATION_HISTORY", "include_conversation_info": True}
		web = dict(
			base, supports_reactions=True, include_ext_edit_control=True, include_quote_count=True, include_cards=1,
			cards_platform="Web-12", include_ext_media_color=True, include_ext_media_availability=True,
			include_ext_alt_text=True,
		)
		variants = (
			("twifork", base), ("web-like", web),
			("web-like with secret chats", dict(web, dm_secret_conversations_enabled=True, krs_registration_enabled=True)),
		)
		stats = {
			name: {"seen": 0, "behind": 0, "kinds": {}, "fields": set(), "reactions": 0, "reaction_fields": set(), "other": set()}
			for name, _params in variants
		}
		gaps = []
		for row in rows:
			convo_id = row["convo_id"] if row["is_group"] else "%s-%s" % (row["partner_id"], own)
			for name, params in variants:
				response, _headers = await _tries(
					client.get, Endpoint.DM_CONVERSATION.format(convo_id), params=params,
					headers=dict(client._base_headers),
				)
				timeline = response.get("conversation_timeline") or {}
				entries = timeline.get("entries") or []
				st = stats[name]
				st["seen"] += 1
				ids = []
				for entry in entries:
					for key, value in entry.items():
						st["kinds"][key] = st["kinds"].get(key, 0) + 1
						if key != "message":
							st["other"].update(value if isinstance(value, dict) else [])
							continue
						data = value.get("message_data") or {}
						st["fields"].update(data)
						ids.append(int(data.get("id") or value.get("id") or 0))
						if data.get("reactions"):
							st["reactions"] += 1
							for reaction in data["reactions"]:
								st["reaction_fields"].update(reaction if isinstance(reaction, dict) else [])
				newest = max(ids) if ids else 0
				if row["last_id"] > newest:
					st["behind"] += 1
					if len(gaps) < 6:
						hours = ((row["last_id"] >> 22) - (newest >> 22)) / 3600000
						gaps.append("%s: the inbox is ahead by about %.1f hours; status %s; %d entries" % (
							name, hours, timeline.get("status"), len(entries),
						))
				await asyncio.sleep(0.7)
		notes = ["conversations checked: %d (trusted, not spam)" % len(rows)]
		for name, _params in variants:
			st = stats[name]
			notes.append("%s: inbox newer than the newest message given in %d of %d; entry kinds: %s" % (
				name, st["behind"], st["seen"], ", ".join("%s %d" % kv for kv in sorted(st["kinds"].items())) or "none",
			))
			notes.append("  message fields: %s; messages with reactions: %d; reaction fields: %s; other entry fields: %s" % (
				", ".join(sorted(st["fields"])), st["reactions"], ", ".join(sorted(st["reaction_fields"])) or "none",
				", ".join(sorted(st["other"])) or "none",
			))
		notes.extend("  example " + g for g in gaps)
		return "\n" + _indent(notes)

	await step("DM completeness: is every message given, with and without the web page's parameters", dm_complete_view)

	async def dm_messages_view():
		"""The newest messages of the first three conversations of the Chat tab (filter All), one line each: how
		old, who sent it, how long, how many links, what is attached. No text and no names: to be compared with the
		web page. A message that is there and not here is missing from what X gives, not from the add-on's reading."""
		inbox = await service._dm_inbox(client, account_id, {})
		own = inbox["own"]
		rows = [r for r in inbox["convos"] if r["trusted"] and not r["low_quality"]][:3]
		now = time.time()
		notes = []
		for number, row in enumerate(rows, 1):
			convo_id = row["convo_id"] if row["is_group"] else "%s-%s" % (row["partner_id"], own)
			raw, _headers = await _tries(client.v11.dm_conversation, convo_id, None)
			entries = (raw.get("conversation_timeline") or {}).get("entries") or []
			with_data = sum(1 for e in entries if isinstance((e.get("message") or {}).get("message_data"), dict))
			options = {
				"convo_id": row["convo_id"], "is_group": row["is_group"], "partner_id": row["partner_id"],
				"read_id": row["read_id"], "pages": 2,
			}
			messages = (await service._dm_history(client, account_id, options))["messages"]
			notes.append("#%d %s: first page %d entries, %d with message data; %d messages read in 2 pages, the newest 25:" % (
				number, "group" if row["is_group"] else "one-to-one", len(entries), with_data, len(messages),
			))
			for m in reversed(messages[-25:]):
				notes.append("  %6.1f days, %s, %d characters, %d links, %s%s" % (
					(now - m["ts"]) / 86400, "me" if m["sender_id"] == own else "them", len(m["text"]),
					m["text"].count("http"), m["attachment"]["kind"] if m["attachment"] else "no attachment",
					", reply" if m["reply"] else "",
				))
			await asyncio.sleep(0.7)
		return "\n" + _indent(notes)

	await step("DM messages: the newest messages of the first three conversations (no text), to compare with the web", dm_messages_view)

	async def dm_inbox_paging_view():
		"""Does the inbox have more conversations than its first page? Statuses and counts only."""
		notes = []
		response, _headers = await _tries(client.v11.dm_inbox, None)
		state = response.get("inbox_initial_state") or response.get("user_events") or {}
		timelines = state.get("inbox_timelines") or {}
		for name in ("trusted", "untrusted"):
			timeline = timelines.get(name) or {}
			notes.append("%s timeline of the first page: status %s, has a place to go on from: %s" % (
				name, timeline.get("status"), bool(timeline.get("min_entry_id")),
			))
		convs = state.get("conversations") or {}
		notes.append("conversations of the first page: %d (%d trusted)" % (
			len(convs), sum(1 for c in convs.values() if c.get("trusted")),
		))
		trusted = timelines.get("trusted") or {}
		max_id = None if trusted.get("status") == "AT_END" else trusted.get("min_entry_id")
		for page in range(3):
			if not max_id:
				break
			await asyncio.sleep(1)
			from .service import dm_inbox_state
			response, _headers = await _tries(client.v11.dm_inbox_timeline, "trusted", max_id)
			more, _users, status, max_id = dm_inbox_state(response)
			inner = response.get("inbox_timeline") or response.get("inbox_initial_state") or {}
			notes.append("older page %d: response keys %s, inside: %s; %d conversations, status %s" % (
				page + 1, ", ".join(sorted(response)) or "none", ", ".join(sorted(inner)) if isinstance(inner, dict) else "-",
				len(more), status,
			))
		return "\n" + _indent(notes)

	await step("DM inbox paging: is there more than the first page of conversations", dm_inbox_paging_view)

	async def dm_age_view():
		"""How old is the newest message of each conversation, in the order of the Chat tab (filter All)? To be
		compared with the web page: a conversation that is active there and old here moved to encrypted chat."""
		inbox = await service._dm_inbox(client, account_id, {})
		rows = [r for r in inbox["convos"] if r["trusted"] and not r["low_quality"]]
		now = time.time()

		def days(seconds):
			return "%.0f days" % ((now - seconds) / 86400) if seconds else "none"

		notes = []
		for number, row in enumerate(rows, 1):
			options = {
				"convo_id": row["convo_id"], "is_group": row["is_group"], "partner_id": row["partner_id"],
				"read_id": row["read_id"], "pages": 1,
			}
			result = await service._dm_history(client, account_id, options)
			messages = result["messages"]
			event = ((row["last_id"] >> 22) + 1288834974657) // 1000 if row["last_id"] else 0
			notes.append("#%d %s: newest message %s old, newest event of the inbox %s old, %d messages on the first page" % (
				number, "group" if row["is_group"] else "one-to-one", days(messages[-1]["ts"] if messages else 0),
				days(event), len(messages),
			))
			await asyncio.sleep(0.7)
		return "\n" + _indent(notes)

	await step("DM age: the newest message of each conversation, in the order of the Chat tab", dm_age_view)

	async def notifications_raw_view():
		"""What X sends for the notification page and for the mentions: the shapes of the entries and the
		objects that come with them. Counts only."""

		def entries_of(node):
			if isinstance(node, dict):
				for key, value in node.items():
					found = value if key == "entries" and isinstance(value, list) else entries_of(value)
					if found is not None:
						return found
			elif isinstance(node, list):
				for value in node:
					found = entries_of(value)
					if found is not None:
						return found
			return None

		notes = []
		for label, call in (("All", client.v11.notifications_all), ("Mentions", client.v11.notifications_mentions)):
			response, _headers = await _tries(call, 40, None)
			objects = response.get("globalObjects") or {}
			entries = entries_of(response.get("timeline")) or []
			shapes, icons = {}, {}
			for entry in entries:
				content = entry.get("content") or {}
				item = (content.get("item") or {}).get("content") or {}
				shape = "%s/%s" % (str(entry.get("entryId", "")).split("-")[0], ",".join(sorted(item)) or ",".join(sorted(content)))
				shapes[shape] = shapes.get(shape, 0) + 1
			for notification in (objects.get("notifications") or {}).values():
				icon = (notification.get("icon") or {}).get("id") or "none"
				icons[icon] = icons.get(icon, 0) + 1
			notes.append("%s: %d entries; shapes: %s" % (
				label, len(entries), ", ".join("%s %d" % kv for kv in sorted(shapes.items())) or "none",
			))
			notes.append("  objects: %s; notification icons: %s" % (
				", ".join("%s %d" % (k, len(v)) for k, v in sorted(objects.items()) if hasattr(v, "__len__")) or "none",
				", ".join("%s %d" % kv for kv in sorted(icons.items())) or "none",
			))
			await asyncio.sleep(1)
		return "\n" + _indent(notes)

	await step("NOTIFICATIONS raw: entry shapes of the All and Mentions pages (counts only)", notifications_raw_view)

	async def notifications_reader_view():
		"""The rows the add-on makes from the first page of the notification list. Counts only."""
		page = await service._notifications_page(client, None)
		posts = service._convert("notifications", page)
		tweets = sum(1 for n in page if (_val(n, "icon") or {}).get("id") == "tweet_entry")
		notes = [
			"page: %d items (%d grouped notifications, %d tweets), %d rows" % (len(page), len(page) - tweets, tweets, len(posts)),
			"rows that are replies: %d, with media: %d, quote tweets: %d, retweets: %d, next page: %s" % (
				sum(1 for p in posts if p.reply_to), sum(1 for p in posts if p.embeds), sum(1 for p in posts if p.quote_text),
				sum(1 for p in posts if p.kind == "retweet"), page.next() is not None,
			),
		]
		return "\n" + _indent(notes)

	await step("NOTIFICATIONS reader: the rows the add-on makes from the first page", notifications_reader_view)

	# ---------------------------------------------------------------- opt-in tests

	if write_tests:
		async def like_round_trip():
			tw = next((t for t in ctx.get("tweets") or [] if _val(t, "favorited") is False), None)
			if tw is None:
				raise RuntimeError("skipped: no unliked tweet found")
			await client.favorite_tweet(str(tw.id))
			try:
				await asyncio.sleep(2)
			finally:
				await client.unfavorite_tweet(str(tw.id))
			return "liked and unliked tweet id %s" % tw.id

		await step("WRITE: like then unlike", like_round_trip)

	if post_tests:
		async def posting():
			notes = []
			created = []
			stamp = time.strftime("%H:%M:%S")
			try:
				try:
					a = await client.create_tweet(text="Connection test %s (deleted again at once)" % stamp)
				except Exception as e:
					return "create_tweet: FAIL " + _short_error(e)
				created.append(a.id)
				notes.append("create_tweet: ok")
				await asyncio.sleep(3)
				try:
					b = await client.create_tweet(text="Reply test", reply_to=a.id)
					created.append(b.id)
					notes.append("reply (create_tweet reply_to): ok")
				except Exception as e:
					notes.append("reply: FAIL " + _short_error(e))
				await asyncio.sleep(2)
				try:
					c = await client.create_tweet(text="Quote test", attachment_url=a.url)
					created.append(c.id)
					notes.append("quote (create_tweet attachment_url): ok")
				except Exception as e:
					notes.append("quote: FAIL " + _short_error(e))
				await asyncio.sleep(2)
				try:
					await client.retweet(a.id)
					await asyncio.sleep(2)
					await client.delete_retweet(a.id)
					notes.append("retweet and undo: ok")
				except Exception as e:
					notes.append("retweet: FAIL " + _short_error(e))
			finally:
				for tid in reversed(created):
					await _delete_with_retry(client, tid, notes)
			return "\n" + _indent(notes)

		await step("POSTING: tweet, reply, quote, retweet (all deleted again)", posting)

	if media_tests:
		async def media_round_trip():
			try:
				from PIL import Image
			except Exception:
				raise RuntimeError("skipped: Pillow is not available")
			notes = []
			created = []
			fd, path = tempfile.mkstemp(suffix=".png")
			os.close(fd)
			try:
				Image.new("RGB", (160, 160), (30, 120, 200)).save(path, "PNG")
				try:
					media_id = await client.upload_media(path, wait_for_completion=True)
					notes.append("upload_media (image): ok, id %s" % media_id)
				except Exception as e:
					return "\n" + _indent(["upload_media: FAIL " + _short_error(e)])
				try:
					await client.create_media_metadata(media_id, alt_text="Probe image")
					notes.append("create_media_metadata (alt text): ok")
				except Exception as e:
					notes.append("create_media_metadata: FAIL " + _short_error(e))
				try:
					tweet = await client.create_tweet(
						text="Media test %s (deleted again at once)" % time.strftime("%H:%M:%S"), media_ids=[media_id],
					)
					created.append(tweet.id)
					notes.append("create_tweet with media_ids: ok")
				except Exception as e:
					notes.append("create_tweet with media_ids: FAIL " + _short_error(e))
			finally:
				try:
					os.remove(path)
				except OSError:
					pass
				for tid in created:
					await _delete_with_retry(client, tid, notes)
			return "\n" + _indent(notes)

		await step("MEDIA: upload a small image with alt text, post it, delete it", media_round_trip)

		async def media_formats_round_trip():
			try:
				from PIL import Image
			except Exception:
				raise RuntimeError("skipped: Pillow is not available")

			def png(path):
				Image.new("RGB", (160, 160), (30, 120, 200)).save(path, "PNG")

			def jpeg(path):
				Image.new("RGB", (160, 160), (200, 80, 30)).save(path, "JPEG")

			def gif(path):
				frames = [Image.new("RGB", (120, 120), c) for c in ((200, 30, 30), (30, 30, 200), (30, 200, 30))]
				frames[0].save(path, "GIF", save_all=True, append_images=frames[1:], duration=300, loop=0)

			cases = (("JPEG", [(".jpg", jpeg)]), ("animated GIF", [(".gif", gif)]), ("4 images", [(".png", png)] * 4))
			notes, created, paths = [], [], []
			try:
				for label, files in cases:
					ids = []
					try:
						for suffix, build in files:
							fd, path = tempfile.mkstemp(suffix=suffix)
							os.close(fd)
							paths.append(path)
							build(path)
							extra = {"media_category": "tweet_gif"} if suffix == ".gif" else {}
							ids.append(await _tries(client.upload_media, path, wait_for_completion=True, **extra))
							await asyncio.sleep(1)
						await client.create_media_metadata(ids[0], alt_text="Probe media")
						tweet = await client.create_tweet(
							text="Media format test, %s (deleted again at once)" % label, media_ids=ids,
						)
						created.append(tweet.id)
						notes.append("%s: upload, alt text and tweet ok (%d media)" % (label, len(ids)))
					except Exception as e:
						notes.append("%s: FAIL %s" % (label, _short_error(e)))
					await asyncio.sleep(3)
			finally:
				for path in paths:
					try:
						os.remove(path)
					except OSError:
						pass
				for tid in created:
					await _delete_with_retry(client, tid, notes)
			return "\n" + _indent(notes)

		await step("MEDIA FORMATS: JPEG, animated GIF, 4 images in one tweet (posted, deleted again)", media_formats_round_trip)

		async def media_shrink_round_trip():
			try:
				from PIL import Image
			except Exception:
				raise RuntimeError("skipped: Pillow is not available")
			notes, created, paths = [], [], []
			try:
				try:
					fd, big = tempfile.mkstemp(suffix=".png")
					os.close(fd)
					paths.append(big)
					Image.frombytes("RGB", (2000, 2000), os.urandom(2000 * 2000 * 3)).save(big, "PNG")
					notes.append("test image: %.1f MB (X limit is 5 MB)" % (os.path.getsize(big) / 1048576))
					small = shrink_image(big, IMAGE_SHRINK_TARGET)
					paths.append(small)
					notes.append("shrunk copy: %.1f MB" % (os.path.getsize(small) / 1048576))
					media_id = await _tries(client.upload_media, small, wait_for_completion=True)
					tweet = await client.create_tweet(
						text="Media shrink test (deleted again at once)", media_ids=[media_id],
					)
					created.append(tweet.id)
					notes.append("upload and tweet with the shrunk copy: ok")
				except Exception as e:
					notes.append("FAIL " + _short_error(e))
			finally:
				for path in paths:
					try:
						os.remove(path)
					except OSError:
						pass
				for tid in created:
					await _delete_with_retry(client, tid, notes)
			return "\n" + _indent(notes)

		await step("MEDIA SHRINK: a 12 MB image is shrunk, uploaded and posted (deleted again)", media_shrink_round_trip)
        
	if mute_tests:
		async def mute_round_trip():
			page = await _tries(client.search_user, "NV Access", count=10)
			target = next(
				(u for u in page if not (_val(u, "following") or _val(u, "muting") or _val(u, "blocking"))), None
			)
			if target is None:
				raise RuntimeError("skipped: no user in the search that the account has no relation with")
			uid, screen = str(_val(target, "id")), str(_val(target, "screen_name"))
			notes = []
			try:
				await _tries(client.mute_user, uid)
				notes.append("mute_user: ok")
				await asyncio.sleep(1.5)
				after = await _tries(client.get_user_by_screen_name, screen)
				notes.append("flag muting after mute: %s" % _val(after, "muting"))
			finally:
				error = ""
				for _attempt in range(3):
					try:
						await _tries(client.unmute_user, uid)
						notes.append("unmute_user: ok")
						break
					except Exception as e:
						error = _short_error(e)
						await asyncio.sleep(3)
				else:
					notes.append("COULD NOT UNMUTE @%s after 3 tries - unmute by hand: %s" % (screen, error))
			await asyncio.sleep(1.5)
			again = await _tries(client.get_user_by_screen_name, screen)
			notes.append("flag muting after unmute: %s" % _val(again, "muting"))
			return "\n" + _indent(notes)

		await step("MUTE: mute one user the account has no relation with, read the flag, unmute again", mute_round_trip)

	if block_tests:
		async def block_round_trip():
			page = await _tries(client.search_user, "NV Access", count=20)
			target = next(
				(u for u in page if not any(_val(u, k) for k in ("following", "followed_by", "muting", "blocking"))), None
			)
			if target is None:
				raise RuntimeError("skipped: no user in the search that has no relation with the account")
			uid, screen = str(_val(target, "id")), str(_val(target, "screen_name"))
			notes = []
			unblocked = False
			try:
				await _tries(client.block_user, uid)
				notes.append("block_user: ok")
				await asyncio.sleep(1.5)
				after = await _tries(client.get_user_by_screen_name, screen)
				notes.append("flag blocking after block: %s" % _val(after, "blocking"))
			finally:
				cookies = service.store.get_cookies(account_id)

				def other(language, impersonate):
					made = twikit.Client(language, impersonate=IMPERSONATE) if impersonate else twikit.Client(language)
					made.set_cookies(cookies)
					return made

				form = {"user_id": uid}
				url = "https://x.com/i/api/1.1/blocks/destroy.json"
				web = dict(client._base_headers)  # what the web page sends: language en, Origin, the profile as Referer
				web.update({
					"content-type": "application/x-www-form-urlencoded", "X-Twitter-Client-Language": "en",
					"Origin": "https://x.com", "Referer": "https://x.com/" + screen,
				})
				web.pop("Accept-Language", None)

				async def with_lang_cookie():
					client.set_cookies({**client.get_cookies(), "lang": "en"})
					return await client.post(url, data=form, headers=web)

				attempts = [
					("the add-on's client", lambda: client.unblock_user(uid)),
					("a client with language en", lambda: other("en", True).unblock_user(uid)),
					("a plain client (no browser impersonation), language en", lambda: other("en", False).unblock_user(uid)),
					("the add-on's client with the web's headers (language en, Origin, Referer of the profile)",
						lambda: client.post(url, data=form, headers=web)),
					("the same with a lang=en cookie", with_lang_cookie),
				]
				for label, call in attempts:
					try:
						await _tries(call)
						notes.append("%s: ok" % label)
						unblocked = True
						break
					except Exception as e:
						notes.append("%s: FAIL %s" % (label, _short_error(e)))
						await asyncio.sleep(1.5)
				if not unblocked:
					notes.append("COULD NOT UNBLOCK @%s - unblock on x.com by hand" % screen)
			await asyncio.sleep(1.5)
			again = await _tries(client.get_user_by_screen_name, screen)
			notes.append("flag blocking after the unblock: %s" % _val(again, "blocking"))
			return "\n" + _indent(notes)

		await step("BLOCK: block one user the account has no relation with, try the ways to unblock", block_round_trip)

	if dm_tests:
		async def dm_round_trip():
			notes, sent = [], []
			stamp = time.strftime("%H:%M:%S")
			fd, path = tempfile.mkstemp(suffix=".png")
			os.close(fd)

			async def stage(label, func):
				try:
					value = await func()
					notes.append("%s: ok" % label)
					return value
				except Exception as e:
					notes.append("%s: FAIL %s" % (label, _short_error(e)))
					return None

			async def raw_message(cid, message_id):
				"""The raw message_data of one message, and the entry kinds of the page it came from."""
				response, _headers = await _tries(client.v11.dm_conversation, cid, None)
				entries = (response.get("conversation_timeline") or {}).get("entries") or []
				kinds = sorted({key for entry in entries for key in entry})
				for entry in entries:
					data = (entry.get("message") or {}).get("message_data") or {}
					if str(data.get("id")) == str(message_id):
						return data, kinds
				return None, kinds

			async def body():
				user = await _tries(client.get_user_by_screen_name, "iannnnn")
				uid = str(_val(user, "id"))
				cid = "%s-%s" % (uid, await client.user_id())
				notes.append("target: can_dm=%s" % _val(user, "can_dm"))
				first = await stage("send_dm", lambda: client.send_dm(uid, "DM test %s" % stamp))
				if first is None:
					return
				sent.append(first.id)
				await asyncio.sleep(2)
				reply = await stage("send_dm with reply_to", lambda: client.send_dm(uid, "Reply test", reply_to=first.id))
				if reply is not None:
					sent.append(reply.id)
					notes.append("  replied_to_id matches: %s; reply_data fields: %s" % (
						reply.replied_to_id == first.id, ", ".join(sorted(reply.reply_data or {})) or "none",
					))
				await asyncio.sleep(2)
				emoji = "\U0001F44D"
				await stage("add_reaction_to_message", lambda: client.add_reaction_to_message(first.id, cid, emoji))
				await asyncio.sleep(2)
				data, kinds = await raw_message(cid, first.id)
				notes.append("  after the reaction: entry kinds %s; message fields %s; reactions: %s" % (
					", ".join(kinds), ", ".join(sorted(data or {})), str((data or {}).get("reactions", "none"))[:160],
				))
				await stage("remove_reaction_from_message", lambda: client.remove_reaction_from_message(first.id, cid, emoji))
				await asyncio.sleep(2)
				try:
					from PIL import Image
					Image.new("RGB", (160, 160), (30, 120, 200)).save(path, "PNG")
					media_id = await _tries(client.upload_media, path, wait_for_completion=True, media_category="dm_image")
				except Exception as e:
					media_id = None
					notes.append("upload_media (dm_image): FAIL " + _short_error(e))
				if media_id:
					picture = await stage("send_dm with an image", lambda: client.send_dm(uid, "Media test", media_id))
					if picture is not None:
						sent.append(picture.id)
						notes.append("  attachment kinds: %s; attachment_url found: %s" % (
							", ".join(sorted(picture.attachment or {})) or "none", bool(picture.attachment_url),
						))
						got = await stage("download_attachment", picture.download_attachment)
						if got is not None:
							notes.append("  downloaded %d bytes" % len(got))
				for label, text in (
					("text and a link", "Link test, text and a link: https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
					("only a link", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
				):
					await asyncio.sleep(2)
					link = await stage("send_dm with " + label, lambda text=text: client.send_dm(uid, text))
					if link is not None:
						sent.append(link.id)
						data, kinds = await raw_message(cid, link.id)
						notes.append("  read back: %s; urls in entities: %d; attachment kinds: %s" % (
							"found" if data else "NOT FOUND", len(((data or {}).get("entities") or {}).get("urls") or []),
							", ".join(sorted((data or {}).get("attachment") or {})) or "none",
						))
				history = await stage("get_dm_history (partner id)", lambda: client.get_dm_history(uid))
				if history is not None:
					ids = [int(m.id) for m in history]
					notes.append("  %d messages on the first page, newest first: %s" % (len(ids), ids == sorted(ids, reverse=True)))

			try:
				await body()
			except Exception as e:
				notes.append("FAIL " + _short_error(e))
			finally:
				try:
					os.remove(path)
				except OSError:
					pass
				for message_id in reversed(sent):
					error = "unknown"
					for _attempt in range(3):
						try:
							await client.delete_dm(message_id)
							notes.append("deleted test message %s (for this account only)" % message_id)
							error = ""
							break
						except Exception as e:
							error = _short_error(e)
							await asyncio.sleep(3)
					if error:
						notes.append("COULD NOT DELETE test message %s - delete it by hand: %s" % (message_id, error))
			return "\n" + _indent(notes)

		await step("DM WRITE: send, reply, react, image, history, delete (to @iannnnn)", dm_round_trip)

	add()
	add("Finished in %.1fs" % (time.time() - started))
	return _ok("probe", [], report="\n".join(lines))


def run_selfcheck(plugin):
	"""Logic checks that need no network and touch no real data (a memory store stands in for the database).
	After them, what the running add-on holds now (read only). Returns the report text."""
	import inspect
	import json
	import secrets
	import shutil
	from types import SimpleNamespace

	from . import autoread, compose, dialogs, explore, feedlist, keymap, mainwindow, tabstate, useractions
	from .config import Config
	from .core import GlobalPlugin
	from .chat import (
		attachment_text, convo_label, convo_name, convo_visible, describe_reactions, has_new_messages, split_cell,
	)
	from .service import FetchService, dm_convo_row, dm_message_row, dm_users, notification_to_post
	from .storage import MemoryStore, Store
	from .tray import fill_app_menu
	from .utils import Post, build_search_query, is_pinned, isolated_modules, main_text, notification_id

	lines = ["Self-check report", "Time: %s" % time.strftime("%Y-%m-%d %H:%M:%S")]
	counts = {"ok": 0, "bad": 0}

	def check(title, func):
		try:
			info = func()
		except AssertionError as e:
			counts["bad"] += 1
			lines.append("[FAIL] %s: %s" % (title, e))
		except Exception as e:
			counts["bad"] += 1
			log.error(TAG + "self-check step failed: " + title, exc_info=True)
			lines.append("[ERR ] %s: %s" % (title, _short_error(e)))
		else:
			counts["ok"] += 1
			lines.append("[ OK ] %s%s" % (title, (": " + info) if info else ""))

	# ---------------------------------------------------------------- keyboard shortcuts

	def keymap_defaults():
		bad = [a for a in keymap.actions() if keymap.default(a) and keymap._parse(keymap.default(a)) is None]
		assert not bad, "defaults that cannot be read: " + ", ".join(bad)
		return "%d actions" % len(keymap.actions())

	def keymap_conflicts():
		defaults = {a: keymap.default(a) for a in keymap.actions()}
		problems = []
		for a in keymap.actions():
			if keymap.is_fixed(a) or not defaults[a]:
				continue
			error = keymap.check(a, defaults[a], defaults)
			if error:
				problems.append("%s: %s" % (a, error))
		assert not problems, "; ".join(problems)

	def keymap_labels():
		missing = [a for a in keymap.actions() if keymap.label(a) == a]
		assert not missing, "no label for: " + ", ".join(missing)

	def keymap_wiring():
		fixed_in_tab = {"next_unread", "open_row", "prev_user", "next_user"}
		special = fixed_in_tab | {
			"check_updates", "load_older", "action_menu", "clear_cache", "select_all", "jump_row", "edit_search",
		}
		table = feedlist.TweetPanel._KEY_ACTIONS
		tab_ids = {a for a in keymap.actions() if keymap.area(a) == "tab"}
		problems = ["tab action without a handler: " + a for a in sorted(tab_ids - set(table) - special)]
		problems += ["handler without a table entry: " + a for a in sorted(set(table) - tab_ids)]
		problems += [
			"no method %s for %s" % (m, a) for a, (m, _need) in table.items()
			if not callable(getattr(feedlist.TweetPanel, m, None))
		]
		sources = {
			"window": inspect.getsource(mainwindow.MainWindow._key_actions) + inspect.getsource(mainwindow.MainWindow.onCharHook),
			"compose": inspect.getsource(compose.ComposeDialog.onCharHook),
			"details": inspect.getsource(dialogs.FullTextDialog.on_char_hook),
		}
		for area, source in sources.items():
			problems += [
				"%s action not used by its window: %s" % (area, a) for a in keymap.actions()
				if keymap.area(a) == area and '"%s"' % a not in source
			]
		assert not problems, "; ".join(problems)

	check("Shortcuts: every default can be read", keymap_defaults)
	check("Shortcuts: defaults are allowed and do not clash", keymap_conflicts)
	check("Shortcuts: every action has a label", keymap_labels)
	check("Shortcuts: every action is used by a window", keymap_wiring)

	# ---------------------------------------------------------------- notifications

	def notification_ids():
		user = SimpleNamespace(id="5", name="Name", screen_name="User")
		tweet = SimpleNamespace(
			id="77", full_text="hello", text="hello", url="https://x.com/User/status/77", urls=[], media=[],
			favorited=False, bookmarked=False,
		)

		def make(raw_id, stamp):
			return SimpleNamespace(
				id=raw_id, timestamp_ms=stamp, message="Name liked your post", from_user=user, tweet=tweet,
			)

		first = notification_to_post(make("AAAA-first-fetch", 1790630329619), 0)
		again = notification_to_post(make("BBBB-next-day", 1790630329619), 9)
		assert first.id == again.id, "the same notification got two ids: %d and %d" % (first.id, again.id)
		later = notification_to_post(make("AAAA-first-fetch", 1790630329620), 0)
		assert first.id // 1000 == 1790630329619 and later.id // 1000 == 1790630329620, "the time is not in the id"

	def notification_migration():
		store = MemoryStore()
		aid = store.add_account("1", "u", "U", {"auth_token": "a", "ct0": "b"})
		stamp = 1790630329619
		url = "https://x.com/u/status/5"

		def row(suffix, read, st=stamp, link=url):
			return Post(st * 1000 + suffix, "U (@u)", "liked", st // 1000, link, is_read=read)

		a, b = row(119, False), row(404, True)
		c = row(7, False, st=stamp + 5000, link="https://x.com/u/status/6")
		store.set_account_meta(aid, "tabfocus:notifications:notifications", str(a.id))
		tabstate.set_cache(store, aid, "notifications", "notifications", [a, b, c])
		rows = tabstate.get_cache(store, aid, "notifications", "notifications")
		assert len(rows) == 2, "%d rows after the migration, 2 expected" % len(rows)
		want = notification_id(stamp, "u", url)
		merged = next((p for p in rows if p.id == want), None)
		assert merged is not None, "the duplicate did not get the stable id"
		assert merged.is_read, "a read mark was lost"
		place = store.get_account_meta(aid, "tabfocus:notifications:notifications")
		assert place == str(want), "the remembered place was not carried over (%s)" % place
		again = tabstate.get_cache(store, aid, "notifications", "notifications")
		assert [p.id for p in again] == [p.id for p in rows], "a second read changed the ids"

	check("Notifications: the same notification gets the same id in every fetch", notification_ids)
	check("Notifications: old cache is converted once, duplicates merged", notification_migration)

	# ---------------------------------------------------------------- background settings and queue

	def config_tabs():
		cfg = Config(MemoryStore(), 1)
		cfg.set("bg_interval_tabs", 15)
		cfg.set("bg_tab_settings", json.dumps({"search:x": [5, False], "user:bad": ["a", 1]}))
		assert cfg.tab_setting("search:x") == (5, False), cfg.tab_setting("search:x")
		assert cfg.tab_setting("thread:1") == (15, True), cfg.tab_setting("thread:1")
		assert cfg.tab_setting("user:bad") == (15, True), "a broken entry must fall back to the default"
		cfg.set("bg_announce_off", "tabs")
		assert cfg.tab_setting("thread:1") == (15, False), "the default must be silent when tabs are silent"
		for broken in ("not json", "[1, 2]"):
			cfg.set("bg_tab_settings", broken)
			assert cfg.tab_setting("search:x") == (15, False), "settings that cannot be read must give the default"

	def queue_rules():
		def fresh():
			return FetchService(MemoryStore(), notify=lambda callback, result: None)

		service = fresh()
		assert service.submit("a", None, {}, background=True), "a background check was refused when idle"
		assert service.submit("b", None, {}), "a reader's request did not queue behind a background check"
		assert not service.submit("c", None, {}), "a third request was accepted"
		assert not service.submit("d", None, {}, background=True), "a background check was accepted while busy"
		service = fresh()
		assert service.submit("a", None, {}), "a reader's request was refused when idle"
		assert not service.submit("b", None, {}, background=True), "a background check went in front of a reader's request"
		assert not service.submit("c", None, {}), "a second reader's request was accepted"

	check("Background: interval and speak of one tab, with the default", config_tabs)
	check("Background: a reader's request goes before background checks", queue_rules)

	# ---------------------------------------------------------------- search

	def filters_order():
		order = explore.ordered_filters("videos,bogus,images,images")
		keys = [k for k, _text in order]
		assert keys[:2] == ["videos", "images"], keys[:3]
		assert len(keys) == len(explore.SEARCH_FILTERS) and len(set(keys)) == len(keys), "a filter is missing or twice"

	def search_query():
		query = build_search_query("cats", "", ["images", "links"], ["retweets"], {})
		assert query == "cats filter:images filter:links -filter:retweets", query

	check("Search: filter order keeps every filter once", filters_order)
	check("Search: the query is built from the filters", search_query)

	# ---------------------------------------------------------------- user menu

	def user_info_text():
		info = {
			"name": "N", "handle": "h", "bio": "a\nb", "followers": 5, "you_follow": True, "follows_you": False,
			"muted": None, "can_dm": False, "joined": 1790630329,
		}
		lines = dialogs.user_info_lines(info)
		assert lines[0] == "N (@h)", lines[0]
		assert "bio: a b" in lines, lines
		assert "followers: 5" in lines, lines
		assert "this user does not follow you" in lines, lines
		assert not any("muted" in line for line in lines), "an unknown value must be left out"

	def pinned_cases():
		import asyncio

		class TweetNotAvailable(Exception):
			pass

		class FakeClient:
			def __init__(self, ids):
				self.ids = ids

			async def get_user_by_screen_name(self, handle):
				return SimpleNamespace(pinned_tweet_ids=self.ids)

			async def get_tweet_by_id(self, tweet_id):
				raise TweetNotAvailable("gone")

		service = FetchService(MemoryStore(), notify=lambda callback, result: None)
		loop = asyncio.new_event_loop()
		try:
			none = loop.run_until_complete(service._pinned(FakeClient([]), 1, {"handle": "x"}))
			assert none["ok"] and none["pinned"] is None and not none["posts"], none
			gone = loop.run_until_complete(service._pinned(FakeClient([123]), 1, {"handle": "x"}))
			assert gone["ok"] and gone["unavailable"] and gone["pinned"] == "123", gone
		finally:
			loop.close()

	def user_menu_entries():
		bound = []
		window = SimpleNamespace(Bind=lambda event, handler, item: bound.append(handler.__defaults__[0]))
		state = {"following": False, "muting": False, "blocking": False}
		useractions.populate_user_menu(wx.Menu(), window, lambda action: None, state)
		missing = [a for a in bound if a not in set(keymap.actions())]
		assert bound and not missing, "menu entries that are not commands: %s" % missing
		assert len(bound) == len(set(bound)), "an entry is in the menu twice"

	check("User menu: the information lines leave out what X did not tell", user_info_text)
	check("User menu: no pinned tweet and a gone pinned tweet are told apart", pinned_cases)
	check("User menu: every entry is a command of its own", user_menu_entries)

	# ---------------------------------------------------------------- shortcuts as they work

	class FakeKey:
		"""Stands in for a key event."""

		def __init__(self, spec):
			self._ctrl, self._alt, self._shift, self._code = spec

		def GetKeyCode(self):
			return self._code

		def ControlDown(self):
			return self._ctrl

		def AltDown(self):
			return self._alt

		def ShiftDown(self):
			return self._shift

	def keymap_runtime():
		kept = {}
		old_load, old_save = keymap._load, keymap._save
		try:
			keymap.configure(lambda: kept.get("text"), lambda text: kept.__setitem__("text", text))
			for a in keymap.actions():
				spec = keymap._parse(keymap.default(a))
				if spec is not None:
					assert keymap.matches(FakeKey(spec), a), "the default key of %s does not match it" % a
			assert keymap.with_key("Search", "search") == "Search (Ctrl+F)", keymap.with_key("Search", "search")
			changed = keymap.effective()
			changed["search"] = "ctrl+k"
			changed["like"] = ""
			keymap.save(changed)
			saved = json.loads(kept["text"])
			assert saved == {"search": "ctrl+k", "like": ""}, "saved %s" % saved
			assert keymap.matches(FakeKey(keymap._parse("ctrl+k")), "search"), "the new key does not work"
			assert not keymap.matches(FakeKey(keymap._parse("ctrl+f")), "search"), "the old key still works"
			assert not keymap.matches(FakeKey(keymap._parse("alt+l")), "like"), "a removed shortcut still works"
			assert keymap.with_key("Search", "search") == "Search (Ctrl+K)", keymap.with_key("Search", "search")
			assert keymap.with_key("Like", "like") == "Like", "the label of an action with no key must have no key"
		finally:
			keymap.configure(old_load, old_save)

	check("Shortcuts: a changed or removed key takes effect, labels follow", keymap_runtime)

	# ---------------------------------------------------------------- pinned tweet in a user timeline

	def pinned_order():
		def post(i, pinned=False):
			return Post(i, "N (@u)", "t%d" % i, 0, "https://x.com/u/status/%d" % i, stats='{"pinned": true}' if pinned else "")

		newest = feedlist.OrderedPosts(True)
		for p in (post(9), post(8), post(3, True), post(7)):
			newest.add(p)
		assert [p.id for p in newest.rows] == [3, 9, 8, 7], [p.id for p in newest.rows]
		assert newest.index_of(3) == 0 and newest.index_of(8) == 2, "index_of does not follow the pinned tweet"
		oldest = feedlist.OrderedPosts(False)
		for p in (post(9), post(3, True), post(7), post(8)):
			oldest.add(p)
		assert [p.id for p in oldest.rows] == [7, 8, 9, 3], [p.id for p in oldest.rows]
		assert oldest.index_of(3) == 3 and oldest.index_of(8) == 1, "index_of does not follow the pinned tweet"
		assert main_text(post(3, True)).startswith("pinned: "), main_text(post(3, True))
		assert not main_text(post(8)).startswith("pinned: "), "a tweet that is not pinned has the prefix"

	def pinned_marked():
		import asyncio

		def tweet(i):
			return SimpleNamespace(
				id=str(i), retweeted_tweet=None, full_text="t%d" % i, url="https://x.com/u/status/%d" % i,
				user=SimpleNamespace(name="N", screen_name="u"),
			)

		class Page(list):
			next_cursor = None

		class FakeClient:
			def __init__(self):
				self.lookups = 0

			async def get_user_by_screen_name(self, handle):
				self.lookups += 1
				return SimpleNamespace(id="5", pinned_tweet_ids=["3"])

			async def get_user_tweets(self, uid, kind, count=20):
				return Page([tweet(3), tweet(9), tweet(8)])

		service = FetchService(MemoryStore(), notify=lambda callback, result: None)
		client = FakeClient()
		options = {"source": "user", "params": {"handle": "u", "uid": "5"}, "chain": "u", "pages": 1}
		loop = asyncio.new_event_loop()
		try:
			opened = loop.run_until_complete(service._feed(client, 1, dict(options, pinned_lookup=True)))
			assert client.lookups == 1, "opening a tab must look the user up once"
			assert [p.id for p in opened["posts"] if is_pinned(p)] == [3], "the new tab did not mark the pinned tweet"
			refreshed = loop.run_until_complete(service._feed(client, 1, dict(options, pinned=[3], known=[3, 9])))
			assert client.lookups == 1, "a refresh looked the user up again"
			assert [p.id for p in refreshed["posts"] if is_pinned(p)] == [3], "a refresh lost the pinned mark"
		finally:
			loop.close()

	check("User timeline: the pinned tweet sits beside the newest in both orders and says so", pinned_order)
	check("User timeline: a new tab marks the pinned tweet, a refresh does not look it up again", pinned_marked)

	def auto_read_pinned_last():
		store = MemoryStore()
		aid = store.add_account("1", "u", "U", {"auth_token": "a", "ct0": "b"})
		posts = [
			Post(i, "N (@u)", "t%d" % i, 0, "https://x.com/u/status/%d" % i, stats='{"pinned": true}' if i == 3 else "")
			for i in (9, 8, 3, 7)
		]
		tabstate.set_cache(store, aid, "user", "u", posts)
		values = {
			"auto_full": True, "embed_first": True, "author_mode": "both", "time_mode": "absolute", "time_pattern": "%Y",
		}
		plugin = SimpleNamespace(
			store=store, config=SimpleNamespace(get=lambda key: values[key]),
			tray_refresh=lambda: None, unread_invalidate=lambda ident: None,
		)
		reader = autoread.AutoReader(plugin)
		order = []
		for _step in range(4):
			text, _events, _moved = reader._next_from_cache(aid, {"kind": "user", "key": "u"})
			order.append(text.split(", ")[-1])
		assert order == ["t7", "t8", "t9", "pinned: t3"], "auto read order: %s" % order

	check("Auto read: the pinned tweet of a user timeline is read last, as the list shows it", auto_read_pinned_last)

	# ---------------------------------------------------------------- the add-on's menus

	def app_menu_entries():
		def entries(choices):
			menu = wx.Menu()
			bound = []
			fill_app_menu(
				menu, lambda item, handler: bound.append(handler), lambda: None, lambda: None, choices, lambda acc: None,
			)
			return [item.IsSeparator() for item in menu.GetMenuItems()], len(bound)

		two = [({"id": 1}, "&1 A"), ({"id": 2}, "&2 B")]
		kinds, count = entries(two)
		assert kinds == [False, False, True, False, False] and count == 4, (kinds, count)
		kinds, count = entries(two[:1])
		assert kinds == [False, False] and count == 2, "one account must add no account entries: %s" % (kinds,)

	def choices_without_counts():
		accounts = [
			{"id": 1, "handle": "a", "name": "A", "active": True}, {"id": 2, "handle": "b", "name": "B", "active": False},
		]
		fake = SimpleNamespace(_store=SimpleNamespace(list_accounts=lambda: accounts))
		texts = [text for _acc, text in GlobalPlugin.account_choices(fake, counts=False)]
		assert texts == ["&1 A (@a) (active)", "&2 B (@b)"], texts

	check("Menus: the tray icon and NVDA's menu share their entries", app_menu_entries)
	check("Menus: the accounts of NVDA's menu carry no unread count", choices_without_counts)

	# ---------------------------------------------------------------- lists of users

	def users_pages():
		import asyncio

		def user(i):
			return SimpleNamespace(id=str(i), screen_name="u%d" % i, name="N%d" % i, description="bio\n%d" % i)

		class Page(list):
			def __init__(self, items, rest=None):
				super().__init__(items)
				self.rest = rest

			def next(self):
				return self.rest

		class FakeClient:
			async def get_user_followers(self, uid, count=20):
				return Page([user(1), user(2), user(2)], Page([user(3), user(4)]))

		service = FetchService(MemoryStore(), notify=lambda callback, result: None)
		options = {"kind": "followers", "params": {"handle": "u", "uid": "5"}, "chain": "followers:u", "pages": 1}
		old_delay, FetchService.PAGE_DELAY = FetchService.PAGE_DELAY, (0, 0)
		loop = asyncio.new_event_loop()
		try:
			first = loop.run_until_complete(service._users(FakeClient(), 1, options))
			assert [u["id"] for u in first["users"]] == ["1", "2"], first["users"]
			assert first["users"][0]["bio"] == "bio 1", "the bio must be on one line"
			more = loop.run_until_complete(service._users(FakeClient(), 1, dict(options, have=["1", "2"])))
			assert [u["id"] for u in more["users"]] == ["3", "4"], more["users"]
		finally:
			loop.close()
			FetchService.PAGE_DELAY = old_delay

	def users_cache():
		store = MemoryStore()
		aid = store.add_account("1", "u", "U", {"auth_token": "a", "ct0": "b"})
		rows = [{"id": "1", "handle": "a", "name": "A", "bio": ""}]
		tabstate.set_cache(store, aid, "users", "followers:x", rows)
		assert tabstate.get_cache(store, aid, "users", "followers:x") == rows, "the rows did not come back"
		assert tabstate.get_cache(store, aid, "users", "following:x") is None, "a tab that was never saved has rows"

	def enter_user_actions():
		from .settings import ENTER_ACTION_CHOICES

		known = set(keymap.actions())
		wanted = useractions.USER_ACTIONS + tuple(useractions.ENTER_USER_ACTIONS.values())
		missing = [a for a in wanted if a not in known]
		assert not missing, "not commands: %s" % missing
		offered = {key for key, _text in ENTER_ACTION_CHOICES}
		handled = set(feedlist.TweetPanel.ENTER_HANDLERS)
		lacking = [k for k in useractions.ENTER_USER_ACTIONS if k not in offered or k not in handled]
		assert not lacking, "Enter key actions not offered or not handled: %s" % lacking

	check("Users tab: pages are read, repeats dropped, the next page comes from the place kept", users_pages)
	check("Users tab: the rows are saved and come back", users_cache)
	check("Users tab: every Enter key action of a user is a command, offered and handled", enter_user_actions)

	# ---------------------------------------------------------------- follow, mute and block

	def relation_job():
		import asyncio

		class FakeClient:
			def __init__(self):
				self.lookups, self.calls = 0, []

			async def get_user_by_screen_name(self, handle):
				self.lookups += 1
				return SimpleNamespace(id="7", following=True, muting=False, blocking=False)

			def __getattr__(self, name):
				if not name.endswith("_user"):
					raise AttributeError(name)

				async def call(uid):
					self.calls.append((name, uid))
				return call

		client = FakeClient()
		service = FetchService(MemoryStore(), notify=lambda callback, result: None)
		loop = asyncio.new_event_loop()
		try:
			checked = loop.run_until_complete(service._relation(client, 1, {"handle": "x", "uid": ""}))
			assert checked["state"] == {"following": True, "muting": False, "blocking": False}, checked
			assert checked["uid"] == "7", checked
			for action in ("follow", "unfollow", "mute", "unmute", "block", "unblock"):
				loop.run_until_complete(service._relation(client, 1, {"handle": "x", "uid": "7", "do": action}))
		finally:
			loop.close()
		assert client.lookups == 1, "doing a change looked the user up again"
		assert [c[0] for c in client.calls] == [
			"follow_user", "unfollow_user", "mute_user", "unmute_user", "block_user", "unblock_user",
		], client.calls
		assert all(uid == "7" for _name, uid in client.calls), client.calls

	def relation_words():
		texts = useractions.relation_texts()
		assert set(texts) == {"follow", "unfollow", "mute", "unmute", "block", "unblock"}, sorted(texts)
		assert all("{user}" in ask and "{user}" in done for ask, done in texts.values()), "a text lacks {user}"

	check("Relations: the state is read once, each change calls its own method with the id", relation_job)
	check("Relations: every change has its question and its answer", relation_words)

	def relation_table():
		store = MemoryStore()
		aid = store.add_account("1", "u", "U", {"auth_token": "a", "ct0": "b"})
		assert store.relation_state(aid, "x")["following"] is None, "a user never seen must be unknown"
		store.set_relations(aid, [{"handle": "x", "uid": "5", "following": True, "muting": None}])
		store.set_relations(aid, [{"handle": "x", "muting": False}])
		state = store.relation_state(aid, "X")
		assert state["following"] is True and state["muting"] is False and state["uid"] == "5", state
		store.replace_relation_list(aid, "following", [{"handle": "y", "uid": "6"}])
		assert store.relation_state(aid, "x")["following"] is False, "a user left out of the whole list still follows"
		assert store.relation_state(aid, "y")["following"] is True, "a user of the whole list does not follow"
		assert store.relation_state(aid, "nobody")["following"] is False, "after the whole list, not in it means no"
		assert store.relation_state(aid, "nobody")["blocking"] is None, "a list that was not read says nothing"

	def user_menu_states():
		def labels(state):
			menu = wx.Menu()
			window = SimpleNamespace(Bind=lambda event, handler, item: None)
			useractions.populate_user_menu(menu, window, lambda action: None, state)
			return [item.GetItemLabelText() for item in menu.GetMenuItems() if not item.IsSeparator()]

		on = labels({"following": True, "muting": True, "blocking": True})
		off = labels({"following": False, "muting": False, "blocking": False})
		for word in ("Unfollow", "Unmute", "Unblock"):
			assert word in on and word not in off, (word, on, off)
		for word in ("Follow", "Mute", "Block"):
			assert word in off and word not in on, (word, on, off)
		unknown = labels({"following": None, "muting": False, "blocking": None})
		said = [w for w in ("Follow", "Unfollow", "Block", "Unblock") if w in unknown]
		assert "Mute" in unknown and not said, "a relation that is not known must have no entry: %s" % unknown

	check("Relations: the table keeps what is known, and a whole list tells who is not in it", relation_table)
	check("Relations: the menu has one entry for each relation, saying what it will do", user_menu_states)

	def authors_recorded():
		def tweet(i, handle, **flags):
			user = SimpleNamespace(name="N", screen_name=handle, id=str(100 + i), **flags)
			return SimpleNamespace(
				id=str(i), retweeted_tweet=None, full_text="t", url="https://x.com/%s/status/%d" % (handle, i), user=user,
			)

		class Page(list):
			next_cursor = None

		store = MemoryStore()
		aid = store.add_account("1", "u", "U", {"auth_token": "a", "ct0": "b"})
		service = FetchService(store, notify=lambda callback, result: None)
		service._job_account = aid
		service._collect(Page([tweet(1, "Ann", following=True, followed_by=False), tweet(2, "bob")]))
		ann = store.relation_state(aid, "ann")
		assert ann["following"] is True and ann["followed_by"] is False and ann["uid"] == "101", ann
		assert store.relation_state(aid, "bob")["following"] is None, "a flag X did not send was taken as no"

	def relation_rules():
		asks = set(mainwindow.MainWindow._RELATION_ASKS)
		assert asks == {"block", "unfollow"}, asks
		assert asks <= set(useractions.relation_texts()), "a change that asks has no question"

	check("Relations: the authors of a page are kept with the flags X sent, none made up", authors_recorded)
	check("Relations: a block and an unfollow ask first, the rest is done at once", relation_rules)

	def trends_rows():
		import asyncio

		class FakeClient:
			async def get_trends(self, category, count=20):
				return [
					SimpleNamespace(name="#Tag", domain_context="Only on X · Trending", tweets_count=None),
					SimpleNamespace(name="A headline, with a comma", domain_context=None, tweets_count=1200),
				]

			async def get_place_trends(self, woeid):
				return {"trends": [SimpleNamespace(name="MR FANBOY", query="%22MR+FANBOY%22", tweet_volume=None)]}

			async def get_available_locations(self):
				return [
					SimpleNamespace(name="Worldwide", woeid=1, country=""),
					SimpleNamespace(name="Bangkok", woeid=1225448, country="Thailand"),
					SimpleNamespace(name="", woeid=5, country=""),
				]

		service = FetchService(MemoryStore(), notify=lambda callback, result: None)
		loop = asyncio.new_event_loop()
		try:
			category = loop.run_until_complete(service._trends(FakeClient(), 1, {"category": "trending"}))
			place = loop.run_until_complete(service._trends(FakeClient(), 1, {"woeid": 1225448}))
			places = loop.run_until_complete(service._places(FakeClient(), 1, {}))
		finally:
			loop.close()
		assert [t["query"] for t in category["trends"]] == ["#Tag", '"A headline, with a comma"'], category["trends"]
		assert category["trends"][1]["note"] == "1200 tweets", category["trends"][1]
		assert place["trends"][0]["query"] == '"MR FANBOY"', place["trends"]
		assert [p["woeid"] for p in places["places"]] == [1, 1225448], places["places"]

	check("Explore: trends give the text to search for, places give their number", trends_rows)

	def search_edit_roundtrip():
		for raw in ("#tag", "(#a OR #b)", '"MR FANBOY CHAPTER 8"', "word"):
			assert build_search_query(raw.replace(",", " "), "", [], [], {}) == raw, raw

	check("Explore: the text of a search made elsewhere comes back unchanged through the keywords", search_edit_roundtrip)

	def clients_made_again():
		import asyncio

		class NotFound(Exception):
			pass

		store = MemoryStore()
		aid = store.add_account("1", "u", "U", {"auth_token": "a", "ct0": "b"})
		service = FetchService(store, notify=lambda callback, result: None)
		made = []
		service._make_client = lambda cookies: made.append(1) or object()
		first = service._get_client(aid)
		assert service._get_client(aid) is first and len(made) == 1, "a young client was made again"
		service._client_born[aid] -= FetchService.CLIENT_MAX_AGE + 1
		assert service._get_client(aid) is not first and len(made) == 2, "an old client was kept"
		calls = []

		async def fake_do(kind, options):
			calls.append(kind)
			if len(calls) == 1:
				raise NotFound("404")
			return "ok"

		async def other_error(kind, options):
			calls.append(kind)
			raise ValueError("no")

		loop = asyncio.new_event_loop()
		try:
			service._do = fake_do
			assert loop.run_until_complete(service._attempt("relation", {"account_id": aid})) == "ok"
			assert calls == ["relation", "relation"], calls
			assert aid not in service._clients, "a 404 did not make the client again"
			calls.clear()
			service._do = other_error
			try:
				loop.run_until_complete(service._attempt("relation", {"account_id": aid}))
				raise AssertionError("an error that is not a 404 was swallowed")
			except ValueError:
				pass
			assert calls == ["relation"], "an error that is not a 404 was tried again: %s" % calls
		finally:
			loop.close()

	check("Client: made again when it is old and after a 404, an error that is not a 404 is not tried again", clients_made_again)

	# ---------------------------------------------------------------- direct messages

	def dm_rows():
		users = dm_users({
			"1": {"id_str": "1", "screen_name": "me", "name": "Me"},
			"9": {"id_str": "9", "screen_name": "Pal", "name": "A Pal"},
		})
		convo = dm_convo_row({
			"conversation_id": "1-9", "type": "ONE_TO_ONE", "trusted": True, "sort_event_id": "50",
			"last_read_event_id": "30", "participants": [{"user_id": "1"}, {"user_id": "9"}],
		}, users, "1")
		assert convo["partner_id"] == "9" and convo["members"][0]["handle"] == "Pal", convo
		assert convo["sort_id"] == 50 and convo["read_id"] == 30 and not convo["is_group"], convo
		assert convo["last_id"] == 50 and not convo["low_quality"] and not convo["nsfw"], convo
		spam = dm_convo_row({
			"conversation_id": "1-3", "type": "ONE_TO_ONE", "low_quality": True, "participants": [{"user_id": "3"}],
		}, users, "1")
		assert spam["low_quality"] and not spam["trusted"], spam
		group = dm_convo_row({
			"conversation_id": "g1", "type": "GROUP_DM", "name": "Club", "created_by_user_id": "9",
			"participants": [{"user_id": "1"}, {"user_id": "9"}, {"user_id": "7"}],
		}, users, "1")
		assert group["is_group"] and group["partner_id"] == "" and len(group["members"]) == 2, group
		assert group["members"][1]["handle"] == "", "a user X did not describe must have no user name"
		data = {
			"id": "40", "sender_id": "9", "time": "1790630329000", "text": "see https://t.co/aa and https://t.co/bb",
			"entities": {"urls": [{"url": "https://t.co/aa", "expanded_url": "https://example.com/x"}]},
			"attachment": {"photo": {
				"url": "https://t.co/bb", "media_url_https": "https://pbs.twimg.com/dm/1.jpg",
				"expanded_url": "https://x.com/messages/media/1",
			}},
			"reply_data": {"id": "30", "sender_id": "1", "text": "before"},
		}
		row = dm_message_row(data, "1", read_id=30)
		assert row["text"] == "see https://example.com/x and", row["text"]
		assert row["attachment"] == {
			"kind": "image", "url": "https://pbs.twimg.com/dm/1.jpg", "link": "https://x.com/messages/media/1",
		}, row["attachment"]
		assert row["reply"]["id"] == "30" and row["ts"] == 1790630329 and not row["is_read"], row
		assert dm_message_row(data, "1", read_id=40)["is_read"], "a message X says was read must be read"
		assert dm_message_row(data, "9")["is_read"], "an own message must be read"
		assert dm_message_row(data, "1", older=True)["is_read"], "an older batch must count as read"

	def dm_store():
		store = MemoryStore()
		aid = store.add_account("1", "u", "U", {"auth_token": "a", "ct0": "b"})
		store.dm_save_convos(aid, [{"convo_id": "a", "sort_id": 5}, {"convo_id": "b", "sort_id": 9}])
		assert [c["convo_id"] for c in store.dm_load_convos(aid)] == ["b", "a"], "conversations are not newest first"
		store.dm_add_messages(aid, "a", [{"id": 2, "is_read": False}, {"id": 1, "is_read": True}])
		store.dm_mark_read(aid, "a", [2])
		store.dm_add_messages(aid, "a", [{"id": 2, "is_read": False, "text": "new"}])
		rows = store.dm_load_messages(aid, "a")
		assert [m["id"] for m in rows] == [1, 2] and rows[1]["is_read"] and rows[1]["text"] == "new", rows
		assert store.dm_unread_counts(aid) == {}, store.dm_unread_counts(aid)
		store.dm_add_messages(aid, "a", [{"id": 3, "is_read": False}])
		assert store.dm_unread_counts(aid) == {"a": 1}, store.dm_unread_counts(aid)
		store.dm_delete_message(aid, "a", 3)
		assert store.dm_unread_counts(aid) == {} and len(store.dm_load_messages(aid, "a")) == 2, "a deleted message stayed"
		store.dm_delete_convo(aid, "a")
		assert [c["convo_id"] for c in store.dm_load_convos(aid)] == ["b"], "a deleted conversation stayed"
		assert store.dm_load_messages(aid, "a") == [], "the messages of a deleted conversation stayed"

	def dm_jobs():
		import asyncio

		class FakeV11:
			def __init__(self):
				self.calls = []

			async def dm_inbox(self, cursor):
				state = {
					"conversations": {"1-9": {
						"conversation_id": "1-9", "type": "ONE_TO_ONE", "trusted": True, "sort_event_id": "50",
						"last_read_event_id": "30", "participants": [{"user_id": "1"}, {"user_id": "9"}],
					}},
					"users": {"9": {"id_str": "9", "screen_name": "Pal", "name": "A Pal"}},
					"inbox_timelines": {"trusted": {"status": "AT_END"}},
				}
				return {"inbox_initial_state": state}, {}

			async def dm_conversation(self, conversation_id, max_id):
				self.calls.append((conversation_id, max_id))
				ids, status = ((40, 30), "HAS_MORE") if max_id is None else ((20,), "AT_END")
				senders = {40: "9", 30: "9", 20: "1"}
				entries = [
					{"message": {"message_data": {
						"id": str(i), "sender_id": senders[i], "text": "t%d" % i, "time": "1790630329000",
					}}}
					for i in ids
				]
				return {"conversation_timeline": {"entries": entries, "status": status, "users": {}}}, {}

		class FakeClient:
			def __init__(self):
				self.v11 = FakeV11()
				self.sent = []

			async def user_id(self):
				return "1"

			async def send_dm(self, user_id, text, media_id=None, reply_to=None):
				self.sent.append((user_id, text, media_id, reply_to))
				return SimpleNamespace(id="60", text=text, time="1790630329000", reply_data=None)

			async def delete_dm(self, message_id):
				self.sent.append(("delete", message_id))

			async def delete_dm_conversation(self, conversation_id):
				self.sent.append(("delete conversation", conversation_id))

		service = FetchService(MemoryStore(), notify=lambda callback, result: None)
		client = FakeClient()
		old_delay, FetchService.PAGE_DELAY = FetchService.PAGE_DELAY, (0, 0)
		loop = asyncio.new_event_loop()
		try:
			inbox = loop.run_until_complete(service._dm_inbox(client, 1, {}))
			row = inbox["convos"][0]
			assert len(inbox["convos"]) == 1 and row["members"][0]["handle"] == "Pal" and row["read_id"] == 30, inbox
			assert inbox["complete"], "the inbox ended, so it was read whole"
			options = {"convo_id": "1-9", "is_group": False, "partner_id": "9", "read_id": 30, "pages": 2}
			result = loop.run_until_complete(service._dm_history(client, 1, options))
			assert [m["id"] for m in result["messages"]] == [20, 30, 40], result["messages"]
			assert [m["is_read"] for m in result["messages"]] == [True, True, False], "unread must follow X and the sender"
			assert client.v11.calls == [("9-1", None), ("9-1", 30)], client.v11.calls
			assert not result["more"], "the last page was reached"
			older = loop.run_until_complete(service._dm_history(client, 1, dict(options, pages=1, max_id=40)))
			assert all(m["is_read"] for m in older["messages"]), "an older batch must count as read"
			sent = loop.run_until_complete(service._dm_send(client, 1, {"partner_id": "9", "text": "hi", "reply_to": 40}))
			assert client.sent[-1] == ("9", "hi", None, "40"), client.sent
			assert sent["message"]["is_read"] and sent["message"]["sender_id"] == "1", sent
			loop.run_until_complete(service._dm_delete(client, 1, {"id": 60}))
			assert client.sent[-1] == ("delete", "60"), client.sent
			gone = loop.run_until_complete(service._dm_delete_convo(client, 1, {"convo_id": "1-9"}))
			assert client.sent[-1] == ("delete conversation", "1-9") and gone["convo_id"] == "1-9", client.sent
		finally:
			loop.close()
			FetchService.PAGE_DELAY = old_delay

	check("Direct messages: rows are made from what X sends, unread follows X and the sender", dm_rows)
	check("Direct messages: the store keeps read marks, counts unread, deletes", dm_store)
	check("Direct messages: inbox, pages, older batch, send and delete jobs", dm_jobs)

	def dm_inbox_paging():
		import asyncio

		def conv(cid, other, stamp):
			return {
				"conversation_id": cid, "type": "ONE_TO_ONE", "trusted": True, "sort_timestamp": str(stamp),
				"sort_event_id": str(stamp), "last_read_event_id": "0",
				"participants": [{"user_id": "1"}, {"user_id": other}],
			}

		class FakeV11:
			def __init__(self, legacy_shape=False, empty_second=False):
				self.calls = []
				self.legacy_shape, self.empty_second = legacy_shape, empty_second

			async def dm_inbox(self, cursor):
				state = {
					"conversations": {"a": conv("a", "9", 300)},
					"users": {"9": {"id_str": "9", "screen_name": "Pal", "name": "A Pal"}},
					"inbox_timelines": {"trusted": {"status": "HAS_MORE", "min_entry_id": "100"}},
				}
				return {"inbox_initial_state": state}, {}

			async def dm_inbox_timeline(self, name, max_id):
				self.calls.append((name, max_id))
				if self.empty_second:
					return {"something_else": {}}, {}
				last = max_id == "50"
				convs = {("c" if last else "b"): conv("c" if last else "b", "8", 100 if last else 200)}
				status, following = ("AT_END", None) if last else ("HAS_MORE", "50")
				if self.legacy_shape:
					state = {"conversations": convs, "inbox_timelines": {"trusted": {"status": status, "min_entry_id": following}}}
					return {"inbox_initial_state": state}, {}
				return {"inbox_timeline": {"conversations": convs, "status": status, "min_entry_id": following, "users": {}}}, {}

		class FakeClient:
			def __init__(self, **kinds):
				self.v11 = FakeV11(**kinds)

			async def user_id(self):
				return "1"

		service = FetchService(MemoryStore(), notify=lambda callback, result: None)
		old_delay, FetchService.PAGE_DELAY = FetchService.PAGE_DELAY, (0, 0)
		loop = asyncio.new_event_loop()
		try:
			for legacy in (False, True):
				client = FakeClient(legacy_shape=legacy)
				result = loop.run_until_complete(service._dm_inbox(client, 1, {}))
				assert [c["convo_id"] for c in result["convos"]] == ["a", "b", "c"], result["convos"]
				assert result["complete"], "the last page said AT_END"
				assert client.v11.calls == [("trusted", "100"), ("trusted", "50")], client.v11.calls
			empty = loop.run_until_complete(service._dm_inbox(FakeClient(empty_second=True), 1, {}))
			assert [c["convo_id"] for c in empty["convos"]] == ["a"] and not empty["complete"], "an empty page is not the end"
			service.DM_INBOX_PAGES = 2
			limited = loop.run_until_complete(service._dm_inbox(FakeClient(), 1, {}))
			assert [c["convo_id"] for c in limited["convos"]] == ["a", "b"] and not limited["complete"], limited["convos"]
		finally:
			loop.close()
			FetchService.PAGE_DELAY = old_delay

	check("Direct messages: older pages of the inbox are read, in both shapes, and the end is told apart", dm_inbox_paging)

	def dm_start_job():
		import asyncio

		class FakeClient:
			def __init__(self):
				self.calls = []

			async def send_dm(self, user_id, text, media_id=None, reply_to=None):
				self.calls.append(("send_dm", user_id, text))
				return SimpleNamespace(id="1")

			async def create_group(self, user_ids, text, media_id=None):
				self.calls.append(("create_group", list(user_ids), text))
				return SimpleNamespace(id="g77")

		service = FetchService(MemoryStore(), notify=lambda callback, result: None)
		client = FakeClient()
		loop = asyncio.new_event_loop()
		try:
			one = loop.run_until_complete(service._dm_start(client, 1, {"user_ids": [9], "text": "hello"}))
			assert one["partner_id"] == "9" and one["group_id"] == "", one
			many = loop.run_until_complete(service._dm_start(client, 1, {"user_ids": ["8", "9"], "text": "hi all"}))
			assert many["group_id"] == "g77" and many["partner_id"] == "", many
			assert client.calls == [("send_dm", "9", "hello"), ("create_group", ["8", "9"], "hi all")], client.calls
		finally:
			loop.close()

	check("Direct messages: a chat starts with one user, a group with several", dm_start_job)

	def chat_labels():
		convo = {
			"convo_id": "1-9", "is_group": False, "partner_id": "9", "trusted": True, "muted": False,
			"members": [{"id": "9", "handle": "Pal", "name": "A Pal"}],
		}
		assert convo_name(convo, "both") == "A Pal (@Pal)", convo_name(convo, "both")
		assert convo_name(convo, "handle") == "@Pal" and convo_name(convo, "name") == "A Pal"
		assert convo_label(convo, 0, "both") == "A Pal (@Pal)", convo_label(convo, 0, "both")
		assert convo_label(convo, 3, "name") == "(3 unread) A Pal", convo_label(convo, 3, "name")
		assert convo_label(convo, 2, "name", "hidden") == "(2 unread) A Pal", "a count says more than the state"
		assert convo_label(convo, 0, "name", "hidden") == "(newer activity not available here) A Pal"
		assert convo_label(convo, 0, "name", "new") == "(new messages) A Pal"
		assert convo_label(dict(convo, is_group=True), 1, "name") == "(1 unread, group) A Pal"
		request = dict(convo, trusted=False, muted=True)
		assert convo_label(request, 0, "name") == "(request, muted) A Pal", convo_label(request, 0, "name")
		group = {
			"convo_id": "g", "is_group": True, "partner_id": "", "trusted": True, "name": "",
			"members": [{"id": str(i), "handle": "u%d" % i, "name": "N%d" % i} for i in range(5)],
		}
		assert convo_name(group, "both") == "N0, N1, N2...", convo_name(group, "both")
		assert convo_name(dict(convo, members=[]), "both") == "User 9", convo_name(dict(convo, members=[]), "both")
		assert attachment_text({"kind": "image"}) == "Image" and attachment_text(None) == ""
		spam = dict(convo, low_quality=True)
		assert convo_visible(convo, "all", False) and not convo_visible(request, "all", False)
		assert convo_visible(request, "requests", False) and not convo_visible(convo, "requests", False)
		assert convo_visible(spam, "spam", False) and not convo_visible(spam, "all", False)
		assert not convo_visible(spam, "unread", True) and not convo_visible(spam, "requests", False)
		assert convo_visible(convo, "unread", True) and not convo_visible(convo, "unread", False)
		assert convo_visible(convo, "direct", False) and not convo_visible(convo, "groups", False)
		assert convo_visible(group, "groups", False) and not convo_visible(group, "direct", False)
		assert has_new_messages({"last_id": 50, "read_id": 30}, 40)
		assert not has_new_messages({"last_id": 50, "read_id": 30}, 50), "everything X has is loaded"
		assert not has_new_messages({"last_id": 50, "read_id": 50}, 0), "X says it is read"
		assert not has_new_messages({"last_id": 50, "read_id": 30, "seen_id": 50}, 0), "marked read here"
		head, tail = split_cell("word " * 200)
		assert head and tail and len(head) <= 450 and len(tail) <= 450, (len(head), len(tail))
		assert split_cell("short") == ("short", "")
		assert describe_reactions({"reactions": [{"emoji": "A"}, {"emoji": "A"}, {"emoji": "B"}]}) == "Ax2 B"
		assert describe_reactions({}) == ""

	check("Chat tab: conversations are labelled with tags, name and unread count", chat_labels)

	def image_shrink():
		try:
			from PIL import Image
		except Exception:
			return "skipped: Pillow is not available"
		fd, path = tempfile.mkstemp(suffix=".png")
		os.close(fd)
		shrunk = None
		try:
			Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3)).save(path, "PNG")
			assert os.path.getsize(path) > 1_000_000, "the test image is not large"
			shrunk = shrink_image(path, 1_000_000)
			size = os.path.getsize(shrunk)
			assert size <= 1_000_000, "still %d bytes" % size
			with Image.open(shrunk) as result:
				assert result.format == "JPEG", result.format
			return "%d bytes after shrinking" % size
		finally:
			for leftover in (path, shrunk):
				if leftover:
					try:
						os.remove(leftover)
					except OSError:
						pass

	check("Media: an oversized image is shrunk under the limit and saved as JPEG", image_shrink)

	def virtual_list_control():
		"""The real control, in a hidden frame: count, focus, selection, refresh and emptying behave as the
		tweet list expects (it keeps focus and selection by index, and DeleteAllItems must empty it)."""
		frame = wx.Frame(None)
		try:
			asked = []

			def cell(item, col):
				asked.append((item, col))
				return "%d,%d" % (item, col)

			lst = feedlist.VirtualList(frame, cell)
			lst.InsertColumn(0, "a")
			lst.InsertColumn(1, "b")
			lst.SetItemCount(5)
			assert lst.GetItemCount() == 5, "count %d" % lst.GetItemCount()
			lst.Focus(2)
			lst.Select(2)
			lst.Select(4)
			assert lst.GetFocusedItem() == 2, "focus %d" % lst.GetFocusedItem()
			assert lst.GetFirstSelected() == 2 and lst.GetNextSelected(2) == 4, "selection by index"
			lst.SetItemState(4, 0, wx.LIST_STATE_SELECTED)
			assert lst.GetNextSelected(2) == -1, "a selection was not cleared"
			lst.RefreshItems(0, 4)
			lst.RefreshItem(1)
			lst.SetItemCount(8)
			assert lst.GetItemCount() == 8 and lst.GetFocusedItem() == 2, "growing moved the focus"
			lst.DeleteAllItems()
			assert lst.GetItemCount() == 0, "DeleteAllItems left %d rows" % lst.GetItemCount()
			assert lst.GetFirstSelected() == -1, "a selection survived the emptying"
			return "%d cells asked for" % len(asked)
		finally:
			frame.Destroy()

	check("Virtual list: the real control keeps count, focus and selection by index and empties", virtual_list_control)

	# ---------------------------------------------------------------- background queue and messages

	def due_logic():
		now = 1000000.0
		scheduled = []
		fake = SimpleNamespace(
			_store=SimpleNamespace(list_accounts=lambda: [{"id": 1}, {"id": 2}, {"id": 3}]),
			bg_enabled=lambda aid: aid != 2,
			_cfg_for=lambda aid: aid,
			_bg_items=lambda aid, cfg: {
				1: [("home", 5), ("bookmarks", 0), ("search:x", 15), ("search:new", 15)],
				2: [("home", 5)],
				3: [("home", 5)],
			}[aid],
			_bg_due={
				(1, "home"): now - 60, (1, "search:x"): now + 600, (1, "user:closed"): now - 5,
				(2, "home"): now - 99, (3, "home"): now - 300,
			},
			_schedule_bg=lambda name, aid: scheduled.append((aid, name)),
		)
		got = GlobalPlugin._collect_due(fake, now)
		order = [(a, n) for _due, a, n in got]
		assert order == [(3, "home"), (1, "home")], "due checks %s (most overdue first, account 2 is off)" % order
		assert (1, "user:closed") not in fake._bg_due, "a closed tab kept its timer"
		assert scheduled == [(1, "search:new")], "first timers set for %s" % scheduled

	def tag_prefix():
		fake = SimpleNamespace(
			account_id=lambda: 1,
			_store=SimpleNamespace(list_accounts=lambda: [
				{"id": 1, "handle": "a", "name": "A"}, {"id": 2, "handle": "b", "name": "B"},
				{"id": 3, "handle": "", "name": "Named"},
			]),
		)

		def tag(aid):
			return GlobalPlugin._tag(fake, aid, "3 new tweets")

		assert tag(1) == "3 new tweets", tag(1)
		assert tag(None) == "3 new tweets", tag(None)
		assert tag(2) == "@b: 3 new tweets", tag(2)
		assert tag(3) == "Named: 3 new tweets", tag(3)

	check("Background: which checks are due (accounts off, closed tabs, order)", due_logic)
	check("Background: news of another account starts with its @user", tag_prefix)

	# ---------------------------------------------------------------- accounts on disk

	def storage_accounts():
		try:
			with isolated_modules():
				from sqlcipher3 import dbapi2 as dbapi
		except Exception as e:
			return "skipped: sqlcipher3 is not available (%s)" % _short_error(e)
		folder = tempfile.mkdtemp(prefix="tweetaccess-selfcheck-")
		store = None
		try:
			store = Store(dbapi, os.path.join(folder, "shared.db"), secrets.token_hex(32))
			cookies = {"auth_token": "aaaaaaaa", "ct0": "bbbbbbbb"}
			a = store.add_account("9001", "a", "A", cookies)
			b = store.add_account("9002", "b", "B", cookies)
			store.add_posts(a, [Post(5, "A (@a)", "hello", 1, "https://x.com/a/status/5")])
			store.set_account_meta(a, "k", "from a")
			store.set_account_meta(b, "k", "from b")
			assert len(store.load_all(a)) == 1 and store.load_all(b) == [], "tweets are not kept apart"
			assert store.get_account_meta(a, "k") == "from a", "meta of account a"
			assert store.get_account_meta(b, "k") == "from b", "meta of account b"
			store.set_relations(a, [{"handle": "x", "uid": "5", "following": True}])
			store.replace_relation_list(b, "blocking", [{"handle": "z", "uid": "7"}])
			assert store.relation_state(a, "x")["following"] is True, "the relations of account a"
			assert store.relation_state(b, "x")["following"] is None, "the relations of account a reached account b"
			assert store.relation_state(b, "q")["blocking"] is False, "the whole blocked list of account b was lost"
			store.dm_save_convos(a, [{"convo_id": "c1", "sort_id": 3}])
			store.dm_add_messages(a, "c1", [{"id": 7, "is_read": False, "text": "hi"}])
			store.dm_mark_read(a, "c1", [7])
			store.dm_add_messages(a, "c1", [{"id": 7, "is_read": False, "text": "hi again"}])
			assert store.dm_load_messages(a, "c1")[0]["is_read"] and store.dm_unread_counts(a) == {}, "a read DM became unread"
			assert store.dm_load_convos(b) == [], "the DMs of account a reached account b"
			accounts_dir = os.path.join(folder, "accounts")
			names = sorted(os.listdir(accounts_dir))
			assert "%d.db" % a in names and "%d.db" % b in names, "no file for each account: %s" % names
			# rows an account still has in the shared database come over once
			c = store.add_account("9003", "c", "C", cookies)
			store._shared.add_posts(c, [Post(8, "C (@c)", "old", 1, "https://x.com/c/status/8")])
			store._shared.set_meta("old_setting:%d" % c, "kept")
			assert len(store.load_all(c)) == 1, "the old tweets of an account were not brought over"
			assert store.get_account_meta(c, "old_setting") == "kept", "the old meta of an account was not brought over"
			# settings: the shared ones once, then apart
			store.set_meta("cfg:auto_pause", "7")
			assert Config(store, c).get("auto_pause") == 7, "an account did not start from the shared settings"
			store.set_meta("cfg:auto_pause", "9")
			assert Config(store, c).get("auto_pause") == 7, "the shared settings came in twice"
			d = store.add_account("9004", "d", "D", cookies)
			assert Config(store, d).get("auto_pause") == 9, "a new account did not start from the shared settings"
			Config(store, c).set("auto_pause", 3)
			assert Config(store, d).get("auto_pause") == 9, "a setting of one account changed another"
			assert Config(store, c).get("auto_pause") == 3, "a setting was not kept"
			# removal
			store.remove_account(a)
			assert not os.path.exists(os.path.join(accounts_dir, "%d.db" % a)), "the file of a removed account is still there"
			assert os.path.exists(os.path.join(accounts_dir, "%d.db" % b)), "the file of another account went with it"
			assert store.get_account_meta(b, "k") == "from b", "another account lost data"
			assert a not in [x["id"] for x in store.list_accounts()], "the account is still listed"
			return "4 accounts, files and settings kept apart, removal deletes only its own"
		finally:
			if store is not None:
				store.close()
			shutil.rmtree(folder, ignore_errors=True)

	check("Accounts: separate files and settings, one-time import, removal", storage_accounts)

	lines.insert(2, "Result: %d ok, %d not ok" % (counts["ok"], counts["bad"]))

	# ---------------------------------------------------------------- what the running add-on holds

	lines.append("")
	lines.append("What the add-on holds now (read only)")
	try:
		store = plugin.store
		for acc in store.list_accounts():
			aid = acc["id"]
			cfg = plugin._cfg_for(aid)
			rows = store.load_all(aid)
			lines.append(
				"account %d (@%s): active=%s, background=%s, Home %d rows, %d unread, notification ids converted=%s" % (
					aid, acc["handle"], acc["active"], plugin.bg_enabled(aid), len(rows), store.unread_count(aid),
					bool(store.get_account_meta(aid, tabstate.NOTIF_IDS)),
				)
			)
			parts = plugin._tray_counts(aid=aid)
			lines.append(
				"    the account menu shows %d unread (%s)" % (
					sum(n for _name, n in parts), ", ".join("%s %d" % part for part in parts) or "nothing unread",
				)
			)
			raw = store.get_account_meta(aid, "tabcache:notifications:notifications")
			if raw:
				cached = [Post.from_dict(d) for d in json.loads(raw)]
				ids = [p.id for p in cached]
				same = {}
				for p in cached:
					same.setdefault((p.id // 1000, p.author, p.url), set()).add(p.id)
				lines.append(
					"    notifications cached: %d, repeated ids: %d, one notification under several ids: %d" % (
						len(cached), len(ids) - len(set(ids)), sum(1 for v in same.values() if len(v) > 1),
					)
				)
			for name, minutes in plugin._bg_items(aid, cfg):
				if name in ("bookmarks", "trends"):
					speak = "never"
				elif ":" in name:
					speak = "yes" if cfg.tab_setting(name)[1] else "no"
				else:
					speak = "yes" if plugin._bg_announces(name, aid) else "no"
				due = plugin._bg_due.get((aid, name))
				when = "not scheduled" if due is None else "next in %.1f min" % ((due - time.time()) / 60)
				lines.append("    %s: every %s min, speaks %s, %s" % (name, minutes or "off", speak, when))
		tray = plugin._tray
		lines.append("tray icon text: %s" % (tray._text if tray is not None else "no icon"))
		service = plugin._service
		lines.append(
			"worker: busy=%s, pending=%s, background running=%s, clients kept for accounts %s" % (
				service.busy, service._pending, service._bg_running, sorted(service._clients),
			)
		)
	except Exception as e:
		log.error(TAG + "self-check could not read the live state", exc_info=True)
		lines.append("could not read the live state: " + _short_error(e))
	lines.append("")
	lines.append("Self-check finished: %d ok, %d not ok" % (counts["ok"], counts["bad"]))
	return "\n".join(lines)


def _set_clipboard_text(text):
	if not wx.TheClipboard.Open():
		return False
	try:
		wx.TheClipboard.SetData(wx.TextDataObject(text))
	finally:
		wx.TheClipboard.Close()
	return True


class DiagnosticsPage(wx.Panel):
	TITLE = "Diagnostics"  # read by settings.py; deliberately not translated
	OPTIONS = (  # (key read by run_probe, label)
		("write_tests", "Write test: briefly likes and unlikes one tweet"),
		("post_tests", "Posting tests: posts a test tweet, replies, quotes and retweets it, then deletes them"),
		("media_tests", "Media tests: uploads images, a GIF and a big image, posts them, then deletes them"),
		("mute_tests", "Mute test: mutes one user you have no relation with, then unmutes"),
		("block_tests", "Block test: blocks one user you have no relation with, then tries to unblock (use a spare account)"),
		("dm_tests", "Direct message test: sends test messages to @iannnnn, reacts, then deletes them for you (the other side keeps them)"),
		("full", "Full run: also the checks that already passed (longer)"),
	)

	def __init__(self, parent, plugin):
		super().__init__(parent)
		self._plugin = plugin
		sizer = wx.BoxSizer(wx.VERTICAL)

		note = wx.StaticText(
			self,
			label=(
				"Checks what X really sends and which features could work. The tests below are "
				"optional and change things on your account for a moment."
			),
		)
		note.Wrap(560)
		sizer.Add(note, 0, wx.ALL, 10)

		sizer.Add(wx.StaticText(self, label="&Tests to include (all optional):"), 0, wx.LEFT, 10)
		self.options = gui.nvdaControls.CustomCheckListBox(
			self, choices=[text for _key, text in self.OPTIONS], size=(-1, 140)
		)
		self.options.SetSelection(0)
		sizer.Add(self.options, 0, wx.EXPAND | wx.ALL, 10)

		self.run_btn = wx.Button(self, label="Run &probe")
		sizer.Add(self.run_btn, 0, wx.ALL, 10)
		self.check_btn = wx.Button(self, label="Run &self-check (no network, no changes)")
		sizer.Add(self.check_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

		sizer.Add(wx.StaticText(self, label="&Report:"), 0, wx.LEFT | wx.TOP, 10)
		self.report = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 200))
		sizer.Add(self.report, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

		self.copy_btn = wx.Button(self, label="&Copy report")
		sizer.Add(self.copy_btn, 0, wx.ALL, 10)
		self.SetSizer(sizer)

		self.run_btn.Bind(wx.EVT_BUTTON, self.on_run)
		self.check_btn.Bind(wx.EVT_BUTTON, self.on_selfcheck)
		self.copy_btn.Bind(wx.EVT_BUTTON, self.on_copy)

	def focus_first(self):
		self.options.SetFocus()

	def on_run(self, event):
		if self._plugin.account_id() is None:
			ui.message("Add an account first.")
			return
		checked = set(self.options.CheckedItems)
		options = {key: i in checked for i, (key, _text) in enumerate(self.OPTIONS)}
		if not self._plugin.start_job("probe", self.on_result, options):
			ui.message("Still loading, please wait.")
			return
		self.run_btn.Disable()
		ui.message("Running the probe. This can take a minute or two.")

	def on_selfcheck(self, event):
		try:
			text = run_selfcheck(self._plugin)
		except Exception as e:
			log.error(TAG + "self-check failed", exc_info=True)
			text = "The self-check itself failed: %s" % _short_error(e)
		self.report.SetValue(text)
		self.report.SetInsertionPoint(0)
		self.report.SetFocus()
		ui.message("Self-check finished. The report is in the report box.")

	def on_result(self, result):
		try:
			self.run_btn.Enable()
			if not result["ok"]:
				ui.message(result["message"])
				return
			self.report.SetValue(result["report"])
			self.report.SetInsertionPoint(0)
			self.report.SetFocus()
		except RuntimeError:
			return  # window already closed
		ui.message("Probe finished. The report is in the report box.")

	def on_copy(self, event):
		text = self.report.GetValue()
		if not text:
			ui.message("There is no report yet.")
			return
		if _set_clipboard_text(text):
			ui.message("Report copied.")
		else:
			ui.message("Could not copy to the clipboard.")

	def apply(self):
		pass