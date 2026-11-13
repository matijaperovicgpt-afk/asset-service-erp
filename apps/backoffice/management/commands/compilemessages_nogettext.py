"""Compile the .po catalogues to .mo without GNU gettext.

``manage.py compilemessages`` shells out to ``msgfmt``. gettext is in the
Docker image and in CI, but not on the Windows development box, where the
command simply fails with "Can't find msgfmt". Since the compiled ``.mo`` is
committed (so that translation works for anyone who does not have gettext
either), somebody has to be able to produce it there.

Deliberately NOT named ``compilemessages``. Overriding a core command would
mean CI silently stopped using the real msgfmt, and msgfmt validates things
this does not.

    python manage.py compilemessages_nogettext
    python manage.py compilemessages_nogettext --check

Three failure modes are guarded, because every one of them is silent in
English and was hit for real while writing this:

1. .po escapes must not be decoded with ``bytes.decode("unicode_escape")``.
   That reinterprets UTF-8 as latin-1, so every euro sign and every š/č/ž
   comes back as mojibake. Nothing crashes - the catalogue simply misses on
   exactly the strings that most needed translating.

2. The metadata entry (the one with an empty msgid) must never be skipped,
   even though .po files conventionally mark it fuzzy. It carries
   ``Content-Type: charset=UTF-8``, and gettext reads the catalogue's charset
   from it. Drop it and gettext assumes ASCII, so the first non-ASCII byte
   raises UnicodeDecodeError and the entire locale fails to load - which,
   with LANGUAGE_CODE = "sr-latn", is every page.

3. That header's fields are separated by escaped newlines. A decoder that
   leaves them as literal backslash-n yields a header gettext parses as one
   meaningless line: same outcome as dropping it.

So the writer refuses to emit a file whose header does not survive both
checks. Failing loudly is worth more than being clever here.
"""

import re
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

BACKSLASH = chr(92)
NEWLINE = chr(10)
NUL = chr(0)
CONTEXT_SEP = chr(4)

#: The complete set of escapes gettext writes into a .po string.
ESCAPES = {
    "n": NEWLINE,
    "t": chr(9),
    "r": chr(13),
    '"': '"',
    BACKSLASH: BACKSLASH,
}

LINE_RE = re.compile(
    r'(msgctxt|msgid_plural|msgid|msgstr(?:\[(\d+)\])?)[ \t]+(".*")[ \t]*$'
)


def unquote(raw):
    """Decode one .po string literal, one character at a time.

    See trap 1 in the module docstring for why this is not a one-liner.
    """
    s = raw.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == BACKSLASH and i + 1 < n:
            out.append(ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_po(text):
    """-> list of entry dicts, in file order."""
    entries, cur, key = [], {}, None

    def flush():
        nonlocal cur
        if "id" in cur:
            entries.append(cur)
        cur = {}

    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                cur["fuzzy"] = True
            continue
        m = LINE_RE.match(line)
        if m:
            head, idx, val = m.group(1), m.group(2), m.group(3)
            if head == "msgctxt":
                flush()
                key = "ctxt"
            elif head == "msgid":
                if "id" in cur and "ctxt" not in cur:
                    flush()
                key = "id"
            elif head == "msgid_plural":
                key = "plural"
            else:
                key = "str%s" % (idx or "0")
            cur[key] = cur.get(key, "") + unquote(val)
        elif line.lstrip().startswith('"') and key:
            cur[key] = cur.get(key, "") + unquote(line)
    flush()
    return entries


def build_catalog(entries):
    catalog = {}
    for e in entries:
        mid = e.get("id")
        if mid is None:
            continue
        # Trap 2: the metadata entry survives whatever its flags say.
        if e.get("fuzzy") and mid != "":
            continue
        if "plural" in e:
            strs = [e[k] for k in sorted(e) if k.startswith("str")]
            if not any(strs):
                continue
            catalog[(e.get("ctxt"), mid + NUL + e["plural"])] = NUL.join(strs)
        else:
            s = e.get("str0", "")
            if mid != "" and not s:
                continue
            catalog[(e.get("ctxt"), mid)] = s
    return catalog


def serialise(catalog):
    """The binary .mo layout: magic, then paired offset/length tables."""
    items = sorted(
        (
            (((c + CONTEXT_SEP + m) if c else m).encode("utf-8"), v.encode("utf-8"))
            for (c, m), v in catalog.items()
        ),
        key=lambda kv: kv[0],
    )
    offsets, ids, strs = [], b"", b""
    for kb, vb in items:
        offsets.append((len(ids), len(kb), len(strs), len(vb)))
        ids += kb + b"\x00"
        strs += vb + b"\x00"
    keystart = 7 * 4 + 16 * len(items)
    valuestart = keystart + len(ids)
    koff, voff = [], []
    for o1, l1, o2, l2 in offsets:
        koff += [l1, o1 + keystart]
        voff += [l2, o2 + valuestart]
    out = struct.pack(
        "<Iiiiiii", 0x950412DE, 0, len(items), 7 * 4, 7 * 4 + len(items) * 8, 0, 0
    )
    out += struct.pack("<%di" % len(koff + voff), *(koff + voff)) + ids + strs
    return out, len(items)


class Command(BaseCommand):
    help = "Compile locale/*/LC_MESSAGES/*.po to .mo without GNU gettext."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Report what would be written; do not touch any .mo file.",
        )

    def handle(self, *args, **options):
        paths = []
        for locale_dir in getattr(settings, "LOCALE_PATHS", []):
            paths.extend(sorted(Path(locale_dir).glob("*/LC_MESSAGES/*.po")))
        if not paths:
            raise CommandError("No .po files found under LOCALE_PATHS.")

        for po in paths:
            entries = parse_po(po.read_text(encoding="utf-8"))
            catalog = build_catalog(entries)

            # Trap 2 and 3: refuse to write a catalogue gettext cannot read.
            header = catalog.get((None, ""), "")
            if "charset=" not in header:
                raise CommandError(
                    f"{po}: metadata header missing or has no charset. gettext "
                    f"would decode this catalogue as ASCII and fail on the "
                    f"first non-ASCII byte."
                )
            if NEWLINE not in header.strip():
                raise CommandError(
                    f"{po}: metadata header has no real newlines - its escapes "
                    f"were not decoded. gettext parses that header line by line."
                )

            blob, count = serialise(catalog)
            mo = po.with_suffix(".mo")
            rel = mo.relative_to(Path(settings.BASE_DIR))
            if options["check"]:
                current = mo.read_bytes() if mo.exists() else b""
                state = "up to date" if current == blob else "STALE"
                self.stdout.write(f"{rel}: {count} entries, {state}")
            else:
                mo.write_bytes(blob)
                self.stdout.write(
                    self.style.SUCCESS(f"{rel}: {count} entries, {len(blob):,} bytes")
                )
