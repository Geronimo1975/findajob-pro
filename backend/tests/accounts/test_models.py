"""Model-level guarantees: canonicalisation, hashing, and the attempt cap."""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import (
    MAX_OTP_ATTEMPTS,
    OTP_HMAC_KEY_SALT,
    PURPOSE_LOGIN,
    EmailOTP,
    SuppressedAddress,
    SuppressionReason,
    User,
    hash_code,
    normalize_email,
)


class NormalizeEmailTests(TestCase):
    def test_lowercases_strips_and_nfkc_normalises(self):
        cases = {
            "  George@Example.DE ": "george@example.de",
            "GEORGE@EXAMPLE.DE": "george@example.de",
            # NFKC folds the fullwidth commercial at and fullwidth letters.
            "ｇｅｏｒｇｅ＠example.de": "george@example.de",
            "": "",
            None: "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_email(raw), expected)

    def test_drops_non_printable_characters(self):
        # Django's ProhibitNullCharactersValidator is form-only and never runs
        # at the model layer, so a NUL would reach the PostgreSQL insert.
        self.assertEqual(normalize_email("a\x00b@example.de"), "ab@example.de")
        self.assertEqual(normalize_email("a\r\nb@example.de"), "ab@example.de")


class HashCodeTests(TestCase):
    def test_is_64_hex_characters(self):
        digest = hash_code("123456", email="a@b.de")
        self.assertEqual(len(digest), 64)
        self.assertRegex(digest, r"\A[0-9a-f]{64}\Z")

    def test_is_deterministic(self):
        self.assertEqual(
            hash_code("123456", email="a@b.de"), hash_code("123456", email="a@b.de")
        )

    def test_never_contains_the_plaintext(self):
        self.assertNotIn("123456", hash_code("123456", email="a@b.de"))

    def test_is_bound_to_the_address(self):
        # A hash captured for one address must not verify against another, even
        # when both were unluckily issued the same six digits.
        self.assertNotEqual(
            hash_code("123456", email="a@b.de"), hash_code("123456", email="c@d.de")
        )

    def test_key_salt_is_the_documented_one(self):
        self.assertEqual(OTP_HMAC_KEY_SALT, "findajob.otp")


class UserModelTests(TestCase):
    def test_email_is_the_username_field_and_there_is_no_username(self):
        self.assertEqual(User.USERNAME_FIELD, "email")
        self.assertEqual(User.REQUIRED_FIELDS, [])
        field_names = {field.name for field in User._meta.get_fields()}
        self.assertNotIn("username", field_names)

    def test_has_is_verified_and_lang(self):
        user = User.objects.create_user(email="a@b.de")
        self.assertFalse(user.is_verified)
        self.assertEqual(user.lang, "en")

    def test_create_user_normalises_and_sets_an_unusable_password(self):
        user = User.objects.create_user(email="  George@Example.DE ")
        self.assertEqual(user.email, "george@example.de")
        self.assertFalse(user.has_usable_password())
        # An unusable password can never match anything, including "".
        self.assertFalse(user.check_password(""))

    def test_email_is_unique_case_insensitively(self):
        User.objects.create_user(email="a@b.de")
        with self.assertRaises(Exception):
            with transaction.atomic():
                User.objects.create_user(email="A@B.DE")

    def test_get_by_natural_key_normalises(self):
        user = User.objects.create_user(email="a@b.de")
        self.assertEqual(User.objects.get_by_natural_key("A@B.DE"), user)

    def test_superuser_requires_a_password(self):
        with self.assertRaises(ValueError):
            User.objects.create_superuser(email="root@b.de", password=None)

    def test_superuser_is_staff_and_verified(self):
        user = User.objects.create_superuser(email="root@b.de", password="x" * 20)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_verified)


