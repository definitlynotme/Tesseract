from __future__ import annotations

import base64
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from client_tls import shared_identity_context
from exit_quality_endpoints import (
    EXIT_IDENTITY_PROVIDERS,
    IdentityProviderEntries,
    SameHostHttpsRedirectHandler,
)

IDENT_ME_PROFILE_FIELDS: tuple[str, ...] = (
    "aso",
    "asn",
    "cc",
    "country",
    "city",
    "tz",
    "type",
)
# ident.me's own network classification; any other value is not shown.
IDENT_ME_NETWORK_TYPES = frozenset(
    {"backbone", "business", "hosting", "mobile", "residential", "isp"}
)
_IANA_TIMEZONE_PATTERN = re.compile(r"(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){1,2})")
_EVENT_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class IdentityAnswerPolicy:
    """Return once the answer is good enough instead of waiting for laggards.

    The primary family returns at its first usable IP plus a short
    confirmation grace, extended while a profile-bearing source for that IP
    is still pending. The other family is best effort and can extend the
    answer only briefly. When the primary family stays silent (its sources
    hang) but the other family has answered, the primary gets a bounded
    extra grace instead of the whole budget. Nothing is ever cut before a
    usable IP exists.
    """

    primary_family: int = 4
    confirm_grace_seconds: float = 0.75
    profile_grace_seconds: float = 1.5
    secondary_confirm_grace_seconds: float = 0.75
    secondary_silent_grace_seconds: float = 0.35
    primary_silent_grace_seconds: float = 1.5
    retry_families: tuple[int, ...] = (4,)
    profile_sources: tuple[str, ...] = ("ident.me",)

    def validate(self) -> None:
        if self.primary_family not in (4, 6):
            raise ValueError("answer policy primary family must be 4 or 6")
        graces = (
            self.confirm_grace_seconds,
            self.profile_grace_seconds,
            self.secondary_confirm_grace_seconds,
            self.secondary_silent_grace_seconds,
            self.primary_silent_grace_seconds,
        )
        if not all(math.isfinite(value) and value >= 0 for value in graces):
            raise ValueError("answer policy graces must be finite and nonnegative")
        if self.profile_grace_seconds < self.confirm_grace_seconds:
            raise ValueError("answer policy profile grace must cover the confirm grace")


OVERVIEW_ANSWER_POLICY = IdentityAnswerPolicy()

def _clean_detail(value: Any) -> str:
    text = str(value or "").strip()
    return text if text and text.lower() not in {"none", "null", "unknown"} else ""


def _public_global_ip(value: Any, *, expected_family: int) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("invalid IP address") from exc
    if address.version != expected_family:
        raise ValueError(f"expected IPv{expected_family}, received IPv{address.version}")
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or bool(getattr(address, "is_site_local", False))
        or getattr(address, "ipv4_mapped", None) is not None
    ):
        raise ValueError(f"IPv{expected_family} address is not public-global")
    return address.compressed


def _identity_observation_from_payload(
    provider: str,
    payload: bytes,
    *,
    expected_family: int,
) -> dict[str, Any]:
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("empty identity response")

    data: dict[str, Any] | None = None
    ip_value: Any
    if text.startswith("{"):
        decoded = json.loads(text)
        if not isinstance(decoded, dict):
            raise ValueError("unexpected identity JSON response")
        data = decoded
        if "ip" not in data:
            raise ValueError(f"{provider} response did not include an IP")
        ip_value = data["ip"]
    else:
        ip_value = text.split()[0]

    observation: dict[str, Any] = {
        "ip": _public_global_ip(ip_value, expected_family=expected_family),
    }
    if provider.casefold() == "ident.me" and data is not None:
        profile = {
            key: data[key]
            for key in IDENT_ME_PROFILE_FIELDS
            if key in data and data[key] not in (None, "")
        }
        if profile:
            observation["profile"] = profile
    return observation


