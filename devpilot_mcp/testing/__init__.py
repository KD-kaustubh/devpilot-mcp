"""Test discovery, controlled test execution and repository validation.

`detection` identifies supported Python test frameworks from repository files
without executing anything; `runner` executes one of a fixed set of test
commands with a bounded, shell-free subprocess; `syntax` checks Python syntax
by parsing (never executing) source files.
"""
