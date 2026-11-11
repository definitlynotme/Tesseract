from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any


IdentityProviderEntries = tuple[tuple[str, int, str], ...]

EXIT_IDENTITY_PROVIDERS: IdentityProviderEntries = (
    ("ipify", 4, "https://api.ipify.org?format=json"),
    ("ident.me", 4, "https://4.ident.me/json"),
    ("ipify", 6, "https://api6.ipify.org?format=json"),
    ("ident.me", 6, "https://6.ident.me/json"),
)

# The overview races a third IPv4 source on a different hosting network, so
# one provider hanging behind a given exit does not hold back the answer.
OVERVIEW_IDENTITY_PROVIDERS: IdentityProviderEntries = (
    *EXIT_IDENTITY_PROVIDERS[:2],
    ("aws-checkip", 4, "https://checkip.amazonaws.com/"),
    *EXIT_IDENTITY_PROVIDERS[2:],
)

TEAM_CYMRU_DOH_URL = "https://cloudflare-dns.com/dns-query"
ONIONOO_DETAILS_URL = "https://onionoo.torproject.org/details"
PROXYCHECK_URL_TEMPLATE = "https://proxycheck.io/v3/{ip}?tag=0&p=0"

AWS_PREFIX_URL = "https://ip-ranges.amazonaws.com/ip-ranges.json"
GCP_PREFIX_URL = "https://www.gstatic.com/ipranges/cloud.json"
AZURE_SERVICE_TAGS_PAGE_URL = "https://www.microsoft.com/en-us/download/details.aspx?id=56519"
AZURE_DOWNLOAD_HOST = "download.microsoft.com"
OCI_PREFIX_URL = "https://docs.oracle.com/en-us/iaas/tools/public_ip_ranges.json"
SPAMHAUS_DROP_V4_URL = "https://www.spamhaus.org/drop/drop_v4.json"
SPAMHAUS_DROP_V6_URL = "https://www.spamhaus.org/drop/drop_v6.json"
SPAMHAUS_ASNDROP_URL = "https://www.spamhaus.org/drop/asndrop.json"
FEODO_ACTIVE_C2_URL = "https://feodotracker.abuse.ch/downloads/ipblocklist.json"


def _https_hostname(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url))
    hostname = str(parsed.hostname or "").strip().lower()
    if parsed.scheme.lower() != "https" or not hostname:
        raise ValueError(f"exit-quality endpoint must be an HTTPS hostname: {url}")
    return hostname


EXIT_QUALITY_SYSTEM_URLS = (
    *dict.fromkeys(
        url
        for _provider, _family, url in (*EXIT_IDENTITY_PROVIDERS, *OVERVIEW_IDENTITY_PROVIDERS)
    ),
    TEAM_CYMRU_DOH_URL,
    ONIONOO_DETAILS_URL,
    PROXYCHECK_URL_TEMPLATE,
    AWS_PREFIX_URL,
    GCP_PREFIX_URL,
    AZURE_SERVICE_TAGS_PAGE_URL,
    f"https://{AZURE_DOWNLOAD_HOST}/",
    OCI_PREFIX_URL,
    SPAMHAUS_DROP_V4_URL,
    SPAMHAUS_DROP_V6_URL,
    SPAMHAUS_ASNDROP_URL,
    FEODO_ACTIVE_C2_URL,
)

# These exact hosts are application-owned system routes.  Keeping the set next
# to the request URLs makes adding a new evidence source fail visibly in tests
# instead of silently inheriting the user's ordinary routing policy.
EXIT_QUALITY_SYSTEM_HOSTS = tuple(
    dict.fromkeys(_https_hostname(url) for url in EXIT_QUALITY_SYSTEM_URLS)
)


def same_host_https_redirect_url(source_url: str, target_url: str) -> str:
    """Return an absolute safe redirect URL or fail before the next request."""

    absolute_target = urllib.parse.urljoin(str(source_url), str(target_url))
    source = urllib.parse.urlparse(str(source_url))
    target = urllib.parse.urlparse(absolute_target)
    source_host = str(source.hostname or "").strip().lower()
    target_host = str(target.hostname or "").strip().lower()
    if (
        source.scheme.lower() != "https"
        or target.scheme.lower() != "https"
        or not source_host
        or target_host != source_host
    ):
        raise ValueError("cross-host or non-HTTPS redirect is not allowed")
    return absolute_target


class SameHostHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject unsafe redirects before urllib opens the redirected URL."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            target = same_host_https_redirect_url(req.full_url, newurl)
        except ValueError as exc:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                str(exc),
                headers,
                fp,
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, target)
