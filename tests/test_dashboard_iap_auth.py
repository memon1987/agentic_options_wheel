"""FC-096 Phase D PR-1 — the IAP roles layer.

Four things are under test here, and they fail in very different ways:

1. **`services/auth.py`'s chain semantics** — absent / invalid / valid-viewer /
   valid-operator, plus the two fail-closed postures (`IAP_AUDIENCE` blank,
   `OPERATORS` empty). Pure; no FastAPI, no network.
2. **A REAL ES256 round trip.** IAP signs with ES256, and google-auth's ES256
   verifier binds to ``None`` when ``cryptography`` is absent — silently, so
   nothing fails until an operator is locked out at cutover. One test generates
   a P-256 keypair, mints a genuine assertion, serves the public key through
   the verifier's own certs seam, and asserts ``google.auth.crypt.es256 is not
   None`` directly. **No network**: the key fetch is injected.
3. **The router wiring** — that all four write routes go through the chain,
   that the header actually binds through FastAPI, and that the two exempt
   routes are genuinely exempt.
4. **That the `SWEEP_SUBMIT_TOKEN` path is GONE** (PR-2). PR-1's third
   branch — no assertion falls through to the bearer token — is deleted, so a
   write with no assertion is a **401 naming IAP**, on all four routes, with
   the detail string pinned here because an operator runbook quotes it. The
   sharp test is `TestTheTokenIsRetired`: the env var is SET and the old bearer
   header presented, and the answer is still 401. That case is not
   hypothetical — the operator unbinds the secret AFTER this deploys, in that
   order, because deleting it first bricks the next revision's startup.

Network discipline: every test either injects the verifier or injects the certs
transport. Nothing here reaches the IAP keys endpoint, GCP, or BigQuery.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from tests._dashboard_path import (  # noqa: E402
    HAS_FASTAPI,
    HAS_TESTCLIENT,
    add_dashboard_backend_to_path,
)

add_dashboard_backend_to_path()

from services import auth as A  # noqa: E402

# Same aliasing rationale as tests/test_dashboard_sweeps.py: a guard on FastAPI
# is not a guard on the dashboard's dependency set. Anything reaching for
# `fastapi.testclient` (which is built on httpx) gates on `_HAS_TESTCLIENT`.
_HAS_FASTAPI = HAS_FASTAPI
_HAS_TESTCLIENT = HAS_TESTCLIENT

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The audience the deployed revision is configured with. Written out here
#: rather than imported so that a change to `cloudbuild.yaml` has to be a
#: deliberate edit in two places, one of which is a test.
DEPLOYED_AUDIENCE = ("/projects/799970961417/locations/us-central1/services/"
                     "options-wheel-dashboard")

OPERATOR = "zeshan@tkzmgroup.com"
VIEWER = "someone.else@example.com"
#: The scheduler's identity once IAP is on: admitted by IAM, never on an
#: allowlist of human operators.
SCHEDULER_SA = "799970961417-compute@developer.gserviceaccount.com"
#: The automation service account. It IS an operator (FC-096 Phase D PR-2,
#: review round 1 F4): `POST /sims/run`, `POST /sims/pins` and
#: `DELETE /sims/pins/{id}` have no frontend caller, so after the bearer token
#: is retired an impersonated id-token for this SA is their only credential.
AUTOMATION_SA = "claude-operator@gen-lang-client-0607444019.iam.gserviceaccount.com"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def claims(email=OPERATOR, *, iss=A.ISSUER, aud=DEPLOYED_AUDIENCE, sub="sub-1",
           **extra):
    """A verified-claims mapping of the shape `verify_token` returns."""
    out = {"iss": iss, "aud": aud, "email": email, "sub": sub}
    out.update(extra)
    return out


def stub_verifier(monkeypatch, result=None, *, raises=None, record=None):
    """Replace `auth.verify_assertion` — the module's declared injection point.

    The real one fetches Google's public keys over the network on every call,
    which the suite's ambient-environment discipline forbids. The ES256 round
    trip below is the one test that exercises the real verifier, and it injects
    the certs transport instead.
    """
    def fake(assertion, audience):
        if record is not None:
            record.append((assertion, audience))
        if raises is not None:
            raise raises
        return result if result is not None else claims()

    monkeypatch.setattr(A, "verify_assertion", fake)
    return fake


def verifier_must_not_be_called(monkeypatch):
    """A verifier that fails the test if anything reaches it.

    Used by the no-op probe: "the assertion branch is not entered" is a
    stronger and more durable claim than "the status code happened to match".
    """
    def bomb(assertion, audience):  # pragma: no cover - the point is it is not called
        raise AssertionError(
            "the assertion branch was entered on a request that carried no "
            "IAP assertion — the PR-1 no-op property is broken")

    monkeypatch.setattr(A, "verify_assertion", bomb)


def configure(monkeypatch, *, audience=DEPLOYED_AUDIENCE, operators=OPERATOR):
    """Set (or delete) the two env vars the roles layer reads."""
    for name, value in ((A.AUDIENCE_ENV, audience), (A.OPERATORS_ENV, operators)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


# ============================================================================ #
# 1. OPERATORS parsing and posture
# ============================================================================ #

class TestOperatorsParsing:
    """Space-separated, casefolded, stripped — and empty means empty."""

    @pytest.mark.parametrize("raw,expected", [
        ("a@x.com", {"a@x.com"}),
        ("a@x.com b@x.com", {"a@x.com", "b@x.com"}),
        # Runs of whitespace, and a value pasted with surrounding space.
        ("  a@x.com   b@x.com  ", {"a@x.com", "b@x.com"}),
        ("a@x.com\tb@x.com\nc@x.com", {"a@x.com", "b@x.com", "c@x.com"}),
        # Casefolded on the CONFIG side.
        ("Zeshan@TKZMGroup.com", {"zeshan@tkzmgroup.com"}),
        ("", set()),
        ("   ", set()),
    ])
    def test_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv(A.OPERATORS_ENV, raw)
        assert set(A.operator_emails()) == expected

    def test_unset_is_empty(self, monkeypatch):
        monkeypatch.delenv(A.OPERATORS_ENV, raising=False)
        assert A.operator_emails() == frozenset()

    def test_a_comma_joined_value_fails_closed(self, monkeypatch):
        """The documented footgun, pinned as fail-CLOSED rather than fail-open.

        `--set-env-vars` uses commas to separate VARIABLES, which is why the
        allowlist is space-separated. If someone sets a comma-joined value
        anyway it parses to a single token that matches no email, and every
        write is refused. The one thing that must never happen is the reverse —
        a parse that admits somebody unintended.
        """
        monkeypatch.setenv(A.OPERATORS_ENV, "a@x.com,b@x.com")
        parsed = A.operator_emails()
        assert parsed == frozenset({"a@x.com,b@x.com"})
        assert "a@x.com" not in parsed
        assert "b@x.com" not in parsed

    def test_the_comma_case_diagnoses_itself_in_the_403(self, monkeypatch):
        """...and when it locks the ONLY operator out, the 403 says why.

        `str.split()` can never return zero entries for a non-blank value, so
        the comma mistake surfaces as "you are not an operator" told to the
        person who owns the service. Without the hint they would go looking at
        their Google account, which is the wrong place entirely.
        """
        configure(monkeypatch, operators=f"{OPERATOR},someone@else.com")
        stub_verifier(monkeypatch, result=claims(email=OPERATOR))
        with pytest.raises(A.NotAnOperator) as exc:
            A.authorize_write("an-assertion")
        assert "SPACE-separated" in exc.value.detail
        assert "comma" in exc.value.detail

    def test_a_clean_allowlist_carries_no_comma_noise(self, monkeypatch):
        """The hint appears only when a comma is actually present."""
        configure(monkeypatch, operators=OPERATOR)
        stub_verifier(monkeypatch, result=claims(email=VIEWER))
        with pytest.raises(A.NotAnOperator) as exc:
            A.authorize_write("an-assertion")
        assert "comma" not in exc.value.detail


class TestTheEmptyOperatorsPosture:
    """Empty allowlist ⇒ 403 that blames the REVISION, never the caller."""

    @pytest.mark.parametrize("operators", [None, "", "   "])
    def test_403_naming_the_env_var(self, monkeypatch, operators):
        configure(monkeypatch, operators=operators)
        stub_verifier(monkeypatch)
        with pytest.raises(A.OperatorsUnconfigured) as exc:
            A.authorize_write("an-assertion")
        assert exc.value.status_code == 403
        assert "OPERATORS" in exc.value.detail
        assert "unset or empty" in exc.value.detail
        # The prescribed wording: never "you are a viewer" — that sends the
        # operator to audit their own account over a missing env var.
        assert "viewer" not in exc.value.detail.lower()
        # And the recovery path is in the message, because it is a
        # `services update` away rather than a build.
        assert "--update-env-vars" in exc.value.detail

    def test_it_is_a_distinct_log_event(self, monkeypatch, caplog):
        configure(monkeypatch, operators=None)
        stub_verifier(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=A.logger.name):
            with pytest.raises(A.OperatorsUnconfigured):
                A.authorize_write("an-assertion")
        assert any("iap_operators_unconfigured" in r.getMessage()
                   for r in caplog.records)


# ============================================================================ #
# 2. IAP_AUDIENCE posture
# ============================================================================ #

class TestTheAudienceUnsetPosture:
    """No expected audience ⇒ the assertion branch fails CLOSED.

    Verifying a signature alone is not enough: IAP signs assertions for every
    service it fronts with the same keys, so without an `aud` to check, an
    assertion minted for a DIFFERENT service verifies here.
    """

    @pytest.mark.parametrize("audience", [None, "", "   "])
    def test_every_assertion_is_refused(self, monkeypatch, audience):
        configure(monkeypatch, audience=audience)
        verifier_calls = []
        stub_verifier(monkeypatch, record=verifier_calls)
        with pytest.raises(A.AudienceUnconfigured) as exc:
            A.authorize_write("an-assertion")
        assert exc.value.status_code == 401
        assert A.AUDIENCE_ENV in exc.value.detail
        # Refused BEFORE the verifier runs — there is nothing to check against.
        assert verifier_calls == []

    def test_it_is_an_assertion_invalid_subclass(self):
        """So a caller matching on the 401 class cannot miss this case."""
        assert issubclass(A.AudienceUnconfigured, A.AssertionInvalid)

    def test_it_is_a_distinct_log_event(self, monkeypatch, caplog):
        configure(monkeypatch, audience=None)
        with caplog.at_level(logging.WARNING, logger=A.logger.name):
            with pytest.raises(A.AudienceUnconfigured):
                A.authorize_write("an-assertion")
        events = [r.getMessage() for r in caplog.records]
        assert any("iap_audience_unconfigured" in m for m in events), events
        # Distinct: it must NOT be reported as a generic invalid assertion, or
        # a misconfigured deploy looks identical to a forged header.
        assert not any("iap_assertion_invalid:" in m for m in events), events

    def test_configured_audience_reads_the_env_every_call(self, monkeypatch):
        """No import-time capture: an `--update-env-vars` fix has to take."""
        monkeypatch.delenv(A.AUDIENCE_ENV, raising=False)
        assert A.configured_audience() is None
        monkeypatch.setenv(A.AUDIENCE_ENV, DEPLOYED_AUDIENCE)
        assert A.configured_audience() == DEPLOYED_AUDIENCE


# ============================================================================ #
# 3. The chain, on a stubbed verifier
# ============================================================================ #

class TestTheChain:

    @pytest.mark.parametrize("assertion", [None, "", "   ", "\n"])
    def test_absent_assertion_returns_none_and_never_verifies(
            self, monkeypatch, assertion):
        """The PR-1 no-op branch, at the source."""
        configure(monkeypatch)
        verifier_must_not_be_called(monkeypatch)
        assert A.authorize_write(assertion) is None

    def test_a_verifier_failure_is_401_never_a_token_fallback(self, monkeypatch):
        configure(monkeypatch)
        stub_verifier(monkeypatch, raises=ValueError("Token expired, 1 < 2"))
        with pytest.raises(A.AssertionInvalid) as exc:
            A.authorize_write("an-assertion")
        assert exc.value.status_code == 401

    def test_the_verifier_message_reaches_neither_the_caller_nor_the_log(
            self, monkeypatch, caplog):
        """Only the exception TYPE and a fingerprint are recorded.

        The caller gets a friendly message — returning "Token has wrong audience
        X, expected Y" to an unauthenticated caller hands them a configuration
        oracle. The LOG gets no more, which is the security fix: google-auth's
        `MalformedError` quotes the raw token back in its message (see
        `TestTheLogNeverCarriesTheToken`).
        """
        configure(monkeypatch)
        stub_verifier(monkeypatch,
                      raises=ValueError("Token has wrong audience /projects/1/..."))
        with caplog.at_level(logging.WARNING, logger=A.logger.name):
            with pytest.raises(A.AssertionInvalid) as exc:
                A.authorize_write("an-assertion")
        assert "wrong audience" not in exc.value.detail
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "iap_assertion_invalid" in logged
        assert "ValueError" in logged
        # The verifier's message is NOT in the log either.
        assert "wrong audience" not in logged, logged
        assert "/projects/1/" not in logged, logged

    def test_a_wrong_issuer_is_invalid(self, monkeypatch):
        """`verify_token` checks signature, aud and expiry — NOT `iss`."""
        configure(monkeypatch)
        stub_verifier(monkeypatch,
                      result=claims(iss="https://accounts.google.com"))
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write("an-assertion")

    @pytest.mark.parametrize("bad_email", [None, "", "   "])
    def test_a_missing_email_claim_is_invalid(self, monkeypatch, bad_email):
        configure(monkeypatch)
        stub_verifier(monkeypatch, result=claims(email=bad_email))
        with pytest.raises(A.AssertionInvalid) as exc:
            A.authorize_write("an-assertion")
        assert "email" in exc.value.detail

    def test_a_valid_operator_is_authorised(self, monkeypatch):
        configure(monkeypatch)
        stub_verifier(monkeypatch, result=claims(email=OPERATOR))
        identity = A.authorize_write("an-assertion")
        assert isinstance(identity, A.Identity)
        assert identity.email == OPERATOR
        assert identity.subject == "sub-1"

    def test_the_assertion_is_stripped_before_verification(self, monkeypatch):
        configure(monkeypatch)
        record = []
        stub_verifier(monkeypatch, record=record)
        A.authorize_write("  an-assertion\n")
        assert record == [("an-assertion", DEPLOYED_AUDIENCE)]

    def test_the_configured_audience_is_what_is_verified_against(self, monkeypatch):
        configure(monkeypatch, audience="/projects/1/locations/r/services/s")
        record = []
        stub_verifier(monkeypatch, record=record)
        A.authorize_write("an-assertion")
        assert record[0][1] == "/projects/1/locations/r/services/s"

    @pytest.mark.parametrize("claim_email,configured", [
        # Case-insensitive on BOTH sides.
        ("Zeshan@TKZMGroup.com", OPERATOR),
        (OPERATOR, "ZESHAN@TKZMGROUP.COM"),
        # Whitespace absorbed on both sides.
        ("  zeshan@tkzmgroup.com  ", "  zeshan@tkzmgroup.com  "),
    ])
    def test_matching_is_casefolded_and_stripped_on_both_sides(
            self, monkeypatch, claim_email, configured):
        configure(monkeypatch, operators=configured)
        stub_verifier(monkeypatch, result=claims(email=claim_email))
        assert A.authorize_write("an-assertion") is not None

    def test_a_viewer_gets_403_naming_the_mechanism(self, monkeypatch):
        configure(monkeypatch)
        stub_verifier(monkeypatch, result=claims(email=VIEWER))
        with pytest.raises(A.NotAnOperator) as exc:
            A.authorize_write("an-assertion")
        assert exc.value.status_code == 403
        assert A.OPERATORS_ENV in exc.value.detail
        assert VIEWER in exc.value.detail
        # It says what the viewer CAN do, so a 403 is not read as "broken".
        assert "read" in exc.value.detail.lower()

    def test_a_viewer_is_a_distinct_log_event(self, monkeypatch, caplog):
        configure(monkeypatch)
        stub_verifier(monkeypatch, result=claims(email=VIEWER))
        with caplog.at_level(logging.WARNING, logger=A.logger.name):
            with pytest.raises(A.NotAnOperator):
                A.authorize_write("an-assertion")
        assert any("iap_not_an_operator" in r.getMessage()
                   for r in caplog.records)

    def test_the_email_claim_is_bare_not_prefixed(self, monkeypatch):
        """`accounts.google.com:` belongs to the PLAIN header, not the JWT.

        An OPERATORS value carrying that prefix would match nothing, so the
        allowlist is compared against the bare claim and this test documents it.
        """
        configure(monkeypatch, operators=f"accounts.google.com:{OPERATOR}")
        stub_verifier(monkeypatch, result=claims(email=OPERATOR))
        with pytest.raises(A.NotAnOperator):
            A.authorize_write("an-assertion")


class TestTheLogNeverCarriesTheToken:
    """An IAP assertion is a live credential. It must never reach the log sink.

    google-auth's ``MalformedError`` embeds the **full raw token** in its
    message — take a genuine signed assertion, append ``.junk``, and the whole
    thing comes back verbatim. Interpolating that message into a WARNING puts a
    replayable credential at rest in Cloud Logging and then in BigQuery, for the
    retention period, readable by anyone with log access.

    It is also, until the console flip, an anonymous **write-anything** channel:
    the origin is world-reachable, so a stranger can put any bytes they like in
    the header and have them written to the sink.

    So the rule is absolute — the logged reason is the exception TYPE plus a
    12-hex SHA-256 fingerprint, and nothing else.
    """

    #: Long enough that a substring scan is meaningful, and shaped like a JWT.
    ASSERTION = ("eyJhbGciOiJFUzI1NiIsImtpZCI6InNlY3JldC1raWQifQ"
                 ".eyJlbWFpbCI6ImF0dGFja2VyQGV2aWwuY29tIn0"
                 ".c2lnbmF0dXJlLWJ5dGVzLXRoYXQtbXVzdC1uZXZlci1iZS1sb2dnZWQ")

    def _log(self, monkeypatch, caplog, exc, assertion=None):
        configure(monkeypatch)
        stub_verifier(monkeypatch, raises=exc)
        with caplog.at_level(logging.WARNING, logger=A.logger.name):
            with pytest.raises(A.AssertionInvalid):
                A.authorize_write(assertion or self.ASSERTION)
        return "\n".join(r.getMessage() for r in caplog.records)

    def test_a_malformed_error_quoting_the_whole_token_logs_none_of_it(
            self, monkeypatch, caplog):
        """The exact probe from review, reproduced as a regression test."""
        message = f"Can't parse segment: {self.ASSERTION}.junk"
        logged = self._log(monkeypatch, caplog, ValueError(message))
        assert self.ASSERTION not in logged
        assert "Can't parse segment" not in logged

    def test_no_substring_of_the_assertion_appears_anywhere_in_the_log(
            self, monkeypatch, caplog):
        """The strong form: not the token, not a chunk of it, not a segment.

        Scanned in 16-character windows — long enough that a coincidence is
        implausible and short enough to catch a partial leak (a truncated
        message, one JWT segment, the header alone).
        """
        logged = self._log(monkeypatch, caplog,
                           ValueError(f"bad token {self.ASSERTION}"))
        windows = [self.ASSERTION[i:i + 16]
                   for i in range(0, len(self.ASSERTION) - 16)]
        leaked = [w for w in windows if w in logged]
        assert not leaked, f"{len(leaked)} fragment(s) of the assertion leaked: {leaked[:3]}"
        # ...and the three JWT segments individually.
        for segment in self.ASSERTION.split("."):
            assert segment not in logged, segment

    def test_the_exception_type_and_a_fingerprint_are_what_is_logged(
            self, monkeypatch, caplog):
        """Sanitised, not silent. A burst of failures still correlates."""
        logged = self._log(monkeypatch, caplog, ValueError("anything at all"))
        assert "iap_assertion_invalid" in logged
        assert "ValueError" in logged
        assert A.assertion_fingerprint(self.ASSERTION) in logged
        # The message itself is gone.
        assert "anything at all" not in logged

    def test_the_detail_returned_to_the_caller_is_also_clean(self, monkeypatch):
        configure(monkeypatch)
        stub_verifier(monkeypatch,
                      raises=ValueError(f"raw {self.ASSERTION} here"))
        with pytest.raises(A.AssertionInvalid) as exc:
            A.authorize_write(self.ASSERTION)
        assert self.ASSERTION not in exc.value.detail
        # And no fingerprint either: the caller learns nothing they did not send.
        assert A.assertion_fingerprint(self.ASSERTION) not in exc.value.detail

    def test_the_same_assertion_fingerprints_the_same_way(self):
        """Correlation is the whole point of keeping anything at all."""
        assert (A.assertion_fingerprint(self.ASSERTION)
                == A.assertion_fingerprint(self.ASSERTION))
        assert (A.assertion_fingerprint(self.ASSERTION)
                != A.assertion_fingerprint(self.ASSERTION + "x"))

    def test_the_fingerprint_is_twelve_hex_and_not_reversible_in_shape(self):
        fp = A.assertion_fingerprint(self.ASSERTION)
        assert len(fp) == 12
        assert all(c in "0123456789abcdef" for c in fp), fp
        # It is a PREFIX of the sha256, so it cannot contain token bytes.
        import hashlib
        assert hashlib.sha256(self.ASSERTION.encode()).hexdigest().startswith(fp)

    def test_it_survives_a_non_utf8_and_a_non_string_assertion(self):
        """Header values are attacker-controlled; hashing must not raise.

        An exception escaping the *logging* path would turn a refusal into a
        500 — and a 500 on the auth path is an availability bug reachable by
        anyone.
        """
        assert len(A.assertion_fingerprint("\udcff\udcfe")) == 12
        assert len(A.assertion_fingerprint(b"\xff\xfe")) == 12
        assert len(A.assertion_fingerprint(None)) == 12

    def test_the_router_401_body_carries_nothing_from_the_assertion(
            self, monkeypatch):
        """End-to-end: what the HTTP client actually receives."""
        if not _HAS_FASTAPI:
            pytest.skip("needs FastAPI (routers.v2)")
        import routers.v2 as v2
        from fastapi import HTTPException

        configure(monkeypatch)
        stub_verifier(monkeypatch,
                      raises=ValueError(f"Can't parse segment: {self.ASSERTION}"))
        with pytest.raises(HTTPException) as exc:
            v2._require_write_access(self.ASSERTION)
        assert exc.value.status_code == 401
        assert self.ASSERTION not in str(exc.value.detail)


class TestTheSignedAssertionIsTheOnlyIdentitySource:
    """The plain `X-Goog-Authenticated-User-Email` header must never be read.

    It is NOT signed, and today's origin is world-reachable, so reading it
    anywhere would let an anonymous caller hand themselves the operator role.
    """

    def test_the_plain_email_header_appears_nowhere_in_the_auth_layer(self):
        text = (REPO_ROOT / "dashboard" / "backend" / "services" / "auth.py").read_text()
        # It is referenced in the module docstring precisely to say it is not
        # read; what must not exist is a lookup of it.
        for forbidden in ('os.getenv("HTTP_X_GOOG_AUTHENTICATED',
                          'headers.get("x-goog-authenticated',
                          'headers["x-goog-authenticated'):
            assert forbidden.lower() not in text.lower(), forbidden

    def test_no_router_binds_the_plain_email_header(self):
        routers = REPO_ROOT / "dashboard" / "backend" / "routers"
        for path in sorted(routers.glob("*.py")):
            text = path.read_text().lower()
            assert "x_goog_authenticated_user_email" not in text, path
            assert "x-goog-authenticated-user-email" not in text, path


# ============================================================================ #
# 4. The REAL ES256 round trip — the silent-None regression
# ============================================================================ #

class _FakeCertsResponse:
    status = 200

    def __init__(self, payload):
        self.data = json.dumps(payload).encode("utf-8")


class _FakeCertsRequest:
    """Stands in for `google.auth.transport.requests.Request`.

    `_fetch_certs` calls it as `request(certs_url, method="GET")` and reads
    `.status` / `.data`. Injecting here — rather than stubbing the verifier —
    is what makes this an end-to-end exercise of the REAL ES256 code path with
    no network.
    """

    def __init__(self, certs):
        self.certs = certs
        self.urls = []

    def __call__(self, url, method="GET", **kwargs):
        assert method == "GET", method
        self.urls.append(url)
        return _FakeCertsResponse(self.certs)


def _has_es256():
    try:
        import google.auth.crypt  # noqa: F401
    except Exception:  # pragma: no cover
        return False
    import google.auth.crypt as crypt
    return crypt.es256 is not None


def test_es256_is_bound():
    """`google.auth.crypt.es256` must not be None. **Never skipped.**

    This is THE regression both round-1 reviews found independently. google-auth
    swallows the ImportError when `cryptography` is missing and binds this
    attribute to None; ES256 then never enters `_ALGORITHM_TO_VERIFIER_CLASS`
    and every IAP assertion fails to verify at REQUEST time — with nothing
    failing at import, at deploy, or in a smoke test. Reproduced in this repo's
    dev venv (google-auth 2.43.0, cryptography absent) before PR-1 pinned it.

    Deliberately a module-level test rather than a member of the round-trip
    class below: that class has to skip when ES256 is unavailable, and a skip is
    not a guard. The failure mode here is an environment silently losing the
    dependency, which is exactly the case a skip would hide.
    """
    import google.auth.crypt

    assert google.auth.crypt.es256 is not None, (
        "google-auth cannot verify ES256 in this environment, so every IAP "
        "assertion would be refused at request time. Install `cryptography` — "
        "it is pinned in dashboard/backend/requirements.txt and declared in the "
        "root requirements.txt for exactly this reason.")


@pytest.mark.skipif(not _has_es256(),
                    reason="google-auth's ES256 verifier is unavailable "
                           "(cryptography not installed)")
class TestTheEs256RoundTrip:
    """One test class, one purpose: prove ES256 assertions actually verify.

    Every other test in this file stubs the verifier, which is the right trade
    for chain semantics but would happily pass in an environment where ES256
    verification cannot work at all. That environment is not hypothetical —
    it was this repo's dev venv before this PR pinned `cryptography`.
    """

    AUDIENCE = DEPLOYED_AUDIENCE
    KID = "test-key-id"

    @staticmethod
    def _keypair():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        return key, pem

    @classmethod
    def _mint(cls, key, payload, *, kid=None):
        import google.auth.jwt
        from google.auth.crypt import es256

        signer = es256.ES256Signer(key, key_id=kid or cls.KID)
        return google.auth.jwt.encode(signer, payload).decode("utf-8")

    @staticmethod
    def _now():
        import calendar
        import datetime

        return calendar.timegm(datetime.datetime.utcnow().utctimetuple())

    @classmethod
    def _payload(cls, *, email=OPERATOR, aud=None, iss=A.ISSUER,
                 iat_offset=-60, exp_offset=900):
        now = cls._now()
        return {"iss": iss, "aud": aud if aud is not None else cls.AUDIENCE,
                "email": email, "sub": "1234567890",
                "iat": now + iat_offset, "exp": now + exp_offset}

    @pytest.fixture
    def wired(self, monkeypatch):
        """A real keypair, a real ES256 assertion, injected certs."""
        key, pem = self._keypair()
        transport = _FakeCertsRequest({self.KID: pem})
        monkeypatch.setattr(A, "_certs_request", lambda: transport)
        configure(monkeypatch, audience=self.AUDIENCE, operators=OPERATOR)
        return key, transport

    # -- the alg on the wire ------------------------------------------------
    def test_the_alg_really_is_es256(self, wired):
        """Not RS256 by accident: the header decides which verifier runs."""
        import google.auth.jwt

        key, _ = wired
        token = self._mint(key, self._payload())
        header = google.auth.jwt.decode_header(token)
        assert header["alg"] == "ES256", header
        assert header["kid"] == self.KID, header

    # -- the happy path ----------------------------------------------------
    def test_a_genuine_assertion_verifies_end_to_end(self, wired):
        key, transport = wired
        token = self._mint(key, self._payload())
        identity = A.authorize_write(token)
        assert identity is not None
        assert identity.email == OPERATOR
        assert identity.subject == "1234567890"
        # And it fetched the keys from IAP's endpoint, not the OAuth2 default —
        # a wrong certs_url rejects every genuine assertion.
        assert transport.urls == [A.PUBLIC_KEYS_URL]
        assert transport.urls[0] == (
            "https://www.gstatic.com/iap/verify/public_key")

    def test_a_genuine_viewer_assertion_verifies_then_403s(self, wired):
        """Signature good, identity good, allowlist says no."""
        key, _ = wired
        token = self._mint(key, self._payload(email=VIEWER))
        with pytest.raises(A.NotAnOperator):
            A.authorize_write(token)

    # -- the refusal matrix, all on real signatures -------------------------
    def test_an_expired_assertion_is_refused(self, wired):
        key, _ = wired
        token = self._mint(key, self._payload(iat_offset=-7200,
                                              exp_offset=-3600))
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(token)

    def test_an_assertion_from_the_future_is_refused(self, wired):
        key, _ = wired
        token = self._mint(key, self._payload(iat_offset=3600,
                                              exp_offset=7200))
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(token)

    def test_a_wrong_audience_assertion_is_refused(self, wired):
        """The cross-service case: IAP signs for every service it fronts."""
        key, _ = wired
        token = self._mint(key, self._payload(
            aud="/projects/799970961417/locations/us-central1/services/"
                "some-other-service"))
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(token)

    def test_an_audience_missing_its_leading_slash_is_refused(self, wired):
        """The documented spelling trap; `aud` is compared exactly."""
        key, _ = wired
        token = self._mint(key, self._payload(aud=self.AUDIENCE.lstrip("/")))
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(token)

    def test_an_assertion_signed_by_another_key_is_refused(self, wired):
        """The forgery case. Same kid, different private key."""
        _, transport = wired
        attacker_key, _ = self._keypair()
        token = self._mint(attacker_key, self._payload())
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(token)

    def test_an_unknown_kid_is_refused(self, wired):
        key, _ = wired
        token = self._mint(key, self._payload(), kid="some-other-kid")
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(token)

    def test_a_wrong_issuer_assertion_is_refused(self, wired):
        """Real signature, real audience, wrong `iss` — `verify_token` would
        accept this, which is why auth.py checks the issuer itself."""
        key, _ = wired
        token = self._mint(key, self._payload(iss="https://accounts.google.com"))
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(token)

    @pytest.mark.parametrize("garbage", [
        "not-a-jwt",
        "a.b.c",
        "eyJhbGciOiJub25lIn0.eyJlbWFpbCI6ImFAeC5jb20ifQ.",  # alg: none
        "." * 3,
    ])
    def test_garbage_is_refused(self, wired, garbage):
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(garbage)

    def test_an_unsigned_alg_none_token_cannot_impersonate(self, wired):
        """The classic JWT bypass, spelled out: a token whose header says
        `alg: none` and whose payload claims the operator must not verify."""
        import base64

        def seg(obj):
            return base64.urlsafe_b64encode(
                json.dumps(obj).encode()).rstrip(b"=").decode()

        token = ".".join([seg({"alg": "none", "typ": "JWT"}),
                          seg(self._payload()), ""])
        with pytest.raises(A.AssertionInvalid):
            A.authorize_write(token)


# ============================================================================ #
# 5. Router wiring
# ============================================================================ #

WRITE_ROUTES = ("run_sim", "create_pin", "delete_pin", "submit_sweep")


@pytest.mark.skipif(not _HAS_FASTAPI, reason="needs FastAPI (routers.v2)")
class TestEveryWriteRouteGoesThroughTheChain:
    """All four money-spending routes, and nothing else, carry the gate."""

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)

    @pytest.fixture
    def v2(self):
        import routers.v2 as v2
        return v2

    def _call(self, v2, name, **kwargs):
        handler = getattr(v2, name)
        if name == "run_sim":
            return self._run(handler(spec={}, **kwargs))
        if name == "create_pin":
            return self._run(handler(body={}, **kwargs))
        if name == "delete_pin":
            return self._run(handler(pin_id="p1", **kwargs))
        if name == "submit_sweep":
            return self._run(handler(spec={}, **kwargs))
        raise AssertionError(name)

    @pytest.mark.parametrize("name", WRITE_ROUTES)
    def test_the_route_calls_the_gate_with_the_assertion(self, v2, monkeypatch,
                                                         name):
        """Wiring, not semantics: the assertion header reaches the gate.

        A route that took the header but passed the wrong variable would sail
        through every semantic test in this file and still admit anyone. PR-2
        drops the second argument with the token gate, so the gate is now
        called with exactly one thing — and it must be the assertion.
        """
        seen = []

        def spy(assertion):
            seen.append(assertion)
            raise RuntimeError("stop here")

        monkeypatch.setattr(v2, "_require_write_access", spy)
        with pytest.raises(RuntimeError):
            self._call(v2, name, x_goog_iap_jwt_assertion="the-assertion")
        assert seen == ["the-assertion"], name

    @pytest.mark.parametrize("name", WRITE_ROUTES)
    def test_the_gate_is_the_first_thing_the_route_does(self, v2, monkeypatch,
                                                        name):
        """Refused before any BigQuery read, any launch, any spend.

        `get_bigquery_service` is replaced with a bomb: if the gate ran second,
        an unauthorised caller would already have touched the store.
        """
        def bomb():  # pragma: no cover - the point is it is not called
            raise AssertionError("the store was touched before the auth gate")

        monkeypatch.setattr(v2, "get_bigquery_service", bomb)
        configure(monkeypatch)
        stub_verifier(monkeypatch, result=claims(email=VIEWER))
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._call(v2, name, x_goog_iap_jwt_assertion="an-assertion")
        assert exc.value.status_code == 403

    def test_an_unresolved_header_default_is_treated_as_no_assertion(
            self, v2, monkeypatch):
        """The `Header(...)` FieldInfo trap, pinned.

        A handler called DIRECTLY — which is how this router is tested, the
        dashboard image's deps being absent from the bot CI image — receives the
        `Header(default=None)` object itself for an omitted argument, not None.
        Only FastAPI resolves that default. The object is TRUTHY, so without the
        coercion in `_require_write_access` an omitted argument would look like
        a PRESENT assertion and be handed to the verifier as one.

        PR-2 makes the consequence louder rather than quieter: the fall-through
        is now a 401, so the coercion is what separates "no assertion" (401
        naming IAP) from "an assertion that is a FieldInfo object" (a
        verification error, also 401, with a misleading log event and a
        pointless sha256 of a repr).
        """
        from fastapi import Header, HTTPException

        configure(monkeypatch)
        verifier_must_not_be_called(monkeypatch)
        unresolved = Header(default=None)
        assert unresolved, "premise: the FieldInfo default is truthy"
        with pytest.raises(HTTPException) as exc:
            v2._require_write_access(unresolved)
        assert exc.value.status_code == 401
        assert exc.value.detail == v2.NO_ASSERTION_DETAIL

    @pytest.mark.parametrize("name", WRITE_ROUTES)
    def test_no_write_handler_takes_an_authorization_argument_any_more(
            self, v2, name):
        """PR-2: the `Authorization` header is not a parameter of these routes.

        Not "ignored" — ABSENT. A handler that still bound the header would
        publish it in `/openapi.json` as part of the route's contract and would
        be one edit away from reading it again. The signature is the check that
        cannot be satisfied by a comment.
        """
        import inspect

        params = inspect.signature(getattr(v2, name)).parameters
        assert "authorization" not in params, name
        assert "x_goog_iap_jwt_assertion" in params, name


@pytest.mark.skipif(not _HAS_FASTAPI, reason="needs FastAPI (routers.v2)")
class TestTheGateTranslatesEveryRefusal:
    """`_require_write_access` maps auth.py's decisions onto HTTP statuses."""

    @pytest.fixture
    def v2(self):
        import routers.v2 as v2
        return v2

    def test_no_assertion_is_401_naming_iap(self, v2, monkeypatch):
        """PR-2's headline change. PR-1 answered this with the token gate."""
        from fastapi import HTTPException

        configure(monkeypatch)
        verifier_must_not_be_called(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            v2._require_write_access(None)
        assert exc.value.status_code == 401
        assert exc.value.detail == v2.NO_ASSERTION_DETAIL

    def test_an_invalid_assertion_is_401(self, v2, monkeypatch):
        """A forged header must be loud, and there is nothing to fall back to."""
        from fastapi import HTTPException

        configure(monkeypatch)
        stub_verifier(monkeypatch, raises=ValueError("bad signature"))
        with pytest.raises(HTTPException) as exc:
            v2._require_write_access("forged")
        assert exc.value.status_code == 401
        # DISTINCT from the no-assertion 401: one is "sign in", the other is
        # "what you sent does not verify", and they send an operator to
        # different places.
        assert exc.value.detail != v2.NO_ASSERTION_DETAIL
        assert "could not be verified" in exc.value.detail

    def test_a_viewer_is_403(self, v2, monkeypatch):
        from fastapi import HTTPException

        configure(monkeypatch)
        stub_verifier(monkeypatch, result=claims(email=VIEWER))
        with pytest.raises(HTTPException) as exc:
            v2._require_write_access("an-assertion")
        assert exc.value.status_code == 403
        assert A.OPERATORS_ENV in exc.value.detail

    def test_an_operator_writes_with_no_token_configured_at_all(self, v2,
                                                                monkeypatch):
        """Post-retirement reality: no secret anywhere, writes still work."""
        configure(monkeypatch)
        monkeypatch.delenv("SWEEP_SUBMIT_TOKEN", raising=False)
        stub_verifier(monkeypatch, result=claims(email=OPERATOR))
        identity = v2._require_write_access("an-assertion")
        assert identity is not None and identity.email == OPERATOR

    def test_the_audience_unset_posture_is_401_through_the_router(self, v2,
                                                                  monkeypatch):
        from fastapi import HTTPException

        configure(monkeypatch, audience=None)
        with pytest.raises(HTTPException) as exc:
            v2._require_write_access("an-assertion")
        assert exc.value.status_code == 401
        assert A.AUDIENCE_ENV in exc.value.detail

    def test_the_operators_unset_posture_is_403_through_the_router(self, v2,
                                                                   monkeypatch):
        from fastapi import HTTPException

        configure(monkeypatch, operators=None)
        stub_verifier(monkeypatch, result=claims(email=OPERATOR))
        with pytest.raises(HTTPException) as exc:
            v2._require_write_access("an-assertion")
        assert exc.value.status_code == 403
        assert "unset or empty" in exc.value.detail


# ============================================================================ #
# 6. The token is RETIRED (PR-2)
# ============================================================================ #

TOKEN_ENV = "SWEEP_SUBMIT_TOKEN"
#: The bearer a caller would still be holding — a leaked one, a stale runbook,
#: a bookmarked curl. It must buy nothing.
RETIRED_TOKEN = "s3cret-token-value"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="needs FastAPI (routers.v2)")
class TestTheTokenIsRetired:
    """A write with no IAP assertion is a 401 naming IAP — never a token gate.

    PR-1 shipped three branches on the write routes and the third was "no
    assertion ⇒ the `SWEEP_SUBMIT_TOKEN` gate, unchanged". PR-2 deletes it. The
    class below pins the replacement in the two ways that matter:

    * the **status and the exact detail string** on all four routes, because an
      operator runbook and the frontend's expiry state both quote it; and
    * that the old credential buys nothing **even when the environment still
      offers it**. That is not a hypothetical: the deploy order is PR-2 first,
      `--remove-secrets` second, delete the secret third (reversing the last two
      bricks the next revision's startup), so there is a real window in which
      this revision runs with `SWEEP_SUBMIT_TOKEN` bound.
    """

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)

    @pytest.fixture
    def v2(self, monkeypatch):
        import routers.v2 as v2

        # Configured exactly as the deployed revision is...
        configure(monkeypatch)
        # ...and the token IS in the environment, which is the point.
        monkeypatch.setenv(TOKEN_ENV, RETIRED_TOKEN)
        # The verifier is a bomb: a request carrying no assertion must not
        # reach it at all.
        verifier_must_not_be_called(monkeypatch)
        monkeypatch.setattr(
            v2, "get_bigquery_service",
            lambda: (_ for _ in ()).throw(
                AssertionError("the store was touched before the auth gate")))
        return v2

    def test_the_gate_401s_with_no_assertion_and_the_token_set(self, v2):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            v2._require_write_access(None)
        assert exc.value.status_code == 401
        assert exc.value.detail == v2.NO_ASSERTION_DETAIL

    @pytest.mark.parametrize("name", WRITE_ROUTES)
    def test_every_write_route_401s_with_no_assertion(self, v2, name):
        """End-to-end per route, through the real handler signature."""
        from fastapi import HTTPException

        args = {"run_sim": {"spec": {}}, "create_pin": {"body": {}},
                "delete_pin": {"pin_id": "p1"}, "submit_sweep": {"spec": {}}}[name]
        handler = getattr(v2, name)
        with pytest.raises(HTTPException) as exc:
            self._run(handler(**args))
        assert exc.value.status_code == 401, name
        assert exc.value.detail == v2.NO_ASSERTION_DETAIL, name

    def test_the_401_is_not_a_403(self, v2):
        """401, deliberately, and the distinction is the operator's next step.

        403 means "we know who you are and you may not"; 401 means "nothing on
        this request says who you are". A browser reading a 403 has no reason to
        re-authenticate, so the SPA's session-expiry state — the whole point of
        `X-Requested-With` — would never fire.
        """
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            v2._require_write_access(None)
        assert exc.value.status_code != 403

    def test_the_detail_names_iap_and_the_programmatic_recipe(self, v2):
        """The message is the runbook for the three ways to arrive here."""
        detail = v2.NO_ASSERTION_DETAIL
        assert "Identity-Aware Proxy" in detail
        assert "print-identity-token" in detail
        # `--include-email` is not decoration: IAP refuses an impersonated
        # id-token that carries no email claim, and the operator hit exactly
        # that live during the console session.
        assert "--include-email" in detail
        assert "reload the page" in detail
        # Says out loud that the old credential is dead, so someone holding one
        # stops trying to make it work.
        assert TOKEN_ENV in detail and "retired" in detail

    def test_the_router_never_reads_the_token_variable(self):
        """Grep, not reasoning. The variable may still be bound to the revision.

        A code path that revives itself the moment an env var reappears is not
        a retired code path, and "we removed the gate" is exactly the claim a
        later refactor can silently undo.
        """
        text = (REPO_ROOT / "dashboard" / "backend" / "routers" / "v2.py").read_text()
        for forbidden in (f'getenv("{TOKEN_ENV}"', f"getenv('{TOKEN_ENV}'",
                          f'environ["{TOKEN_ENV}"]', f'environ.get("{TOKEN_ENV}"'):
            assert forbidden not in text, forbidden
        # The name survives ONLY in prose: the 401 detail and the comments that
        # say why it is gone.
        assert "_require_sweep_token" not in text
        assert "_sweep_token" not in text

    def test_the_auth_module_does_not_even_NAME_the_retired_variable(self):
        """Not `getenv`, not a comment, not a docstring — the string is gone.

        Stricter than the router check above on purpose (review round 1, F5).
        `services/auth.py` is the module that decides who may spend money, and
        until this round its module docstring and `authorize_write`'s docstring
        both described a fall-through "to the pre-existing SWEEP_SUBMIT_TOKEN
        gate ... byte-identically". That branch does not exist any more. A false
        contract in the one file a reader opens to learn the contract is worse
        than no comment: the next person to touch this code would have written
        against a gate that was deleted, and the grep that would have caught
        them ("is the token really gone?") comes back positive on the prose.

        So the bar here is the whole file, not just its `getenv` calls. If a
        future change genuinely needs to discuss the retired credential, it says
        "the legacy shared-bearer gate" — as this module now does.
        """
        text = (REPO_ROOT / "dashboard" / "backend" / "services"
                / "auth.py").read_text()
        assert TOKEN_ENV not in text, (
            f"{TOKEN_ENV} appears in services/auth.py. The gate it names was "
            f"deleted in PR-2; anything this file says about it is a contract "
            f"that no code honours.")

    def test_the_service_layer_has_no_token_helpers_left(self):
        """`extract_bearer` / `token_matches` are DELETED, not unused.

        Left in place they would be one call site away from a second gate, and
        `hasattr` is the check a grep of the router cannot make.
        """
        import services.sweeps as S

        assert not hasattr(S, "extract_bearer")
        assert not hasattr(S, "token_matches")
        # The AST, not a substring: the module still *mentions* the retired
        # compare in the comment that records why it is gone, and a grep test
        # that a comment can fail is a test people delete.
        import ast

        tree = ast.parse((REPO_ROOT / "dashboard" / "backend" / "services"
                          / "sweeps.py").read_text())
        imported = {alias.name.split(".")[0]
                    for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {(node.module or "").split(".")[0]
                     for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom)}
        assert "hmac" not in imported


