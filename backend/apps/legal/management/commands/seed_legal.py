"""``manage.py seed_legal`` — load ``web/legal.js`` into ``LegalDocument``.

    Six documents x three languages = 18 rows, each with a content hash that a
    consent record can point at years later.

Why the source is a JavaScript file
-----------------------------------

``web/legal.js`` is what the browser actually renders, and it must stay that way
(the site is UI-first and the legal pages have to work before the backend
exists). Duplicating the text into a fixture would guarantee that the served
text and the hashed text drift apart — and the whole point of the hash is that
they cannot. So the seeder reads the shipped file directly, through
:mod:`apps.legal.jsparse`, a ~200-line recursive-descent reader for the JS
object-literal subset that file uses. See that module's docstring for why not
``json.loads``, not a regex, and not a shell-out to Node.

Idempotence
-----------

Running this twice changes nothing: the version is derived from each document's
``updated`` date (see :mod:`apps.legal.versioning`), so the same file always
yields the same ``(doc_id, lang, version)`` and the same hash.

If a stored version's text has *drifted* — same date, different words — the
command refuses to overwrite it and tells you to bump the date in ``legal.js``.
Silently rewriting the row would leave every ``ConsentRecord`` pointing at a
hash that no longer describes any text that ever existed. ``--force`` exists for
the case where you know the stored row is the wrong one.

Usage::

    python manage.py seed_legal                  # from repo web/legal.js
    python manage.py seed_legal --dry-run        # report, write nothing
    python manage.py seed_legal --path other.js
    python manage.py seed_legal --force          # overwrite drifted versions
    python manage.py seed_legal --doc-version 2026-08-01
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.legal.hashing import canonical_sections, compute_content_hash
from apps.legal.jsparse import JsLiteralError, parse_module_exports
from apps.legal.models import (
    DOC_ID_ORDER,
    LANG_ORDER,
    MAX_TITLE_LENGTH,
    MAX_VERSION_LENGTH,
    LegalDocument,
)
from apps.legal.versioning import (
    UnparseableDate,
    effective_from_for_date,
    parse_document_date,
    version_for_date,
)

#: Keys in a language block that are metadata, not documents.
_META_KEYS = ("_note",)


class _Rollback(Exception):
    """Internal sentinel used to undo a ``--dry-run`` transaction."""


def default_source_path() -> Path:
    """``<repo>/web/legal.js`` — the file nginx actually serves."""
    return Path(settings.REPO_ROOT) / "web" / "legal.js"


class Command(BaseCommand):
    help = "Seed LegalDocument rows from web/legal.js (6 documents x EN/DE/RO)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--path",
            default=None,
            help="Path to legal.js (default: <repo>/web/legal.js).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report, then roll back without writing.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Overwrite a stored version whose text has changed. Detaches "
                "existing consent records from their hash — use knowingly."
            ),
        )
        # NOT `--version`: Django's BaseCommand already owns that flag, and
        # adding it here makes every invocation die in argparse.
        parser.add_argument(
            "--doc-version",
            dest="doc_version",
            default=None,
            help=(
                "Override the version for every document instead of deriving "
                "it from the `updated` date."
            ),
        )
        parser.add_argument(
            "--lang",
            action="append",
            default=None,
            help="Seed only these languages (repeatable).",
        )

    # -- entry point ------------------------------------------------------

    def handle(self, *args, **options) -> None:
        path = Path(options["path"]) if options["path"] else default_source_path()
        if not path.is_file():
            raise CommandError(f"No such file: {path}")

        source = path.read_text(encoding="utf-8")
        try:
            exports = parse_module_exports(source, names=("LEGAL", "LEGAL_DOCS"))
        except JsLiteralError as exc:
            raise CommandError(f"Could not parse {path}: {exc}") from None

        legal = exports["LEGAL"]
        doc_ids = exports["LEGAL_DOCS"]
        self._validate_shape(legal, doc_ids, path)

        languages = self._languages(legal, options["lang"])
        parsed = self._collect(legal, doc_ids, languages, options["doc_version"])
        self._warn_on_version_disagreement(parsed)

        stats = {"created": 0, "updated": 0, "unchanged": 0}
        drift: list[str] = []
        try:
            with transaction.atomic():
                for entry in parsed:
                    self._apply(entry, stats, drift, force=options["force"])
                if drift and not options["force"]:
                    raise CommandError(
                        "Refusing to overwrite stored document versions whose "
                        "text has changed:\n  - "
                        + "\n  - ".join(drift)
                        + "\n\nBump the document's `updated` date in "
                        f"{path.name} so the change gets its own version, or "
                        "re-run with --force if the stored row is the wrong one."
                    )
                if options["dry_run"]:
                    raise _Rollback
        except _Rollback:
            if options["verbosity"]:
                self.stdout.write(self.style.WARNING("Dry run — nothing was written."))

        if not options["verbosity"]:
            return

        total = LegalDocument.objects.count() if not options["dry_run"] else len(parsed)
        self.stdout.write(
            self.style.SUCCESS(
                "seed_legal: {created} created, {updated} updated, "
                "{unchanged} unchanged ({total} rows, {langs} languages, "
                "{docs} documents).".format(
                    total=total,
                    langs=len(languages),
                    docs=len(doc_ids),
                    **stats,
                )
            )
        )

    # -- parsing ----------------------------------------------------------

    def _validate_shape(self, legal, doc_ids, path: Path) -> None:
        if not isinstance(legal, dict) or not legal:
            raise CommandError(f"{path}: LEGAL is not a non-empty object.")
        if not isinstance(doc_ids, list) or not doc_ids:
            raise CommandError(f"{path}: LEGAL_DOCS is not a non-empty array.")

        unknown_docs = [doc for doc in doc_ids if doc not in DOC_ID_ORDER]
        if unknown_docs:
            raise CommandError(
                f"{path}: LEGAL_DOCS names document(s) this backend does not "
                f"know: {', '.join(map(str, unknown_docs))}. Add them to "
                "apps.legal.models.DocId (and make a migration) first."
            )
        unknown_langs = [lang for lang in legal if lang not in LANG_ORDER]
        if unknown_langs:
            raise CommandError(
                f"{path}: LEGAL has language(s) this backend does not know: "
                f"{', '.join(unknown_langs)}. Add them to "
                "apps.legal.models.Lang and settings.LANGUAGES first."
            )

    def _languages(self, legal: dict, wanted) -> list[str]:
        available = [lang for lang in LANG_ORDER if lang in legal]
        if not wanted:
            return available
        chosen = [lang for lang in available if lang in set(wanted)]
        missing = sorted(set(wanted) - set(chosen))
        if missing:
            raise CommandError(
                f"Requested language(s) not present in the source: {', '.join(missing)}"
            )
        return chosen

    def _collect(
        self, legal: dict, doc_ids: list, languages: list[str], version_override
    ) -> list[dict]:
        entries: list[dict] = []
        for lang in languages:
            block = legal[lang]
            if not isinstance(block, dict):
                raise CommandError(f"LEGAL.{lang} is not an object.")
            note = str(block.get("_note", "") or "")
            missing = [doc for doc in doc_ids if doc not in block]
            if missing:
                raise CommandError(
                    f"LEGAL.{lang} is missing document(s): {', '.join(missing)}"
                )
            for doc_id in doc_ids:
                if doc_id in _META_KEYS:
                    continue
                entries.append(self._entry(lang, doc_id, block[doc_id], note, version_override))
        return entries

    def _entry(self, lang: str, doc_id: str, raw, note: str, version_override) -> dict:
        if not isinstance(raw, dict):
            raise CommandError(f"LEGAL.{lang}.{doc_id} is not an object.")
        title = str(raw.get("title", "") or "").strip()
        if not title:
            raise CommandError(f"LEGAL.{lang}.{doc_id} has no title.")
        if len(title) > MAX_TITLE_LENGTH:
            raise CommandError(
                f"LEGAL.{lang}.{doc_id}: title is {len(title)} characters, "
                f"the column holds {MAX_TITLE_LENGTH}."
            )
        raw_sections = raw.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise CommandError(f"LEGAL.{lang}.{doc_id} has no sections.")
        try:
            pairs = canonical_sections(raw_sections)
        except (TypeError, ValueError) as exc:
            raise CommandError(f"LEGAL.{lang}.{doc_id}: {exc}") from None

        updated = str(raw.get("updated", "") or "")
        if version_override:
            version = str(version_override)
            try:
                document_date = parse_document_date(version)
            except UnparseableDate:
                # An arbitrary --version string still needs an effective_from;
                # fall back to the document's own date, which must then parse.
                document_date = self._date_or_die(lang, doc_id, updated)
        else:
            document_date = self._date_or_die(lang, doc_id, updated)
            version = version_for_date(document_date)

        if len(version) > MAX_VERSION_LENGTH:
            raise CommandError(
                f"LEGAL.{lang}.{doc_id}: version {version!r} exceeds "
                f"{MAX_VERSION_LENGTH} characters."
            )

        sections = [{"heading": heading, "body": body} for heading, body in pairs]
        return {
            "doc_id": doc_id,
            "lang": lang,
            "version": version,
            "title": title,
            "sections": sections,
            "note": note,
            "effective_from": effective_from_for_date(document_date),
            "content_hash": compute_content_hash(
                doc_id=doc_id,
                lang=lang,
                version=version,
                title=title,
                sections=pairs,
            ),
            "updated_label": updated,
        }

    def _date_or_die(self, lang: str, doc_id: str, updated: str):
        try:
            return parse_document_date(updated)
        except UnparseableDate as exc:
            raise CommandError(
                f"LEGAL.{lang}.{doc_id}: cannot derive a version — {exc} "
                "Fix the `updated` string in legal.js (ISO `2026-08-01` always "
                "works) or pass --version."
            ) from None

    def _warn_on_version_disagreement(self, entries: list[dict]) -> None:
        """A document whose languages carry different dates breaks staleness.

        ``record_registration_consent`` compares the client's version against
        the German document only, so an English page advertising a different
        version would 409 every English visitor.
        """
        by_doc: dict[str, dict[str, str]] = {}
        for entry in entries:
            by_doc.setdefault(entry["doc_id"], {})[entry["lang"]] = entry["version"]
        for doc_id, versions in sorted(by_doc.items()):
            if len(set(versions.values())) > 1:
                detail = ", ".join(f"{lang}={ver}" for lang, ver in sorted(versions.items()))
                self.stderr.write(
                    self.style.WARNING(
                        f"WARNING: {doc_id} has different dates per language "
                        f"({detail}). The staleness check uses the German "
                        "version, so visitors reading another language may be "
                        "told the terms are outdated. Align the `updated` "
                        "dates in legal.js."
                    )
                )

    # -- writing ----------------------------------------------------------

    def _apply(self, entry: dict, stats: dict, drift: list[str], *, force: bool) -> None:
        existing = LegalDocument.objects.filter(
            doc_id=entry["doc_id"], lang=entry["lang"], version=entry["version"]
        ).first()

        if existing is None:
            document = LegalDocument.objects.create(
                doc_id=entry["doc_id"],
                lang=entry["lang"],
                version=entry["version"],
                title=entry["title"],
                sections=entry["sections"],
                note=entry["note"],
                content_hash=entry["content_hash"],
                effective_from=entry["effective_from"],
                is_current=False,
            )
            stats["created"] += 1
            self._make_current(document)
            return

        content_changed = existing.content_hash != entry["content_hash"]
        if content_changed and not force:
            drift.append(
                f"{entry['doc_id']}/{entry['lang']} version {entry['version']}: "
                f"stored {existing.content_hash[:12]}, file "
                f"{entry['content_hash'][:12]}"
            )
            return

        metadata_changed = (
            existing.note != entry["note"]
            or existing.effective_from != entry["effective_from"]
        )
        if content_changed or metadata_changed:
            existing.title = entry["title"]
            existing.sections = entry["sections"]
            existing.note = entry["note"]
            existing.content_hash = entry["content_hash"]
            existing.effective_from = entry["effective_from"]
            existing.save(
                update_fields=[
                    "title",
                    "sections",
                    "note",
                    "content_hash",
                    "effective_from",
                    "updated_at",
                ]
            )
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1
        self._make_current(existing)

    def _make_current(self, document: LegalDocument) -> None:
        """Point ``(doc_id, lang)`` at ``document``, demoting any predecessor.

        Order matters: the partial unique index allows exactly one
        ``is_current=True`` row per (doc_id, lang), so the old one has to be
        cleared *before* the new one is set, in the same transaction.

        ``.update()`` does not fire ``auto_now``, so the demoted rows keep their
        old ``updated_at``. That is deliberate — they were not edited.
        """
        (
            LegalDocument.objects.filter(doc_id=document.doc_id, lang=document.lang)
            .exclude(pk=document.pk)
            .filter(is_current=True)
            .update(is_current=False)
        )
        if not document.is_current:
            document.is_current = True
            document.save(update_fields=["is_current", "updated_at"])
