"""McpToken — spec §4 phase 1."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.tenancy.managers import OrgScopeError
from apps.tenancy.mcp.models import (
    PREFIX_LENGTH, SCOPES, TOKEN_LENGTH, TOKEN_PREFIX, McpToken,
    generate_token, hash_token,
)
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user


class GenerateTokenTests(TestCase):
    def test_shape(self):
        raw, prefix, digest = generate_token()
        self.assertTrue(raw.startswith(TOKEN_PREFIX))
        self.assertEqual(len(raw), TOKEN_LENGTH)
        self.assertEqual(prefix, raw[:PREFIX_LENGTH])
        self.assertEqual(digest, hash_token(raw))
        self.assertEqual(len(digest), 64)

    def test_two_tokens_differ(self):
        self.assertNotEqual(generate_token()[0], generate_token()[0])


class McpTokenModelTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.membership = bootstrap_workspace(self.user)
        self.org = self.membership.org

    def _make(self, **overrides) -> McpToken:
        raw, prefix, digest = generate_token()
        fields = dict(org=self.org, user=self.user, name="laptop",
                      prefix=prefix, token_hash=digest, scopes=list(SCOPES))
        fields.update(overrides)
        return McpToken.objects.create(**fields)

    def test_unscoped_read_raises(self):
        self._make()
        with self.assertRaises(OrgScopeError):
            list(McpToken.objects.all())

    def test_for_org_reads(self):
        token = self._make()
        self.assertEqual(list(McpToken.objects.for_org(self.org)), [token])

    def test_is_usable_states(self):
        now = timezone.now()
        self.assertTrue(self._make().is_usable(now))
        self.assertFalse(self._make(expires_at=now - timedelta(seconds=1)).is_usable(now))
        self.assertFalse(self._make(revoked_at=now).is_usable(now))
        self.assertEqual(self._make(revoked_at=now).status_code(), "revoked")
        self.assertEqual(self._make(expires_at=now - timedelta(days=1)).status_code(), "expired")
        self.assertEqual(self._make().status_code(), "active")

    def test_has_scope(self):
        token = self._make(scopes=["jobs:read"])
        self.assertTrue(token.has_scope("jobs:read"))
        self.assertFalse(token.has_scope("applications:send"))

    def test_token_hash_is_unique(self):
        token = self._make()
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            McpToken.objects.create(org=self.org, user=self.user, name="dup",
                                    prefix=token.prefix, token_hash=token.token_hash,
                                    scopes=[])
