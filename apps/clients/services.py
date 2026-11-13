"""
Client-domain services: triage matching, linking (GEN-03) and the
asset identification the procedure's §7.2 asks for.

Public case submissions arrive with raw contact/asset text on
``cases.Case`` (``raw_*`` fields). ``suggest_matches`` reconciles that
raw data with structured ``Client`` / ``Asset`` records; ``link_case``
attaches the chosen records to the case while keeping the raw fields
untouched for audit.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.cases.models import ActivityLog, Case

from .models import Client, Asset

# Score weights: a registration-number match outranks a phone match,
# which outranks an email match, which outranks a name match (names are
# the weakest signal: common and easily misspelled). Weights combine
# when several channels hit the same client.
SCORE_REGISTRATION = 4
SCORE_PHONE = 2
SCORE_EMAIL = 1
SCORE_NAME = 1


#: The site's own country code (settings.PHONE_COUNTRY_CODE): numbers
#: without an international prefix are read as domestic, and domestic
#: numbers compare in their national form.
HOME_COUNTRY_CODE = settings.PHONE_COUNTRY_CODE


def normalize_phone(raw: str) -> str:
    """
    Normalize a phone number for comparison.

    Strips spaces, dashes and parentheses. A domestic number loses its
    country-code / trunk prefix, so (with PHONE_COUNTRY_CODE=44)
    ``+44 67 123-456``, ``004467123456`` and ``067123456`` all compare
    equal. A foreign number keeps its country code in digits-only form,
    so ``+49 91 123 456`` and ``0049 91 123 456`` match each other (they
    used to be two different clients at triage) without colliding with a
    domestic ``091``.
    """
    if not raw:
        return ""
    cleaned = "".join(ch for ch in raw if ch not in " -()")
    international = None
    for prefix in ("+", "00"):
        if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
            international = cleaned[len(prefix):]
            break
    if international is not None:
        if international.startswith(HOME_COUNTRY_CODE) and len(
            international
        ) > len(HOME_COUNTRY_CODE):
            return international[len(HOME_COUNTRY_CODE):].lstrip("0")
        return international
    if cleaned.startswith("0") and len(cleaned) > 1:
        return cleaned[1:]
    return cleaned


def normalize_name(raw: str) -> str:
    """Normalize a person/company name for comparison: casefold and
    collapse internal whitespace so "ana  petrović" == "Ana Petrović"."""
    if not raw:
        return ""
    return " ".join(raw.casefold().split())


def suggest_matches(case: Case) -> list:
    """
    Rank clients (and assets) likely matching the case's raw_* data.

    Returns a list of dicts ``{"client", "asset", "score", "reasons"}``
    sorted by descending score (``asset`` is None unless a registration
    number matched). Pure read - never writes to the DB.
    """
    raw_phone = normalize_phone(case.raw_phone)
    raw_email = (case.raw_email or "").strip().lower()
    raw_name = normalize_name(case.raw_contact_name)
    raw_asset_info = (case.raw_asset_info or "").lower()

    # Client-level hits (phone / email), keyed by client pk.
    base: dict = {}

    def _base_entry(client):
        return base.setdefault(client.pk, {"client": client, "score": 0, "reasons": []})

    for client in Client.objects.all():
        if raw_phone and normalize_phone(client.phone) == raw_phone:
            entry = _base_entry(client)
            entry["score"] += SCORE_PHONE
            entry["reasons"].append(
                _("phone matches %(phone)s") % {"phone": client.phone}
            )
        if raw_email and client.email and client.email.strip().lower() == raw_email:
            entry = _base_entry(client)
            entry["score"] += SCORE_EMAIL
            entry["reasons"].append(
                _("email matches %(email)s") % {"email": client.email}
            )
        if raw_name and normalize_name(client.name) == raw_name:
            entry = _base_entry(client)
            entry["score"] += SCORE_NAME
            entry["reasons"].append(
                _("name matches %(name)s") % {"name": client.name}
            )

    candidates = []
    asset_matched_clients = set()
    if raw_asset_info:
        for asset in Asset.objects.exclude(registration_no="").select_related("client"):
            if asset.registration_no.lower() in raw_asset_info:
                entry = base.get(
                    asset.client_id,
                    {"client": asset.client, "score": 0, "reasons": []},
                )
                candidates.append({
                    "client": asset.client,
                    "asset": asset,
                    "score": entry["score"] + SCORE_REGISTRATION,
                    "reasons": entry["reasons"]
                    + [
                        _("asset registration %(registration)s found in raw asset info")
                        % {"registration": asset.registration_no}
                    ],
                })
                asset_matched_clients.add(asset.client_id)

    # Clients matched only by phone/email (no asset identified).
    for pk, entry in base.items():
        if pk not in asset_matched_clients:
            candidates.append({
                "client": entry["client"],
                "asset": None,
                "score": entry["score"],
                "reasons": entry["reasons"],
            })

    candidates.sort(key=lambda c: (-c["score"], c["client"].name))
    return candidates


def complete_client_contact(client: Client, phone: str = "", email: str = "") -> Client:
    """
    Fill in / refresh a client's contact data from an intake form.

    Only non-empty submitted values are applied, so an untouched or
    cleared form field never wipes contact data already on record.
    """
    updates = []
    for field, value in (("phone", phone), ("email", email)):
        value = (value or "").strip()
        if value and value != getattr(client, field):
            setattr(client, field, value)
            updates.append(field)
    if updates:
        client.save(update_fields=updates)
    return client


#: §7.2's six asset data, in the order the procedure names them.
#: ``registration_no`` is paired with ``is_unregistered`` rather than
#: demanded outright - see ``Asset.registration_recorded``.
ASSET_IDENTIFICATION_FIELDS = (
    "name",
    "asset_type",
    "manufacturer",
    "model",
    "registration_no",
    "is_unregistered",
    "length_m",
)


def record_asset_identification(asset: Asset, user, *, case=None, **fields) -> Asset:
    """
    §7.2: complete the asset's identification - name, type,
    manufacturer, model, registration, length.

    Until now there was no way to do this at all outside the Django
    admin: the backoffice can *view* an asset and can create a partial
    one at triage, and nothing anywhere could fill in what triage left
    blank. So this is the missing half of the rule, not only its
    enforcement.

    A registration number and the "not registered" tick contradict each
    other and are refused together; either alone is an answer. The
    change is logged on the case it was made from, when there is one -
    an asset's identity is corrected in the middle of somebody's job,
    and that job's log is where anyone would look for it.
    """
    unknown = set(fields) - set(ASSET_IDENTIFICATION_FIELDS)
    if unknown:
        raise ValueError("not a §7.2 asset datum: %s" % ", ".join(sorted(unknown)))

    registration = (fields.get("registration_no") or "").strip()
    if registration and fields.get("is_unregistered"):
        raise ValidationError(
            _(
                "A registration number and 'not registered' contradict each "
                "other - record one or the other (§7.2)."
            )
        )

    changed = []
    for name, value in fields.items():
        if isinstance(value, str):
            value = value.strip()
        if getattr(asset, name) == value:
            continue
        setattr(asset, name, value)
        changed.append(name)
    if not changed:
        return asset
    asset.save(update_fields=changed)

    if case is not None:
        still_missing = asset.missing_identification()
        text = "§7.2: asset identification updated (%s)" % ", ".join(
            sorted(changed)
        )
        if still_missing:
            text += "; still missing: %s" % ", ".join(str(m) for m in still_missing)
        ActivityLog.objects.create(
            case=case,
            author=user,
            kind=ActivityLog.Kind.SYSTEM,
            text=text + ".",
        )
    return asset


def link_case(case: Case, client: Client, asset: Asset = None, user=None) -> Case:
    """
    Link a triaged case to a structured client (and optionally asset).

    Saves ONLY the ``client`` / ``asset`` FKs (the ``raw_*`` audit
    fields are never modified, GEN-03) and records the link in the
    case's activity log. Never touches ``case.status``.

    An asset already on the case is KEPT when none is named and it still
    belongs to the new client. Overwriting it unconditionally quietly
    detached an asset that §7.2 had identified and an intake record may
    already have been signed for; an asset belonging to the previous
    client is still dropped, because it is not this client's asset.

    §7.1: re-linking can invalidate an OWNER contact designation - the
    new client may have neither a phone number nor an e-mail - so the
    designation is re-checked here and retired if it no longer resolves
    to anybody. Retiring it rather than keeping it is the honest option:
    a designation that resolves to nothing sends nothing, and the guard
    should ask again rather than let the case go quiet.
    """
    if asset is not None and asset.client_id != client.pk:
        raise ValueError(
            "Asset #%s does not belong to client #%s." % (asset.pk, client.pk)
        )

    parts = ["Case linked to client '%s' (#%s)" % (client.name, client.pk)]
    kept_asset = None
    if asset is None and case.asset_id and case.asset.client_id == client.pk:
        kept_asset = case.asset
    elif asset is None and case.asset_id:
        parts.append(
            "asset '%s' (#%s) detached - it belongs to another client"
            % (case.asset.name, case.asset_id)
        )

    case.client = client
    case.asset = asset if asset is not None else kept_asset
    case.save(update_fields=["client", "asset", "updated_at"])

    if asset is not None:
        parts.append("asset '%s' (#%s)" % (asset.name, asset.pk))
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=" and ".join(parts) + " during triage.",
    )
    _revalidate_owner_contact(case, user)
    return case


def _revalidate_owner_contact(case: Case, user) -> None:
    """§7.1: an OWNER designation only means something while the owner it
    points at can be reached.

    ``record_contact_person`` refuses OWNER on a client with neither a
    phone nor an e-mail, but that check is made once, at designation
    time. Triage can move the case to a different client afterwards, and
    then the designation still reads OWNER while
    ``contact_details()`` resolves to nothing at all - every notification
    skipped with a log line nobody reads.
    """
    from apps.cases.models import ContactRole

    if case.contact_role != ContactRole.OWNER:
        return
    client = case.client
    if client is not None and (client.phone or client.email):
        return

    case.contact_role = ""
    case.save(update_fields=["contact_role"])
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "§7.1: the contact person was the owner, and the client now "
            "linked has neither a phone number nor an e-mail address - "
            "the designation is retired and has to be made again."
        ),
    )
