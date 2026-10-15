"""Internal S3-compatible object store (MinIO / Ceph RGW) with AWS Signature V4, path-style URLs.

No SDK dependency: three signed requests (PUT / GET / DELETE). The endpoint must pass the outbound URL guard.
"""

from __future__ import annotations

import hashlib
import hmac
import ssl
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from branchops.application.interfaces.attachment_store import StoredBlob
from branchops.config.settings import NetworkSettings, S3StoreSettings
from branchops.infrastructure.security.network_guard import assert_internal_url

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(
    method: str,
    url: str,
    *,
    region: str,
    access_key: str,
    secret_key: str,
    payload_sha256: str,
    now: datetime | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Headers for an AWS SigV4-signed S3 request (service ``s3``, no query string)."""
    now = now or datetime.now(UTC)
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    parts = urlsplit(url)
    host = parts.netloc
    headers = {"host": host, "x-amz-content-sha256": payload_sha256, "x-amz-date": amz_date, **(extra or {})}
    signed = sorted(k.lower() for k in headers)
    canonical_headers = "".join(f"{k}:{headers[k].strip() if k in headers else ''}\n" for k in signed)
    canonical = "\n".join(
        [
            method,
            quote(parts.path or "/", safe="/-_.~"),
            "",
            canonical_headers,
            ";".join(signed),
            payload_sha256,
        ]
    )
    scope = f"{date}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date), region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        **{k: v for k, v in headers.items() if k != "host"},
        "authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={';'.join(signed)}, Signature={signature}",
    }


class S3CompatibleStore:
    name = "s3"

    def __init__(
        self,
        settings: S3StoreSettings,
        network: NetworkSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not settings.endpoint:
            raise ValueError("attachments.s3.endpoint is required")
        assert_internal_url(settings.endpoint, network, purpose="attachments.s3.endpoint")
        creds = settings.credentials()
        if creds is None:
            raise ValueError(
                f"S3 credentials missing ({settings.access_key_env} / {settings.secret_key_env})"
            )
        self._access, self._secret = creds
        self._settings = settings
        self._base = settings.endpoint.rstrip("/")
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=settings.ca_bundle) if settings.ca_bundle else True
        )
        self._client = httpx.Client(
            timeout=settings.timeout_seconds, verify=verify, transport=transport, follow_redirects=False
        )

    def _url(self, ref: str) -> str:
        return f"{self._base}/{self._settings.bucket}/{quote(ref, safe='/-_.~')}"

    def _request(
        self, method: str, ref: str, body: bytes = b"", content_type: str | None = None
    ) -> httpx.Response:
        url = self._url(ref)
        payload_sha = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        extra = {"content-type": content_type} if content_type else None
        headers = sigv4_headers(
            method,
            url,
            region=self._settings.region,
            access_key=self._access,
            secret_key=self._secret,
            payload_sha256=payload_sha,
            extra=extra,
        )
        response = self._client.request(method, url, content=body or None, headers=headers)
        response.raise_for_status()
        return response

    def put(self, key: str, data: bytes, content_type: str) -> StoredBlob:
        ref = f"{self._settings.prefix}{key}"
        self._request("PUT", ref, data, content_type)
        return StoredBlob(storage=self.name, ref=ref)

    def get(self, ref: str, inline_data: bytes | None) -> bytes:
        return self._request("GET", ref).content

    def delete(self, ref: str) -> None:
        try:
            self._request("DELETE", ref)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
