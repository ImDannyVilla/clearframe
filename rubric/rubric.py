#!/usr/bin/env python3
"""Read rubric/rubric.yaml. Standard library only, for the worker, the agent and the evidence page.

The GB10's offline paths have no PyYAML, so this reads a small part of YAML and
refuses anything else, rather than guessing:

- two-space indentation, no tabs
- `key: value` mappings, with plain keys
- `- value` lists of single values
- values: double-quoted strings, integers, floats, true, false, null and
  bare lowercase words such as `exposure_flicker`
- full-line comments, and comments after a value

`load()` also checks the rubric against the contract's names and returns a dict.
Run this file to check rubric.yaml by hand.
"""
from __future__ import annotations

import json
import re
import string
import sys
from pathlib import Path

RUBRIC = Path(__file__).resolve().parent / "rubric.yaml"

SCHEMA = "clearframe.rubric/1"

# From the contract's issue-types table.
ISSUE_TYPES = ("exposure_flicker", "road_markings", "extra_finger")
STATUSES = (
    "review_pending",
    "needs_confirmation",
    "flagged",
    "clean",
    "accepted_as_is",
    "approved_for_regeneration",
)
# Issue states counted on the card. Dismissed notes aren't counted.
COUNTED_STATES = ("measured", "confirmed", "candidate")
# The contract's order for fix lines.
FIX_LABEL_KEYS = ("dont_include", "add", "make_sure")
# Each card template and the placeholders it may use: the contract's card fields
# only. An empty set is plain text.
CARD_TEMPLATES = {
    ("count",): {"count", "state", "category"},
    ("count_separator",): set(),
    ("check_error",): {"category"},
    ("shot_card", "header"): {"shot", "reviewed", "expected_takes"},
    ("shot_card", "take"): {"take_id", "status", "counts"},
    ("shot_card", "take_no_counts"): {"take_id", "status"},
    ("shot_card", "clean"): {"clean_takes"},
    ("shot_card", "clean_none"): set(),
    ("shot_card", "clean_separator"): set(),
    ("shot_card", "link"): {"link"},
    ("retake_card", "header"): {"take_id", "parent_take_id", "status", "counts"},
    ("retake_card", "header_no_counts"): {"take_id", "parent_take_id", "status"},
    ("retake_card", "link"): {"link"},
}

KEY = re.compile(r"[a-z_][a-z0-9_]*")
WORD = re.compile(r"[a-z_][a-z0-9_.-]*")
INT = re.compile(r"-?[0-9]+")
FLOAT = re.compile(r"-?[0-9]+\.[0-9]+")
# Bare words that YAML 1.1 reads as booleans or null. Quote them instead.
AMBIGUOUS = {"yes", "no", "on", "off", "y", "n", "true", "false", "null", "~"}


class RubricError(ValueError):
    """rubric.yaml is outside the supported YAML, or breaks the contract."""


def _lines(text: str) -> list[tuple[int, int, str]]:
    """(line number, indent, content) for every line that isn't blank or a comment."""
    out = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise RubricError(f"line {number}: tabs are not allowed; indent with spaces")
        content = raw.strip()
        if not content or content.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise RubricError(f"line {number}: indent by two spaces")
        out.append((number, indent, content))
    return out


def _value(text: str, number: int):
    text = text.strip()
    if text.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as error:
            raise RubricError(f"line {number}: bad quoted string: {error.msg}") from None
        rest = text[end:]
        if rest.strip() and not re.match(r"\s+#", rest):
            raise RubricError(f"line {number}: unexpected text after the string")
        return value
    if text.startswith("'"):
        raise RubricError(f"line {number}: use double quotes")
    text = re.split(r"\s+#", text, maxsplit=1)[0]
    if text == "true":
        return True
    if text == "false":
        return False
    if text == "null":
        return None
    if FLOAT.fullmatch(text):
        return float(text)
    if INT.fullmatch(text):
        return int(text)
    if WORD.fullmatch(text) and text not in AMBIGUOUS:
        return text
    raise RubricError(f"line {number}: put this value in double quotes: {text}")


def _block(lines: list[tuple[int, int, str]], i: int, indent: int):
    """Parse the block starting at lines[i]; return (value, index of the next line)."""
    if lines[i][2] == "-" or lines[i][2].startswith("- "):
        return _list(lines, i, indent)
    return _mapping(lines, i, indent)


def _list(lines, i, indent):
    items = []
    while i < len(lines) and lines[i][1] == indent:
        number, _, content = lines[i]
        if not content.startswith("- "):
            raise RubricError(f"line {number}: expected a list item")
        items.append(_value(content[2:], number))
        i += 1
    return items, i


def _mapping(lines, i, indent):
    mapping: dict = {}
    while i < len(lines) and lines[i][1] == indent:
        number, _, content = lines[i]
        key, colon, rest = content.partition(":")
        if not colon or not KEY.fullmatch(key) or (rest and not rest[0].isspace()):
            raise RubricError(f"line {number}: expected `key: value` with a lowercase key")
        if key in mapping:
            raise RubricError(f"line {number}: duplicate key: {key}")
        i += 1
        if rest.strip() and not rest.strip().startswith("#"):
            mapping[key] = _value(rest, number)
        elif i < len(lines) and lines[i][1] == indent + 2:
            mapping[key], i = _block(lines, i, indent + 2)
        else:
            raise RubricError(f"line {number}: {key} has no value")
    if i < len(lines) and lines[i][1] > indent:
        raise RubricError(f"line {lines[i][0]}: unexpected indent")
    return mapping, i


