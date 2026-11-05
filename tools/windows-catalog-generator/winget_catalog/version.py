"""WinGet package version comparison.

The rules follow the current winget-cli Version implementation: split on '.',
take a leading integer from each part, keep the remaining text, drop trailing
0 parts, and pad the shorter version during comparison. A part with extra text
is older than the same integer without that text, so 1.0.0-beta is older than
1.0.0. This is not lexicographic sorting and it is not semantic versioning.
"""

from __future__ import annotations

from dataclasses import dataclass

_UINT64_MAX = (1 << 64) - 1


@dataclass(frozen=True)
class Version:
    text: str
    parts: tuple[tuple[int, str], ...]
    approximate: int


def compare(left: str, right: str) -> int:
    """Return -1, 0, or 1. Zero means the versions are not ordered."""
    parsed_left = parse_version(left)
    parsed_right = parse_version(right)
    if _less(parsed_left, parsed_right):
        return -1
    if _less(parsed_right, parsed_left):
        return 1
    return 0


def parse_version(value: str) -> Version:
    original = value.strip()
    approximate = 0
    base = original
    folded = base.casefold()
    if folded.startswith("< "):
        approximate = -1
        base = base[2:]
    elif folded.startswith("> "):
        approximate = 1
        base = base[2:]

    digit = next((index for index, character in enumerate(base) if character.isdigit()), None)
    split = base.find(".")
    if digit is not None and (split < 0 or digit < split):
        base = base[digit:]

    parts = [_parse_part(piece) for piece in base.split(".")] if base != "" else []
    while parts and parts[-1] == (0, ""):
        parts.pop()
    return Version(original, tuple(parts), approximate)


def _parse_part(piece: str) -> tuple[int, str]:
    text = piece.strip()
    if text == "":
        return (0, "")

    index = 0
    while index < len(text) and text[index].isdigit():
        index += 1
    if index == 0:
        return (0, text)

    try:
        number = int(text[:index], 10)
    except ValueError:
        return (0, text)
    if number > _UINT64_MAX:
        return (0, text)
    return (number, text[index:])


def _is_latest(version: Version) -> bool:
    return (
        len(version.parts) == 1
        and version.parts[0][0] == 0
        and version.parts[0][1].casefold() == "latest"
    )


def _is_unknown(version: Version) -> bool:
    return (
        len(version.parts) == 1
        and version.parts[0][0] == 0
        and version.parts[0][1].casefold() == "unknown"
    )


def _less(left: Version, right: Version) -> bool:
    left_latest = _is_latest(left)
    right_latest = _is_latest(right)
    if left_latest or right_latest:
        if left_latest and right_latest:
            return _approximate_less(left.approximate, right.approximate)
        return right_latest and not left_latest

    left_unknown = _is_unknown(left)
    right_unknown = _is_unknown(right)
    if left_unknown or right_unknown:
        if left_unknown and right_unknown:
            return _approximate_less(left.approximate, right.approximate)
        return left_unknown and not right_unknown

    length = max(len(left.parts), len(right.parts))
    for index in range(length):
        left_part = left.parts[index] if index < len(left.parts) else (0, "")
        right_part = right.parts[index] if index < len(right.parts) else (0, "")
        if _part_less(left_part, right_part):
            return True
        if _part_less(right_part, left_part):
            return False
    return _approximate_less(left.approximate, right.approximate)


def _part_less(left: tuple[int, str], right: tuple[int, str]) -> bool:
    if left[0] != right[0]:
        return left[0] < right[0]
    if left[1] == "":
        return False
    if right[1] == "":
        return True
    return left[1].casefold() < right[1].casefold()


def _approximate_less(left: int, right: int) -> bool:
    return (left < 0 and right >= 0) or (left == 0 and right > 0)
