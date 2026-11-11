"""Single-owner bounded log history; public DTOs never contain source cursors."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets
from threading import RLock
import time

from log_event_reports import build_log_event_report

PAGE_BYTES = 256 * 1024


def wire_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


class SnapshotRequired(ValueError):
    code = "snapshot_required"


@dataclass(frozen=True)
class _Entry:
    row: dict
    report: dict
    run_id: str | None
    size: int
    source_cursor: tuple[str, int, int] | None = None


class LogView:
    """Retain at most 10k records / 16 MiB of serialized DTO + report facts.

    The collector is the only writer. Queries copy references under the lock;
    stored entries are private and never mutated. Cursor tokens are stateless,
    authenticated, expire after 60s and bind query, clear revision and watermark.
    No per-client snapshot cache can escape the retention bounds.
    """
    def __init__(self, *, max_items=10_000, max_bytes=16 * 1024 * 1024,
                 cursor_ttl=60, clock=time.monotonic):
        if (type(max_items) is not int or max_items < 1 or type(max_bytes) is not int
                or max_bytes < 1024 or not 0 < cursor_ttl <= 3600):
            raise ValueError("invalid history bounds")
        self.max_items, self.max_bytes = max_items, max_bytes
        self.cursor_ttl, self.clock = cursor_ttl, clock
        self._lock = RLock()
        self._rows = deque()
        self._bytes, self.latest_seq, self.evicted = 0, 0, 0
        self._clear_seq, self._clear_rev = 0, 0
        self._key = secrets.token_bytes(32)
        self.query_cpu_ns = 0

    @property
    def min_seq(self):
        with self._lock:
            return self._rows[0].row["seq"] if self._rows else self.latest_seq + 1

    def append(self, dto, *, report=None, run_id=None, payload_bytes=None, source_cursor=None):
        # Inputs are already redacted by the collector. Own all mutable facts.
        # payload_bytes is the collector's already computed conservative size.
        if payload_bytes is not None and (type(payload_bytes) is not int or payload_bytes < 1):
            raise ValueError("invalid payload byte bound")
        row, facts = dict(dto), deepcopy(report) if report else {}
        row["key"] = row["key"] or "log.raw"
        with self._lock:
            row["seq"] = self.latest_seq + 1
            size = ((len(wire_bytes(row)) if payload_bytes is None else payload_bytes)
                    + (len(wire_bytes(facts)) if facts else 0) + len(run_id or ""))
            # Transport provenance stays owner-local; public Log has a closed schema.
            size += len(wire_bytes(source_cursor)) if source_cursor is not None else 0
            if size > self.max_bytes:
                raise ValueError("history record too large")
            self.latest_seq = row["seq"]
            self._rows.append(_Entry(row, facts, run_id, size, source_cursor))
            self._bytes += size
            while len(self._rows) > self.max_items or self._bytes > self.max_bytes:
                self._bytes -= self._rows.popleft().size
                self.evicted += 1
            return row["seq"]

    @property
    def clear_seq(self):
        """Public view-clear watermark (T-P2-31b, 决定_第一波裁定2 T-P2-19 #6).

        Records with ``seq <= clear_seq`` were cleared from the page view;
        exports filter by it. 0 means the view was never cleared.
        """
        with self._lock:
            return self._clear_seq

    def clear_view(self):
        with self._lock:
            self._clear_seq = self.latest_seq
            self._clear_rev += 1
            return self._clear_seq

    def snapshot(self):
        """Owned evidence snapshot, including run IDs, for report/export adapters."""
        with self._lock:
            rows = tuple(self._rows)
        return [(dict(e.row), deepcopy(e.report), e.run_id) for e in rows]

    def metrics(self):
        with self._lock:
            return dict(history_count=len(self._rows), history_bytes=self._bytes,
                        history_evicted=self.evicted, min_seq=self.min_seq,
                        query_cpu_ns=self.query_cpu_ns)

    def _encode_cursor(self, payload):
        data = wire_bytes(payload).hex()
        return data + "." + hmac.new(self._key, data.encode(), hashlib.sha256).hexdigest()

    def _decode_cursor(self, token):
        try:
            if not isinstance(token, str) or len(token) > 2048:
                raise ValueError()
            data, signature = token.split(".")
            expected = hmac.new(self._key, data.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            payload = json.loads(bytes.fromhex(data))
            if self.clock() >= payload["expires"]:
                raise ValueError()
            return payload
        except (ValueError, KeyError, TypeError):
            raise SnapshotRequired("invalid or expired log cursor") from None

    @staticmethod
    def _filter(value):
        if not isinstance(value, dict) or set(value) - {"levels", "sources", "categories", "text"}:
            raise ValueError("invalid log filter")
        result = {}
        for key in ("levels", "sources", "categories"):
            items = value.get(key, [])
            if not isinstance(items, list) or len(items) > 4000 or any(
                    not isinstance(item, str) or len(item) > 128 for item in items):
                raise ValueError("invalid log filter")
            result[key] = sorted(set(items))
        text = value.get("text", "")
        if not isinstance(text, str) or len(text) > 16_384:
            raise ValueError("invalid log text")
        result["text"] = text.casefold()
        return result

    def tail(self, *, n, since_seq=None, cursor=None, translate=None, language=None):
        # T-P4-C1 #2: key-event titles in the caller's language, like query().
        return self._page("tail", n, since_seq, {}, cursor, translate, language)

    def query(self, *, before_seq, filter, n, cursor=None, translate=None, language=None):
        return self._page("query", n, before_seq, self._filter(filter), cursor, translate, language)

    def _page(self, mode, n, bound, filters, cursor, translate, language):
        started = time.thread_time_ns()
        try:
            return self._page_impl(mode, n, bound, filters, cursor, translate, language)
        finally:
            with self._lock:
                self.query_cpu_ns += time.thread_time_ns() - started

    def _page_impl(self, mode, n, bound, filters, cursor, translate, language):
        if type(n) is not int or not 1 <= n <= 4000 or (
                bound is not None and (type(bound) is not int or bound < 0)):
            raise ValueError("invalid log page bounds")
        # T-P4-C1 fix S2: titles render (and the text filter matches) in the
        # language; a cursor from another language is a changed query.
        language = language if isinstance(language, str) else None
        binding = hashlib.sha256(wire_bytes([mode, n, bound, filters, language])).hexdigest()
        previous = self._decode_cursor(cursor) if cursor is not None else None
        with self._lock:
            rows = tuple(self._rows)  # Bounded snapshot; no filtering/translation here.
            minimum, latest, clear, rev = self.min_seq, self.latest_seq, self._clear_seq, self._clear_rev
        if previous:
            if previous["binding"] != binding or previous["rev"] != rev:
                raise SnapshotRequired("log query changed")
            watermark, lower, upper, position = (previous[k] for k in ("watermark", "lower", "upper", "position"))
            if minimum > previous["minimum"]:
                raise SnapshotRequired("log snapshot evicted")
            expires = previous["expires"]
        else:
            watermark = latest
            lower = max(clear, bound or 0) if mode == "tail" else clear
            upper = latest + 1 if mode == "tail" else min(bound, latest + 1)
            if mode == "tail" and bound is None:
                lower = max(clear, latest - n)
            position = lower if mode == "tail" else upper
            expires = self.clock() + self.cursor_ttl
        missing = max(0, min(minimum, upper) - lower - 1)
        membership = {k: set(filters.get(k, [])) for k in ("levels", "sources", "categories")}
        def matching_rows():
            for entry in (rows if mode == "tail" else reversed(rows)):
                seq = entry.row["seq"]
                if not lower < seq < upper or not (seq > position if mode == "tail" else seq < position):
                    continue
                boundary = entry.row["key"] == "source_boundary"
                if not boundary and any(membership[key] and entry.row[field] not in membership[key] for key, field in
                       (("levels", "level"), ("sources", "source"), ("categories", "category"))):
                    continue
                row = dict(entry.row)
                if entry.report:
                    report = build_log_event_report(entry.report, translate=translate)
                    row["title"], row["detail"] = report.headline, report.detail
                if not boundary and filters.get("text") and filters["text"] not in " ".join(
                        str(row[k]) for k in ("key", "title", "detail", "raw")).casefold():
                    continue
                yield row
        matched = matching_rows()
        page = dict(records=[], watermark=watermark, next_cursor=None,
                    gap=bool(missing), dropped=missing)
        encoded_size = 0
        row = next(matched, None)
        while row is not None:
            following = next(matched, None)  # One matching lookahead proves continuation.
            next_payload = dict(binding=binding, rev=rev, watermark=watermark, lower=lower,
                                upper=upper, position=row["seq"], minimum=minimum, expires=expires)
            next_token = self._encode_cursor(next_payload) if following is not None else None
            candidate = dict(page, next_cursor=next_token)
            # Account the exact UTF-8 LogPage including cursor and commas.
            empty_size = len(wire_bytes(dict(candidate, records=[])))
            row_size = len(wire_bytes(row))
            if empty_size + encoded_size + row_size + len(page["records"]) > PAGE_BYTES:
                if not page["records"]:
                    raise SnapshotRequired("rendered log exceeds page budget")
                break
            page["records"].append(row)
            page["next_cursor"] = next_token
            encoded_size += row_size
            if len(page["records"]) == n:
                break
            row = following
        return page
