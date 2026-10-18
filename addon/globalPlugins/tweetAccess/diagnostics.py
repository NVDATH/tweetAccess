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

from .service import _legacy, _ok, _short_error, _shown, _val, tweet_to_post
from .utils import TAG, user_id_from_twid

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
	"Direct message history (counts only)",
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
	add("Write tests: %s; posting tests: %s; media tests: %s; mute test: %s; block test: %s; full run: %s" % tuple(
		"yes" if v else "no" for v in (write_tests, post_tests, media_tests, mute_tests, block_tests, full)
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
				headers = dict(client._base_headers)
				headers["content-type"] = "application/x-www-form-urlencoded"
				form = {"user_id": uid}
				attempts = [
					("unblock_user, what the add-on uses", lambda: client.unblock_user(uid)),
					("x.com, form", lambda: client.post("https://x.com/i/api/1.1/blocks/destroy.json", data=form, headers=headers)),
					("api.x.com, form", lambda: client.post("https://api.x.com/1.1/blocks/destroy.json", data=form, headers=headers)),
					("x.com, query", lambda: client.post(
						"https://x.com/i/api/1.1/blocks/destroy.json?user_id=%s&skip_status=1" % uid, headers=headers,
					)),
					("twitter.com, form", lambda: client.post("https://twitter.com/i/api/1.1/blocks/destroy.json", data=form, headers=headers)),
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

	from . import autoread, compose, dialogs, feedlist, keymap, mainwindow, searchdialog, tabstate, useractions
	from .config import Config
	from .core import GlobalPlugin
	from .service import FetchService, notification_to_post
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
		special = fixed_in_tab | {"check_updates", "load_older", "action_menu", "clear_cache", "select_all", "jump_row"}
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
		order = searchdialog.ordered_filters("videos,bogus,images,images")
		keys = [k for k, _text in order]
		assert keys[:2] == ["videos", "images"], keys[:3]
		assert len(keys) == len(searchdialog.SEARCH_FILTERS) and len(set(keys)) == len(keys), "a filter is missing or twice"

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
				if name == "bookmarks":
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

		self.write_check = wx.CheckBox(self, label="Include a &write test (briefly likes and unlikes one tweet)")
		sizer.Add(self.write_check, 0, wx.LEFT | wx.RIGHT, 10)
		self.post_check = wx.CheckBox(
			self, label="Include &posting tests (posts a test tweet, replies, quotes and retweets it, then deletes them)"
		)
		sizer.Add(self.post_check, 0, wx.ALL, 10)
		self.media_check = wx.CheckBox(
			self, label="Include a &media test (uploads a small image with alt text, posts it, then deletes it)"
		)
		sizer.Add(self.media_check, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		self.mute_check = wx.CheckBox(
			self, label="Include a mu&te test (mutes one user you have no relation with, then unmutes)"
		)
		sizer.Add(self.mute_check, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		self.block_check = wx.CheckBox(
			self, label="Include a &block test (blocks one user you have no relation with, then tries to unblock; use a spare account)"
		)
		sizer.Add(self.block_check, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		self.full_check = wx.CheckBox(self, label="Run the &checks that already passed too (a full run, longer)")
		sizer.Add(self.full_check, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

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
		self.write_check.SetFocus()

	def on_run(self, event):
		if self._plugin.account_id() is None:
			ui.message("Add an account first.")
			return
		options = {
			"write_tests": self.write_check.GetValue(), "post_tests": self.post_check.GetValue(),
			"media_tests": self.media_check.GetValue(), "mute_tests": self.mute_check.GetValue(),
			"block_tests": self.block_check.GetValue(), "full": self.full_check.GetValue(),
		}
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