def parse(text: str) -> dict:
    """Parse rubric YAML into plain dicts and lists, without checking its meaning."""
    lines = _lines(text)
    if not lines:
        raise RubricError("the rubric is empty")
    if lines[0][1] != 0:
        raise RubricError(f"line {lines[0][0]}: unexpected indent")
    value, i = _block(lines, 0, 0)
    if i < len(lines):
        raise RubricError(f"line {lines[i][0]}: unexpected indent")
    if not isinstance(value, dict):
        raise RubricError("the rubric must be a mapping")
    return value


def _require(condition: object, message: str) -> None:
    if not condition:
        raise RubricError(message)


def _as_list(value: object, message: str) -> list:
    """value, if it is a list with at least one item."""
    if not isinstance(value, list) or not value:
        raise RubricError(message)
    return value


def _as_dict(value: object, message: str) -> dict:
    """value, if it is a mapping with at least one key."""
    if not isinstance(value, dict) or not value:
        raise RubricError(message)
    return value


def _is_text(value) -> bool:
    return isinstance(value, str) and value.strip() == value and value != ""


def validate(rubric: dict) -> dict:
    """Check the rubric against the contract; return it unchanged."""
    _require(rubric.get("schema") == SCHEMA, f"schema must be {SCHEMA}")

    checks = _as_list(rubric.get("checks"), "checks must list at least one issue type")
    for check in checks:
        _require(check in ISSUE_TYPES, f"checks: unknown issue type: {check}")
    _require(len(set(checks)) == len(checks), "checks: an issue type is listed twice")

    threshold = rubric.get("flicker_threshold")
    _require(
        isinstance(threshold, float) and 0 < threshold < 255,
        "flicker_threshold must be a number like 8.0, between 0 and 255",
    )
    return_frames = rubric.get("flicker_return_frames")
    _require(
        type(return_frames) is int and return_frames >= 1,
        "flicker_return_frames must be a whole number of frames, 1 or more",
    )

    approvers = _as_list(rubric.get("approvers"), "approvers must list at least one name")
    for name in approvers:
        _require(_is_text(name), f"approvers: not a name: {name!r}")
    _require(len(set(approvers)) == len(approvers), "approvers: a name is listed twice")

    message = f"fix_labels must have exactly {', '.join(FIX_LABEL_KEYS)}, in that order"
    labels = _as_dict(rubric.get("fix_labels"), message)
    _require(tuple(labels) == FIX_LABEL_KEYS, message)
    for key, label in labels.items():
        _require(_is_text(label), f"fix_labels.{key}: not a label")

    message = f"fix_library needs one entry per issue type: {', '.join(ISSUE_TYPES)}"
    library = _as_dict(rubric.get("fix_library"), message)
    _require(set(library) == set(ISSUE_TYPES), message)
    for issue_type, entry in library.items():
        entry = _as_dict(entry, f"fix_library.{issue_type}: no lines")
        keys = tuple(entry)
        _require(
            set(keys) <= set(FIX_LABEL_KEYS)
            and keys == tuple(k for k in FIX_LABEL_KEYS if k in entry),
            f"fix_library.{issue_type}: lines must be {', '.join(FIX_LABEL_KEYS)}, in that order",
        )
        for key, text in entry.items():
            _require(
                _is_text(text) and "\n" not in text,
                f"fix_library.{issue_type}.{key}: must be one line of text",
            )
    order = [t for t in library if t in checks]
    _require(order == checks, "checks must follow fix_library order")

    card = _as_dict(rubric.get("card"), "card is missing")
    message = f"card.status_labels must have every status, in contract order: {', '.join(STATUSES)}"
    status_labels = _as_dict(card.get("status_labels"), message)
    _require(tuple(status_labels) == STATUSES, message)
    message = f"card.state_labels must have exactly {', '.join(COUNTED_STATES)}"
    state_labels = _as_dict(card.get("state_labels"), message)
    _require(tuple(state_labels) == COUNTED_STATES, message)
    for state, forms in state_labels.items():
        message = f"card.state_labels.{state} needs `one` and `many`"
        _require(tuple(_as_dict(forms, message)) == ("one", "many"), message)
    message = "card.category_labels needs one label per issue type, in fix_library order"
    category_labels = _as_dict(card.get("category_labels"), message)
    _require(tuple(category_labels) == tuple(library), message)
    for path, allowed in CARD_TEMPLATES.items():
        name = "card." + ".".join(path)
        template: object = card
        for part in path:
            template = template.get(part) if isinstance(template, dict) else None
        if not isinstance(template, str) or template == "":
            raise RubricError(f"{name} is missing")
        try:
            parts = string.Formatter().parse(template)
            fields = {field for _, field, _, _ in parts if field is not None}
        except ValueError as error:
            raise RubricError(f"{name}: bad template: {error}") from None
        _require(
            fields <= allowed,
            f"{name} may use only {sorted(allowed) or 'no placeholders'}, "
            f"not {sorted(fields - allowed)}",
        )
    for labels in (status_labels, category_labels):
        for key, label in labels.items():
            _require(_is_text(label), f"card: no label for {key}")
    for forms in state_labels.values():
        for form in forms.values():
            _require(_is_text(form), "card.state_labels: a label is empty")
    return rubric


def load(path: Path | str = RUBRIC) -> dict:
    """Read, parse and check rubric.yaml."""
    return validate(parse(Path(path).read_text(encoding="utf-8")))


def fix_lines(rubric: dict, issue_type: str) -> list[str]:
    """The fix lines for one issue type, as prompt-fix appends them: `Make sure: ...`."""
    labels = rubric["fix_labels"]
    return [f"{labels[key]}: {text}" for key, text in rubric["fix_library"][issue_type].items()]


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else RUBRIC
    try:
        rubric = load(path)
    except (OSError, RubricError) as error:
        print(f"rubric: {path}: {error}", file=sys.stderr)
        return 1
    print(f"rubric: {path.name} OK, checks: {', '.join(rubric['checks'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
