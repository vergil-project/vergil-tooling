"""Render, parse, and tick the release progress checklist in an issue body.

The checklist lives in an HTML-comment-delimited block so writes never disturb
the human-written parts of the tracking-issue body. The block is the resume
cursor for vrg-release --resume (issue #1612). Stage names are supplied by the
caller; this module has no knowledge of the pipeline.
"""

from __future__ import annotations

import re

BEGIN = "<!-- vrg-release:progress -->"
END = "<!-- /vrg-release:progress -->"


class ChecklistError(Exception):
    """The checklist block is missing, malformed, or version-skewed."""
