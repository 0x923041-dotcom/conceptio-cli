"""Public HTTPS client for the Conceptio Open Knowledge API.

Only ever talks to the public REST endpoints (default
https://www.conceptio.app — the apex host 308s to www, so machine
clients must target www). No internal keys, configs, or infrastructure.
"""

import ipaddress
import os
import re
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__
from .config import AUTH_REQUIRED_HINT, DEFAULT_API_BASE, load_config

USER_AGENT = f"conceptio-cli/{__version__}"
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
UPGRADE_HINT = (
    "Rate limit / free trial quota exhausted. Upgrade to Pro at "
    "https://conceptio.app (EUR 4.99/month) or run `conceptio auth <key>`."
)

_DIRECTIVE_RE = re.compile(
    r"\b(source|src|language|lang|category|cat)\s*:\s*(\"[^\"]+\"|'[^']+'|[^\s]+)",
    re.IGNORECASE,
)


def _server_detail(resp: httpx.Response) -> str:
    """Pull the API's own ``detail`` message from an error response, if any."""
    try:
        body = resp.json()
        detail = body.get("detail")
        return str(detail).strip() if detail else ""
    except Exception:
        return ""


class ConceptioError(Exception):
    """Friendly error surfaced to CLI/MCP users."""


def _same_origin(left: str, right: str) -> bool:
    """Return whether two URLs have the same scheme and network location."""
    a, b = urlsplit(left), urlsplit(right)
    return (
        a.scheme.lower() == b.scheme.lower()
        and a.netloc.lower() == b.netloc.lower()
    )