class EmailOTPTests(TestCase):
    def _make(self, **kwargs) -> EmailOTP:
        now = timezone.now()
        defaults = {
            "email": "a@b.de",
            "code_hash": hash_code("123456", email="a@b.de"),
            "purpose": PURPOSE_LOGIN,
            "expires_at": now + timedelta(minutes=10),
        }
        defaults.update(kwargs)
        return EmailOTP.objects.create(**defaults)

    def test_attempts_constraint_rejects_six(self):
        otp = self._make()
        otp.attempts = MAX_OTP_ATTEMPTS + 1
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                otp.save(update_fields=["attempts"])

    def test_attempts_constraint_allows_exactly_five(self):
        otp = self._make()
        otp.attempts = MAX_OTP_ATTEMPTS
        otp.save(update_fields=["attempts"])
        otp.refresh_from_db()
        self.assertEqual(otp.attempts, MAX_OTP_ATTEMPTS)

    def test_composite_index_exists(self):
        names = {index.name for index in EmailOTP._meta.indexes}
        self.assertIn("otp_email_created_idx", names)
        index = next(i for i in EmailOTP._meta.indexes if i.name == "otp_email_created_idx")
        self.assertEqual(index.fields, ["email", "-created_at"])

    def test_live_excludes_consumed_expired_and_exhausted(self):
        now = timezone.now()
        live = self._make()
        self._make(consumed_at=now)
        self._make(expires_at=now - timedelta(seconds=1))
        self._make(attempts=MAX_OTP_ATTEMPTS)
        self.assertEqual(list(EmailOTP.objects.live(email="a@b.de")), [live])

    def test_live_filters_by_normalised_address(self):
        live = self._make()
        self.assertEqual(list(EmailOTP.objects.live(email="A@B.DE")), [live])

    def test_matches_is_true_only_for_the_right_code(self):
        otp = self._make()
        self.assertTrue(otp.matches("123456"))
        self.assertFalse(otp.matches("123457"))
        self.assertFalse(otp.matches(""))

    def test_str_never_leaks_the_hash(self):
        otp = self._make()
        self.assertNotIn(otp.code_hash, str(otp))


class SuppressedAddressTests(TestCase):
    def test_suppress_is_idempotent_and_normalises(self):
        first = SuppressedAddress.suppress("A@B.DE", SuppressionReason.HARD_BOUNCE)
        second = SuppressedAddress.suppress("a@b.de", SuppressionReason.HARD_BOUNCE)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SuppressedAddress.objects.count(), 1)
        self.assertEqual(first.email, "a@b.de")

    def test_suppress_updates_the_reason(self):
        SuppressedAddress.suppress("a@b.de", SuppressionReason.HARD_BOUNCE)
        row = SuppressedAddress.suppress("a@b.de", SuppressionReason.COMPLAINT)
        self.assertEqual(row.reason, SuppressionReason.COMPLAINT)

    def test_is_suppressed_is_case_insensitive(self):
        SuppressedAddress.suppress("a@b.de")
        self.assertTrue(SuppressedAddress.is_suppressed("A@B.DE"))
        self.assertFalse(SuppressedAddress.is_suppressed("other@b.de"))

    def test_blank_address_is_ignored(self):
        self.assertIsNone(SuppressedAddress.suppress(""))
        self.assertEqual(SuppressedAddress.objects.count(), 0)


class ModuleBoundaryTests(TestCase):
    """Spec §2: accounts owns User, EmailOTP and SuppressedAddress. No more."""

    def test_accounts_defines_exactly_three_models(self):
        from django.apps import apps as django_apps

        labels = {model.__name__ for model in django_apps.get_app_config("accounts").get_models()}
        self.assertEqual(labels, {"User", "EmailOTP", "SuppressedAddress"})

    def test_accounts_modules_never_import_tenancy_models(self):
        """Parsed, not grepped — the names appear all over the prose."""
        import ast
        import pathlib

        import apps.accounts as accounts_package

        root = pathlib.Path(accounts_package.__file__).parent
        forbidden = {"Organization", "Membership", "AuditLog", "Invitation"}
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
            with self.subTest(path=path.name):
                self.assertEqual(imported & forbidden, set())

    def test_accounts_never_imports_a_tenancy_model_module(self):
        import ast
        import pathlib

        import apps.accounts as accounts_package

        root = pathlib.Path(accounts_package.__file__).parent
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = getattr(node, "module", None) or ""
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [module] + [alias.name for alias in node.names]
                    for name in names:
                        with self.subTest(path=path.name, name=name):
                            self.assertNotIn("tenancy.models", name)
