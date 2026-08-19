"""Криптография Hub: JWT HS256, шифрование токенов систем (Fernet), PKCE, случайные токены.

Значения секретов приходят из настроек (``HUB_SECRET_KEY``, ``HUB_ENCRYPTION_KEY``) и наружу не
выводятся (R-T4). Все функции детерминированы и не обращаются ко времени напрямую — срок жизни
проверяет вызывающий код по часам приложения (``Clock``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

TOKEN_BYTES = 32  # ≥ 32 байта случайности для кодов, refresh-токенов, state (R-O7, R-O9, R-B2)


class InvalidJWT(ValueError):
    """Некорректный JWT: формат, алгоритм или подпись."""


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().lower()


def random_token(nbytes: int = TOKEN_BYTES) -> str:
    """Непрозрачная строка ≥ ``nbytes`` байт случайности (urlsafe)."""
    return secrets.token_urlsafe(nbytes)


def code_challenge_s256(verifier: str) -> str:
    """``BASE64URL(SHA256(code_verifier))`` — PKCE S256."""
    return b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())


def verify_pkce(verifier: str | None, challenge: str, method: str = "S256") -> bool:
    if not verifier or method != "S256":
        return False
    return hmac.compare_digest(code_challenge_s256(verifier), challenge)


def jwt_encode(claims: dict[str, Any], secret: str) -> str:
    """Подписать claims алгоритмом HS256 (R-O9)."""
    header = {"alg": "HS256", "typ": "JWT"}
    segments = [
        b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")),
        b64url_encode(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    segments.append(b64url_encode(signature))
    return ".".join(segments)


def jwt_decode(token: str, secret: str) -> dict[str, Any]:
    """Проверить подпись HS256 и вернуть claims. Любое нарушение → ``InvalidJWT``.

    Срок действия (``exp``) и аудитория (``aud``) проверяются вызывающим кодом (R-O12).
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidJWT("токен должен состоять из трёх частей")
    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(b64url_decode(header_b64))
        signature = b64url_decode(signature_b64)
    except (ValueError, TypeError) as exc:
        raise InvalidJWT("некорректная кодировка токена") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise InvalidJWT("ожидается алгоритм HS256")
    expected = hmac.new(
        secret.encode("utf-8"), f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidJWT("подпись не совпадает")
    try:
        claims = json.loads(b64url_decode(payload_b64))
    except (ValueError, TypeError) as exc:
        raise InvalidJWT("некорректное тело токена") from exc
    if not isinstance(claims, dict):
        raise InvalidJWT("тело токена должно быть объектом")
    return claims


class TokenCipher:
    """Шифрование токенов целевых систем (Fernet: AES-128-CBC + HMAC-SHA256, R-B3)."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")

    def try_decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self.decrypt(value)
        except (InvalidToken, ValueError):
            return None


__all__ = [
    "TOKEN_BYTES",
    "InvalidJWT",
    "TokenCipher",
    "b64url_decode",
    "b64url_encode",
    "code_challenge_s256",
    "jwt_decode",
    "jwt_encode",
    "random_token",
    "sha256_hex",
    "verify_pkce",
]