def _private_or_local_host(host: str) -> bool:
    """Reject local/private download destinations, including DNS answers."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    try:
        addr = ipaddress.ip_address(host)
        return not addr.is_global
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return True
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        # A public source hostname that is temporarily unresolvable will be
        # handled by httpx; do not turn that into a false SSRF verdict.
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return True
        except ValueError:
            continue
    return False


def _assert_public_download_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ConceptioError("Download failed: only HTTP(S) URLs are allowed.")
    if _private_or_local_host(parsed.hostname):
        raise ConceptioError("Download failed: local and private-network URLs are not allowed.")


def _validate_api_base(value: str) -> str:
    """Validate the configured API origin before any credential is sent."""
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ConceptioError("API base must be an absolute HTTP(S) URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ConceptioError("API base must not contain embedded credentials.")
    if parsed.scheme.lower() == "http" and not _private_or_local_host(parsed.hostname):
        raise ConceptioError("API credentials require HTTPS except for loopback development hosts.")
    return value.rstrip("/")


def parse_query_directives(raw: str) -> Dict[str, Any]:
    """Extract ``source:`` / ``lang:`` / ``category:`` directives from a query.

    Mirrors the Conceptio web frontend: directives are stripped from the search text
    and applied as real API filters (the backend does not parse them itself).
    """
    text = str(raw or "")
    sources: List[str] = []
    category = ""
    language = ""

    def _replace(m):
        nonlocal category, language
        key = (m.group(1) or "").lower()
        val = (m.group(2) or "").strip().strip("\"'")
        if not val:
            return ""
        if key in ("source", "src"):
            sources.append(val)
        elif key in ("category", "cat"):
            category = val
        elif key in ("language", "lang"):
            language = val.lower()
        return ""

    cleaned = _DIRECTIVE_RE.sub(_replace, text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    # De-dupe while preserving order.
    seen = set()
    sources = [s for s in sources if not (s in seen or seen.add(s))]
    return {"query": cleaned, "sources": sources, "category": category, "language": language}


class ConceptioClient:
    def __init__(
        self,
        api_base: Optional[str] = None,
        license_key: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        cfg = load_config()
        self.api_base = _validate_api_base(api_base or cfg.get("api_base") or DEFAULT_API_BASE)
        self.license_key = license_key or cfg.get("license_key") or ""
        self.api_key = api_key or cfg.get("api_key") or ""

    def _headers(self) -> Dict[str, str]:
        # Exactly one credential is sent: an API key wins over a license key.
        # (set_api_key clears the license key; this is a belt-and-suspenders
        # guard for a config edited by hand.)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        elif self.license_key:
            headers["X-License-Key"] = self.license_key
        return headers

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.api_base}{path}"
        last_err: Optional[Exception] = None
        for attempt in range(2):  # one retry on transport/5xx errors
            try:
                current_url = url
                with httpx.Client(timeout=15.0, follow_redirects=False) as client:
                    for _ in range(6):
                        resp = client.get(current_url, params=params if current_url == url else None, headers=self._headers())
                        if 300 <= resp.status_code < 400:
                            location = resp.headers.get("location")
                            if not location:
                                raise ConceptioError("API returned a redirect without a destination.")
                            next_url = urljoin(current_url, location)
                            if not _same_origin(current_url, next_url):
                                raise ConceptioError("API refused a cross-origin redirect to protect credentials.")
                            current_url = next_url
                            continue
                        break
                    else:
                        raise ConceptioError("API returned too many redirects.")
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and attempt == 0:
                        try:
                            wait_s = min(max(float(retry_after), 0.5), 2.0)
                            time.sleep(wait_s)
                            continue
                        except Exception:
                            pass
                    detail = _server_detail(resp)
                    return {"error": detail or UPGRADE_HINT, "results": []}
                if resp.status_code == 401:
                    # 401: no stored credential, or the one saved was rejected.
                    # The fix is to store a valid key and retry.
                    raise ConceptioError(AUTH_REQUIRED_HINT)
                if resp.status_code == 403:
                    # 403 on the free plan: the account's 200-search allowance
                    # is spent (the web app and your agents share one balance).
                    raise ConceptioError(_server_detail(resp) or UPGRADE_HINT)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    return {"error": "Unexpected API response shape", "results": []}
                return data
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt == 0:
                    last_err = e
                    continue
                raise ConceptioError(
                    f"API returned HTTP {e.response.status_code} for {path} — "
                    "try again shortly or check `conceptio --help`."
                ) from e
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_err = e
                if attempt == 0:
                    continue
        raise ConceptioError(
            f"Could not reach the Conceptio API at {self.api_base}{path} "
            f"({type(last_err).__name__ if last_err else 'unknown error'}). "
            "Check your connection or the API base in ~/.conceptio/config.json."
        ) from last_err

    # ── Public API surface ───────────────────────────────────────────────────
    def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        category: Optional[str] = None,
        language: Optional[str] = None,
        sources: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Search the archive. ``query`` may contain ``source:`` directives."""
        parsed = parse_query_directives(query)
        params: Dict[str, Any] = {
            "q": parsed["query"] or query,
            "limit": max(1, min(int(limit), 100)),
            "offset": max(0, int(offset)),
        }
        srcs = list(dict.fromkeys([s for s in (sources or []) if s] + parsed["sources"]))
        if srcs:
            params["sources"] = ",".join(srcs)
        if category or parsed["category"]:
            params["category"] = category or parsed["category"]
        if language or parsed["language"]:
            params["language"] = language or parsed["language"]
        if not (params["q"] or srcs):
            return {"error": "Empty search query.", "results": []}
        return self._get_json("/api/search", params)

    def resolve(self, identifier: str, limit: int = 10) -> Dict[str, Any]:
        """Resolve a known identifier (RFC, DOI, arXiv, PMID, PMCID, NIST,
        W3C) to document(s) in the archive; falls back to a text search."""
        return self._get_json("/api/resolve", {"id": str(identifier), "limit": max(1, min(int(limit), 50))})

    def get_document(self, doc_id: int) -> Dict[str, Any]:
        return self._get_json(f"/api/document/{int(doc_id)}")

    def get_citation(self, doc_id: int, format: str = "bibtex") -> str:
        data = self._get_json(f"/api/cite/{int(doc_id)}", {"format": format})
        return str(data.get("citation", ""))

    def quota(self) -> Dict[str, Any]:
        """Current tier/identity as granted by the API (honors the api/license key).

        Response carries: tier, email (Firebase only), and ``auth`` — which
        credential the server actually honored (api_key | license | firebase |
        public).
        """
        return self._get_json("/api/me")

    def resolve_download_url(self, target: str) -> str:
        """Resolve a doc ID or URL to a direct PDF URL."""
        target = str(target or "").strip()
        if not target:
            raise ConceptioError("No download target given (document ID or URL).")
        if target.isdigit():
            doc = self.get_document(int(target))
            direct = doc.get("direct_pdf_url")
            if not direct:
                url_hint = doc.get("url") or ""
                raise ConceptioError(
                    f"Document {target} has no direct PDF link available. "
                    + (f"Open it via {url_hint} " if url_hint else "")
                    + "or run `conceptio info <id>` for the source link."
                )
            return str(direct)
        if target.lower().startswith(("http://", "https://")):
            return target
        raise ConceptioError(
            f"'{target}' is neither a document ID nor a URL. Use `conceptio search` "
            "to find IDs."
        )

    def download_pdf(self, url: str, output_path: str) -> str:
        """Stream a public PDF to disk without credential or disk-exhaustion risks.

        Direct URLs are user-controlled. Local/private destinations are rejected,
        redirects are checked at every hop, credentials never cross origins, and
        the response is capped at ``_MAX_DOWNLOAD_BYTES``.
        """
        configured = urlsplit(self.api_base)
        current_url = str(url)
        temp_path = None
        try:
            _assert_public_download_url(current_url)
            with httpx.Client(timeout=45.0, follow_redirects=False) as client:
                for _ in range(6):
                    target = urlsplit(current_url)
                    _assert_public_download_url(current_url)
                    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf"}
                    if (
                        target.scheme.lower() == "https"
                        and target.netloc.lower() == configured.netloc.lower()
                        and configured.scheme.lower() == "https"
                    ):
                        request_headers.update(self._headers())
                    with client.stream("GET", current_url, headers=request_headers) as stream_resp:
                        if 300 <= stream_resp.status_code < 400:
                            location = stream_resp.headers.get("location")
                            if not location:
                                raise ConceptioError("Download failed: redirect without a destination.")
                            next_url = urljoin(current_url, location)
                            _assert_public_download_url(next_url)
                            current_url = next_url
                            continue
                        if stream_resp.status_code == 429:
                            raise ConceptioError(_server_detail(stream_resp) or "Rate limit exceeded. Try again shortly.")
                        if stream_resp.status_code == 403:
                            raise ConceptioError(_server_detail(stream_resp) or UPGRADE_HINT)
                        stream_resp.raise_for_status()
                        content_length = stream_resp.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > _MAX_DOWNLOAD_BYTES:
                                    raise ConceptioError("Download is larger than the 100 MiB safety limit.")
                            except ValueError:
                                pass
                        destination = Path(output_path)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(
                            mode="wb", prefix=".conceptio-", suffix=".part",
                            dir=str(destination.parent), delete=False,
                        ) as f:
                            temp_path = f.name
                            total = 0
                            for chunk in stream_resp.iter_bytes(chunk_size=8192):
                                total += len(chunk)
                                if total > _MAX_DOWNLOAD_BYTES:
                                    raise ConceptioError("Download is larger than the 100 MiB safety limit.")
                                f.write(chunk)
                        os.replace(temp_path, output_path)
                        temp_path = None
                        return output_path
            raise ConceptioError("Download failed: too many redirects.")
        except httpx.HTTPStatusError as e:
            raise ConceptioError(
                f"Download failed with HTTP {e.response.status_code} from {current_url}."
            ) from e
        except httpx.TransportError as e:
            raise ConceptioError(f"Download failed: {e}") from e
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def download_by_target(self, target: str, output_path: str) -> str:
        """Resolve a doc ID/URL and stream the PDF to ``output_path``."""
        url = self.resolve_download_url(target)
        return self.download_pdf(url, output_path)
