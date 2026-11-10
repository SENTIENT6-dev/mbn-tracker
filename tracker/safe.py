"""Return addresses (`next`) are only followed when they point inside this tracker."""
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, default="", source=None):
    """The request's `next` address if it's safe (same site), otherwise `default`.

    `source` is request.POST or request.GET; by default POST is checked first, then GET.
    """
    if source is not None:
        nxt = source.get("next", "")
    else:
        nxt = request.POST.get("next", "") or request.GET.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return default