@pytest.mark.skipif(not _HAS_TESTCLIENT,
                    reason="needs FastAPI AND httpx (fastapi.testclient)")
class TestTheOldBearerHeaderBuysNothing:
    """The sharp one, over a REAL request: old header + configured secret ⇒ 401.

    A direct handler call cannot express this any more — the parameter is gone —
    which is itself the strongest form of the guarantee. So it is asserted on
    the wire instead: the header is sent, the secret matches, and the answer is
    the same 401 an unauthenticated stranger gets.
    """

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import routers.v2 as v2

        configure(monkeypatch)
        monkeypatch.setenv(TOKEN_ENV, RETIRED_TOKEN)
        verifier_must_not_be_called(monkeypatch)
        monkeypatch.setattr(
            v2, "get_bigquery_service",
            lambda: (_ for _ in ()).throw(
                AssertionError("the store was touched before the auth gate")))
        app = FastAPI()
        app.include_router(v2.router, prefix="/api/v2")
        return TestClient(app)

    def _writes(self, client, headers):
        return {
            "POST /sweeps": client.post("/api/v2/sweeps", headers=headers,
                                        json={}),
            "POST /sims/run": client.post("/api/v2/sims/run", headers=headers,
                                          json={}),
            "POST /sims/pins": client.post("/api/v2/sims/pins", headers=headers,
                                           json={"spec": {}}),
            "DELETE /sims/pins/{id}": client.delete(
                "/api/v2/sims/pins/p1", headers=headers),
        }

    def test_the_exact_retired_bearer_is_401_on_every_write(self, client):
        import routers.v2 as v2

        for route, r in self._writes(
                client, {"authorization": f"Bearer {RETIRED_TOKEN}"}).items():
            assert r.status_code == 401, f"{route}: {r.status_code} {r.text}"
            assert r.json()["detail"] == v2.NO_ASSERTION_DETAIL, route

    def test_nothing_at_all_is_the_same_401(self, client):
        """The retired token is worth exactly what sending nothing is worth."""
        import routers.v2 as v2

        for route, r in self._writes(client, {}).items():
            assert r.status_code == 401, f"{route}: {r.status_code} {r.text}"
            assert r.json()["detail"] == v2.NO_ASSERTION_DETAIL, route

    def test_the_openapi_schema_no_longer_advertises_authorization(self, client):
        """The route CONTRACT drops the header, not just the code path.

        `/openapi.json` is the machine-readable statement of what these routes
        accept. Leaving `authorization` in it would keep telling every generated
        client to send a credential this service refuses to look at.
        """
        schema = client.get("/openapi.json").json()
        for path, method in (("/api/v2/sweeps", "post"),
                             ("/api/v2/sims/run", "post"),
                             ("/api/v2/sims/pins", "post"),
                             ("/api/v2/sims/pins/{pin_id}", "delete")):
            params = schema["paths"][path][method].get("parameters", [])
            names = {p["name"].lower() for p in params}
            assert "authorization" not in names, (path, method, names)
            assert "x-goog-iap-jwt-assertion" in names, (path, method, names)


