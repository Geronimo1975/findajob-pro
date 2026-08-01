"""Legal consent — LegalDocument and ConsentRecord.

Owned by a separate work stream (build-order position 4). Empty apart from the
migrations package.

Implementation spec §10: the terms version and content hash are resolved on the
**server** (`LegalDocument.objects.get(doc_id="terms", lang="de", is_current=True)`)
— the DE document is the legally binding one. A client-sent version string is
used only to detect staleness -> 409 `terms_outdated`.
"""
