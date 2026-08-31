"""Password hashing, token generation and session cookie signing.

Choices worth stating, since they are the ones that matter if this is ever
attacked:

* **Argon2id** for passwords. It is memory-hard, which is what defeats GPU
  cracking; bcrypt and PBKDF2 are not. Parameters follow RFC 9106's
  low-memory profile and are configurable.
* **Constant-time comparison** everywhere a secret is checked, so a timing
  side-channel cannot reveal a token byte by byte.
* **Tokens are stored hashed.** Session and one-time tokens live in the
  database as SHA-256 digests. Somebody who reads the database still cannot
  log in as anyone.
* **Signed cookies.** The session cookie carries an HMAC over its payload, so a
  tampered cookie is rejected before any database lookup happens.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from backend.core.config import get_settings

# Length of the raw secret inside session and one-time tokens, in bytes.
TOKEN_BYTES = 32

_hasher: PasswordHasher | None = None


def _password_hasher() -> PasswordHasher:
    global _hasher
    if _hasher is None:
        s = get_settings()
        _hasher = PasswordHasher(
            time_cost=s.argon2_time_cost,
            memory_cost=s.argon2_memory_kib,
            parallelism=s.argon2_parallelism,
        )
    return _hasher


def reset_hasher() -> None:
    """Drop the cached hasher (tests change the cost parameters)."""
    global _hasher
    _hasher = None


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Argon2id hash. The plaintext is never stored or logged."""
    return _password_hasher().hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verification. Any malformed hash fails closed."""
    if not stored_hash:
        return False
    try:
        return _password_hasher().verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError, ValueError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash predates the current cost parameters."""
    try:
        return _password_hasher().check_needs_rehash(stored_hash)
    except (InvalidHashError, TypeError, ValueError):
        return True


@dataclass
class PasswordCheck:
    ok: bool
    problems: list[str]

    @property
    def message(self) -> str:
        return "; ".join(self.problems)


#: Rejected outright regardless of length: these are the first guesses made.
COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "letmein", "welcome1", "admin123", "iloveyou", "sunshine",
    "princess", "football", "baseball", "trustno1", "changeme", "passw0rd",
    "gmg12345", "egypt123", "cairo123",
}


def check_password_strength(password: str, *, email: str | None = None) -> PasswordCheck:
    """Validate a password before it is ever hashed.

    Length carries most of the weight, per NIST 800-63B: a long passphrase beats
    a short string with a symbol bolted on. Composition rules are kept light and
    the checks that actually matter — length, known-common passwords, and reuse
    of the user's own email — are enforced.
    """
    problems: list[str] = []
    if len(password) < 10:
        problems.append("must be at least 10 characters")
    if len(password) > 200:
        problems.append("must be at most 200 characters")
    if password.lower() in COMMON_PASSWORDS:
        problems.append("is a commonly used password")
    if password and password == password.lower() and password.isalpha():
        problems.append("should mix letters with numbers or symbols")
    if email:
        local = email.split("@")[0].lower()
        if len(local) >= 3 and local in password.lower():
            problems.append("must not contain your email address")
    if re.fullmatch(r"(.)\1*", password or ""):
        problems.append("must not be a single repeated character")
    return PasswordCheck(ok=not problems, problems=problems)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def generate_token() -> str:
    """A URL-safe random secret. Only its hash is persisted."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 of a token. Fast by design — the token is already high entropy,
    so key stretching buys nothing here and would only slow every request."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


# ---------------------------------------------------------------------------
# Signed cookie values
# ---------------------------------------------------------------------------
def _sign(payload: str) -> str:
    key = get_settings().signing_key.encode("utf-8")
    digest = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sign_value(value: str) -> str:
    """Return ``value.signature``. Tampering is detected without a DB lookup."""
    return f"{value}.{_sign(value)}"


def unsign_value(signed: str) -> str | None:
    """Return the value if the signature verifies, else ``None``."""
    if not signed or "." not in signed:
        return None
    value, _, signature = signed.rpartition(".")
    if not value:
        return None
    return value if hmac.compare_digest(_sign(value), signature) else None


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def csrf_token_for(session_token: str) -> str:
    """Derive a CSRF token from the session secret.

    Deriving rather than storing means the token is stateless, rotates with the
    session, and cannot be read by an attacker who only has the cookie name.
    """
    key = get_settings().signing_key.encode("utf-8")
    digest = hmac.new(key, f"csrf:{session_token}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_csrf(session_token: str, submitted: str) -> bool:
    return hmac.compare_digest(csrf_token_for(session_token), submitted or "")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email_address(email: str) -> tuple[bool, str]:
    """Validate syntax and deliverability-shape. Returns ``(ok, normalised_or_error)``."""
    from email_validator import EmailNotValidError, validate_email

    try:
        # No DNS lookups: this environment has no resolver, and syntax is what
        # matters at signup. Deliverability is proven by the verification email.
        result = validate_email(email, check_deliverability=False)
        return True, result.normalized.lower()
    except EmailNotValidError as exc:
        return False, str(exc)
