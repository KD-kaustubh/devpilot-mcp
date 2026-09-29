"""Shared definitions of sensitive files and secret-looking values.

Used by investigate_repository (evidence exclusion and redaction), the patch
system (protected write targets) and the test runner (output redaction). Each
of those applies its own exceptions on top; this module only holds the shared
patterns so they cannot drift apart. It has no dependencies on the rest of
DevPilot.
"""

from __future__ import annotations

import re

# File names that are likely to hold secrets, keys or credentials (fnmatch patterns, lower case).
SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials.json", "secrets.*", "*.secrets.*", ".npmrc", ".pypirc", ".netrc",
)  # fmt: skip

# Values that look like secrets wherever they appear in text. Redaction is best-effort:
# only these known shapes (plus values the caller supplies) are recognised.
SECRET_VALUE_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

REDACTED = "[REDACTED]"
