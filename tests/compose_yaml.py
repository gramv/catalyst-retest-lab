"""A strict reader for the plain block-YAML subset ``compose.yaml`` is written in (package
oss-packaging). The project has no YAML dependency; this reader accepts exactly: full-line
comments, ``key: value`` mappings, ``- item`` sequences (of scalars or mappings), JSON-quoted
strings, JSON flow lists, ``true``/``false``, integers and empty values. Anything else -- an
anchor, an alias, a merge key, a block scalar, a tab, an unquoted string -- is refused, so a
change to ``compose.yaml`` outside the subset fails the tests instead of being misread.
"""

import json
import re

KEY = re.compile(r"[A-Za-z0-9_.-]+")


class ComposeYamlError(ValueError):
    pass


def _scalar(text, line_no):
    text = text.strip()
    if text == "":
        return None
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?[0-9]+", text):
        return int(text)
    if text[0] in "\"[":
        try:
            return json.loads(text)
        except ValueError:
            raise ComposeYamlError(f"line {line_no}: invalid quoted value") from None
    raise ComposeYamlError(f"line {line_no}: unquoted or unsupported value {text[:20]!r}")


def _lines(text):
    out = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise ComposeYamlError(f"line {number}: tab")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(token in stripped for token in (" &", " *", "<<:", ": |", ": >")) \
                or stripped.startswith(("&", "*")):
            raise ComposeYamlError(f"line {number}: anchors, aliases and block scalars refused")
        out.append([len(raw) - len(raw.lstrip(" ")), stripped, number])
    return out


def _split_key(content, number):
    if content.endswith(":"):
        key, rest = content[:-1], ""
    elif ": " in content:
        key, rest = content.split(": ", 1)
    else:
        raise ComposeYamlError(f"line {number}: expected key: value")
    if not KEY.fullmatch(key):
        raise ComposeYamlError(f"line {number}: unsupported key {key!r}")
    return key, rest


def _block(lines, i, indent):
    if lines[i][1].startswith("- "):
        return _sequence(lines, i, indent)
    return _mapping(lines, i, indent)


def _mapping(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        _, content, number = lines[i]
        key, rest = _split_key(content, number)
        if key in out:
            raise ComposeYamlError(f"line {number}: duplicate key {key!r}")
        i += 1
        if rest == "" and i < len(lines) and lines[i][0] > indent:
            out[key], i = _block(lines, i, lines[i][0])
        else:
            out[key] = _scalar(rest, number)
    if i < len(lines) and lines[i][0] > indent:
        raise ComposeYamlError(f"line {lines[i][2]}: unexpected indentation")
    return out, i


def _sequence(lines, i, indent):
    out = []
    while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
        _, content, number = lines[i]
        item = content[2:]
        if re.match(r"[A-Za-z0-9_.-]+:( |$)", item):
            lines[i] = [indent + 2, item, number]
            value, i = _mapping(lines, i, indent + 2)
            out.append(value)
        else:
            out.append(_scalar(item, number))
            i += 1
    return out, i


def loads(text):
    lines = _lines(text)
    if not lines:
        return {}
    value, i = _block(lines, 0, lines[0][0])
    if i != len(lines):
        raise ComposeYamlError(f"line {lines[i][2]}: trailing content")
    return value
