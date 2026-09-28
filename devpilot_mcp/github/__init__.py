"""Read-only access to GitHub for the repository behind the workspace's `origin` remote.

`remote` discovers and validates the owner/repository from local Git
configuration; `client` is the only code that talks to the GitHub API.
"""
