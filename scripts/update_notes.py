"""notes.json for one published catalogue (pfBlockerNG/pfBlockerNG#3816).

``docs/<channel>/<varver>/notes.json`` sits next to ``packagesite.pkg`` and tells a
pfSense box what the newest package in that catalogue changed: the Release's bullet
list for tagged builds, the commit subjects since the previous Nightly for Nightly.
The box shows it in its update notice and falls back to a one-line notice when the
file is absent, so every helper here degrades to "no notes" and none of them may
fail a publication.

Standard library only. The bytes are deterministic (no timestamps, sorted keys) so
a republish with unchanged notes leaves git with no diff.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata

NOTES_FILENAME = "notes.json"
_SCHEMA = 1
_MAX_ITEMS = 5
_MAX_ITEM_CHARS = 100
_SOURCE_REPOSITORY = "pfBlockerNG/pfBlockerNG"
_SHA = re.compile(r"[0-9a-f]{40}")

# One level of nested parentheses is allowed in a link target: wiki-style URLs.
_URL = r"\((?:[^()]|\([^()]*\))*\)"
_IMAGE = re.compile(r"!\[[^\]]*\]" + _URL)
_LINK = re.compile(r"\[([^\]]*)\]" + _URL)
_UNPRINTABLE = re.compile(r"[^\x20-\x7e]")
_PUNCTUATION = str.maketrans(
    {
        "\u2192": "->",
        "\u2190": "<-",
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u2022": "-",
    }
)


def clean_item(text: str) -> str:
    """One plain printable-ASCII line of at most 100 characters."""
    text = _LINK.sub(r"\1", _IMAGE.sub("", text))
    text = text.replace("`", "").replace("**", "").replace("__", "")
    text = unicodedata.normalize("NFKD", text.translate(_PUNCTUATION))
    text = _UNPRINTABLE.sub(" ", text.encode("ascii", "ignore").decode("ascii"))
    text = " ".join(text.split())
    if len(text) > _MAX_ITEM_CHARS:
        text = text[: _MAX_ITEM_CHARS - 3] + "..."
    return text


def release_items(body: str) -> list[str]:
    """The cleaned text of every top-level Markdown bullet in a Release body.

    Indented (nested) bullets, headings, paragraphs and the changelog line are not
    bullets; a bullet that cleans down to nothing is dropped.
    """
    items = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith(("- ", "* ")) and (item := clean_item(line[2:])):
            items.append(item)
    return items


def commit_subjects(compare_json: dict) -> tuple[list[str], int]:
    """(cleaned commit subjects newest first, total commit count) from a GitHub
    compare response. ``ValueError`` on any shape the publisher cannot trust,
    including a response that lists fewer commits than ``total_commits`` (the listed
    ones would not be the newest)."""
    try:
        commits = compare_json["commits"]
        total = compare_json["total_commits"]
    except (KeyError, TypeError):
        raise ValueError("compare response lacks commits/total_commits") from None
    if not isinstance(commits, list) or type(total) is not int:
        raise ValueError("compare response commits/total_commits have the wrong type")
    if total != len(commits):
        raise ValueError(
            f"compare response lists {len(commits)} of {total} commits"
        )
    subjects = []
    # The compare API lists commits oldest first.
    for entry in reversed(commits):
        commit = entry.get("commit") if isinstance(entry, dict) else None
        message = commit.get("message") if isinstance(commit, dict) else None
        if not isinstance(message, str):
            raise ValueError("compare response commit lacks a string message")
        if subject := clean_item(message.split("\n", 1)[0]):
            subjects.append(subject)
    return subjects, total


def render(version: str, items: list[str], total: int, link: dict) -> bytes | None:
    """The notes.json bytes, or ``None`` when there is nothing to say."""
    if not items:
        return None
    doc = {
        "schema": _SCHEMA,
        "version": version,
        "items": items[:_MAX_ITEMS],
        "total": total,
        "link": link,
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (text + "\n").encode("ascii")


def describes(raw: bytes, version: str | None) -> bool:
    """True when ``raw`` is a JSON object whose ``version`` is exactly ``version``."""
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    if not isinstance(doc, dict):
        return False
    return version is not None and doc.get("version") == version


def fetch_compare(prev_sha: str, new_sha: str) -> dict | None:
    """The GitHub compare response for two source commits, or ``None``.

    Never raises: anything but a clean JSON object costs one stderr warning line.
    Both SHAs are checked before they reach the command line.
    """
    if not (_SHA.fullmatch(prev_sha) and _SHA.fullmatch(new_sha)):
        return None
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{_SOURCE_REPOSITORY}/compare/{prev_sha}...{new_sha}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            reason = f"gh exited {proc.returncode}: {proc.stderr.strip()[:200]}"
        elif isinstance(data := json.loads(proc.stdout), dict):
            return data
        else:
            reason = "response is not a JSON object"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
    print(
        f"::warning::update_notes: no notes for {prev_sha}...{new_sha} "
        f"({' '.join(reason.split())})",
        file=sys.stderr,
    )
    return None
