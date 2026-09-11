"""``devices.TrustedDevice`` and its services — the secret-handling invariants.

See ``docs/2026-08-22-trusted-device-spec.md``.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.devices.models import (
    TRUSTED_DEVICE_TTL_DAYS,
    TrustedDevice,
    hash_secret,
)
from apps.devices.services import (
    DEVICE_COOKIE_NAME,
    build_cookie_value,
    revoke_device,
    trust_device,
)
from tests.support import make_user


class HashSecretTests(TestCase):
    def test_the_hash_is_sixty_four_hex_characters(self):
        digest = hash_secret("s3cret", device_id="d0d0")
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(char in "0123456789abcdef" for char in digest))

    def test_the_same_secret_under_a_different_device_hashes_differently(self):
        # The device id is mixed into the key salt, so a hash captured for one
        # device cannot be replayed against another.
        self.assertNotEqual(
            hash_secret("s3cret", device_id="aaaa"),
            hash_secret("s3cret", device_id="bbbb"),
        )

    def test_hashing_is_deterministic(self):
        self.assertEqual(
            hash_secret("s3cret", device_id="aaaa"),
            hash_secret("s3cret", device_id="aaaa"),
        )


class TrustDeviceTests(TestCase):
    def setUp(self):
        self.user = make_user("george@example.de")

    def test_the_plaintext_secret_is_never_stored(self):
        device, secret = trust_device(user=self.user)
        self.assertNotIn(secret, device.secret_hash)
        row = TrustedDevice.objects.get(pk=device.pk)
        for value in vars(row).values():
            self.assertNotEqual(value, secret)

    def test_the_returned_secret_matches_the_row(self):
        device, secret = trust_device(user=self.user)
        self.assertTrue(device.matches(secret))

    def test_a_wrong_secret_does_not_match(self):
        device, _ = trust_device(user=self.user)
        self.assertFalse(device.matches("not-the-secret"))

    def test_it_expires_thirty_days_out(self):
        device, _ = trust_device(user=self.user)
        delta = device.expires_at - timezone.now()
        self.assertAlmostEqual(delta.days, TRUSTED_DEVICE_TTL_DAYS, delta=1)

    def test_two_devices_get_different_ids_and_secrets(self):
        first, first_secret = trust_device(user=self.user)
        second, second_secret = trust_device(user=self.user)
        self.assertNotEqual(first.device_id, second.device_id)
        self.assertNotEqual(first_secret, second_secret)
        # And neither cookie works on the other row.
        self.assertFalse(first.matches(second_secret))

    def test_the_cookie_value_carries_both_halves(self):
        device, secret = trust_device(user=self.user)
        value = build_cookie_value(device, secret)
        self.assertEqual(value, f"{device.device_id}.{secret}")


class LivenessTests(TestCase):
    def setUp(self):
        self.user = make_user("george@example.de")

    def test_a_fresh_device_is_live(self):
        device, _ = trust_device(user=self.user)
        self.assertTrue(device.is_live())

    def test_an_expired_device_is_not_live(self):
        device, _ = trust_device(user=self.user)
        device.expires_at = timezone.now() - timedelta(seconds=1)
        self.assertFalse(device.is_live())

    def test_a_revoked_device_is_not_live(self):
        device, _ = trust_device(user=self.user)
        revoke_device(device)
        self.assertFalse(device.is_live())

    def test_revocation_is_idempotent(self):
        device, _ = trust_device(user=self.user)
        revoke_device(device)
        first = device.revoked_at
        revoke_device(device)
        self.assertEqual(device.revoked_at, first)

    def test_the_live_queryset_excludes_dead_rows(self):
        live, _ = trust_device(user=self.user)
        revoked, _ = trust_device(user=self.user)
        revoke_device(revoked)
        expired, _ = trust_device(user=self.user)
        TrustedDevice.objects.filter(pk=expired.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        pks = set(TrustedDevice.objects.live(user=self.user).values_list("pk", flat=True))
        self.assertEqual(pks, {live.pk})


class CookieNameTests(TestCase):
    def test_the_cookie_is_not_the_session_cookie(self):
        from django.conf import settings

        self.assertNotEqual(DEVICE_COOKIE_NAME, settings.SESSION_COOKIE_NAME)
        self.assertNotEqual(DEVICE_COOKIE_NAME, settings.CSRF_COOKIE_NAME)