class _IdentityDeadline:
    """Bound the collection and interrupt response reads when its budget ends."""

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds
        self.deadline = time.monotonic() + timeout_seconds
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._sockets: set[Any] = set()

    def remaining(self, timeout_seconds: float) -> float:
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("identity collection deadline exceeded")
        # Subtracting large monotonic values can round above the original budget.
        return min(timeout_seconds, self._timeout_seconds, remaining)

    @staticmethod
    def _interrupt(sock: Any) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass

    def bind_response(self, response: Any, timeout_seconds: float) -> Any | None:
        # urllib's HTTPResponse owns this socket. Shutdown wakes a blocked read;
        # its worker keeps responsibility for closing the response in __exit__.
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is None:
            self.remaining(timeout_seconds)
            return None
        with self._lock:
            if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                self._interrupt(sock)
                raise TimeoutError("identity collection deadline exceeded")
            sock.settimeout(self.remaining(timeout_seconds))
            self._sockets.add(sock)
        return sock

    def release_response(self, sock: Any | None) -> None:
        if sock is not None:
            with self._lock:
                self._sockets.discard(sock)

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            sockets = tuple(self._sockets)
        for sock in sockets:
            self._interrupt(sock)


def _request_identity(
    opener: Any,
    url: str,
    *,
    timeout_seconds: float,
    deadline: _IdentityDeadline | None = None,
) -> bytes:
    headers = {"User-Agent": "Tesseract/1.0"}
    request = urllib.request.Request(url, headers=headers, method="GET")
    with opener.open(request, timeout=timeout_seconds) as response:
        sock = deadline.bind_response(response, timeout_seconds) if deadline is not None else None
        try:
            final_url = str(getattr(response, "geturl", lambda: url)() or url)
            expected = urllib.parse.urlparse(url)
            final = urllib.parse.urlparse(final_url)
            if final.scheme.lower() != "https" or final.hostname != expected.hostname:
                raise ValueError("identity endpoint redirected outside its HTTPS host")
            payload = response.read(8193)
            if deadline is not None:
                deadline.remaining(timeout_seconds)
            if len(payload) > 8192:
                raise ValueError("identity endpoint response exceeded the size limit")
            return payload
        finally:
            if deadline is not None:
                deadline.release_response(sock)


def _identity_error(exc: Exception, *, url: str) -> str:
    message = " ".join(str(exc).split()).replace(url, "<endpoint>")
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"[:240]


def _identity_http_status(exc: Exception) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, Exception):
        return _identity_http_status(exc.reason)
    # http.client wraps a rejected CONNECT response in OSError, not HTTPError.
    # Match that exact protocol error; unrelated OS failures are not retryable.
    if isinstance(exc, OSError):
        match = re.fullmatch(r"Tunnel connection failed: ([1-5][0-9]{2})(?:\s+[^\r\n]*)?", str(exc))
        if match:
            return int(match.group(1))
    return None


