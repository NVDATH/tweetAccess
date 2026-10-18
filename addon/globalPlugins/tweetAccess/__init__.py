# -*- coding: utf-8 -*-
# NVTweet - read your X (Twitter) home timeline with NVDA.
# Prototype: package entry point.

import addonHandler

addonHandler.initTranslation()

from .utils import APP_NAME  # noqa: E402

# NVDA looks for GlobalPlugin in the package namespace.
from .core import GlobalPlugin  # noqa: E402,F401

addon_description = _(
	"{name}: read your X (Twitter) home timeline with NVDA. Prototype build."
).format(name=APP_NAME)