# ============================================================================ #
# 7. The two exemptions, through a real request
# ============================================================================ #

@pytest.mark.skipif(not _HAS_TESTCLIENT,
                    reason="needs FastAPI AND httpx (fastapi.testclient)")
class TestTheExemptRoutes:
    """`pause-alert-check` and `/api/errors` are exempt BY DECISION.

    Exercised through `TestClient` rather than by calling the handlers, because
    the claim under test is about a real HTTP request carrying a real IAP
    header — a handler call cannot distinguish "exempt" from "does not read the
    header".

    **What "exempt" means differs between the two, deliberately** (plan rev 4):

    * `pause-alert-check` is exempt from the OPERATORS chain but **verifies when
      an assertion is present** — absent passes, valid passes whoever it is,
      INVALID is a 401. Otherwise the documented exception would also be the one
      endpoint on this service where a forged header is accepted in silence, and
      it is the first place a prober looks precisely because it is documented.
    * `/api/errors` is ungated outright: viewers' browsers post it, and IAP
      admission is the only thing standing in front of it post-flip.
    """

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import routers.errors as errors_router
        import routers.v2 as v2

        async def uncovered():
            return {"uncovered": [], "positions_available": True,
                    "decision_source_available": True,
                    "unknown_uncovered_days": []}

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", uncovered)
        configure(monkeypatch)
        # A VALID assertion for an identity that is NOT an operator — the
        # scheduler's SA for one route, a human viewer for the other.
        stub_verifier(monkeypatch, result=claims(email=SCHEDULER_SA))

        app = FastAPI()
        app.include_router(v2.router, prefix="/api/v2")
        app.include_router(errors_router.router, prefix="/api/errors")
        return TestClient(app)

    ASSERTION = {"x-goog-iap-jwt-assertion": "a-valid-assertion"}

    def test_a_valid_non_operator_assertion_reaches_pause_alert_check(self,
                                                                      client):
        """The FC-030 silence class, prevented.

        The daily scheduler runs as a service account. A uniform OPERATORS gate
        would 403 it every evening and the drawdown alert would go quietly
        offline — the exact failure mode the alert exists to prevent, arriving
        through the door built to prevent it.
        """
        r = client.post("/api/v2/bot-health/pause-alert-check",
                        headers=self.ASSERTION)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

    def test_pause_alert_check_also_works_with_no_assertion(self, client):
        """Pre-flip behaviour, unchanged."""
        r = client.post("/api/v2/bot-health/pause-alert-check")
        assert r.status_code == 200, r.text

    def test_pause_alert_check_is_not_403d_by_an_empty_operators(self, client,
                                                                 monkeypatch):
        """Even a revision with no allowlist keeps the scheduler working."""
        monkeypatch.delenv(A.OPERATORS_ENV, raising=False)
        r = client.post("/api/v2/bot-health/pause-alert-check",
                        headers=self.ASSERTION)
        assert r.status_code == 200, r.text

    def test_pause_alert_check_401s_an_INVALID_assertion(self, client,
                                                         monkeypatch):
        """Exempt from AUTHORIZATION, not from VERIFICATION (plan rev 4).

        A forged header here must be as loud as a forged header anywhere else.
        The alternative is a documented blind spot: the one route a prober is
        told to try, answering 200 to a token that verifies against nothing.
        """
        stub_verifier(monkeypatch, raises=ValueError("bad signature"))
        r = client.post("/api/v2/bot-health/pause-alert-check",
                        headers={"x-goog-iap-jwt-assertion": "forged"})
        assert r.status_code == 401, r.text
        assert "could not be verified" in r.json()["detail"]

    def test_pause_alert_check_401s_when_the_audience_is_unconfigured(
            self, client, monkeypatch):
        """The fail-closed posture reaches this route too."""
        monkeypatch.delenv(A.AUDIENCE_ENV, raising=False)
        r = client.post("/api/v2/bot-health/pause-alert-check",
                        headers=self.ASSERTION)
        assert r.status_code == 401, r.text
        assert A.AUDIENCE_ENV in r.json()["detail"]

    def test_the_401_happens_before_any_evaluation(self, client, monkeypatch):
        """A refused request must not have cost a BigQuery read.

        The evaluator is replaced with a bomb: if verification ran second, a
        forged header would already have spent the query.
        """
        import routers.v2 as v2

        async def bomb():  # pragma: no cover - the point is it is not called
            raise AssertionError("evaluated before verifying the assertion")

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", bomb)
        stub_verifier(monkeypatch, raises=ValueError("bad signature"))
        r = client.post("/api/v2/bot-health/pause-alert-check",
                        headers={"x-goog-iap-jwt-assertion": "forged"})
        assert r.status_code == 401, r.text

    def test_the_scheduler_still_passes_with_a_valid_sa_assertion(self, client):
        """The whole reason for the exemption, stated as its own test."""
        r = client.post("/api/v2/bot-health/pause-alert-check",
                        headers=self.ASSERTION)
        assert r.status_code == 200, r.text

    def test_the_errors_sink_ignores_an_INVALID_assertion(self, client,
                                                          monkeypatch):
        """`/api/errors` is ungated OUTRIGHT — it does not even verify.

        Different from `pause-alert-check` on purpose. A browser whose IAP
        session has just expired is exactly the session whose crash report is
        worth having, and a 401 here would drop it. Nothing behind this route
        reads the identity, so there is nothing for a forged header to reach.
        """
        stub_verifier(monkeypatch, raises=ValueError("bad signature"))
        r = client.post("/api/errors",
                        headers={"x-goog-iap-jwt-assertion": "forged"}, json={
                            "error": "boom", "stack": "at x", "url": "https://d/",
                            "timestamp": "2026-09-02T00:00:00Z",
                            "userAgent": "pytest"})
        assert r.status_code == 200, r.text

    def test_the_errors_sink_accepts_a_viewers_report(self, client, monkeypatch):
        """Viewers are read-only accounts whose browsers still throw.

        Gating this would drop exactly the crash reports nobody is watching for.
        """
        stub_verifier(monkeypatch, result=claims(email=VIEWER))
        r = client.post("/api/errors", headers=self.ASSERTION, json={
            "error": "boom", "stack": "at x", "url": "https://d/",
            "timestamp": "2026-09-02T00:00:00Z", "userAgent": "pytest"})
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "logged"}

    def test_the_errors_sink_needs_no_assertion_and_no_token(self, client):
        r = client.post("/api/errors", json={
            "error": "boom", "stack": "at x", "url": "https://d/",
            "timestamp": "2026-09-02T00:00:00Z", "userAgent": "pytest"})
        assert r.status_code == 200, r.text


