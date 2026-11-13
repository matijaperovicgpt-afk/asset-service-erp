"""Static files storage that ignores sourcemap references.

``CompressedManifestStaticFilesStorage`` rewrites every asset reference it
finds inside collected CSS and JS so the reference points at the hashed
filename. With ``manifest_strict`` on - the default, and the reason the
manifest is worth having - a reference it cannot resolve is a hard
``ValueError``, not a warning.

That is exactly the behaviour we want for the references that matter. The
rebrand depends on it: ``app.css`` names ten ``.woff2`` files, and if one
of them were missing a deploy SHOULD fail loudly rather than ship a page
with no typeface.

It is not the behaviour we want for sourcemaps. The vendored libraries in
``static/vendor/`` are the distributed minified builds, and those builds end
with a comment pointing at a ``.map`` file:

    //# sourceMappingURL=bootstrap.bundle.min.js.map
    /*# sourceMappingURL=bootstrap.min.css.map */

The maps themselves are not vendored, and deliberately so: together they are
roughly 900 KB, against ~360 KB for the whole of ``static/vendor``. They are
a debugging aid that a browser fetches only when devtools is open, so
carrying them would nearly triple the vendored payload to serve something no
user ever receives.

The result, before this module existed, was that ``collectstatic`` raised on
``bootstrap.bundle.min.js.map`` and - because ``docker/entrypoint.sh`` runs
collectstatic under ``set -e`` - the container never started. Only on a fresh
static volume, which is what makes it nasty: an existing volume still holds
the previously collected files, so the same image boots fine on the machine
you tested and dies on a new one.

The two alternatives were both worse. Vendoring the maps pays 900 KB
forever. Editing the vendored files to strip the comments works until the
next Bootstrap upgrade re-introduces them, and quietly puts a local
modification into files whose whole value is being the untouched upstream
build.

So: keep the manifest strict, and simply stop looking for sourcemaps.

The pattern list is filtered rather than retyped. Django's default is four
rules - ``url()``, ``@import`` and ``sourceMappingURL`` for CSS, and
``sourceMappingURL`` for JS - and copying the surviving three in here by
hand would silently fall behind the day Django changes one of them. The
``url()`` rule in particular is the one the fonts ride on, so it must stay
whatever upstream decides it should look like.
"""

from django.contrib.staticfiles.storage import HashedFilesMixin
from whitenoise.storage import CompressedManifestStaticFilesStorage


def _rule_regex(rule):
    """The pattern string out of a rule, which may be a bare regex or a
    ``(regex, template)`` pair."""
    return rule[0] if isinstance(rule, (tuple, list)) else rule


def _without_sourcemaps(patterns):
    """``patterns`` minus every rule that resolves a sourcemap reference.

    An extension whose rules are all dropped - ``*.js``, which has no other
    rule - is dropped with them, so the file type is not scanned at all.
    """
    kept_by_extension = []
    for extension, rules in patterns:
        kept = tuple(
            rule for rule in rules
            if "sourceMappingURL" not in _rule_regex(rule)
        )
        if kept:
            kept_by_extension.append((extension, kept))
    return tuple(kept_by_extension)


class SourcemapTolerantStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Hashed + compressed static files, minus sourcemap resolution.

    Everything else about the parent is unchanged, ``manifest_strict``
    included: a missing font, image or stylesheet still fails the deploy.
    """

    patterns = _without_sourcemaps(HashedFilesMixin.patterns)
