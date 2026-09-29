"""Parsing of declared Python dependency names from manifest data.

Pure functions over already-read text or parsed TOML, shared by
analyze_repository (framework indicators) and test detection (pytest
evidence). Nothing is executed; setup.py is never read. No dependencies on
the rest of DevPilot.
"""

from __future__ import annotations

import re
from typing import Any


def normalize_python_name(name: str) -> str:
    """PEP 503 normalisation: 'Flask_SQLAlchemy' -> 'flask-sqlalchemy'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def python_requirement_name(requirement: str) -> str | None:
    """'Flask[async]>=2.0 ; python_version>"3"' -> 'flask'."""
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return normalize_python_name(match.group(1)) if match else None


def requirements_txt_names(text: str) -> set[str]:
    """Normalised package names declared in a requirements*.txt file."""
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):  # skip options such as -r, -e, --index-url
            if name := python_requirement_name(line):
                names.add(name)
    return names


def pyproject_dependency_names(data: dict[str, Any]) -> set[str]:
    """Normalised package names declared in parsed pyproject.toml data (PEP 621, PEP 735, Poetry)."""
    project = data.get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    for group in data.get("dependency-groups", {}).values():
        requirements.extend(item for item in group if isinstance(item, str))
    names = {n for r in requirements if isinstance(r, str) and (n := python_requirement_name(r))}

    poetry = data.get("tool", {}).get("poetry", {})
    tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    tables += [group.get("dependencies", {}) for group in poetry.get("group", {}).values()]
    names |= {normalize_python_name(name) for table in tables for name in table}
    return names