@pytest.mark.skipif(not _HAS_TESTCLIENT,
                    reason="needs FastAPI AND httpx (fastapi.testclient)")
class TestTheHeaderBindsThroughFastapi:
    """The wire name reaches the handler parameter.

    FastAPI maps `x_goog_iap_jwt_assertion` onto `x-goog-iap-jwt-assertion` by
    converting underscores. If that mapping ever broke — a rename, a
    `convert_underscores=False` — every request would look assertion-less, and
    since PR-2 that means **every write on the service answers 401** to a
    correctly signed-in operator, with nothing anywhere saying why. This is the
    only test that can catch it.
    """

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import routers.v2 as v2

        configure(monkeypatch)
        monkeypatch.setattr(
            v2, "get_bigquery_service",
            lambda: (_ for _ in ()).throw(
                AssertionError("the store was touched before the auth gate")))
        app = FastAPI()
        app.include_router(v2.router, prefix="/api/v2")
        return TestClient(app)

    def test_an_invalid_assertion_header_produces_401(self, client,
                                                      monkeypatch):
        stub_verifier(monkeypatch, raises=ValueError("bad signature"))
        r = client.post("/api/v2/sims/pins",
                        headers={"x-goog-iap-jwt-assertion": "forged"},
                        json={"spec": {}})
        assert r.status_code == 401, r.text
        assert "could not be verified" in r.json()["detail"]

    def test_the_header_is_case_insensitive_on_the_wire(self, client,
                                                        monkeypatch):
        stub_verifier(monkeypatch, raises=ValueError("bad signature"))
        r = client.post("/api/v2/sims/pins",
                        headers={"X-Goog-IAP-JWT-Assertion": "forged"},
                        json={"spec": {}})
        assert r.status_code == 401, r.text

    def test_a_viewer_assertion_produces_403_naming_operators(self, client,
                                                              monkeypatch):
        stub_verifier(monkeypatch, result=claims(email=VIEWER))
        r = client.post("/api/v2/sims/pins",
                        headers={"x-goog-iap-jwt-assertion": "valid"},
                        json={"spec": {}})
        assert r.status_code == 403, r.text
        assert "OPERATORS" in r.json()["detail"]

    def test_no_header_at_all_is_the_iap_401(self, client, monkeypatch):
        """PR-2: the fall-through is a 401 naming IAP, not a token prompt."""
        import routers.v2 as v2

        verifier_must_not_be_called(monkeypatch)
        r = client.post("/api/v2/sims/pins", json={"spec": {}})
        assert r.status_code == 401, r.text
        assert r.json()["detail"] == v2.NO_ASSERTION_DETAIL

    def test_a_blank_assertion_header_is_treated_as_absent(self, client,
                                                           monkeypatch):
        """By design (PR-1 disposition), and PR-2 does not change it.

        IAP never stamps an empty header. A client that sends one gets the
        "sign in" 401 rather than the "your assertion did not verify" one,
        which is the more useful of the two answers and reaches the same
        refusal.
        """
        import routers.v2 as v2

        verifier_must_not_be_called(monkeypatch)
        r = client.post("/api/v2/sims/pins",
                        headers={"x-goog-iap-jwt-assertion": "   "},
                        json={"spec": {}})
        assert r.status_code == 401, r.text
        assert r.json()["detail"] == v2.NO_ASSERTION_DETAIL


