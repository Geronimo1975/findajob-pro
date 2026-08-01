"""Backend test suite.

Runs two ways, with identical results::

    DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test
    pytest                                    # pytest.ini already points there

Everything is a plain ``django.test.TestCase``, so the runtime dependency set
is enough — pytest and pytest-django are optional conveniences.
"""
