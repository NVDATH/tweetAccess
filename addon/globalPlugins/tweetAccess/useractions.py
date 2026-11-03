# -*- coding: utf-8 -*-
# tweetAccess - useractions.py: the user menu, after NVSky's UserActionMixin. One builder serves every
# place that offers it (Alt+U, the User... submenu of the tweet action menu, later the lists of users),
# and every entry is also a command of its own in keymap.py, so each one can have a shortcut.

import addonHandler
import wx

from . import keymap

addonHandler.initTranslation()

# The entries of the user menu that have a shortcut of their own on the Shortcuts page (see populate_user_menu).
USER_ACTIONS = (
	"user_timeline", "user_info", "user_pinned", "user_followers", "user_following", "user_known_followers",
	"user_follow", "user_mute", "user_block", "user_message", "user_copy_url", "user_open_web",
)
# Enter key action (Settings > General) -> the user action it runs, in a list of users too.
ENTER_USER_ACTIONS = {
	"user": "user_timeline", "user_info": "user_info", "user_pinned": "user_pinned",
	"user_followers": "user_followers", "user_following": "user_following",
	"user_known_followers": "user_known_followers",
}


def populate_user_menu(menu, window, run, state=None):
	"""Fill menu with the user actions. run(action id) does the one picked, after the menu has closed.
	window: the window that receives the menu events. state: how the account stands to the user (following,
	muting, blocking: True, False or None when not known). Each relation has one entry that says what it will
	do; one that is not known has no entry, so no entry can say the wrong thing."""

	def add(target, label, action_id):
		item = target.Append(wx.ID_ANY, keymap.with_key(label, action_id))
		window.Bind(wx.EVT_MENU, lambda e, a=action_id: wx.CallAfter(run, a), item)

	view = wx.Menu()
	# Translators: Entry of the View submenu of the user menu. The shortcut is added after the label when there is one.
	add(view, _("&Timeline"), "user_timeline")
	# Translators: Entry of the View submenu of the user menu.
	add(view, _("&Information..."), "user_info")
	# Translators: Entry of the View submenu of the user menu.
	add(view, _("&Pinned tweet"), "user_pinned")
	# Translators: Entry of the View submenu of the user menu: the users who follow this user.
	add(view, _("&Followers..."), "user_followers")
	# Translators: Entry of the View submenu of the user menu: the users this user follows.
	add(view, _("Follo&wing..."), "user_following")
	# Translators: Entry of the View submenu of the user menu: the followers of this user that you follow too.
	add(view, _("Followers you &know..."), "user_known_followers")
	# Translators: Submenu of the user menu.
	menu.AppendSubMenu(view, _("&View..."))
	menu.AppendSeparator()
	relations = (
		# Translators: Entries of the user menu: the first label when the relation is on now, the second when it is off, so each one says what it will do. The shortcut is added after the label.
		("following", "user_follow", _("Un&follow"), _("&Follow")),
		("muting", "user_mute", _("Un&mute"), _("&Mute")),
		("blocking", "user_block", _("Un&block"), _("&Block")),
	)
	known = [r for r in relations if (state or {}).get(r[0]) is not None]
	for flag, action_id, undo_label, label in known:
		add(menu, undo_label if state[flag] else label, action_id)
	if known:
		menu.AppendSeparator()
	# Translators: Entry of the user menu that starts a chat. It has no access key on purpose: a key pressed by mistake would open the New chat window.
	add(menu, _("Start chat..."), "user_message")
	copy_menu = wx.Menu()
	# Translators: Entry of the Copy submenu of the user menu.
	add(copy_menu, _("Copy &profile URL"), "user_copy_url")
	# Translators: Entry of the Copy submenu of the user menu.
	add(copy_menu, _("&Open on x.com"), "user_open_web")
	# Translators: Submenu of the user menu.
	menu.AppendSubMenu(copy_menu, _("&Copy..."))


def relation_texts():
	"""action -> (the question asked before it, what is said when it is done). {user} is the user name."""
	return {
		# Translators: Question before following a user, then what is said after. {user} is the user name.
		"follow": (_("Follow @{user}?"), _("Followed @{user}.")),
		# Translators: Question before unfollowing a user, then what is said after. {user} is the user name.
		"unfollow": (_("Unfollow @{user}?"), _("Unfollowed @{user}.")),
		# Translators: Question before muting a user, then what is said after. {user} is the user name.
		"mute": (_("Mute @{user}?"), _("Muted @{user}.")),
		# Translators: Question before unmuting a user, then what is said after. {user} is the user name.
		"unmute": (_("Unmute @{user}?"), _("Unmuted @{user}.")),
		# Translators: Question before blocking a user, then what is said after. {user} is the user name.
		"block": (_("Block @{user}?"), _("Blocked @{user}.")),
		# Translators: Question before unblocking a user, then what is said after. {user} is the user name.
		"unblock": (_("Unblock @{user}?"), _("Unblocked @{user}.")),
	}