# ============================================================================ #
# 8. Deploy config
# ============================================================================ #

class TestTheDeployConfigCarriesTheRolesEnv:
    """`cloudbuild.yaml` is the only place these two values persist.

    `--set-env-vars` REPLACES the whole env set on every deploy, so a value
    applied out of band with `--update-env-vars` survives exactly until the next
    merge. A missing `IAP_AUDIENCE` post-flip is a total write outage; a missing
    `OPERATORS` is the same. Both are frozen in the cloudbuild fixture as well;
    this test additionally ties the deployed *shape* to what the code expects.
    """

    @pytest.fixture(scope="class")
    def env(self):
        """The dashboard deploy's `--set-env-vars`, parsed.

        Reuses the cloudbuild contract test's own `deploy_flags()` rather than
        scanning the script text: that helper walks the backslash-continued
        `gcloud run deploy` command, so a COMMENT mentioning the flag - and this
        step now carries a worked `^;^` example in one - cannot be mistaken for
        the flag itself.
        """
        from tests.test_cloudbuild_contract import deploy_flags, load_steps

        step = next(s for s in load_steps() if s["id"] == "deploy-dashboard-canary")
        flag = next(f for f in deploy_flags(step)
                    if f.startswith("--set-env-vars="))
        raw = flag[len("--set-env-vars="):].strip("\'\"")
        return self._parse_env_value(raw)

    @staticmethod
    def _parse_env_value(raw):
        """Handle BOTH the comma form and gcloud's `^<delim>^` alternate form.

        The `^;^` form is the one IN USE since PR-2's review round: a second
        operator put a SPACE in `OPERATORS`, which the comma form cannot carry
        through an unquoted bash line. Both branches are kept because a future
        edit could go either way and a parser that only reads today's shape
        turns a delimiter change into an unrelated-looking failure.
        """
        sep = ","
        if len(raw) > 2 and raw[0] == "^":
            end = raw.index("^", 1)
            sep = raw[1:end]
            raw = raw[end + 1:]
        return dict(kv.split("=", 1) for kv in raw.split(sep))

    def test_the_parser_reads_the_alternate_delimiter_form(self):
        """The form the deploy actually uses; documented in three places."""
        parsed = self._parse_env_value(
            "^;^GCP_PROJECT=p;OPERATORS=a@x.com b@x.com")
        assert parsed["GCP_PROJECT"] == "p"
        assert parsed["OPERATORS"] == "a@x.com b@x.com"
        assert set(parsed["OPERATORS"].split()) == {"a@x.com", "b@x.com"}

    def test_the_audience_is_deployed_and_is_the_documented_shape(self, env):
        assert env[A.AUDIENCE_ENV] == DEPLOYED_AUDIENCE
        # Leading slash and project NUMBER — the two things that are easy to get
        # plausibly wrong and that fail closed with a generic 401.
        assert env[A.AUDIENCE_ENV].startswith("/projects/799970961417/")
        assert "/locations/us-central1/services/options-wheel-dashboard" in \
            env[A.AUDIENCE_ENV]
        assert "$PROJECT_ID" not in env[A.AUDIENCE_ENV], (
            "$PROJECT_ID is the project ID; IAP's audience uses the project "
            "NUMBER. Substituting it here yields a wrong-but-plausible audience "
            "that refuses every assertion.")

    def test_operators_is_deployed_and_space_separated(self, env):
        value = env[A.OPERATORS_ENV]
        assert value, "OPERATORS must be seeded, or the flip is a write outage"
        assert "," not in value, (
            "a comma in OPERATORS would be parsed by --set-env-vars as the "
            "separator between VARIABLES, silently creating a second env var")
        assert OPERATOR in value.split()

    def test_the_automation_service_account_is_an_operator(self, env):
        """`POST /sims/run` and the two pin routes have NO frontend caller.

        They are curl-only surfaces. Once PR-2 retires the SWEEP_SUBMIT_TOKEN
        bearer, an impersonated id-token for this service account is the ONLY
        credential that can reach them - so dropping it from OPERATORS is not a
        tidy-up, it makes three write routes unreachable by anything. (The SA
        must ALSO hold roles/iap.httpsResourceAccessor on the service; that is
        an IAM grant, not a repo artifact, and it is step 1 of the PR body's
        operator sequence.)
        """
        assert AUTOMATION_SA in env[A.OPERATORS_ENV].split(), (
            f"{AUTOMATION_SA} must be in OPERATORS: it is the only identity "
            f"that can call the three curl-only write routes post-retirement")

    def test_the_multi_operator_value_survives_the_bash_line_it_lives_in(self, env):
        """A value with a space MUST be inside the `^;^` quoted form.

        The trap this pins: the deploy line sits in an unquoted `bash -c`
        script. Unquoted, bash word-splits `OPERATORS=a@x.com b@x.com` and
        gcloud receives the second address as a POSITIONAL argument - a red
        build. `deploy_flags()` reproduces the grouping, so if the quoting were
        dropped this fixture would parse a truncated value and the operator
        assertions above would fail rather than passing on a half-read flag.
        """
        from tests.test_cloudbuild_contract import deploy_flags, load_steps

        step = next(s for s in load_steps() if s["id"] == "deploy-dashboard-canary")
        flag = next(f for f in deploy_flags(step) if f.startswith("--set-env-vars="))
        raw = flag[len("--set-env-vars="):]
        if " " in env[A.OPERATORS_ENV]:
            assert raw.startswith("'^") and raw.endswith("'"), (
                "OPERATORS contains a space, so the whole --set-env-vars flag "
                "must be ONE quoted argument in gcloud's ^<delim>^ form")

    def test_the_seed_operator_would_be_admitted(self, env, monkeypatch):
        """The value as deployed actually authorises the operator's own email."""
        configure(monkeypatch, operators=env[A.OPERATORS_ENV],
                  audience=env[A.AUDIENCE_ENV])
        stub_verifier(monkeypatch, result=claims(email=OPERATOR,
                                                 aud=env[A.AUDIENCE_ENV]))
        assert A.authorize_write("an-assertion") is not None

    def test_the_stale_dashboard_cloudbuild_is_gone(self):
        """It carried `--allow-unauthenticated` and another project's bot URL.

        No trigger ever referenced it (the only trigger,
        `deploy-options-wheel-strategy`, uses the root file), and leaving a
        second build config that re-opens anonymous access lying around during
        an access-control migration is the definition of a footgun.
        """
        stale = REPO_ROOT / "dashboard" / "cloudbuild.yaml"
        assert not stale.exists(), (
            "dashboard/cloudbuild.yaml is back. It deploys the dashboard with "
            "--allow-unauthenticated, which would undo the whole of FC-096 "
            "Phase D if anyone ever pointed a trigger at it.")


