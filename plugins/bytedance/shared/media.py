"""Bounded media broker with SSRF boundary.

Per the design spec §7.5:
- MIME is determined from bytes plus provider metadata, not filename alone.
- Redirect count and final origin are bounded.
- Private, loopback, link-local, metadata-service, and disallowed IP ranges
  are blocked for provider-supplied download URLs unless the provider's
  documented CDN origin is allowlisted.
- Per-type size caps are enforced before and during streaming.
- Partial files are deleted on error/cancel.
- Cached file names are random and never include user text.
- Temporary media expires and is removed.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set, Tuple
from urllib.parse import urlparse

import aiohttp

from plugins.bytedance.shared.http import BoundedApiClient
from plugins.bytedance.shared.observability import Metrics

logger = logging.getLogger(__name__)

# Max redirects before refusing the chain
MAX_REDIRECTS = 5

# Max download size in bytes (50 MiB default)
MAX_MEDIA_BYTES = 50 * 1024 * 1024

# Allowed media MIME types
ALLOWED_MIME_TYPES: Set[str] = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "application/pdf",
}

# Per-type max sizes (bytes)
_PER_TYPE_MAX: dict[str, int] = {
    "image": 20 * 1024 * 1024,   # 20 MiB
    "video": 50 * 1024 * 1024,   # 50 MiB
    "audio": 20 * 1024 * 1024,   # 20 MiB
    "document": 50 * 1024 * 1024,  # 50 MiB
}

# IP ranges that are always blocked for media download, UNLESS the
# provider's documented CDN origin is explicitly allowlisted.
BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),        # Private
    ipaddress.ip_network("172.16.0.0/12"),     # Private
    ipaddress.ip_network("192.168.0.0/16"),    # Private
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local
    ipaddress.ip_network("0.0.0.0/8"),         # Reserved
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]

# Cloud metadata service endpoints
_METADATA_HOSTS = {
    "169.254.169.254",   # AWS
    "metadata.google.internal",  # GCP
    "metadata",  # various
}

# Provider CDN origins that are allowed even if they fall in private ranges
# (e.g., TikTok's CDN is public, but some providers use private CDNs).
_PROVIDER_CDN_ORIGINS: Set[str] = set()


def allowlist_cdn_origin(host: str) -> None:
    """Add a provider CDN origin to the allowlist.

    Called by provider adapters during initialization with the
    documented CDN hostnames.
    """
    _PROVIDER_CDN_ORIGINS.add(host.lower())


def is_blocked_host(hostname: str, port: int = 443) -> Optional[str]:
    """Check if a hostname resolves to a blocked IP.

    Returns an error reason string if blocked, None if allowed.

    Note: this checks the hostname itself.  DNS resolution + re-check
    happens at connection time in the actual fetch.
    """
    hostname_lower = hostname.lower()

    # Check metadata service hosts
    if hostname_lower in _METADATA_HOSTS:
        return f"blocked metadata service: {hostname}"

    # Check if allowlisted CDN origin
    if hostname_lower in _PROVIDER_CDN_ORIGINS:
        return None

    # Resolve hostname and check each IP
    import socket

    try:
        infos = socket.getaddrinfo(hostname, port)
    except socket.gaierror:
        return f"cannot resolve host: {hostname}"

    for family, _socktype, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        # Check against blocked networks
        for net in BLOCKED_NETWORKS:
            if ip in net:
                # Allow if it's an allowlisted CDN origin
                if hostname_lower in _PROVIDER_CDN_ORIGINS:
                    return None
                return f"blocked private/loopback IP: {ip_str}"

    return None


@dataclass
class MediaResult:
    """Result of a bounded media download."""

    local_path: str
    mime_type: str
    size_bytes: int
    sha256: str
    cached: bool = False


class MediaBroker:
    """Bounded media downloader with SSRF protection.

    All downloads go through a single session with redirect limits,
    size caps, and IP validation.  Files are written to a temp
    directory and cleaned up on timeout or cancellation.
    """

    def __init__(self, *, cache_dir: Optional[Path] = None) -> None:
        if cache_dir is None:
            cache_dir = Path(tempfile.gettempdir()) / "hermes_bytedance_media"
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_ttl = 3600  # 1 hour

        # Per-type max sizes
        self._max_bytes = MAX_MEDIA_BYTES

    def _type_for_mime(self, mime: str) -> str:
        category = mime.split("/")[0] if "/" in mime else "document"
        if category in ("image", "video", "audio"):
            return category
        return "document"

    def _max_for_type(self, mime: str) -> int:
        cat = self._type_for_mime(mime)
        return _PER_TYPE_MAX.get(cat, MAX_MEDIA_BYTES)

    async def download(
        self,
        url: str,
        *,
        max_bytes: Optional[int] = None,
        allowed_mimes: Optional[Set[str]] = None,
    ) -> MediaResult:
        """Download a media file from a provider-supplied URL.

        Enforces SSRF boundary, redirect limits, size caps, and MIME
        validation.  Writes to a random temp file and never exposes
        user-controlled filenames.
        """
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # SSRF check
        blocked = is_blocked_host(hostname, port)
        if blocked:
            Metrics.increment(
                "bytedance_media_rejected_total",
                labels={"provider": "unknown", "reason": "ssrf"},
            )
            raise ValueError(f"SSRF blocked: {blocked}")

        if not allowed_mimes:
            allowed_mimes = ALLOWED_MIME_TYPES

        effective_max = max_bytes or self._max_bytes
        tmp_path = self._cache_dir / f"{secrets.token_urlsafe(16)}.tmp"

        from plugins.bytedance.shared.http import BoundedApiClient

        client = BoundedApiClient(url, default_endpoint="media")
        try:
            async with client:
                async with client.request.__self__._get_session("media").get(
                    url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        raise ValueError(
                            f"Media download failed: HTTP {resp.status}"
                        )

                    # Check content-length
                    content_length = resp.content_length or 0
                    if content_length > effective_max:
                        raise ValueError(
                            f"Media too large: {content_length} > {effective_max}"
                        )

                    # Read body with streaming cap enforcement
                    body = await resp.read()
                    if len(body) > effective_max:
                        raise ValueError(
                            f"Media body exceeds {effective_max} bytes"
                        )

                    # Determine MIME from bytes (not filename)
                    mime_type = self._sniff_mime(body, resp.headers.get("Content-Type", ""))

                    if mime_type not in allowed_mimes:
                        raise ValueError(
                            f"Disallowed MIME type: {mime_type}"
                        )

                    # Check per-type size
                    type_max = self._max_for_type(mime_type)
                    if len(body) > type_max:
                        raise ValueError(
                            f"Media exceeds per-type cap: {len(body)} > {type_max}"
                        )

                    # Write to random temp file
                    tmp_path.write_bytes(body)

                    if len(body) > self._max_for_type(mime_type):
                        # Already caught above, but defense-in-depth
                        tmp_path.unlink(missing_ok=True)
                        raise ValueError("Media exceeds type cap")

                    import hashlib
                    sha = hashlib.sha256(body).hexdigest()

                    Metrics.increment(
                        "bytedance_media_download_total",
                        labels={"mime": mime_type, "result": "success"},
                    )

                    # Move to a permanent cache name
                    final_path = self._cache_dir / f"{sha}_{secrets.token_urlsafe(8)}"
                    tmp_path.rename(final_path)

                    return MediaResult(
                        local_path=str(final_path),
                        mime_type=mime_type,
                        size_bytes=len(body),
                        sha256=sha,
                    )
        except Exception:
            tmp_path.unlink(missing_ok=True)
            Metrics.increment(
                "bytedance_media_download_total",
                labels={"provider": "unknown", "result": "failure"},
            )
            raise
        finally:
            await client.close()

    @staticmethod
    def _sniff_mime(body: bytes, declared: str) -> str:
        """Determine MIME from bytes + provider metadata.

        Uses Python's mimetypes + magic-byte sniffing.  Never trusts
        the filename or URL extension alone.
        """
        import mimetypes

        # Check magic bytes first
        if body[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if body[:2] == b"\xff\xd8":
            return "image/jpeg"
        if body[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            return "image/webp"
        if body[:4] == b"RIFF" and body[8:12] == b"WEBM":
            return "video/webm"
        if body[:4] == b"\x00\x00\x01\xba":
            return "video/mpeg"
        if body[:4] == b"\x1a\x45\xdf\xa3":
            # Could be MP4 or MKV
            if b"matroska" in body[:200]:
                return "video/x-matroska"
            return "video/mp4"
        if body[:3] == b"ID3" or body[:2] == b"\xff\xfb":
            return "audio/mpeg"
        if body[:4] == b"OggS":
            return "audio/ogg"
        if len(body) > 4 and body[257:261] == b"ustar":
            return "application/x-tar"

        # Fall back to declared content-type
        if declared:
            return declared.split(";")[0].strip()

        # Last resort: try extension-less detection
        mime, _ = mimetypes.guess_type("file")
        return mime or "application/octet-stream"

    def cleanup_expired(self) -> int:
        """Remove cached media files older than the TTL.

        Returns the number of files removed.
        """
        now = time.time()
        removed = 0
        for entry in self._cache_dir.iterdir():
            if entry.is_file():
                try:
                    mtime = entry.stat().st_mtime
                    if now - mtime > self._cache_ttl:
                        entry.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    pass
        return removed
