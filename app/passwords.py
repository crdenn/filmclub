"""Password hashing for local accounts.

Uses the standard library's memory-hard scrypt (no external dependency). Hashes
are self-describing (`scrypt$N$r$p$salt$dk`, all base64) so the cost parameters
travel with the stored value and can be raised later without breaking old hashes.
"""
import base64
import hashlib
import hmac
import secrets

# Cost parameters. n=2**14, r=8, p=1 needs ~16 MiB per hash — comfortable for a
# small private club while staying well within scrypt's default memory ceiling.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=dklen, maxmem=_MAXMEM)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = _derive(password, salt, _N, _R, _P, _DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        scheme, n, r, p, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = _unb64(salt_b64), _unb64(dk_b64)
        dk = _derive(password, salt, int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)
