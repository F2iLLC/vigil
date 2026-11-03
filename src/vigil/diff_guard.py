"""Bound PR diff material before it reaches any model call (issue #127)."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_DIFF_BYTES = 200 * 1024
DEFAULT_MAX_FILE_BYTES = 50 * 1024
DEFAULT_IGNORES = (
    "**/dist/**",
    "**/build/**",
    "**/*.min.*",
    "f2i.bio/index.html",
    "f2i.bio/**/index.html",
    "site/index.html",
    "site/**/index.html",
    "public/index.html",
    "public/**/index.html",
    "evidence/*.json",
    "evidence/**/*.json",
    "*.lock",
    "**/*.lock",
    "pnpm-lock.yaml",
    "**/pnpm-lock.yaml",
    "**/package-lock.json",
    "**/yarn.lock",
    "**/poetry.lock",
    "**/Cargo.lock",
    "**/Pipfile.lock",
)
DIFF_BOUNDARY = re.compile(r"(?=^diff --git )", re.MULTILINE)


@dataclass(frozen=True)
class PreparedDiff:
    diff: str
    original_bytes: int
    excluded_bytes: int
    filtered_bytes: int
    prepared_bytes: int
    estimated_tokens: int
    excluded_files: tuple[str, ...]
    stubbed_files: tuple[tuple[str, int], ...]
    max_diff_bytes: int
    max_file_bytes: int

    @property
    def over_limit(self) -> bool:
        return self.filtered_bytes > self.max_diff_bytes


def _patterns() -> tuple[str, ...]:
    patterns = list(DEFAULT_IGNORES)
    ignore_file = Path(".vigil/ignore")
    try:
        patterns.extend(
            line.strip() for line in ignore_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except FileNotFoundError:
        pass

    extra = os.environ.get("VIGIL_DIFF_IGNORE", "")
    patterns.extend(line.strip() for line in extra.splitlines() if line.strip())
    return tuple(patterns)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(
        fnmatch.fnmatchcase(normalized, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatchcase(normalized, pattern[3:]))
        for pattern in patterns
    )


def _diff_parts(raw_diff: str) -> list[tuple[str, str]]:
    parts = [part for part in DIFF_BOUNDARY.split(raw_diff) if part.startswith("diff --git ")]
    parsed: list[tuple[str, str]] = []
    for part in parts:
        first_line = part.splitlines()[0]
        match = re.match(r"diff --git a/(.+?) b/(.+)", first_line)
        if not match:
            # Preserve an unparseable section. The size cap still protects us.
            parsed.append(("", part))
            continue
        path = match.group(2).strip('"')
        parsed.append((path, part))
    return parsed


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def prepare_diff(raw_diff: str) -> PreparedDiff:
    """Exclude configured generated paths, stub large files, then measure."""
    max_diff_bytes = _positive_int("VIGIL_MAX_DIFF_BYTES", DEFAULT_MAX_DIFF_BYTES)
    max_file_bytes = _positive_int("VIGIL_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)
    patterns = _patterns()
    original_bytes = len(raw_diff.encode("utf-8"))
    excluded_bytes = 0
    excluded_files: list[str] = []
    stubbed_files: list[tuple[str, int]] = []
    filtered_parts: list[tuple[str, str]] = []

    for path, part in _diff_parts(raw_diff):
        part_bytes = len(part.encode("utf-8"))
        if path and _matches(path, patterns):
            excluded_bytes += part_bytes
            excluded_files.append(path)
            continue
        filtered_parts.append((path, part))

    if len(filtered_parts) == len(_diff_parts(raw_diff)):
        # Keep GitHub's original separators and final newline byte-for-byte.
        filtered_diff = raw_diff
    else:
        filtered_diff = "\n\n".join(part.rstrip("\n") for _, part in filtered_parts)
        if raw_diff.endswith("\n"):
            filtered_diff += "\n"
    filtered_bytes = len(filtered_diff.encode("utf-8"))
    output: list[str] = []
    for path, part in filtered_parts:
        part_bytes = len(part.encode("utf-8"))
        if path and part_bytes > max_file_bytes:
            stubbed_files.append((path, part_bytes))
            header = part.split("\n@@", 1)[0]
            part = (
                f"{header}\n@@ -0,0 +0,1 @@\n"
                f"+Vigil omitted this file diff ({part_bytes} bytes; per-file limit {max_file_bytes} bytes)."
            )
        output.append(part)

    prepared = filtered_diff if not stubbed_files else "\n\n".join(
        part.rstrip("\n") for part in output
    ) + ("\n" if filtered_diff.endswith("\n") else "")
    prepared_bytes = len(prepared.encode("utf-8"))
    return PreparedDiff(
        diff=prepared,
        original_bytes=original_bytes,
        excluded_bytes=excluded_bytes,
        filtered_bytes=filtered_bytes,
        prepared_bytes=prepared_bytes,
        estimated_tokens=(prepared_bytes + 3) // 4,
        excluded_files=tuple(excluded_files),
        stubbed_files=tuple(stubbed_files),
        max_diff_bytes=max_diff_bytes,
        max_file_bytes=max_file_bytes,
    )
