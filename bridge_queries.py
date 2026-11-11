"""Four one-shot normal-domain queries; no repair, refresh or periodic query."""

from threading import Lock

from bridge_jobs import JobFailure
from bridge_runtime_access import observation_ms
from bridge_tiers import owned


def _error(code, key):
    return {"code": code, "message_key": key, "params": {}}


class ReadQueries:
    """Shared config revision seam for the future controlled-write owner.

    rev=0 means no controlled bridge write in this epoch. External file changes
    do not increment it. The config writer must inject revision/pending getters
    when it is wired; neither polling nor a query fabricates a write revision.
    """

    def __init__(self, access, *, revision=lambda: 0, pending=lambda: None,
                 autostart_manager=None):
        self.access, self.revision, self.pending = access, revision, pending
        self.autostart_manager = autostart_manager
        self._autostart_lock = Lock()

    @staticmethod
    def _current(raw):
        raw = owned(raw)
        if raw.get("stale"):
            raise JobFailure("snapshot_required")
        return raw

    def app_info(self, args, context):
        raw = self._current(self.access.app_info())
        return {key: raw[key] for key in ("version", "language", "caps", "listeners")}

    def autostart_state(self, args, context):
        from bridge_autostart import autostart_state, sidecar_autostart_manager
        # Never call the legacy Qt manager's projection: target must describe
        # the validated shell installation, even before P2-28 runtime injection.
        with self._autostart_lock:
            if self.autostart_manager is None:
                self.autostart_manager = sidecar_autostart_manager()
            return autostart_state(self.autostart_manager)

    def config_snapshot(self, args, context):
        before = self.revision()
        raw = self._current(self.access.config_values())
        pending = owned(self.pending())
        if self.revision() != before:
            raise JobFailure("snapshot_required")
        return {"rev": before, "values": raw["values"],
                "secret_fields_set": raw["secret_fields_set"],
                "read_errors": raw["read_errors"], "pending": pending}

    def geo_status(self, args, context):
        from bridge_runtime_access import geo_items, geo_status_facts
        raw = self._current(geo_status_facts(self.access))
        if raw.get("unknown"):
            return {"running": False, "updated_at_ms": None,
                    "errors": [_error("internal", "errors.geo.status_unknown")]}
        stamps = [observation_ms(value) for value in raw.get("last_update", {}).values()]
        return {"running": raw["running"],
                "updated_at_ms": max((stamp for stamp in stamps if stamp is not None), default=None),
                # Raw errors can contain download credentials and file paths.
                "errors": [_error("io", "errors.geo.update_failed")
                           for _ in raw.get("errors", {})],
                # T-P4-C2 #11 (settings R-11), wired by T-P2-H2: per-kind rows.
                "items": geo_items(raw)}

    def register(self, register_query):
        for name, handler in (("app.info", self.app_info),
                              ("autostart.state", self.autostart_state),
                              ("config.snapshot", self.config_snapshot),
                              ("geo.status", self.geo_status)):
            register_query(name, handler)