class TestTheRequirementsDeclareTheEs256Dependency:
    """Both files, because the test that guards the regression runs in the BOT
    CI image while the code that needs it runs in the DASHBOARD image."""

    def test_the_dashboard_image_pins_both(self):
        text = (REPO_ROOT / "dashboard" / "backend" / "requirements.txt").read_text()
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        assert any(ln.startswith("google-auth==") for ln in lines), lines
        assert any(ln.startswith("cryptography==") for ln in lines), lines

    def test_the_root_requirements_declare_cryptography(self):
        text = (REPO_ROOT / "requirements.txt").read_text()
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        assert any(ln.startswith("cryptography") for ln in lines), (
            "the ES256 round-trip test runs in the bot CI image; without this "
            "declaration it would skip there, and a skipped test is not a guard")


class TestTheModuleConstantsMatchTheDocumentedIapFacts:
    """Doc-verified in the plan's Context section; wrong values fail closed
    with a generic 401, which is exactly the kind of bug nobody finds."""

    def test_the_keys_url(self):
        assert A.PUBLIC_KEYS_URL == "https://www.gstatic.com/iap/verify/public_key"

    def test_the_issuer(self):
        assert A.ISSUER == "https://cloud.google.com/iap"

    def test_the_header_name(self):
        assert A.ASSERTION_HEADER == "x-goog-iap-jwt-assertion"

    def test_the_verifier_uses_the_iap_keys_url_not_the_oauth2_default(self):
        """`verify_token`'s default certs_url serves Google SIGN-IN keys — a
        different signer. Leaving the default would reject every assertion."""
        import inspect

        src = inspect.getsource(A.verify_assertion)
        assert "certs_url=PUBLIC_KEYS_URL" in src, src


