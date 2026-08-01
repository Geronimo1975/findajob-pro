"""The canonical content hash for a legal document.

Implementation spec §10 makes the hash — not the text, and not a client-sent
version string — the defensible record of *what the user actually agreed to*::

    resolve LegalDocument.objects.get(doc_id="terms", lang="de", is_current=True)
    on the server — the DE version is the legally binding one and its hash is
    the only defensible record.

Everything about the function below therefore optimises for one property:
**the same document text must always produce the same digest**, on any Python
version, on any machine, no matter how ``web/legal.js`` is re-indented.

What goes into the digest
-------------------------

``doc_id``, ``lang``, ``version``, ``title`` and the ordered ``sections``.

``note`` (the per-language ``_note`` in ``legal.js``, e.g. *"English translation
for convenience"*) is deliberately **excluded**: it is identical for all six
documents of a language and says nothing about the contractual text. Including
it would mean that fixing a typo in that one sentence silently invalidates the
hash of every consent record ever taken in that language.

Normalisation applied to every string, in this order:

1. Unicode NFC — so ``ä`` typed as U+00E4 and as ``a`` + U+0308 hash the same.
   The German documents contain a lot of them, and editors disagree about which
   form they save.
2. CRLF / CR line endings collapse to LF — a Windows checkout must not change
   the digest.
3. Trailing whitespace is stripped from each line, and from the string as a
   whole. Leading whitespace is kept: it is never accidental in this corpus.

The serialisation is JSON with sorted keys, no insignificant whitespace and
``ensure_ascii=False``, encoded UTF-8. ``sections`` is hashed as a list of
``[heading, body]`` pairs regardless of how the model stores it, so changing
the storage shape later does not invalidate historical consent records.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Iterable, Sequence

__all__ = [
    "canonical_sections",
    "canonical_payload",
    "compute_content_hash",
    "normalise_text",
]


def normalise_text(value: Any) -> str:
    """NFC-normalise, unify line endings and strip trailing whitespace."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def canonical_sections(sections: Iterable[Any]) -> list[list[str]]:
    """Coerce any supported section shape into ordered ``[heading, body]`` pairs.

    Accepts what ``legal.js`` uses (two-element arrays) and what the model
    stores (``{"heading": …, "body": …}``), so the digest is identical either
    way.
    """
    pairs: list[list[str]] = []
    for section in sections or []:
        if isinstance(section, dict):
            heading = section.get("heading", section.get("title", ""))
            body = section.get("body", section.get("text", ""))
        elif isinstance(section, (list, tuple)):
            if len(section) < 2:
                raise ValueError(
                    "A legal section needs both a heading and a body; got "
                    f"{section!r}"
                )
            heading, body = section[0], section[1]
        else:
            raise TypeError(
                "A legal section must be a [heading, body] pair or a mapping; "
                f"got {type(section).__name__}"
            )
        pairs.append([normalise_text(heading), normalise_text(body)])
    return pairs


def canonical_payload(
    *,
    doc_id: str,
    lang: str,
    version: str,
    title: str,
    sections: Sequence[Any],
) -> dict[str, Any]:
    """The exact structure that gets serialised and digested."""
    return {
        "doc_id": normalise_text(doc_id).lower(),
        "lang": normalise_text(lang).lower(),
        "version": normalise_text(version),
        "title": normalise_text(title),
        "sections": canonical_sections(sections),
    }


def compute_content_hash(
    *,
    doc_id: str,
    lang: str,
    version: str,
    title: str,
    sections: Sequence[Any],
) -> str:
    """Return the 64-character lowercase SHA-256 hex digest of a document."""
    payload = canonical_payload(
        doc_id=doc_id, lang=lang, version=version, title=title, sections=sections
    )
    serialised = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()
