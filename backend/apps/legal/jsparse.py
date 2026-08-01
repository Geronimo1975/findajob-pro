"""A small, dependency-free reader for the JavaScript object literals in
``web/legal.js``.

Why this exists
---------------

``web/legal.js`` is the single source of the six legal documents in three
languages, and it is an **ES module**, not JSON::

    export const LEGAL_DOCS = ['terms', 'privacy', ...];
    export const LEGAL = {
      en: {
        _note: 'English translation ...',
        terms: { title: 'Terms & Conditions (AGB)', updated: 'Aug 1, 2026', sections: [
          ['1. Scope', 'These Terms govern ...'],
        ]},
      },
    };

``json.loads`` cannot read that: the keys are bare identifiers, the strings are
single-quoted, there are trailing commas, and the payload contains ``\\n``
escapes and typographic quotes.  Three approaches were considered:

1. **Shell out to Node.** Rejected — the production image is ``python:3.12-slim``
   and there is no Node in it, so the seed command would work on a laptop and
   fail on the VPS.
2. **A pile of regexes.** Rejected — the German text contains ``„wir"``, the
   Impressum contains embedded ``\\n``, and section bodies contain both ``[``
   and ``'``.  Any regex that survives those is less readable than a parser.
3. **A recursive-descent parser for the JSON-superset subset that JS object
   literals actually use.** Chosen, and implemented here.  It is ~200 lines,
   has no dependencies, and is directly unit-tested.

What it supports
----------------

Objects with identifier / string / numeric keys, arrays, single-, double- and
backtick-quoted strings (no ``${}`` interpolation), numbers, ``true`` /
``false`` / ``null`` / ``undefined``, trailing commas, and ``//`` + ``/* */``
comments.  That is a strict superset of what ``web/legal.js`` uses and a strict
subset of JavaScript — anything cleverer raises :class:`JsLiteralError` loudly
rather than silently producing half a document.

What it does **not** do is evaluate JavaScript.  There is no ``eval``, no
expression support, and no way for the parsed file to execute code.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["JsLiteralError", "parse_js_literal", "parse_module_exports"]


class JsLiteralError(ValueError):
    """Raised when the source is not a literal this reader understands."""


# Whitespace, including the BOM (U+FEFF), NBSP (U+00A0) and the two Unicode
# line separators (U+2028 / U+2029) that JS treats as terminators. Written as
# explicit escapes on purpose: a literal NBSP here is invisible in a diff and
# silently degrades to a plain space when the file travels through a chat client.
_WHITESPACE = " \t\r\n\f\v\u00a0\ufeff\u2028\u2029"

#: The first character of a JS identifier.
_IDENT_START = re.compile(r"[A-Za-z_$]")
_IDENT_PART = re.compile(r"[A-Za-z0-9_$]")

_NUMBER_RE = re.compile(
    r"""
    -?
    (?:
        0[xX][0-9a-fA-F]+
      | 0[bB][01]+
      | 0[oO][0-7]+
      | (?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?
    )
    """,
    re.VERBOSE,
)

_SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "`": "`",
    "/": "/",
}

#: Line terminators after a backslash are a *line continuation* in JS: the
#: backslash and the newline both vanish.
_LINE_TERMINATORS = "\n\r\u2028\u2029"


class _Reader:
    """Cursor over the source, with position-aware errors."""

    def __init__(self, source: str, pos: int = 0) -> None:
        self.src = source
        self.pos = pos

    # -- diagnostics ------------------------------------------------------

    def fail(self, message: str) -> "JsLiteralError":
        line = self.src.count("\n", 0, self.pos) + 1
        line_start = self.src.rfind("\n", 0, self.pos) + 1
        column = self.pos - line_start + 1
        excerpt = self.src[self.pos : self.pos + 40].replace("\n", "\\n")
        return JsLiteralError(f"{message} at line {line} column {column}: {excerpt!r}")

    # -- primitives -------------------------------------------------------

    def at_end(self) -> bool:
        return self.pos >= len(self.src)

    def peek(self) -> str:
        return self.src[self.pos] if self.pos < len(self.src) else ""

    def skip_trivia(self) -> None:
        """Consume whitespace and comments until real content or EOF."""
        src = self.src
        length = len(src)
        while self.pos < length:
            char = src[self.pos]
            if char in _WHITESPACE:
                self.pos += 1
                continue
            if char == "/" and self.pos + 1 < length:
                nxt = src[self.pos + 1]
                if nxt == "/":
                    end = src.find("\n", self.pos)
                    self.pos = length if end == -1 else end + 1
                    continue
                if nxt == "*":
                    end = src.find("*/", self.pos + 2)
                    if end == -1:
                        raise self.fail("Unterminated block comment")
                    self.pos = end + 2
                    continue
            return

    def expect(self, char: str) -> None:
        self.skip_trivia()
        if self.peek() != char:
            raise self.fail(f"Expected {char!r}")
        self.pos += 1

    # -- values -----------------------------------------------------------

    def parse_value(self) -> Any:
        self.skip_trivia()
        char = self.peek()
        if not char:
            raise self.fail("Unexpected end of input")
        if char == "{":
            return self.parse_object()
        if char == "[":
            return self.parse_array()
        if char in "'\"`":
            return self.parse_string()
        if char == "-" or char.isdigit() or char == ".":
            return self.parse_number()
        if _IDENT_START.match(char):
            word = self.parse_identifier()
            if word == "true":
                return True
            if word == "false":
                return False
            if word == "null":
                return None
            if word in ("undefined", "NaN", "Infinity"):
                # Present for completeness; `legal.js` uses none of them.
                return None
            raise JsLiteralError(
                f"Unsupported bare word {word!r} — this reader parses literals, "
                "not expressions"
            )
        raise self.fail("Unsupported token")

    def parse_object(self) -> dict[str, Any]:
        self.expect("{")
        result: dict[str, Any] = {}
        while True:
            self.skip_trivia()
            char = self.peek()
            if char == "}":
                self.pos += 1
                return result
            if not char:
                raise self.fail("Unterminated object")
            if char in "'\"`":
                key = self.parse_string()
            elif char.isdigit() or char == "-" or char == ".":
                key = str(self.parse_number())
            elif _IDENT_START.match(char):
                key = self.parse_identifier()
            elif char == ".":  # spread
                raise JsLiteralError("Object spread is not supported")
            else:
                raise self.fail("Unsupported object key")
            self.expect(":")
            result[key] = self.parse_value()
            self.skip_trivia()
            if self.peek() == ",":
                self.pos += 1
                continue
            if self.peek() == "}":
                self.pos += 1
                return result
            raise self.fail("Expected ',' or '}' in object")

    def parse_array(self) -> list[Any]:
        self.expect("[")
        result: list[Any] = []
        while True:
            self.skip_trivia()
            char = self.peek()
            if char == "]":
                self.pos += 1
                return result
            if not char:
                raise self.fail("Unterminated array")
            result.append(self.parse_value())
            self.skip_trivia()
            if self.peek() == ",":
                self.pos += 1
                continue
            if self.peek() == "]":
                self.pos += 1
                return result
            raise self.fail("Expected ',' or ']' in array")

    def parse_identifier(self) -> str:
        start = self.pos
        if not _IDENT_START.match(self.peek()):
            raise self.fail("Expected an identifier")
        self.pos += 1
        while self.pos < len(self.src) and _IDENT_PART.match(self.src[self.pos]):
            self.pos += 1
        return self.src[start : self.pos]

    def parse_number(self) -> float | int:
        match = _NUMBER_RE.match(self.src, self.pos)
        if match is None:
            raise self.fail("Malformed number")
        raw = match.group(0)
        self.pos = match.end()
        try:
            if re.fullmatch(r"-?0[xXbBoO].*", raw):
                return int(raw, 0)
            if re.fullmatch(r"-?\d+", raw):
                return int(raw)
            return float(raw)
        except ValueError:  # pragma: no cover - _NUMBER_RE already validated
            raise self.fail("Malformed number") from None

    def parse_string(self) -> str:
        quote = self.peek()
        if quote not in "'\"`":
            raise self.fail("Expected a string")
        self.pos += 1
        out: list[str] = []
        src = self.src
        length = len(src)
        while True:
            if self.pos >= length:
                raise self.fail("Unterminated string")
            char = src[self.pos]
            if char == quote:
                self.pos += 1
                return "".join(out)
            if char == "\\":
                self.pos += 1
                out.append(self._parse_escape())
                continue
            if quote != "`" and char in "\n\r":
                raise self.fail("Unterminated string (newline inside a quoted string)")
            if quote == "`" and char == "$" and src[self.pos : self.pos + 2] == "${":
                raise JsLiteralError(
                    "Template literal interpolation (${...}) is not supported"
                )
            out.append(char)
            self.pos += 1

    def _parse_escape(self) -> str:
        if self.pos >= len(self.src):
            raise self.fail("Unterminated escape sequence")
        char = self.src[self.pos]
        self.pos += 1
        if char in _LINE_TERMINATORS:
            # Line continuation: consume a CRLF pair as one terminator.
            if char == "\r" and self.peek() == "\n":
                self.pos += 1
            return ""
        if char in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[char]
        if char == "x":
            digits = self.src[self.pos : self.pos + 2]
            if len(digits) < 2 or not re.fullmatch(r"[0-9a-fA-F]{2}", digits):
                raise self.fail("Malformed \\x escape")
            self.pos += 2
            return chr(int(digits, 16))
        if char == "u":
            if self.peek() == "{":
                end = self.src.find("}", self.pos)
                if end == -1:
                    raise self.fail("Malformed \\u{...} escape")
                digits = self.src[self.pos + 1 : end]
                if not re.fullmatch(r"[0-9a-fA-F]{1,6}", digits):
                    raise self.fail("Malformed \\u{...} escape")
                self.pos = end + 1
                return chr(int(digits, 16))
            digits = self.src[self.pos : self.pos + 4]
            if len(digits) < 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                raise self.fail("Malformed \\u escape")
            self.pos += 4
            code = int(digits, 16)
            # A UTF-16 surrogate pair spells one astral codepoint.
            if 0xD800 <= code <= 0xDBFF and self.src[self.pos : self.pos + 2] == "\\u":
                low_digits = self.src[self.pos + 2 : self.pos + 6]
                if re.fullmatch(r"[0-9a-fA-F]{4}", low_digits):
                    low = int(low_digits, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        self.pos += 6
                        return chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
            return chr(code)
        # Any other escaped character stands for itself (\e -> e), matching JS.
        return char


def parse_js_literal(source: str) -> Any:
    """Parse one standalone JavaScript literal and return the Python value."""
    reader = _Reader(source)
    value = reader.parse_value()
    reader.skip_trivia()
    if not reader.at_end() and reader.peek() != ";":
        raise reader.fail("Trailing content after the literal")
    return value


#: ``export const NAME =`` / ``export let`` / ``export var`` / plain ``const``.
_EXPORT_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*=[ \t]*"
)


def parse_module_exports(source: str, *, names: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Return every top-level ``const``/``let``/``var`` binding in ``source``.

    Bindings whose right-hand side is not a literal this reader understands are
    skipped unless they are explicitly requested via ``names``, in which case
    the underlying :class:`JsLiteralError` propagates.  That asymmetry is
    deliberate: the seed command asks for ``LEGAL`` and ``LEGAL_DOCS`` by name
    and must fail loudly if either is unreadable, while an unrelated helper
    added to ``legal.js`` later must not break seeding.
    """
    wanted = set(names) if names else None
    found: dict[str, Any] = {}
    for match in _EXPORT_RE.finditer(source):
        name = match.group(1)
        if wanted is not None and name not in wanted:
            continue
        reader = _Reader(source, match.end())
        try:
            found[name] = reader.parse_value()
        except JsLiteralError:
            if wanted is not None:
                raise
            continue
    if wanted is not None:
        missing = sorted(wanted - set(found))
        if missing:
            raise JsLiteralError(
                f"Could not find the export(s) {', '.join(missing)} in the source."
            )
    return found