class TestIdentityIsUsableAsAValue:
    """Defining `__eq__` without `__hash__` makes a class UNHASHABLE.

    Python sets `__hash__ = None` in that case, so an `Identity` could not go
    into a set or be used as a dict key — and the failure would surface at the
    first caller that tried it, far from here, as a bare `TypeError`.
    """

    def test_equal_identities_hash_equally(self):
        a = A.Identity(email="a@x.com", subject="1")
        b = A.Identity(email="a@x.com", subject="1")
        assert a == b
        assert hash(a) == hash(b)

    def test_it_can_go_in_a_set_and_dedupes(self):
        a = A.Identity(email="a@x.com", subject="1")
        b = A.Identity(email="a@x.com", subject="1")
        c = A.Identity(email="b@x.com", subject="2")
        assert len({a, b, c}) == 2

    def test_a_different_identity_is_unequal(self):
        assert (A.Identity(email="a@x.com", subject="1")
                != A.Identity(email="a@x.com", subject="2"))
        assert A.Identity(email="a@x.com") != "a@x.com"


@pytest.mark.skipif(not _HAS_TESTCLIENT,
                    reason="needs FastAPI AND httpx (fastapi.testclient)")
class TestTheErrorSinkBoundsWhatItWrites:
    """Every caller-controlled field is length-capped, not just `stack`.

    This route writes straight through to Cloud Logging and on into BigQuery,
    and it is reachable by anyone the perimeter admits — anonymous today, every
    signed-in viewer after the flip. An untruncated `error` or `url` is an
    unbounded write into log storage: the exposure the pre-existing
    `stack[:500]` bound already acknowledged, left open on the three fields
    beside it.
    """

    @pytest.fixture
    def sink(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import routers.errors as errors_router

        captured = {}

        def fake_error(_message, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(errors_router.logger, "error", fake_error)
        app = FastAPI()
        app.include_router(errors_router.router, prefix="/api/errors")
        return TestClient(app), captured

    def test_every_free_text_field_is_capped(self, sink):
        client, captured = sink
        huge = "A" * 100_000
        r = client.post("/api/errors", json={
            "error": huge, "stack": huge, "url": huge,
            "timestamp": "2026-09-02T00:00:00Z", "userAgent": huge,
            "component": huge})
        assert r.status_code == 200, r.text
        assert len(captured["error"]) == 500
        assert len(captured["stack"]) == 500
        assert len(captured["url"]) == 500
        assert len(captured["component"]) == 200
        # Nothing anywhere near the 100k that was posted.
        for key in ("error", "stack", "url", "component"):
            assert len(captured[key]) < 1000, key

    def test_a_null_component_stays_null(self, sink):
        """The optional field must not become `""` or raise on slicing None."""
        client, captured = sink
        r = client.post("/api/errors", json={
            "error": "boom", "stack": "at x", "url": "https://d/",
            "timestamp": "2026-09-02T00:00:00Z", "userAgent": "pytest"})
        assert r.status_code == 200, r.text
        assert captured["component"] is None

    def test_short_values_are_untouched(self, sink):
        client, captured = sink
        r = client.post("/api/errors", json={
            "error": "boom", "stack": "at x", "url": "https://d/",
            "timestamp": "2026-09-02T00:00:00Z", "userAgent": "pytest",
            "component": "Overview"})
        assert r.status_code == 200, r.text
        assert captured["error"] == "boom"
        assert captured["url"] == "https://d/"
        assert captured["component"] == "Overview"


def test_the_module_imports_without_fastapi():
    """`services/auth.py` must stay FastAPI-free.

    The rules deciding who may spend money are then exercised by the ROOT
    suite, which runs in the bot CI image. `services/sweeps.py` is written this
    way for the same reason, and `routers/v2.py` does the HTTPException
    translation.
    """
    import subprocess

    program = (
        "import sys; sys.modules['fastapi'] = None; "
        "sys.path.insert(0, %r); "
        "import services.auth as A; "
        "assert A.ISSUER; print('ok')"
        % str(REPO_ROOT / "dashboard" / "backend")
    )
    proc = subprocess.run([sys.executable, "-c", program], capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "ok" in proc.stdout
