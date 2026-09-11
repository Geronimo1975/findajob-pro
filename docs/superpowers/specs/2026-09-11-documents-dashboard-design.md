# Documents dashboard — upload/list/download/delete API (design, 2026-09-11)

## Goal

The Documents screen in `web/app.html` already renders a full UI (upload button, doc cards,
ATS-score bar) against the `DOCUMENTS` fixture, but every action is fake: `onUpload` appends a
client-only row with an invented `ats_score` (68 or 95, hardcoded) and nothing is ever sent to
the server. This closes that gap with real endpoints behind the `Document` model that already
exists in `apps.jobs` (`backend/apps/jobs/models.py::Document`, org-scoped, `FileField`,
`kind`/`tags`/`ats_score` fields, `document_upload_to` partitioning by org).

## Constraint that shapes everything: `/media/` is not reachable

`deploy/nginx.conf` and `deploy/README.md` §"`/media/` must stay unreachable through nginx,
forever" are explicit: candidate documents are personal data, live on a volume mounted `0700`,
and there is deliberately no nginx `location` for `/media/`. `MEDIA_URL`/`FileField.url` is
therefore useless to the frontend — every read of file bytes must go through an authenticated
Django view that checks org/user ownership and streams the content itself.

## Endpoints

All four live in `apps/jobs/views.py` (the app that already owns `Document`), wired at
`/api/v1/documents/` per `web/api.js`'s existing `ENDPOINTS.documents` entry — no new app.

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/v1/documents/` | List the requesting member's documents (`Document.objects.for_user(...)`), shaped exactly like the `DOCUMENTS` fixture: `id, name, kind, tags, size, updated, ats_score`. |
| `POST` | `/api/v1/documents/` | Multipart upload. Validates extension and size (below), infers `kind` server-side if not supplied, creates the row, returns the same shape as one list row. |
| `GET` | `/api/v1/documents/:id/download/` | Streams the file (`FileResponse`) after confirming the document belongs to the requesting member's org. 404 (not 403) on someone else's id — existence of another org's document is not information to leak. |
| `DELETE` | `/api/v1/documents/:id/` | Deletes the row and the underlying file. Not in the shipped UI yet, but any document manager needs it; added now rather than as a second round. |

### Wire shape (`GET`/`POST` response), matching the fixture exactly

```json
{"id": 7, "name": "CV — Backend (EN).pdf", "kind": "cv", "tags": [],
 "size": "184 KB", "updated": "Sep 11, 2026", "ats_score": null}
```

- `size`: formatted server-side (`"184 KB"`), not raw bytes — the fixture and the UI's
  `docCards` mapping both expect a display string.
- `updated`: formatted `"Sep 11, 2026"` (matches the fixture's date style), derived from
  `updated_at`.
- `ats_score`: **always `null` on upload.** Computing it is out of scope (roadmap item 5,
  "document optimization") — the current client-side `68`/`95` placeholders are exactly the
  invented behavior this design removes, not something to reproduce server-side.

### Upload validation

- Extension allow-list: `.pdf`, `.doc`, `.docx` — matches the `accept` attribute already on the
  file input in `app.html`, now enforced server-side too (client-side `accept` is a UX hint,
  not a security boundary).
- Max size: 10 MB, checked before the file is written to disk. Above it: `413` with the
  project's error envelope (`{"error": {"code": "document_too_large", "detail": "..."}}`).
- Wrong extension: `400 unsupported_document_type`.
- `kind`: optional form field, one of `Document.DocumentKind` choices. If omitted, the server
  applies the same heuristic the prototype used client-side (`cv`/`resume` in filename → `cv`,
  `cert` → `certificate`, else `other`) so behavior doesn't silently change for a client that
  still doesn't send it.
- No virus/content scanning, no MIME-sniffing beyond the extension check — adding one (e.g.
  `python-magic`, `clamd`) is a new dependency and stays a follow-up ask, not part of this
  round.

### Ownership and scoping

`Document.objects` is already `OrgScopedManager(user_field="user")` — list/upload/delete use
`.for_user(request.user)` (raises `OrgScopeError` if ever called unscoped, consistent with the
rest of the codebase). Download additionally re-checks `document.user_id == request.user.id`
even though the scoped queryset already guarantees it, because a download view is exactly the
kind of code a future refactor might accidentally query unscoped from — the check is cheap
insurance for personal-data exposure, not redundant defense-in-depth theater.

## Serializers

`DocumentSerializer` in `apps/jobs/serializers.py` (same file as the existing
`PlatformSerializer`/`JobRowSerializer`), read/write split:

- `DocumentSerializer` (list/create response): the wire shape above.
- Upload accepts `serializers.FileField` + optional `kind` `ChoiceField`; `name` is taken from
  the uploaded filename, not a separate form field (the UI doesn't offer one).

## Views

`DocumentListCreateView(APIView)` (GET list, POST upload) and `DocumentDetailView(APIView)`
(GET download, DELETE), both `permission_classes = [IsOrgMember]` matching `JobListView`'s
existing pattern. Multipart parsing via DRF's default `MultiPartParser`/`FormParser`.

## Frontend change

`web/api.js`: `documents` moves from a fixture-only export to a real `request()` call for list;
add `uploadDocument(file, kind)` and `deleteDocument(id)` helpers following the existing
`request()`/`ApiError`/CSRF pattern (same as the auth calls already wired). `app.html`'s
`onUpload` handler calls `api.uploadDocument(f)` and refreshes from the real list response
instead of synthesizing a row; `ats_score` rendering (`docCards`) needs no change — it already
handles `undefined`/`null` via `hasAts: !!d.ats_score`.

## Out of scope (explicit)

- ATS scoring (roadmap item 5).
- Virus/content scanning.
- Editing tags/kind after upload (no such control in the shipped UI).
- Document versioning / replacing an existing file in place.
- The AI-generation flow (`runGenerate`, Canva templates) — separate, already-planned feature.

## Testing

`backend/tests/jobs/test_documents.py`:

- Upload: accepted extensions succeed; rejected extension → 400; oversized file → 413; `kind`
  inference matches the filename heuristic when omitted, honors an explicit valid `kind`,
  rejects an invalid one.
- List: only the requesting user's org-scoped documents are returned; shape matches the fixture
  fields exactly (regression guard against re-introducing `psd`-style naming drift).
- Download: 200 + correct bytes for the owner; 404 for another org's document id; 401
  unauthenticated.
- Delete: removes both the row and the file from storage; 404 on someone else's document.
- `Document.objects.all()` still raises `OrgScopeError` (unaffected by this change — regression
  guard only).

## Dependencies

None new. `MultiPartParser`/`FormParser` and `FileResponse` are stock DRF/Django, already
available.