def _identity_retry_after_seconds(exc: Exception) -> float | None:
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        return _identity_retry_after_seconds(exc.reason) if isinstance(exc.reason, Exception) else None
    if not isinstance(exc, urllib.error.HTTPError) or exc.headers is None:
        return None
    value = str(exc.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            requested = parsedate_to_datetime(value)
            if requested.tzinfo is None:
                requested = requested.replace(tzinfo=timezone.utc)
            seconds = max(0.0, requested.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _identity_error_is_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, Exception):
        return _identity_error_is_timeout(exc.reason)
    return False


def _identity_transport_error_is_transient(exc: Exception) -> bool:
    http_status = _identity_http_status(exc)
    if http_status is not None:
        return http_status in {408, 429} or 500 <= http_status <= 599
    if isinstance(exc, urllib.error.URLError):
        return (
            isinstance(exc.reason, Exception)
            and _identity_transport_error_is_transient(exc.reason)
        )
    if isinstance(exc, socket.gaierror):
        return exc.errno == socket.EAI_AGAIN
    return isinstance(exc, (TimeoutError, ConnectionError, ssl.SSLEOFError))


def _probe_identity_provider(
    provider: str,
    family: int,
    url: str,
    round_number: int,
    *,
    proxy_url: str | None,
    timeout_seconds: float,
    opener: Any | None,
    opener_factory: Callable[[str | None], Any] | None,
    deadline: _IdentityDeadline | None = None,
    retry_transient_errors: bool = False,
    on_transport_retry: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        if deadline is not None:
            deadline.remaining(timeout_seconds)
        request_opener = opener
        if request_opener is None:
            request_opener = opener_factory(proxy_url) if opener_factory else _build_opener(proxy_url)
        request_timeout = deadline.remaining(timeout_seconds) if deadline is not None else timeout_seconds
        payload = _request_identity(
            request_opener,
            url,
            timeout_seconds=request_timeout,
            deadline=deadline,
        )
        observation = _identity_observation_from_payload(
            provider,
            payload,
            expected_family=family,
        )
        return {
            "round": round_number,
            "status": "ok",
            **observation,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except ValueError as exc:
        return {
            "round": round_number,
            "status": "invalid",
            "error": _identity_error(exc, url=url),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:
        attempt = {
            "round": round_number,
            "status": "error",
            "error": _identity_error(exc, url=url),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        http_status = _identity_http_status(exc)
        retry_after = _identity_retry_after_seconds(exc)
        if http_status is not None:
            attempt["http_status"] = http_status
        if http_status == 429:
            attempt["error_code"] = "rate_limited"
        elif _identity_error_is_timeout(exc):
            attempt["error_code"] = "timeout"
        if retry_after is not None:
            attempt["retry_after_seconds"] = retry_after
        transient = _identity_transport_error_is_transient(exc)
        if isinstance(exc, urllib.error.HTTPError):
            # HTTPError owns the failed response. Release its stream before
            # another attempt rather than keeping it alive in this except.
            try:
                exc.close()
            except Exception:
                pass
        if (
            retry_transient_errors
            and deadline is not None
            and not deadline.cancelled.is_set()
            and deadline.deadline - time.monotonic() > (retry_after or 0.0) + 0.2
            and transient
        ):
            # A transport retry contributes no additional source or round.
            # It runs here so another family's slow provider cannot prevent
            # this source from recovering within the existing shared deadline.
            if on_transport_retry is not None:
                on_transport_retry(attempt)
            if retry_after and deadline.cancelled.wait(retry_after):
                return attempt
            if deadline.cancelled.is_set() or deadline.deadline - time.monotonic() <= 0.2:
                return attempt
            retried = _probe_identity_provider(
                provider, family, url, round_number,
                proxy_url=proxy_url,
                timeout_seconds=timeout_seconds,
                opener=opener,
                opener_factory=opener_factory,
                deadline=deadline,
            )
            retried["transport_retries"] = [attempt]
            return retried
        return attempt


def _identity_source_evidence(
    provider: str,
    family: int,
    url: str,
    attempts: list[dict[str, Any]],
    *,
    expected_rounds: int,
) -> dict[str, Any]:
    ordered_attempts = sorted(attempts, key=lambda item: int(item["round"]))
    observed_ips = list(
        dict.fromkeys(
            str(item["ip"])
            for item in ordered_attempts
            if item.get("status") == "ok" and item.get("ip")
        )
    )
    successful_attempts = sum(item.get("status") == "ok" for item in ordered_attempts)
    if not observed_ips:
        stability = "unavailable"
    elif len(observed_ips) > 1:
        stability = "rotating"
    elif successful_attempts == expected_rounds:
        stability = "stable"
    else:
        stability = "unconfirmed"

    evidence: dict[str, Any] = {
        "source": provider,
        "family": family,
        "endpoint": url,
        "stability": stability,
        "observed_ips": observed_ips,
        "successful_attempts": successful_attempts,
        "attempted_rounds": expected_rounds,
        "attempts": ordered_attempts,
    }
    profiles = {
        str(item["ip"]): item["profile"]
        for item in ordered_attempts
        if item.get("status") == "ok"
        and item.get("ip")
        and isinstance(item.get("profile"), dict)
    }
    if profiles:
        evidence["profiles"] = profiles
        if len(observed_ips) == 1 and observed_ips[0] in profiles:
            evidence["profile"] = profiles[observed_ips[0]]
    return evidence


def _identity_family_result(family: int, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    responding = [item for item in evidence if item["observed_ips"]]
    rotating = [item for item in responding if len(item["observed_ips"]) > 1]
    voters = [item for item in responding if len(item["observed_ips"]) == 1]
    stable_voters = [item for item in voters if item["stability"] == "stable"]
    votes = Counter(item["observed_ips"][0] for item in voters)
    stable_votes = Counter(item["observed_ips"][0] for item in stable_voters)
    agreeing_sources = max(votes.values(), default=0)
    stable_agreeing_sources = max(stable_votes.values(), default=0)
    observed_ips = list(
        dict.fromkeys(
            ip
            for item in evidence
            for ip in item["observed_ips"]
        )
    )

    if rotating:
        state = "rotating"
        confidence = "low"
        selected_ip = None
    elif not responding:
        state = "unavailable"
        confidence = "none"
        selected_ip = None
    elif len(votes) > 1:
        state = "conflict"
        confidence = "low"
        selected_ip = None
    elif stable_agreeing_sources >= 2:
        state = "confirmed"
        confidence = "medium"
        selected_ip = next(iter(votes))
    else:
        state = "unconfirmed"
        confidence = "low"
        selected_ip = next(iter(votes))

    result = {
        "family": family,
        "state": state,
        "confidence": confidence,
        "ip": selected_ip,
        "observed_ips": observed_ips,
        "agreement": {
            "agreeing_sources": agreeing_sources,
            "stable_agreeing_sources": stable_agreeing_sources,
            "successful_sources": len(responding),
            "attempted_sources": len(evidence),
            "voting_sources": len(voters),
            "disagreeing_sources": len(voters) - agreeing_sources,
            "stable_sources": sum(item["stability"] == "stable" for item in evidence),
            "rotating_sources": len(rotating),
        },
        "evidence": evidence,
    }
    result.update(_identity_family_profile_fields(family, selected_ip, evidence))
    return result


def _identity_family_profile_fields(
    family: int,
    selected_ip: str | None,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Expose source-reported context for this exact observed address only.

    The bounded overview lookup does not run the slower quality enrichment
    service. ident.me already includes these fields with its IP response, so
    retain them on the family returned to the caller without another request.
    Conflicting/rotating identities have no selected address and no context.
    """
    if not selected_ip:
        return {}
    profiles = [
        attempt["profile"]
        for item in evidence
        if str(item.get("source") or "").casefold() == "ident.me"
        for attempt in item.get("attempts", [])
        if isinstance(attempt, dict)
        and attempt.get("status") == "ok"
        and attempt.get("ip") == selected_ip
        and isinstance(attempt.get("profile"), dict)
    ]
    if not profiles:
        return {}

    def reported_fields(fields: tuple[tuple[str, str], ...]) -> tuple[dict[str, str], list[str]]:
        reported: dict[str, str] = {}
        conflicts: list[str] = []
        for source_key, target_key in fields:
            values = []
            for profile in profiles:
                value = _clean_detail(profile.get(source_key))
                if target_key == "country_code":
                    value = value.upper()
                elif target_key == "timezone":
                    if len(value) > 64 or _IANA_TIMEZONE_PATTERN.fullmatch(value) is None:
                        value = ""
                elif target_key == "type":
                    value = value.casefold()
                    if value not in IDENT_ME_NETWORK_TYPES:
                        value = ""
                elif target_key == "asn":
                    value = value.upper().removeprefix("AS").strip()
                    valid_number = (
                        0 < len(value) <= 10 and value.isascii() and value.isdecimal()
                    )
                    number = int(value) if valid_number else 0
                    value = str(number) if 0 < number < 2**32 else ""
                if value and value not in values:
                    values.append(value)
            if len(values) == 1:
                reported[target_key] = values[0]
            elif len(values) > 1:
                conflicts.append(target_key)
        return reported, conflicts

    result: dict[str, Any] = {}
    location, location_conflicts = reported_fields((
        ("cc", "country_code"), ("country", "country"), ("city", "city"),
        ("tz", "timezone"),
    ))
    if location or location_conflicts:
        result["location"] = {
            **location,
            "ip": selected_ip,
            "state": "conflict" if location_conflicts else "reported",
            "source": "ident.me",
            "observations_consistent": not bool(location_conflicts),
            "conflicting_fields": location_conflicts,
        }
    network, network_conflicts = reported_fields((
        ("asn", "asn"), ("aso", "organisation"), ("type", "type"),
    ))
    if network or network_conflicts:
        result["network"] = {
            **network,
            "family": family,
            "ip": selected_ip,
            "source": "ident.me",
            "source_conflict": bool(network_conflicts),
            "conflicts": network_conflicts,
        }
    return result


def collect_exit_identity(
    *,
    proxy_url: str | None = None,
    timeout_seconds: float = 4.0,
    total_timeout_seconds: float | None = None,
    opener: Any | None = None,
    opener_factory: Callable[[str | None], Any] | None = None,
    providers: IdentityProviderEntries | None = None,
    rounds: int = 2,
    max_workers: int = 4,
    retry_transient_errors: bool = False,
    completed_family_grace_seconds: float | None = None,
    answer_policy: IdentityAnswerPolicy | None = None,
    not_before: threading.Event | None = None,
    cancel: threading.Event | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Collect independent IPv4/IPv6 exit observations without scoring them.

    Each provider contributes at most one vote per address family. Repeated rounds
    establish provider stability and are retained verbatim as source evidence.
    ident.me JSON profile fields, when supplied, stay attached to their observed IP.
    An optional total budget bounds all providers and rounds together. Unfinished
    attempts remain timeout evidence and cannot replace the returned snapshot.
    Opt-in transport retries stay within that budget and keep one observation
    per source and round, retaining the first failure as retry evidence.
    A single-round overview may stop waiting for a silent address family once
    the other family's sources have all settled on a usable IP. Any response
    from the other family keeps its remaining evidence collection in scope.

    ``answer_policy`` (single round, bounded) instead returns shortly after the
    first usable answer; sources it stopped waiting for are reported as
    ``abandoned``. ``not_before`` holds such an early answer open until the
    event is set (a complete collection still returns at once), and ``cancel``
    ends the collection immediately.
    """

    if opener is not None and opener_factory is not None:
        raise ValueError("pass opener or opener_factory, not both")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if total_timeout_seconds is not None and (
        not math.isfinite(total_timeout_seconds) or total_timeout_seconds <= 0
    ):
        raise ValueError("total_timeout_seconds must be finite and positive")
    if rounds <= 0:
        raise ValueError("rounds must be positive")
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    if retry_transient_errors and total_timeout_seconds is None:
        raise ValueError("retry_transient_errors requires total_timeout_seconds")
    if completed_family_grace_seconds is not None:
        if not math.isfinite(completed_family_grace_seconds) or completed_family_grace_seconds < 0:
            raise ValueError("completed_family_grace_seconds must be finite and nonnegative")
        if total_timeout_seconds is None or rounds != 1:
            raise ValueError("completed_family_grace_seconds requires a total timeout and rounds=1")
    if answer_policy is not None:
        answer_policy.validate()
        if total_timeout_seconds is None or rounds != 1:
            raise ValueError("answer_policy requires a total timeout and rounds=1")
        if completed_family_grace_seconds is not None:
            raise ValueError("answer_policy replaces completed_family_grace_seconds")
    elif not_before is not None:
        raise ValueError("not_before requires an answer_policy")

    started = time.monotonic()
    deadline = _IdentityDeadline(total_timeout_seconds) if total_timeout_seconds is not None else None
    selected_providers = tuple(EXIT_IDENTITY_PROVIDERS if providers is None else providers)
    normalized: list[tuple[str, int, str]] = []
    seen_sources: set[tuple[int, str]] = set()
    seen_endpoints: set[tuple[int, str]] = set()
    for entry in selected_providers:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ValueError("identity providers must be (name, family, url) entries")
        provider = _clean_detail(entry[0])
        try:
            family = int(entry[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("identity provider family must be 4 or 6") from exc
        url = _clean_detail(entry[2])
        if not provider or family not in (4, 6) or not url.startswith("https://"):
            raise ValueError("identity providers require a name, IPv4/IPv6 family, and HTTPS URL")
        source_key = (family, provider.casefold())
        endpoint_key = (family, url)
        if source_key in seen_sources or endpoint_key in seen_endpoints:
            continue
        seen_sources.add(source_key)
        seen_endpoints.add(endpoint_key)
        normalized.append((provider, family, url))

    attempts_by_provider: dict[tuple[int, int], dict[str, Any]] = {}
    retry_evidence: dict[tuple[int, int], dict[str, Any]] = {}
    retry_evidence_lock = threading.Lock()
    family_completion_deadline: float | None = None
    completion_reason = "complete"

    def completed_family_with_silent_other() -> bool:
        for family in (4, 6):
            indices = [index for index, entry in enumerate(normalized) if entry[1] == family]
            if not indices or not all((index, 1) in attempts_by_provider for index in indices):
                continue
            observed = {
                str(attempts_by_provider[(index, 1)]["ip"])
                for index in indices
                if attempts_by_provider[(index, 1)].get("status") == "ok"
                and attempts_by_provider[(index, 1)].get("ip")
            }
            if len(observed) != 1:
                continue
            other_responded = any(
                entry[1] != family
                and attempts_by_provider.get((index, 1), {}).get("status") == "ok"
                for index, entry in enumerate(normalized)
            )
            if not other_responded:
                return True
        return False

    completed_at_by_key: dict[tuple[int, int], float] = {}
    answer_family: list[int | None] = [None]

    def family_answer_state(family: int) -> dict[str, Any]:
        indices = [index for index, entry in enumerate(normalized) if entry[1] == family]
        settled = [index for index in indices if (index, 1) in attempts_by_provider]
        ok = sorted(
            (completed_at_by_key[(index, 1)], str(attempts_by_provider[(index, 1)]["ip"]))
            for index in settled
            if attempts_by_provider[(index, 1)].get("status") == "ok"
            and attempts_by_provider[(index, 1)].get("ip")
        )
        ips = {ip for _at, ip in ok}
        state: dict[str, Any] = {
            "indices": indices,
            "settled": bool(indices) and len(settled) == len(indices),
            "settled_at": max((completed_at_by_key[(index, 1)] for index in settled), default=None),
            "ips": ips,
            "first_ok_at": ok[0][0] if ok else None,
            "confirmed_at": ok[1][0] if len(ips) == 1 and len(ok) >= 2 else None,
            "profile_at": None,
            "profile_pending": False,
        }
        if len(ips) == 1 and answer_policy is not None:
            ip = next(iter(ips))
            profile_names = {name.casefold() for name in answer_policy.profile_sources}
            state["profile_at"] = min(
                (
                    completed_at_by_key[(index, 1)]
                    for index in settled
                    if normalized[index][0].casefold() in profile_names
                    and attempts_by_provider[(index, 1)].get("status") == "ok"
                    and attempts_by_provider[(index, 1)].get("ip") == ip
                    and isinstance(attempts_by_provider[(index, 1)].get("profile"), dict)
                ),
                default=None,
            )
            state["profile_pending"] = any(
                normalized[index][0].casefold() in profile_names
                and (index, 1) not in attempts_by_provider
                for index in indices
            )
        return state

    def answer_stop_at() -> float | None:
        """Absolute stop time from fixed arrival timestamps, or None to keep waiting."""
        assert answer_policy is not None
        answer_family[0] = None
        primary = family_answer_state(answer_policy.primary_family)
        secondary = family_answer_state(10 - answer_policy.primary_family)
        silent_primary_grace = 0.0
        if len(primary["ips"]) == 1:
            answer, other = primary, secondary
            answer_family[0] = answer_policy.primary_family
        elif len(primary["ips"]) > 1 or primary["settled"] or not primary["indices"]:
            if len(secondary["ips"]) != 1:
                return None
            answer, other = secondary, None
            answer_family[0] = 10 - answer_policy.primary_family
        elif len(secondary["ips"]) == 1:
            # The primary has no IP and its sources are still out (hung):
            # answer with the other family after a bounded wait for a late
            # primary. Any primary IP before then takes the branch above.
            answer, other = secondary, None
            answer_family[0] = 10 - answer_policy.primary_family
            silent_primary_grace = answer_policy.primary_silent_grace_seconds
        else:
            return None
        first_ok = answer["first_ok_at"]
        if answer["profile_at"] is not None or not answer["profile_pending"]:
            ready_at = answer["profile_at"] if answer["profile_at"] is not None else first_ok
            stop = max(first_ok + answer_policy.confirm_grace_seconds, ready_at)
            if answer["confirmed_at"] is not None:
                stop = min(stop, max(answer["confirmed_at"], ready_at))
        else:
            stop = first_ok + answer_policy.profile_grace_seconds
        if answer["settled"]:
            stop = min(stop, answer["settled_at"])
        if other is not None and other["indices"] and not other["settled"] and len(other["ips"]) <= 1:
            if other["ips"]:
                stop = max(stop, min(
                    other["first_ok_at"] + answer_policy.secondary_confirm_grace_seconds,
                    stop + answer_policy.secondary_confirm_grace_seconds,
                ))
            else:
                stop += answer_policy.secondary_silent_grace_seconds
        return stop + silent_primary_grace

    # When the collector first saw the hold released (-inf without a hold).
    release_seen_at: list[float | None] = [None if not_before is not None else float("-inf")]

    def policy_cutoff() -> float:
        """The deadline, or the answer stop once any hold is released.

        Results that arrived while the answer was held still count: the stop
        never falls before the moment the release was first observed.
        """
        assert answer_policy is not None and deadline is not None
        answer_stop = answer_stop_at()
        if answer_stop is None:
            return deadline.deadline
        if release_seen_at[0] is None:
            if not not_before.is_set():
                return deadline.deadline
            release_seen_at[0] = time.monotonic()
        return min(deadline.deadline, max(answer_stop, release_seen_at[0]))

    def cutoff_reason(cutoff: float) -> str:
        if deadline is not None and cutoff < deadline.deadline:
            if answer_policy is not None:
                return "answered"
            if family_completion_deadline is not None:
                return "completed_family"
        return "deadline"

    def run_probe(index: int, provider: str, family: int, url: str, round_number: int) -> tuple[dict[str, Any], float]:
        def retain_retry_evidence(attempt: dict[str, Any]) -> None:
            # Keep the first failure even when the retry itself runs until
            # the deadline and receives the collector's timeout placeholder.
            with retry_evidence_lock:
                retry_evidence[(index, round_number)] = dict(attempt)

        attempt = _probe_identity_provider(
            provider,
            family,
            url,
            round_number,
            proxy_url=proxy_url,
            timeout_seconds=timeout_seconds,
            opener=opener,
            opener_factory=opener_factory,
            deadline=deadline,
            retry_transient_errors=bool(
                retry_transient_errors
                and (answer_policy is None or family in answer_policy.retry_families)
            ),
            on_transport_retry=retain_retry_evidence,
        )
        return attempt, time.monotonic()

    if normalized:
        worker_count = 1 if opener is not None else min(max_workers, 8, len(normalized))
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="exit-identity")
        try:
            for round_number in range(1, rounds + 1):
                if deadline is not None and time.monotonic() >= deadline.deadline:
                    completion_reason = "deadline"
                    break
                pending = {
                    executor.submit(
                        run_probe,
                        index,
                        provider,
                        family,
                        url,
                        round_number,
                    ): index
                    for index, (provider, family, url) in enumerate(normalized)
                }
                while pending:
                    if cancel is not None and cancel.is_set():
                        completion_reason = "cancelled"
                        break
                    cutoff = deadline.deadline if deadline is not None else None
                    if answer_policy is not None:
                        cutoff = policy_cutoff()
                    elif family_completion_deadline is not None:
                        cutoff = min(cutoff, family_completion_deadline)
                    remaining = max(0.0, cutoff - time.monotonic()) if cutoff is not None else None
                    wait_timeout = remaining
                    if cancel is not None or (not_before is not None and not not_before.is_set()):
                        # Events cannot be waited on together with futures.
                        wait_timeout = (
                            _EVENT_POLL_SECONDS
                            if wait_timeout is None
                            else min(wait_timeout, _EVENT_POLL_SECONDS)
                        )
                    done, _unfinished = wait(pending, timeout=wait_timeout, return_when=FIRST_COMPLETED)
                    if not done:
                        if cutoff is None or time.monotonic() < cutoff:
                            continue
                        completion_reason = cutoff_reason(cutoff)
                        break
                    finished = []
                    for future in done:
                        index = pending.pop(future)
                        attempt, completed_at = future.result()
                        finished.append((completed_at, index, attempt))
                    # Accept a batch in arrival order: an earlier arrival can
                    # move the answer stop for a later one in the same batch,
                    # so the decision depends on arrival timestamps, not on
                    # how late the collector thread woke up.
                    for completed_at, index, attempt in sorted(finished, key=lambda item: item[:2]):
                        if answer_policy is not None:
                            cutoff = policy_cutoff()
                        if cutoff is None or completed_at <= cutoff:
                            attempts_by_provider[(index, round_number)] = attempt
                            completed_at_by_key[(index, round_number)] = completed_at
                        else:
                            completion_reason = cutoff_reason(cutoff)
                    if pending and completed_family_grace_seconds is not None:
                        if completed_family_with_silent_other():
                            if family_completion_deadline is None:
                                family_completion_deadline = time.monotonic() + completed_family_grace_seconds
                        else:
                            # A responding second family deserves the full
                            # budget to confirm it or retain conflicting IPs.
                            family_completion_deadline = None
                if pending:
                    for future in pending:
                        future.cancel()
                    break
        finally:
            if deadline is not None:
                deadline.cancel()
            # Running requests own and close their response resources. They use
            # the remaining socket budget; queued work is cancelled immediately.
            executor.shutdown(wait=deadline is None, cancel_futures=True)

    with retry_evidence_lock:
        retry_snapshot = dict(retry_evidence)
    abandoned = completion_reason in {"answered", "cancelled"}
    family_evidence: dict[int, list[dict[str, Any]]] = {4: [], 6: []}
    for index, (provider, family, url) in enumerate(normalized):
        attempts = [
            attempts_by_provider.get(
                (index, round_number),
                {
                    "round": round_number,
                    "status": "abandoned" if abandoned else "timeout",
                    "error": (
                        "identity source not awaited after answer"
                        if abandoned
                        else "TimeoutError: silent address-family wait budget exceeded"
                        if completion_reason == "completed_family"
                        else "TimeoutError: identity collection deadline exceeded"
                    ),
                    **({"transport_retries": [retry_snapshot[(index, round_number)]]}
                       if (index, round_number) in retry_snapshot else {}),
                },
            )
            for round_number in range(1, rounds + 1)
        ]
        family_evidence[family].append(
            _identity_source_evidence(
                provider,
                family,
                url,
                attempts,
                expected_rounds=rounds,
            )
        )

    def first_ok_ms(family: int) -> float | None:
        times = [
            completed_at_by_key[key]
            for key, attempt in attempts_by_provider.items()
            if normalized[key[0]][1] == family and attempt.get("status") == "ok"
        ]
        return round((min(times) - started) * 1000, 1) if times else None

    collection: dict[str, Any] = {
        "completion_reason": completion_reason,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "first_ok_ms": {"ipv4": first_ok_ms(4), "ipv6": first_ok_ms(6)},
    }
    if answer_policy is not None:
        answer_stop_at()  # report the family that answers in the final state
        collection["answer_family"] = (
            f"ipv{answer_family[0]}" if answer_family[0] is not None else None
        )
        collection["abandoned_sources"] = (
            sum(1 for index in range(len(normalized)) if (index, 1) not in attempts_by_provider)
            if abandoned
            else 0
        )
    return {
        "schema_version": 2,
        "checked_at": float(clock()),
        "proxy": proxy_url or "",
        "rounds": rounds,
        "collection": collection,
        "identity": {
            "ipv4": _identity_family_result(4, family_evidence[4]),
            "ipv6": _identity_family_result(6, family_evidence[6]),
        },
    }


class _ExplicitProxyHandler(urllib.request.ProxyHandler):
    """Keep identity evidence on its selected proxy, including after redirects."""

    def proxy_open(self, req: Any, proxy: str, protocol: str) -> Any:
        # ProxyHandler.proxy_open consults NO_PROXY / Windows ProxyOverride even
        # for an explicitly supplied mapping. Identity probes must never bypass
        # the route whose result the caller is validating.
        parsed = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("identity proxy must be an HTTP or HTTPS proxy")
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        if parsed.username is not None and parsed.password is not None:
            credentials = ":".join(urllib.parse.unquote(value) for value in (parsed.username, parsed.password))
            encoded = base64.b64encode(credentials.encode()).decode("ascii")
            req.add_header("Proxy-Authorization", f"Basic {encoded}")
        original_protocol = req.type
        req.set_proxy(host, parsed.scheme)
        if original_protocol != parsed.scheme and original_protocol != "https":
            return self.parent.open(req, timeout=req.timeout)
        return None


def _build_opener(proxy_url: str | None) -> urllib.request.OpenerDirector:
    # A base-class handler with the shared context: urllib would otherwise load
    # the trust store again for every provider request and retry.
    https_handler = urllib.request.HTTPSHandler(context=shared_identity_context())
    if proxy_url:
        return urllib.request.build_opener(
            _ExplicitProxyHandler({"http": proxy_url, "https": proxy_url}),
            SameHostHttpsRedirectHandler(),
            https_handler,
        )
    return urllib.request.build_opener(SameHostHttpsRedirectHandler(), https_handler)
