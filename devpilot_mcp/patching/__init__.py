"""Controlled modification of workspace files from an explicit, caller-supplied unified diff.

`unified_diff` parses a patch and applies hunks to text in memory (no I/O);
`changes` validates targets and limits, commits a whole patch atomically,
and records what is needed to revert it.
"""
