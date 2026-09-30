"""Shared definitions of sensitive files and secret-looking values.

Used by the read and search tools (content never returned), investigate_repository
(evidence exclusion and redaction), the patch system (protected write targets)
and the test runner (output redaction). This module holds the shared patterns so
they cannot drift apart. It has no dependencies on the rest of DevPilot.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase

# File names that are likely to hold secrets, keys or credentials (fnmatch patterns, lower case).
SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials.json", "secrets.*", "*.secrets.*", ".npmrc", ".pypirc", ".netrc",
)  # fmt: skip

# Templates that document which variables exist but hold no real values; they stay readable.
# (Patching keeps its own, stricter exception list.)
SECRET_FILE_TEMPLATES = (".env.example", ".env.sample", ".env.template")


def is_secret_file_name(name: str) -> bool:
    """True for a file name that likely holds secrets (case-insensitive; templates excluded)."""
    name = name.lower()
    return name not in SECRET_FILE_TEMPLATES and any(fnmatchcase(name, p) for p in SECRET_FILE_PATTERNS)


def is_sensitive_path(rel: str) -> bool:
    """True for anything inside a .git directory and for likely secret files, at any depth."""
    parts = [p for p in rel.replace("\\", "/").lower().split("/") if p not in ("", ".")]
    return ".git" in parts or (bool(parts) and is_secret_file_name(parts[-1]))

# Values that look like secrets wherever they appear in text. Redaction is best-effort:
# only these known shapes (plus values the caller supplies) are recognised.
SECRET_VALUE_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

REDACTED = "[REDACTED]"
