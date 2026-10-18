"""Session gate for the server-rendered dashboard."""

from functools import wraps
from urllib.parse import quote

from django.http import HttpResponseRedirect


def session_required(view_func):
    """Redirect anonymous visitors to the static login page."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)
        return HttpResponseRedirect("/auth.html?next=" + quote(request.get_full_path(), safe=""))

    return wrapper
