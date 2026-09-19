"""Settings read from environment variables (set them in the MCP client's config -> mcpServers.<name>.env).

The names match sentry-cli's (SENTRY_URL, SENTRY_ORG, SENTRY_AUTH_TOKEN), so one set of variables serves both.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

KEYRING_SERVICE = "sentry-mcp"

READ_SCOPES = ("org:read", "project:read", "team:read", "member:read", "event:read")
WRITE_SCOPES = ("event:write",)


class ConfigError(Exception):
    """The server is misconfigured; the message tells the user which variable to fix."""


def _text(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if value.startswith("${"):
        return None  # an optional Claude Desktop extension setting left blank may arrive as its literal placeholder
    return value or None


def _flag(name: str, default: bool) -> bool:
    raw = (_text(name) or "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def normalize_url(url: str) -> str:
    """https://sentry.company.com, whatever was pasted: trailing slash, /api/0, or a page URL inside Sentry."""
    url = url.strip()
    if not url.lower().startswith(("https://", "http://")):
        raise ConfigError(f"SENTRY_URL must be a full address such as https://sentry.company.com (got {url!r}).")
    parts = urlsplit(url)
    if not parts.netloc:
        raise ConfigError(f"SENTRY_URL has no host name: {url!r}")
    path = parts.path.rstrip("/")
    # Only a path prefix before Sentry's own routes is kept (Sentry served under e.g. /sentry).
    for marker in ("/api/0", "/organizations/", "/settings/", "/issues/", "/auth/"):
        if marker in path + "/":
            path = path[: (path + "/").index(marker)]
    return f"{parts.scheme.lower()}://{parts.netloc}{path}"


@dataclass(frozen=True)
class Settings:
    url: str
    org: str | None
    read_only: bool
    verify_ssl: bool
    ca_bundle: str | None
    use_system_certs: bool
    timeout: float

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc

    @classmethod
    def from_env(cls, url: str | None = None) -> "Settings":
        url = url or _text("SENTRY_URL")
        if not url:
            raise ConfigError("SENTRY_URL is not set (your Sentry address, e.g. https://sentry.company.com).")

        ca_bundle = _text("SENTRY_CA_BUNDLE")
        if ca_bundle and not Path(ca_bundle).is_file():
            raise ConfigError(f"SENTRY_CA_BUNDLE points to a missing file: {ca_bundle}")

        raw_timeout = _text("SENTRY_TIMEOUT") or "30"
        try:
            timeout = float(raw_timeout)
        except ValueError:
            raise ConfigError(f"SENTRY_TIMEOUT must be a number of seconds (got {raw_timeout!r}).") from None

        return cls(
            url=normalize_url(url),
            org=_text("SENTRY_ORG"),
            read_only=_flag("SENTRY_READ_ONLY", False),
            verify_ssl=_flag("SENTRY_VERIFY_SSL", True),
            ca_bundle=ca_bundle,
            use_system_certs=_flag("SENTRY_USE_SYSTEM_CERTS", True),
            timeout=max(timeout, 1.0),
        )


def resolve_token(settings: Settings) -> str:
    """SENTRY_AUTH_TOKEN wins; otherwise the credential-store entry written by `sentry-mcp set-token` for this host."""
    token = _text("SENTRY_AUTH_TOKEN")
    if token:
        return token

    import keyring

    stored = keyring.get_password(KEYRING_SERVICE, settings.host)
    if stored:
        return stored

    raise ConfigError(
        f"No auth token for {settings.host}. Create a User Auth Token at {settings.url}/settings/account/api/auth-tokens/ "
        f"and run `sentry-mcp set-token --url {settings.url}` once (stores it in the system credential store), "
        "or set SENTRY_AUTH_TOKEN."
    )
