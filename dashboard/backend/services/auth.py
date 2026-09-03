"""FC-096 Phase D — the IAP roles layer.

Identity-Aware Proxy sits in front of the dashboard and, for every request it
admits, stamps a **signed** JWT on ``x-goog-iap-jwt-assertion``. This module is
the only thing in the repo that reads it, and the only thing that decides who
may write.

**Three rules, each of which exists because breaking it is a known way to lose
this migration:**

1. **The signed assertion, never the plain email header.** IAP also sets
   ``X-Goog-Authenticated-User-Email``, which is *not* signed. Any client that
   reaches the origin directly can set it to anything. The origin is behind IAP
   now — ``allUsers`` came off the invoker policy at the console session on
   2026-09-02, which is what closed FC-094 — so "directly" means an identity
   IAP already admitted, or a future misconfiguration that re-opens the door.
   Neither is a reason to trust it: a header-spoof would hand a viewer, or an
   anonymous caller on the day someone re-adds ``allUsers``, the operator role,
   and the perimeter would be the only thing standing between them and the
   write routes. The assertion is verified cryptographically, so forging it
   requires Google's private key.

2. **ES256, and `cryptography` must be installed.** IAP signs with ES256.
   ``google/auth/crypt/__init__.py`` does ``try: from google.auth.crypt import
   es256 / except ImportError: es256 = None``, and ``cryptography`` is an
   extras-only dependency of ``google-auth``. In an environment missing it,
   ``google.auth.crypt.es256`` is **None**, ES256 is absent from
   ``_ALGORITHM_TO_VERIFIER_CLASS``, and every verification fails — at request
   time, with nothing failing at import, at deploy, or in a smoke test. That is
   why both packages are pinned in ``dashboard/backend/requirements.txt`` AND
   ``cryptography`` in the root ``requirements.txt``, and why the test suite
   asserts ``google.auth.crypt.es256 is not None`` directly.

3. **Fail closed, and be loud about it.** A present-but-invalid assertion is a
   401 with a distinct log event — never a quiet fall-through to the legacy
   token gate, because a forged header or a broken audience configuration must
   surface as an alertable event rather than as "the token still works".

**"No assertion" is a refusal, not a fall-through** (PR-2). ``authorize_write``
still answers ``None`` for a request that carries no assertion, but that now
means only one thing: *nobody claimed to be anybody*. The **router** turns it
into a **401 naming IAP** and carrying both remedies — reload the page, or mint
an OIDC id-token for the IAP OAuth client. There is nothing behind it. PR-1's
third branch fell through to the legacy shared-bearer gate; PR-2 deleted that
gate, and this module reads no environment credential of any kind — the name of
the retired variable does not appear in this file, which is checkable by grep
and is checked by a test. The distinction is deliberate, and it is why the 401
lives in the router rather than here: on the
four write routes "nobody claimed to be anybody" is a refusal, and on
``pause-alert-check`` — where IAP admission *is* the authorization — it is a
pass. That is a route-class decision, which is not a thing this module knows.

Why ``None`` rather than raising: the two exempt routes share
``authenticate_only`` with the gated ones precisely so that what "invalid"
means, and what is safe to log when it happens, cannot drift between them.

**FastAPI-free on purpose.** This module raises its own ``IapAuthError`` and the
router translates it into an ``HTTPException``. The rules that decide who may
spend money are then testable in any environment, including the bot CI image,
which is the same reason ``services/sweeps.py`` is written the way it is.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Mapping as _MappingABC
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Doc-verified IAP constants (docs/plans/fc-096-d.md §Context).
# --------------------------------------------------------------------------- #

#: The header IAP stamps with the signed assertion. Lower-case because that is
#: how it appears on the wire and how Starlette normalises it; FastAPI's
#: ``Header`` parameter maps ``x_goog_iap_jwt_assertion`` onto it.
ASSERTION_HEADER = "x-goog-iap-jwt-assertion"

#: Where IAP publishes the ES256 public keys. **Not** the OAuth2 certs URL that
#: ``verify_token`` defaults to — that one serves the keys for Google *sign-in*
#: ID tokens, which is a different signer entirely, so leaving the default in
#: place would reject every genuine assertion.
PUBLIC_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"

#: The ``iss`` every IAP assertion carries. ``verify_token`` checks the
#: signature, ``iat``/``exp`` and ``aud`` but **not** the issuer, so this one is
#: checked here.
ISSUER = "https://cloud.google.com/iap"

#: Env var holding the expected ``aud``. For direct Cloud Run IAP the value is
#: ``/projects/<PROJECT_NUMBER>/locations/<REGION>/services/<SERVICE>`` — leading
#: slash, project NUMBER not id. It is deployed from ``cloudbuild.yaml`` rather
#: than derived, because deriving it needs the project number at runtime and a
#: wrong-but-plausible derivation fails closed *silently*.
AUDIENCE_ENV = "IAP_AUDIENCE"

#: Env var holding the write allowlist: **space-separated** emails. Commas are
#: not used because ``gcloud run deploy --set-env-vars`` treats a comma as the
#: separator between variables, so a two-operator comma value cannot be
#: deployed at all.
OPERATORS_ENV = "OPERATORS"


# --------------------------------------------------------------------------- #
# Errors — carrying the status, the operator-facing detail and the log event.
# --------------------------------------------------------------------------- #

class IapAuthError(Exception):
    """Base for every refusal this module issues.

    Carries the HTTP status and the detail string so the router stays a
    translation layer: the *decision* and the *message the operator reads* are
    both made here, where they are tested without FastAPI.
    """

    status_code = 401
    log_event = "iap_auth_error"

    def __init__(self, detail: str, reason: str = ""):
        super().__init__(detail)
        self.detail = detail
        #: The technical cause, for the log line only. Never returned to the
        #: caller: a verification failure's exact reason is a probing oracle.
        self.reason = reason or detail


class AssertionInvalid(IapAuthError):
    """A present assertion that did not verify. **401, always.**

    Never a fall-through to the token gate. A forged pre-flip header, an
    expired session or a broken ``IAP_AUDIENCE`` all land here, and all three
    are things an operator needs to see rather than have silently routed
    around.
    """

    status_code = 401
    log_event = "iap_assertion_invalid"


class AudienceUnconfigured(AssertionInvalid):
    """``IAP_AUDIENCE`` unset or blank on this revision.

    The assertion branch fails CLOSED: without the expected audience there is
    nothing to check ``aud`` against, and verifying a signature alone would
    admit an assertion minted by IAP for a *different* service. Distinct log
    event, because the remedy is a deploy-config fix rather than an
    investigation.
    """

    log_event = "iap_audience_unconfigured"


class OperatorsUnconfigured(IapAuthError):
    """``OPERATORS`` unset or empty on this revision.

    403 rather than 401 — the caller authenticated fine; the *service* has no
    allowlist. Deliberately NOT phrased as "you are a viewer": that message
    sends the operator to check their own account when the fault is a missing
    env var, and the recovery is a ``gcloud run services update
    --update-env-vars`` away.
    """

    status_code = 403
    log_event = "iap_operators_unconfigured"


class NotAnOperator(IapAuthError):
    """A valid identity that is not on the allowlist. 403, naming the mechanism.

    There is deliberately **no token fallback** here. Allowing a valid
    non-operator to present the legacy bearer token would mean one leaked token
    defeats the whole migration.
    """

    status_code = 403
    log_event = "iap_not_an_operator"


# --------------------------------------------------------------------------- #
# The verified identity.
# --------------------------------------------------------------------------- #

class Identity:
    """A verified IAP caller.

    ``email`` is the assertion's **bare** ``email`` claim —
    ``someone@example.com``, with no ``accounts.google.com:`` prefix. That
    prefix belongs to the plain ``X-Goog-Authenticated-User-Email`` header,
    which this module does not read; putting it in an ``OPERATORS`` value would
    silently match nothing.
    """

    __slots__ = ("email", "subject")

    def __init__(self, email: str, subject: Optional[str] = None):
        self.email = email
        self.subject = subject

    @property
    def key(self) -> str:
        """The comparison form: stripped and casefolded, both sides."""
        return self.email.strip().casefold()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Identity(email={self.email!r}, subject={self.subject!r})"

    def __eq__(self, other: Any) -> bool:
        return (isinstance(other, Identity)
                and other.email == self.email
                and other.subject == self.subject)

    def __hash__(self) -> int:
        # Defining `__eq__` sets `__hash__` to None, making the class
        # unhashable — so an `Identity` could not go in a set or be a dict key,
        # and the failure would appear at the first caller that tried, not here.
        # Kept consistent with `__eq__`: equal identities hash equally.
        return hash((self.email, self.subject))


# --------------------------------------------------------------------------- #
# Configuration readers. Read the env on EVERY call, never at import: an
# `--update-env-vars` recovery has to take effect on the new revision without
# anyone reasoning about when this module was first imported.
# --------------------------------------------------------------------------- #

def configured_audience() -> Optional[str]:
    """The expected ``aud``, or None when unset/blank."""
    return (os.getenv(AUDIENCE_ENV) or "").strip() or None


def _raw_operators() -> str:
    return os.getenv(OPERATORS_ENV) or ""


def operator_emails() -> frozenset:
    """The allowlist, parsed.

    ``str.split()`` with no argument, so any run of whitespace separates and
    stray leading/trailing space is absorbed — a value pasted with a trailing
    newline still works. Every entry is ``casefold()``ed, and so is the claim it
    is compared against, because email local-parts are case-insensitive in
    practice at every provider this will ever see and a capitalised invite
    should not be a lockout.
    """
    return frozenset(part.casefold() for part in _raw_operators().split() if part)


# --------------------------------------------------------------------------- #
# Verification.
# --------------------------------------------------------------------------- #

def _certs_request():
    """The transport used to FETCH the IAP public keys.

    A seam, extracted for one reason: the ES256 round-trip test needs to
    exercise the *real* verifier — the real ``certs_url``, the real audience
    wiring, the real ES256 code path — without making a network call, which the
    suite's ambient-environment discipline forbids. It replaces this function
    with a callable that serves a generated test key.
    """
    import google.auth.transport.requests

    return google.auth.transport.requests.Request()


def verify_assertion(assertion: str, audience: str) -> Mapping[str, Any]:
    """Verify signature, audience, ``iat``/``exp``; return the claims.

    Delegates to ``google.oauth2.id_token.verify_token`` with IAP's ``certs_url``
    — that call is what checks the ES256 signature against Google's published
    keys, that ``aud`` equals ``audience`` exactly, and that the token is neither
    expired nor issued in the future.

    **This function is the unit tests' injection point.** They replace it
    wholesale (``monkeypatch.setattr(auth, "verify_assertion", ...)``) so that
    the chain semantics can be exercised without a network fetch of the IAP
    keys. Callers must therefore reach it through the module attribute, never
    via a local alias captured at import.

    Raises whatever ``verify_token`` raises — every one of which the caller
    turns into ``AssertionInvalid``.
    """
    import google.oauth2.id_token

    # A HIDDEN SECOND BRANCH lives inside `verify_token`: if the certs response
    # contains a `keys` member it is treated as a JWKS document, and the
    # function then constructs its OWN `jwt.PyJWKClient(certs_url)` and fetches
    # the URL again itself — bypassing the `request` object entirely. Two
    # consequences worth knowing before anyone edits this:
    #   * it needs `pyjwt`, which this image does not install, so that branch
    #     would raise ImportError -> AssertionInvalid (fail closed, loudly);
    #   * the `_certs_request` seam would NOT contain it, so a test injecting a
    #     JWKS-shaped payload would make a real network call.
    # IAP publishes the x.509 dict format (`{kid: PEM}`) at PUBLIC_KEYS_URL
    # today, so the dict branch is the live one and the tests inject that shape.
    # If Google ever moves that endpoint to JWKS, this function needs `pyjwt`
    # pinned and a different seam — a failure that would present as every
    # assertion refused, immediately and everywhere.
    return google.oauth2.id_token.verify_token(
        assertion,
        _certs_request(),
        audience=audience,
        certs_url=PUBLIC_KEYS_URL,
    )


def assertion_fingerprint(assertion) -> str:
    """A stable, non-reversible 12-hex handle for one assertion.

    **This is what gets logged. The token never is, and neither does anything
    derived from it — including the verifier's own exception message.**

    google-auth's ``MalformedError`` embeds the FULL RAW TOKEN in its message
    (probed on this branch: a genuine signed assertion with ``.junk`` appended
    comes back with the whole thing verbatim). Interpolating that message into
    a log line puts a replayable credential at rest in Cloud Logging and then in
    BigQuery, readable by anyone with log access, for the retention period. And
    because the origin is world-reachable until the console flip, an anonymous
    caller can currently POST any header value they like and have it written to
    the sink — a write-anything channel dressed up as an auth failure.

    Twelve hex characters is enough to correlate a burst of failures with one
    another (same session, same forged header, same broken client) without
    being enough to reconstruct anything. Correlating a fingerprint back to a
    specific token requires already possessing that token.
    """
    if isinstance(assertion, str):
        data = assertion.encode("utf-8", "replace")
    elif isinstance(assertion, (bytes, bytearray)):
        data = bytes(assertion)
    else:  # pragma: no cover - defensive; callers pass str
        data = repr(assertion).encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()[:12]


def identity_from_assertion(assertion: str) -> Identity:
    """Verify an assertion and return the caller's identity.

    Raises ``AudienceUnconfigured`` when the revision has no ``IAP_AUDIENCE``,
    and ``AssertionInvalid`` for anything else that does not check out —
    including a valid-looking token from the wrong issuer or one with no
    ``email`` claim.
    """
    audience = configured_audience()
    if audience is None:
        raise AudienceUnconfigured(
            detail=(f"this revision cannot verify IAP assertions: {AUDIENCE_ENV} "
                    f"is unset or blank, so there is no expected audience to "
                    f"check `aud` against. Every assertion is refused until it "
                    f"is set. Expected value for this service: "
                    f"`/projects/<PROJECT_NUMBER>/locations/<REGION>/services/"
                    f"<SERVICE>` (leading slash, project NUMBER). Recovery: "
                    f"`gcloud run services update options-wheel-dashboard "
                    f"--update-env-vars {AUDIENCE_ENV}=...`."),
            reason=f"{AUDIENCE_ENV} unset or blank",
        )

    try:
        claims = verify_assertion(assertion, audience)
    except Exception as exc:  # noqa: BLE001 - every failure mode is a refusal
        # The EXCEPTION TYPE and a fingerprint, and deliberately nothing else.
        # `str(exc)` is not safe to log here: google-auth's `MalformedError`
        # quotes the full raw token back at you. See `assertion_fingerprint`.
        raise AssertionInvalid(
            detail=("the IAP assertion on this request could not be verified. "
                    "If your session has expired, reload the page to sign in "
                    "again."),
            reason=(f"{type(exc).__name__} "
                    f"(assertion sha256:{assertion_fingerprint(assertion)})"),
        )

    if not isinstance(claims, _MappingABC):  # pragma: no cover - library contract
        raise AssertionInvalid(
            detail="the IAP assertion on this request could not be verified.",
            reason=f"verifier returned {type(claims).__name__}, not a mapping",
        )

    issuer = claims.get("iss")
    if issuer != ISSUER:
        # `verify_token` does not check `iss`. Without this, any Google-signed
        # token whose `aud` happened to match would be accepted.
        raise AssertionInvalid(
            detail="the IAP assertion on this request could not be verified.",
            reason=f"issuer {issuer!r} is not {ISSUER!r}",
        )

    email = str(claims.get("email") or "").strip()
    if not email:
        raise AssertionInvalid(
            detail=("the IAP assertion on this request carries no `email` "
                    "claim, so the caller cannot be matched against the "
                    "OPERATORS allowlist."),
            reason="assertion carries no `email` claim",
        )

    return Identity(email=email, subject=claims.get("sub"))


# --------------------------------------------------------------------------- #
# The write chain.
# --------------------------------------------------------------------------- #

def authenticate_only(assertion: Optional[str]) -> Optional[Identity]:
    """**Verify when present; never authorize.** Two outcomes only:

    * **assertion ABSENT** → ``None``. Nothing was claimed, so nothing is
      checked. Pre-flip that is every request; post-flip it cannot happen on a
      request IAP itself admitted.
    * **assertion PRESENT** → the verified ``Identity``, or ``AssertionInvalid``
      (401).

    This is the whole gate for a route EXEMPT from the OPERATORS chain
    (``POST /bot-health/pause-alert-check``): its authorization is IAP
    admission, so there is nothing further to decide. But "exempt from the role
    check" must not decay into "never looks at the header". A route that
    ignored a present-but-invalid assertion would be the one place in this
    service where a forged header is silently accepted — and it is the place a
    prober finds FIRST, precisely because it is the documented exception.

    Every refusal is logged HERE rather than at the call site, so a route that
    acquires the gate cannot forget the log line.
    """
    if not assertion or not assertion.strip():
        return None

    try:
        return identity_from_assertion(assertion.strip())
    except IapAuthError as exc:
        logger.warning("%s: %s", exc.log_event, exc.reason)
        raise


def authorize_write(assertion: Optional[str]) -> Optional[Identity]:
    """The Phase D write gate. Three outcomes, pinned by the plan:

    * **assertion ABSENT** → ``None``, meaning "nobody claimed to be anybody".
      The ROUTER answers that with a **401 naming IAP** on every write route —
      there is nothing to fall through to, and this module reads no credential
      of any other kind. (PR-1 fell through here to the legacy shared-bearer
      gate; PR-2 deleted it. A retired credential path that still compiles is
      a path something can call again.) It is returned rather than raised because the
      two exempt routes answer the same fact by proceeding, and which of those
      is right is a route-class decision the router makes.
    * **assertion PRESENT and INVALID** → ``AssertionInvalid`` (401). Never the
      token path.
    * **assertion PRESENT and VALID** → the ``OPERATORS`` check. A non-operator
      gets ``NotAnOperator`` (403) and the bearer token is **ignored**; an empty
      allowlist gets ``OperatorsUnconfigured`` (403). An operator gets their
      ``Identity`` back and the token is not consulted at all.

    The verify half is ``authenticate_only`` — shared rather than repeated, so
    the exempt route and the gated ones cannot drift on what "invalid" means,
    on what gets logged when it happens, or on what is safe to put in the log.
    """
    identity = authenticate_only(assertion)
    if identity is None:
        return None

    operators = operator_emails()
    if not operators:
        exc = OperatorsUnconfigured(
            detail=(f"writes are disabled on this revision: {OPERATORS_ENV} is "
                    f"unset or empty, so no identity is authorised to write. "
                    f"This is a service configuration problem, not a problem "
                    f"with your account. Recovery: `gcloud run services update "
                    f"options-wheel-dashboard --update-env-vars "
                    f"{OPERATORS_ENV}='a@example.com b@example.com'` — a new "
                    f"revision, no build."),
            reason=f"{OPERATORS_ENV} parsed to 0 entries",
        )
        logger.warning("%s: %s", exc.log_event, exc.reason)
        raise exc

    if identity.key not in operators:
        detail = (f"{identity.email} is signed in and may read everything, "
                  f"but writes are limited to the accounts listed in this "
                  f"service's {OPERATORS_ENV} allowlist. Ask an operator to "
                  f"add you: `gcloud run services update "
                  f"options-wheel-dashboard --update-env-vars "
                  f"{OPERATORS_ENV}='<existing> {identity.email}'`.")
        if any("," in entry for entry in operators):
            # The one way this 403 lies to an operator who IS on the list: a
            # comma-joined value parses to a single token that matches nobody,
            # so the account that owns the service is told it is a viewer.
            # `str.split()` can never yield zero entries from a non-blank value,
            # so this is the branch where the mistake actually surfaces.
            detail += (f" (NOTE: {OPERATORS_ENV} on this revision contains a "
                       f"comma. It is SPACE-separated — `a@x.com,b@x.com` is "
                       f"read as ONE address and matches nobody. Commas are not "
                       f"usable here at all: `--set-env-vars` treats a comma as "
                       f"the separator between VARIABLES.)")
        exc = NotAnOperator(
            detail=detail,
            reason=f"{identity.email} is not in {OPERATORS_ENV}",
        )
        logger.warning("%s: %s", exc.log_event, exc.reason)
        raise exc

    return identity
