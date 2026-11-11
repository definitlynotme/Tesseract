//! T-P3-05 shell-only topics: `alerts.desktop` (routing and the delivery receipt) and
//! `ui.language` (native text and `host:language`).
//!
//! PROTOCOL §13 "通知": Windows success = the shell showed the notice outside the window (the
//! notice card is on screen, or the tray balloon, the card window's fallback, was submitted);
//! in-app success = the page confirmed it rendered the notice. A queued, held or received notice
//! is not success. No visible surface is `dropped/none`; a refused balloon is `failed/windows`.
//! The page never sends a receipt itself: `host_notify_rendered` is checked against the
//! notices this shell handed to the current page connection, then the shell sends
//! `alerts.desktopDelivered` once per delivery id.
//!
//! 10-30 notice redesign (`.build/提醒模块重设计_20261030.md` §4, §10.3–§10.4): a frame may carry a
//! `notice` object (kind, incident, state, message ids, params). Routing: window in front and
//! page ready → in-app; otherwise, and always for a forced preview → the notice card window
//! (notice.rs); while Windows says the user is busy → held (latest frame per incident) and shown
//! once the user is free. The tray balloon is only the fallback when the card window cannot be
//! made or does not render. One incident lives in one place: when it moves between the page and
//! the card window, the other side lets go of it silently.
use crate::{native_text, presence, transport::{Hub, TOPICS, V1_PAGE_TOPICS}, tray::{Balloon, BalloonDone, Severity}};
use serde_json::{json, Value};
use std::{collections::{HashMap, HashSet, VecDeque}, path::PathBuf, sync::{atomic::{AtomicU64, Ordering}, Arc, Mutex}, time::{Duration, Instant}};

pub const DESKTOP: &str = "alerts.desktop";
pub const LANGUAGE: &str = "ui.language";
/// T-P3-06: the update hand-off (update.rs); handled by the worker, never by the page.
pub const PREPARE_EXIT: &str = crate::update::TOPIC;
pub const SHELL_TOPICS: [&str; 3] = [DESKTOP, LANGUAGE, PREPARE_EXIT];
/// The page has this long to confirm an in-app notice, then the receipt is `dropped/none`.
pub const RENDER_WAIT: Duration = Duration::from_secs(3);
/// A frame without `alerts.desktop.force` (a sidecar before T-P2-31b) still marks a forced test
/// by this prefix (bridge_alerts_desktop.TEST_DELIVERY_PREFIX); kept for one version (PROTOCOL §11).
pub const TEST_PREFIX: &str = "desktop-test-";
/// Receipted delivery ids remembered per session (each id is receipted at most once).
const DONE_CAPACITY: usize = 256;
/// In-app notices awaiting the page; the sidecar's own pending table holds 64.
const PENDING_CAPACITY: usize = 64;
const SEVERITIES: [&str; 4] = ["info", "warn", "error", "critical"];
pub const TARGETS: [&str; 3] = ["overview", "alerts", "update"];
/// `alerts.desktop.notice.kind` values the sidecar sends (`service_stopped` is the shell's own).
pub const KINDS: [&str; 7] = ["outage", "capture_lost", "start_failed", "fallback", "unstable", "update", "preview"];
pub const GROUPS: [&str; 4] = ["connection", "tier_fallback", "update", "preview"];
/// At most this many `notice.params`.
pub const MAX_PARAMS: usize = 8;
/// Incidents held while the user is busy (the latest frame of each).
pub const HOLD_CAPACITY: usize = 20;
/// How often a non-empty hold asks Windows again whether the user is busy.
pub const HOLD_RECHECK: Duration = Duration::from_secs(5);
/// Incidents remembered as shown in the page (so a banner goes when its incident moves on).
const IN_APP_CAPACITY: usize = 32;

/// The subscribe list: the P1 topics, then each v1 page topic and each shell topic that
/// `hello.caps.topics` announces (subscribing to an undeclared topic is refused by the sidecar,
/// PROTOCOL §2). T-P2-31b: the shell is the subscriber of every v1 page topic (§11); `shutdown`
/// is never subscribed (PROTOCOL §13).
pub fn subscription(hello: &Value) -> Vec<&'static str> {
    let declared = |t: &str| hello["caps"]["topics"].as_array().is_some_and(|v| v.iter().any(|x| x == t));
    TOPICS.iter().copied().chain(V1_PAGE_TOPICS.iter().copied().filter(|t| declared(t)))
        .chain(SHELL_TOPICS.iter().copied().filter(|t| declared(t))).collect()
}
/// `hello.caps.commands` lists `cmd` (the shell only sends shell commands the sidecar declared).
pub fn supports(hello: &Value, cmd: &str) -> bool {
    hello["caps"]["commands"].as_array().is_some_and(|v| v.iter().any(|x| x == cmd))
}
fn shell_topic(item: &Value) -> bool {
    item["t"] == "evt" && item["topic"].as_str().is_some_and(|t| SHELL_TOPICS.contains(&t))
}
/// Takes the shell-topic events out of a sidecar frame before the page hub sees it (the hub
/// only accepts page topics). A shell-topic `evt` is taken whole and the frame becomes `null`;
/// a batch keeps its other items, possibly none.
pub fn take_shell_items(frame: &mut Value) -> Vec<Value> {
    match frame["t"].as_str() {
        Some("evt") if shell_topic(frame) => vec![frame.take()],
        Some("batch") => match frame["items"].as_array_mut() {
            Some(items) if items.iter().any(shell_topic) => {
                let (shell, rest): (Vec<Value>, Vec<Value>) = items.drain(..).partition(shell_topic);
                *items = rest; shell
            },
            _ => Vec::new(),
        },
        _ => Vec::new(),
    }
}
/// Batch atomicity for the shell items (PROTOCOL §4, as the hub does for page items): every
/// shell-topic item must carry the session epoch and both timestamps, checked BEFORE the items
/// are split off, so one foreign item rejects the whole frame instead of only itself.
pub fn check_batch(frame: &Value, epoch: &str) -> Result<(), &'static str> {
    if frame["t"] != "batch" { return Ok(()); }
    for item in frame["items"].as_array().into_iter().flatten().filter(|i| shell_topic(i)) {
        if item["epoch"] != epoch { return Err("epoch_mismatch"); }
        if item["produced_at_ms"].as_u64().is_none() || item["sent_at_ms"].as_u64().is_none() { return Err("missing_field"); }
    }
    Ok(())
}

/// An `edge` whose seq is not the next one: accepted (no session rebuild for shell topics), but
/// reported so a lost edge without a `gap` leaves a trace in shell.log.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Skip { pub expected: u64, pub got: u64 }
/// Per-session checks of shell-topic events (the hub's rules for page topics, PROTOCOL §4/§11).
#[derive(Default)]
pub struct Cursor { revisions: HashMap<&'static str, u64>, edges: HashMap<&'static str, u64> }
impl Cursor {
    pub fn validate(&mut self, item: &Value, epoch: &str) -> Result<(&'static str, Option<Skip>), &'static str> {
        if item["epoch"] != epoch { return Err("epoch_mismatch"); }
        if item["produced_at_ms"].as_u64().is_none() || item["sent_at_ms"].as_u64().is_none() { return Err("missing_field"); }
        let topic = SHELL_TOPICS.iter().copied().find(|t| item["topic"] == *t).ok_or("invalid_enum")?;
        let kind = item["kind"].as_str().ok_or("missing_field")?;
        let rev = item["rev"].as_u64().filter(|r| *r > 0).ok_or("invalid_type")?;
        let kinds: &[&str] = if topic == DESKTOP || topic == PREPARE_EXIT { &["edge", "gap"] } else { &["edge", "gap", "snapshot"] };
        if !kinds.contains(&kind) || !item["data"].is_object() || item.get("key").is_some() { return Err("invalid_enum"); }
        if rev <= *self.revisions.get(topic).unwrap_or(&0) { return Err("sequence_gap"); }
        let mut skip = None;
        if kind != "snapshot" {
            let seq = item["seq"].as_u64().filter(|s| *s > 0).ok_or("invalid_type")?;
            let previous = *self.edges.get(topic).unwrap_or(&0);
            if seq <= previous { return Err("sequence_gap"); }
            if kind == "gap" {
                // The hub's gap rule: it covers exactly the missing edges and seq follows it.
                let data = &item["data"];
                let from = data["from_seq"].as_u64().filter(|v| *v > 0).ok_or("invalid_type")?;
                let to = data["to_seq"].as_u64().ok_or("invalid_type")?;
                if from != previous + 1 || to < from || seq != to.checked_add(1).ok_or("invalid_type")?
                    || data["dropped"].as_u64() != Some(to - from + 1) { return Err("sequence_gap"); }
            } else if seq != previous + 1 { skip = Some(Skip { expected: previous + 1, got: seq }); }
            self.edges.insert(topic, seq);
        }
        self.revisions.insert(topic, rev);
        Ok((topic, skip))
    }
}

/// `alerts.desktop.notice.state`: `closed` ends an incident without a recovered notice (only its
/// card or banner goes, silently).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State { Open, Update, Resolved, Closed }
impl State {
    pub fn parse(value: &str) -> Option<Self> {
        Some(match value { "open" => Self::Open, "update" => Self::Update, "resolved" => Self::Resolved, "closed" => Self::Closed, _ => return None })
    }
    pub fn wire(self) -> &'static str {
        match self { Self::Open => "open", Self::Update => "update", Self::Resolved => "resolved", Self::Closed => "closed" }
    }
}
/// A UI message id (`notice.outage.title`, `errors.start.port_in_use`): lower-case dotted segments.
fn message_id(value: &str) -> bool {
    value.len() <= 96 && value.split('.').count() >= 2
        && value.split('.').all(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_'))
}
/// Default dwell of a card that is not sticky (plan §10.3: info 8 s, warning 10 s, recovered 6 s).
fn default_ttl(severity: &str, state: State) -> u64 {
    if state == State::Resolved { 6000 } else if severity == "info" { 8000 } else { 10000 }
}
/// One validated `alerts.desktop.notice` object (plan §10.3). The page and the card window render
/// its message ids through the UI catalog; `title` / `detail` of the frame stay the fallback text.
#[derive(Clone, Debug, PartialEq)]
pub struct Spec {
    pub kind: &'static str, pub incident: String, pub state: State, pub group: &'static str,
    pub title_id: String, pub detail_id: Option<String>, pub reason_id: Option<String>, pub fix_id: Option<String>,
    pub params: serde_json::Map<String, Value>, pub action: bool, pub sound: bool, pub sticky: bool, pub ttl_ms: u64,
}
impl Spec {
    /// Optional keys (`detail_id`, `reason_id`, `fix_id`, `action`, `params`, the flags) may be
    /// absent or null alike. Anything malformed fails the whole object (the frame then falls back
    /// to its title / detail text).
    pub fn parse(value: &Value) -> Result<Self, &'static str> {
        let o = value.as_object().ok_or("notice_not_object")?;
        let pick = |key: &str, allowed: &[&'static str]| o.get(key).and_then(Value::as_str).and_then(|v| allowed.iter().copied().find(|a| *a == v));
        let kind = pick("kind", &KINDS).ok_or("notice_kind")?;
        let incident = o.get("incident").and_then(Value::as_str)
            .filter(|s| !s.is_empty() && s.len() <= 64 && s.bytes().all(|b| b.is_ascii_graphic())).ok_or("notice_incident")?.to_owned();
        let state = o.get("state").and_then(Value::as_str).and_then(State::parse).ok_or("notice_state")?;
        let group = pick("group", &GROUPS).ok_or("notice_group")?;
        let title_id = o.get("title_id").and_then(Value::as_str).filter(|s| message_id(s)).ok_or("notice_title_id")?.to_owned();
        let optional = |key: &str| -> Result<Option<String>, &'static str> {
            match o.get(key) {
                None | Some(Value::Null) => Ok(None),
                Some(Value::String(s)) if message_id(s) => Ok(Some(s.clone())),
                Some(_) => Err("notice_message_id"),
            }
        };
        let (detail_id, reason_id, fix_id) = (optional("detail_id")?, optional("reason_id")?, optional("fix_id")?);
        let params = match o.get("params") {
            None | Some(Value::Null) => serde_json::Map::new(),
            Some(Value::Object(params)) if params.len() <= MAX_PARAMS => {
                let key = |k: &str| !k.is_empty() && k.len() <= 32 && !k.as_bytes()[0].is_ascii_digit()
                    && k.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
                let value = |v: &Value| match v { Value::String(s) => s.chars().count() <= 200, Value::Number(n) => n.is_i64() || n.is_u64(), _ => false };
                if !params.iter().all(|(k, v)| key(k) && value(v)) { return Err("notice_params"); }
                params.clone()
            },
            Some(_) => return Err("notice_params"),
        };
        let action = match o.get("action") { None | Some(Value::Null) => false, Some(v) if v == "view" => true, Some(_) => return Err("notice_action") };
        let flag = |key: &str| match o.get(key) { None | Some(Value::Null) => Ok(false), Some(Value::Bool(b)) => Ok(*b), Some(_) => Err("notice_flag") };
        let (sound, sticky) = (flag("sound")?, flag("sticky")?);
        let ttl_ms = match o.get("ttl_ms") { None | Some(Value::Null) => 0, Some(v) => v.as_u64().ok_or("notice_ttl")? };
        Ok(Self { kind, incident, state, group, title_id, detail_id, reason_id, fix_id, params, action, sound, sticky, ttl_ms })
    }
    /// The object the page and the card window render; absent optional keys stay absent.
    pub fn to_json(&self) -> Value {
        let mut o = json!({"kind":self.kind,"incident":self.incident,"state":self.state.wire(),"group":self.group,
            "title_id":self.title_id,"params":self.params,"sticky":self.sticky,"ttl_ms":self.ttl_ms});
        for (key, value) in [("detail_id", &self.detail_id), ("reason_id", &self.reason_id), ("fix_id", &self.fix_id)] {
            if let Some(value) = value { o[key] = json!(value); }
        }
        if self.action { o["action"] = json!("view"); }
        o
    }
}

/// One validated `alerts.desktop` DTO.
#[derive(Clone, Debug, PartialEq)]
pub struct Notice { pub delivery_id: String, pub severity: &'static str, pub title: String, pub detail: String, pub target: &'static str, pub force: bool,
    /// The `notice` object; `None` without one, or when it was malformed (`spec_error` says why).
    pub spec: Option<Spec>, pub spec_error: Option<&'static str> }
impl Notice {
    pub fn parse(data: &Value) -> Result<Self, &'static str> {
        let delivery_id = data["delivery_id"].as_str().filter(|s| !s.is_empty() && s.len() <= 128 && s.bytes().all(|b| b.is_ascii_graphic()))
            .ok_or("invalid_delivery_id")?.to_owned();
        let pick = |key: &str, allowed: &[&'static str]| data[key].as_str().and_then(|v| allowed.iter().copied().find(|a| *a == v));
        let severity = pick("severity", &SEVERITIES).ok_or("invalid_severity")?;
        let target = pick("target", &TARGETS).ok_or("invalid_target")?;
        let (title, detail) = (data["title"].as_str().ok_or("invalid_title")?, data["detail"].as_str().ok_or("invalid_detail")?);
        let strings = |key: &str| data[key].as_array().is_some_and(|v| v.iter().all(Value::is_string));
        if !strings("event_ids") || !strings("members") { return Err("invalid_members"); }
        // The explicit field (integration ticket) wins; until then the test prefix marks a forced test.
        let force = match data.get("force") { Some(Value::Bool(f)) => *f, Some(_) => return Err("invalid_force"), None => delivery_id.starts_with(TEST_PREFIX) };
        // A malformed notice never refuses the frame: it is shown with its title / detail text.
        let (spec, spec_error) = match data.get("notice") {
            None | Some(Value::Null) => (None, None),
            Some(value) => match Spec::parse(value) {
                Ok(mut spec) => {
                    spec.ttl_ms = if spec.sticky { 0 } else if spec.ttl_ms == 0 { default_ttl(severity, spec.state) } else { spec.ttl_ms.clamp(2000, 120_000) };
                    (Some(spec), None)
                },
                Err(code) => (None, Some(code)),
            },
        };
        Ok(Self { delivery_id, severity, title: title.to_owned(), detail: detail.to_owned(), target, force, spec, spec_error })
    }
    /// Shell → page in-app notice; the hub adds the epoch. `notice` only when the frame has one.
    pub fn host_frame(&self) -> Value {
        let mut frame = json!({"t":"host:notify","delivery_id":self.delivery_id,"severity":self.severity,"title":self.title,"detail":self.detail,"target":self.target});
        if let Some(spec) = &self.spec { frame["notice"] = spec.to_json(); }
        frame
    }
    /// The incident this frame belongs to; a frame without a notice is its own incident.
    pub fn key(&self) -> String {
        self.spec.as_ref().map_or_else(|| format!("delivery:{}", self.delivery_id), |s| s.incident.clone())
    }
    pub fn state(&self) -> State { self.spec.as_ref().map_or(State::Open, |s| s.state) }
    fn sound(&self) -> bool { self.spec.as_ref().is_some_and(|s| s.sound) }
    /// The card the notice window renders (ui/src/notice): the notice object plus the frame's
    /// delivery id, severity, target and fallback text. A frame without a notice is a `legacy`
    /// card of its title / detail text (an error stays until dismissed, as the plan's errors do).
    pub fn card(&self) -> Value {
        let mut card = match &self.spec {
            Some(spec) => spec.to_json(),
            None => json!({"kind":"legacy","incident":self.key(),"state":"open","group":"legacy","params":{},"action":"view",
                "sticky":matches!(self.severity, "error" | "critical"),
                "ttl_ms":if matches!(self.severity, "error" | "critical") { 0 } else { default_ttl(self.severity, State::Open) }}),
        };
        for (key, value) in [("delivery_id", json!(self.delivery_id)), ("severity", json!(self.severity)), ("target", json!(self.target)),
            ("title", json!(self.title)), ("detail", json!(self.detail)), ("shell", json!(false))] { card[key] = value; }
        card
    }
}

/// A card the shell makes itself (no delivery id from the sidecar, never receipted): the
/// `service_stopped` card after a sidecar crash and the summary after a busy period.
pub fn shell_card(seq: u64, kind: &str, severity: &str, title_id: &str, detail_id: &str, params: Value, sticky: bool) -> Value {
    json!({"delivery_id":format!("shell-{kind}-{seq}"),"kind":kind,"incident":kind,"state":"open","group":"shell","severity":severity,
        "title_id":title_id,"detail_id":detail_id,"params":params,"action":"view","sticky":sticky,
        "ttl_ms":if sticky { 0 } else { default_ttl(severity, State::Open) },"target":"overview","title":"","detail":"","shell":true})
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Route { InApp, Card, Hold }
/// The window is in front and the page is ready: in-app. Otherwise, and always for a forced
/// preview, the notice card. While Windows says the user is busy (full screen, presentation,
/// quiet time) a card is held instead; a forced preview is never held (the user just asked for
/// it). `busy` is asked only when it decides.
pub fn route(force: bool, foreground: bool, page_ready: bool, busy: impl FnOnce() -> bool) -> Route {
    if !force && foreground && page_ready { Route::InApp } else if !force && busy() { Route::Hold } else { Route::Card }
}

/// What became of one card (notice.rs): on screen, given up without being shown (an older frame
/// of the same incident replaced it, or the incident closed), or the card window could not show
/// it (it could not be made, or the page did not render it in time).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CardResult { Shown, Dropped, Unavailable }
pub type CardDone = Box<dyn FnOnce(CardResult) + Send>;
/// The notice card window. `show` calls `done` exactly once, possibly later and on another thread.
pub trait Cards: Send + Sync {
    fn show(&self, card: Value, done: CardDone);
    /// Takes an incident's card down silently (nothing is receipted).
    fn close(&self, incident: &str);
    /// A new sidecar epoch: the old sidecar's cards go; the shell's own stay.
    fn reset(&self);
    /// The UI language changed (`ui.language`): the cards on screen are rewritten in it.
    fn language(&self, _locale: &'static str) {}
}
/// No card window (tests, and before the Tauri app exists): every card falls back.
pub struct NoCards;
impl Cards for NoCards {
    fn show(&self, _: Value, done: CardDone) { done(CardResult::Unavailable) }
    fn close(&self, _: &str) {}
    fn reset(&self) {}
}

/// The frames held while the user is busy: the latest frame of each incident (oldest incident
/// first, at most [`HOLD_CAPACITY`]), and a shell card waiting for the same moment.
#[derive(Default)]
pub struct Hold { entries: Vec<Notice>, shell: Option<Value>, check_at: Option<Instant> }
impl Hold {
    /// Keeps `notice` as its incident's latest frame. Returns the delivery ids it superseded (the
    /// same incident's older frame, the oldest incident beyond the capacity).
    pub fn push(&mut self, notice: Notice, now: Instant) -> Vec<String> {
        let key = notice.key();
        let mut superseded = Vec::new();
        if let Some(i) = self.entries.iter().position(|n| n.key() == key) { superseded.push(self.entries.remove(i).delivery_id); }
        self.entries.push(notice);
        while self.entries.len() > HOLD_CAPACITY { superseded.push(self.entries.remove(0).delivery_id); }
        self.check_at.get_or_insert(now + HOLD_RECHECK);
        superseded
    }
    /// The held frame of `key`, taken out (a newer frame of the incident is being shown).
    pub fn take(&mut self, key: &str) -> Option<Notice> {
        let at = self.entries.iter().position(|n| n.key() == key)?;
        Some(self.entries.remove(at))
    }
    pub fn hold_shell(&mut self, card: Value, now: Instant) { self.shell = Some(card); self.check_at.get_or_insert(now + HOLD_RECHECK); }
    /// Something is held and the next look at the user's state is due.
    pub fn due(&self, now: Instant) -> bool { self.check_at.is_some_and(|at| now >= at) }
    pub fn later(&mut self, now: Instant) { if self.check_at.is_some() { self.check_at = Some(now + HOLD_RECHECK); } }
    pub fn release(&mut self) -> (Vec<Notice>, Option<Value>) { self.check_at = None; (std::mem::take(&mut self.entries), self.shell.take()) }
    /// A new epoch: the old sidecar's frames go (their ids mean nothing to the new one); a held
    /// shell card stays (the crash card is held across the relaunch that follows the crash).
    pub fn session(&mut self) -> usize {
        let dropped = self.entries.len();
        self.entries.clear();
        if self.shell.is_none() { self.check_at = None; }
        dropped
    }
}
/// The summary after a busy period (plan §4.1): shown only when a held incident recovered
/// meanwhile. `count` is the held incidents (closed ones aside: they end silently), `resolved`
/// the ones whose last frame was `resolved`.
pub fn summary(released: &[Notice]) -> Option<(usize, usize)> {
    let resolved = released.iter().filter(|n| n.state() == State::Resolved).count();
    (resolved > 0).then(|| (released.iter().filter(|n| n.state() != State::Closed).count(), resolved))
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome { DeliveredWindows, FailedWindows, DeliveredInApp, Dropped }
impl Outcome {
    /// PROTOCOL §10 `alerts.desktopDelivered{outcome, surface}`.
    pub fn wire(self) -> (&'static str, &'static str) {
        match self {
            Self::DeliveredWindows => ("delivered", "windows"), Self::FailedWindows => ("failed", "windows"),
            Self::DeliveredInApp => ("delivered", "in_app"), Self::Dropped => ("dropped", "none"),
        }
    }
    /// No tray (or no icon yet) has no visible surface: dropped. Submitted is the only success;
    /// a refused or empty balloon is a delivery failure.
    pub fn from_balloon(result: Option<Balloon>) -> Self {
        match result {
            None | Some(Balloon::NoIcon) => Self::Dropped,
            Some(Balloon::Submitted) => Self::DeliveredWindows,
            Some(Balloon::Rejected | Balloon::Empty) => Self::FailedWindows,
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Receipt { pub delivery_id: String, pub outcome: Outcome }
/// `alerts.desktopDelivered` request (shell-only command).
pub fn receipt_frame(id: u64, receipt: &Receipt) -> Value {
    let (outcome, surface) = receipt.outcome.wire();
    json!({"t":"req","id":id,"cmd":"alerts.desktopDelivered","args":{"delivery_id":receipt.delivery_id,"outcome":outcome,"surface":surface},
        "from":"shell","req_id":format!("shell-notify-{id}"),"owner":"shell"})
}

struct Pending { since: Instant, page: u64, sound: bool }
/// Delivery ids of one sidecar session: in-app notices awaiting the page, held notices, and the
/// ids already receipted (each id gets exactly one receipt). `generation` counts sessions, so a
/// result that arrives after a new epoch began (a card or balloon of the old sidecar) is dropped.
#[derive(Default)]
pub struct Notifier { pending: HashMap<String, Pending>, done: VecDeque<String>, done_set: HashSet<String>, outbox: Vec<Receipt>,
    held: HashSet<String>, generation: u64 }
impl Notifier {
    pub fn known(&self, id: &str) -> bool { self.pending.contains_key(id) || self.done_set.contains(id) || self.held.contains(id) }
    /// Claims the single receipt of `id`; false when it was already claimed.
    fn claim(&mut self, id: &str) -> bool {
        if !self.done_set.insert(id.to_owned()) { return false; }
        self.done.push_back(id.to_owned());
        while self.done.len() > DONE_CAPACITY { if let Some(old) = self.done.pop_front() { self.done_set.remove(&old); } }
        true
    }
    fn finish(&mut self, id: &str, outcome: Outcome) {
        if self.claim(id) { self.outbox.push(Receipt { delivery_id: id.to_owned(), outcome }); }
    }
    /// The receipt of an id claimed in session `generation`; a later session never sees it.
    fn push(&mut self, generation: u64, receipt: Receipt) { if generation == self.generation { self.outbox.push(receipt); } }
    /// A notice is held while the user is busy: known (a repeat is a duplicate), not claimed yet.
    fn hold(&mut self, id: &str) -> bool { if self.known(id) { return false; } self.held.insert(id.to_owned()) }
    fn unhold(&mut self, id: &str) -> bool { self.held.remove(id) }
    /// An in-app notice now waits for the page connection `page` to confirm it.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn await_page(&mut self, id: &str, page: u64, now: Instant) -> bool { self.await_page_with(id, page, now, false) }
    /// `sound`: play the notification sound once the page confirms it really showed the notice.
    pub fn await_page_with(&mut self, id: &str, page: u64, now: Instant, sound: bool) -> bool {
        if self.known(id) || self.pending.len() >= PENDING_CAPACITY { return false; }
        self.pending.insert(id.to_owned(), Pending { since: now, page, sound }); true
    }
    /// The page confirmed rendering. Only a notice this shell handed to the same page connection
    /// and still waiting is accepted (a reloaded page, a late or a repeated answer is refused).
    /// Ok(true) when the notice asked for the sound.
    pub fn rendered(&mut self, id: &str, page: u64) -> Result<bool, &'static str> {
        match self.pending.get(id) {
            None if self.done_set.contains(id) => Err("already_receipted"),
            None => Err("unknown_delivery"),
            Some(p) if p.page != page => Err("stale_page"),
            Some(p) => { let sound = p.sound; self.pending.remove(id); self.finish(id, Outcome::DeliveredInApp); Ok(sound) },
        }
    }
    /// In-app notices the page did not confirm within [`RENDER_WAIT`] are dropped.
    pub fn expire(&mut self, now: Instant) {
        let late: Vec<String> = self.pending.iter().filter(|(_, p)| now.saturating_duration_since(p.since) >= RENDER_WAIT).map(|(id, _)| id.clone()).collect();
        for id in late { self.pending.remove(&id); self.finish(&id, Outcome::Dropped); }
    }
    pub fn take(&mut self) -> Vec<Receipt> { std::mem::take(&mut self.outbox) }
    /// A new epoch: ids of the old sidecar are unknown to the new one (a receipt would be a
    /// conflict), so they are forgotten. Nothing is redelivered for them: an unreceipted runtime
    /// alert just expires in the old sidecar, and an update notice is offered again at the next
    /// start (bridge_alerts_desktop `_release_expired`).
    pub fn reset(&mut self) -> usize {
        let abandoned = self.pending.len() + self.outbox.len() + self.held.len();
        let generation = self.generation + 1;
        *self = Self { generation, ..Self::default() }; abandoned
    }
}

/// How one notice was handled. `Card`: handed to the notice card window; its outcome (and the
/// `notify_route` line) follows once the card is on screen, or once the tray balloon it fell back
/// to was answered. `Held`: kept until the user is no longer busy. `Closed`: an incident ended;
/// its card or banner went silently (receipted `dropped/none` at once).
#[derive(Clone, Debug, PartialEq)]
pub enum Handled { Duplicate, InApp, InAppFull, Card, Held, Closed }
/// Submits one balloon and calls `done` with the result, possibly later and on another thread.
type BalloonPort = Box<dyn Fn(&Notice, BalloonDone) + Send + Sync>;
/// The page-side state `notify_route` records: online and declared (`host_notify_ready`), only
/// online, or no page Channel.
#[derive(Clone, Copy)]
enum PageState { Ready, Unready, Offline }
/// One `notify_route` line (ids, enums and booleans only, never title or detail).
#[derive(Clone)]
struct RouteLine { delivery: String, force: bool, window: Option<presence::Window>, severity: &'static str, target: &'static str, page: PageState,
    /// `notice.state`, or `legacy` for a frame without a notice.
    state: &'static str }
impl RouteLine {
    fn write(&self, data: &Option<PathBuf>, route: &str) {
        let Some(data) = data else { return };
        let flag = |f: fn(presence::Window) -> bool| self.window.map_or("unknown", |w| if f(w) { "true" } else { "false" });
        let page = match self.page { PageState::Ready => "ready", PageState::Unready => "unready", PageState::Offline => "offline" };
        crate::shell_log::fields(data, "notify_route", &[("delivery", &self.delivery), ("route", route), ("force", if self.force { "true" } else { "false" }),
            ("visible", flag(|w| w.visible)), ("focused", flag(|w| w.focused)), ("severity", self.severity), ("target", self.target), ("page", page),
            ("state", self.state)]);
    }
}
/// Plays the Windows notification sound (notice.rs `play_sound`).
pub type SoundPort = Arc<dyn Fn() + Send + Sync>;
/// Whether Windows says the user is busy (notice.rs `user_busy`).
pub type BusyPort = Box<dyn Fn() -> bool + Send + Sync>;
/// What the worker, the Tauri commands and the tray share for notices and presence.
pub struct Desk {
    notifier: Arc<Mutex<Notifier>>,
    pub page: presence::PageSlot,
    /// A clicked balloon's or card's target, sent once the restored window is visible (presence.rs).
    pub navigate: presence::Navigate,
    pub probe: Box<dyn presence::Probe>,
    balloon: Arc<dyn Fn(&Notice, BalloonDone) + Send + Sync>,
    cards: Arc<dyn Cards>,
    busy: BusyPort,
    sound: SoundPort,
    hold: Mutex<Hold>,
    /// Incidents the page was given (open / update / resolved), newest last, with their notice:
    /// when one moves on to the card window, its banner is taken down.
    in_app: Mutex<VecDeque<Spec>>,
    /// Numbers the shell's own frames (shell cards, banner clears).
    seq: AtomicU64,
    /// The page connection (`Hub::page_generation`, ≥ 1) that declared it renders `host:notify`
    /// and confirms with `host_notify_rendered` (`host_notify_ready`); 0 = none yet. Only that
    /// connection gets in-app notices; any other page state uses the card window.
    notify_page: AtomicU64,
    /// shell.log directory; `None` keeps unit tests off the disk.
    data: Option<PathBuf>,
}
impl Desk {
    /// Without `with_cards` every card falls back to the balloon (tests, and the tray-only path);
    /// without `with_busy` the user is never busy; without `with_sound` nothing plays.
    pub fn new(probe: Box<dyn presence::Probe>, balloon: BalloonPort, data: Option<PathBuf>) -> Self {
        Self { notifier: Arc::new(Mutex::new(Notifier::default())), page: presence::PageSlot::default(), navigate: presence::Navigate::default(),
            probe, balloon: Arc::from(balloon), cards: Arc::new(NoCards), busy: Box::new(|| false), sound: Arc::new(|| ()),
            hold: Mutex::new(Hold::default()), in_app: Mutex::new(VecDeque::new()), seq: AtomicU64::new(1), notify_page: AtomicU64::new(0), data }
    }
    pub fn with_cards(mut self, cards: Arc<dyn Cards>) -> Self { self.cards = cards; self }
    pub fn with_busy(mut self, busy: BusyPort) -> Self { self.busy = busy; self }
    pub fn with_sound(mut self, sound: SoundPort) -> Self { self.sound = sound; self }
    /// The production balloon: queued for the P3-03 tray's UI thread, which submits it with
    /// `Shell_NotifyIconW` and remembers the target for a click. The bridge worker never makes
    /// that call (a hung Explorer would stall the pipe, pings and receipts).
    pub fn tray_balloon() -> BalloonPort {
        Box::new(|n: &Notice, done: BalloonDone| match crate::tray::installed() {
            Some(tray) => tray.post_alert_balloon(&n.title, &n.detail, Severity::parse(n.severity), n.target, done),
            None => done(None),
        })
    }
    fn notifier(&self) -> std::sync::MutexGuard<'_, Notifier> { self.notifier.lock().unwrap_or_else(|e| e.into_inner()) }
    fn held(&self) -> std::sync::MutexGuard<'_, Hold> { self.hold.lock().unwrap_or_else(|e| e.into_inner()) }
    fn log(&self, code: &str, fields: &[(&str, &str)]) { if let Some(data) = &self.data { crate::shell_log::fields(data, code, fields); } }
    /// `host_notify_ready` from the page connection `page`.
    pub fn notify_ready(&self, page: u64) {
        if self.notify_page.swap(page, Ordering::AcqRel) != page { self.log("notify_page_ready", &[("page", &page.to_string())]); }
    }
    /// Routes one notice. The notifier lock is never held across the card window, the balloon or
    /// the hub.
    pub fn deliver(&self, notice: &Notice, hub: &Mutex<Hub>, now: Instant) -> Handled {
        let (online, page) = { let h = hub.lock().unwrap_or_else(|e| e.into_inner()); (h.page_connected(), h.page_generation()) };
        let state = if !online { PageState::Offline } else if self.notify_page.load(Ordering::Acquire) == page { PageState::Ready } else { PageState::Unready };
        let line = RouteLine { delivery: notice.delivery_id.clone(), force: notice.force, window: self.probe.window(),
            severity: notice.severity, target: notice.target, page: state, state: notice.spec.as_ref().map_or("legacy", |s| s.state.wire()) };
        if let Some(code) = notice.spec_error { self.log("notify_notice_invalid", &[("delivery", &notice.delivery_id), ("code", code)]); }
        if self.notifier().known(&notice.delivery_id) { line.write(&self.data, "duplicate"); return Handled::Duplicate; }
        let way = route(notice.force, line.window.is_some_and(|w| w.foreground()), matches!(state, PageState::Ready), || (self.busy)());
        // Shown now (the user became free between two looks, or it is a preview): an older held
        // frame of the incident is superseded, so the release never shows it after this one.
        if way != Route::Hold {
            let stale = self.held().take(&notice.key());
            if let Some(old) = stale {
                { let mut n = self.notifier(); n.unhold(&old.delivery_id); n.finish(&old.delivery_id, Outcome::Dropped); }
                self.log("notify_hold_superseded", &[("delivery", &old.delivery_id)]);
            }
        }
        match way {
            Route::InApp => self.in_app(notice, hub, page, now, line),
            Route::Hold => self.hold(notice, now, line),
            Route::Card => self.card(notice, hub, line),
        }
    }
    fn in_app(&self, notice: &Notice, hub: &Mutex<Hub>, page: u64, now: Instant, line: RouteLine) -> Handled {
        // One incident, one place: the card window lets go of it.
        if let Some(spec) = &notice.spec { self.cards.close(&spec.incident); }
        if notice.state() == State::Closed {
            let Some(generation) = self.claim(&notice.delivery_id) else { line.write(&self.data, "duplicate"); return Handled::Duplicate };
            // The page takes the incident's banner down silently; there is nothing to confirm.
            hub.lock().unwrap_or_else(|e| e.into_inner()).host(notice.host_frame());
            self.forget_in_app(&notice.key());
            self.notifier().push(generation, Receipt { delivery_id: notice.delivery_id.clone(), outcome: Outcome::Dropped });
            line.write(&self.data, "in_app_closed");
            return Handled::Closed;
        }
        // Registered before the page can see it, so an immediate answer finds it.
        if self.notifier().await_page_with(&notice.delivery_id, page, now, notice.sound()) {
            hub.lock().unwrap_or_else(|e| e.into_inner()).host(notice.host_frame());
            if let Some(spec) = &notice.spec { self.remember_in_app(spec); }
            line.write(&self.data, "in_app");
            Handled::InApp
        } else {
            let duplicate = self.notifier().known(&notice.delivery_id);
            if duplicate { line.write(&self.data, "duplicate"); return Handled::Duplicate; }
            self.notifier().finish(&notice.delivery_id, Outcome::Dropped);
            line.write(&self.data, "in_app_full");
            Handled::InAppFull
        }
    }
    /// Claims `id`'s receipt; the session it belongs to, or `None` for a repeat.
    fn claim(&self, id: &str) -> Option<u64> { let mut n = self.notifier(); n.claim(id).then_some(n.generation) }
    fn card(&self, notice: &Notice, hub: &Mutex<Hub>, line: RouteLine) -> Handled {
        let Some(generation) = self.claim(&notice.delivery_id) else { line.write(&self.data, "duplicate"); return Handled::Duplicate };
        // One incident, one place: a banner the page still shows for it goes.
        self.clear_page(&notice.key(), hub);
        if notice.state() == State::Closed {
            self.cards.close(&notice.key());
            self.notifier().push(generation, Receipt { delivery_id: notice.delivery_id.clone(), outcome: Outcome::Dropped });
            line.write(&self.data, "card_closed");
            return Handled::Closed;
        }
        let (notifier, data, balloon, sound) = (self.notifier.clone(), self.data.clone(), self.balloon.clone(), self.sound.clone());
        let (fallback, ring, id) = (notice.clone(), notice.sound(), notice.delivery_id.clone());
        self.cards.show(notice.card(), Box::new(move |result| match result {
            CardResult::Shown => {
                notifier.lock().unwrap_or_else(|e| e.into_inner()).push(generation, Receipt { delivery_id: id, outcome: Outcome::DeliveredWindows });
                if ring { sound(); }
                line.write(&data, "card");
            },
            CardResult::Dropped => {
                notifier.lock().unwrap_or_else(|e| e.into_inner()).push(generation, Receipt { delivery_id: id, outcome: Outcome::Dropped });
                line.write(&data, "card_dropped");
            },
            // The card window could not be made or did not render in time: the tray balloon, as
            // before the redesign (its submission happens on the tray's UI thread).
            CardResult::Unavailable => balloon(&fallback, Box::new(move |result| {
                let outcome = Outcome::from_balloon(result);
                notifier.lock().unwrap_or_else(|e| e.into_inner()).push(generation, Receipt { delivery_id: id, outcome });
                line.write(&data, match outcome { Outcome::DeliveredWindows => "windows", Outcome::FailedWindows => "windows_failed", _ => "no_surface" });
            })),
        }));
        Handled::Card
    }
    fn hold(&self, notice: &Notice, now: Instant, line: RouteLine) -> Handled {
        if !self.notifier().hold(&notice.delivery_id) { line.write(&self.data, "duplicate"); return Handled::Duplicate; }
        let superseded = self.held().push(notice.clone(), now);
        for id in superseded {
            { let mut n = self.notifier(); n.unhold(&id); n.finish(&id, Outcome::Dropped); }
            self.log("notify_hold_superseded", &[("delivery", &id)]);
        }
        line.write(&self.data, "held");
        Handled::Held
    }
    /// Worker tick: a due hold asks Windows again. Once the user is free, the held incidents whose
    /// last frame is open / update are shown (routed afresh), the others take their card or
    /// banner down quietly (`dropped/none`), and one summary card follows when some recovered
    /// meanwhile (no sound, no receipt).
    pub fn tick(&self, hub: &Mutex<Hub>, now: Instant) {
        if !self.held().due(now) { return; }
        if (self.busy)() { self.held().later(now); return; }
        let (released, shell) = self.held().release();
        if let Some(card) = shell { self.show_shell(card); }
        for notice in &released {
            self.notifier().unhold(&notice.delivery_id);
            match notice.state() {
                State::Open | State::Update => { self.deliver(notice, hub, now); },
                State::Resolved | State::Closed => {
                    let key = notice.key();
                    self.cards.close(&key);
                    self.clear_page(&key, hub);
                    self.notifier().finish(&notice.delivery_id, Outcome::Dropped);
                },
            }
        }
        let recap = summary(&released);
        self.log("notify_hold_released", &[("count", &released.len().to_string()), ("resolved", &recap.map_or(0, |s| s.1).to_string())]);
        if let Some((count, resolved)) = recap {
            let seq = self.seq.fetch_add(1, Ordering::Relaxed);
            self.show_shell(shell_card(seq, "summary", "info", "notice.summary.title", "notice.summary.detail", json!({"count":count,"resolved":resolved}), false));
        }
    }
    /// The sidecar crashed (main.rs `crash_restart`): while the window is not in front the shell
    /// shows its own `service_stopped` card (no delivery id, no receipt; held while the user is
    /// busy). In front, the page's crash banner says it.
    pub fn service_stopped(&self, now: Instant) {
        if self.probe.window().is_some_and(|w| w.foreground()) { return; }
        let seq = self.seq.fetch_add(1, Ordering::Relaxed);
        let card = shell_card(seq, "service_stopped", "error", "notice.service_stopped.title", "notice.service_stopped.detail", json!({}), true);
        if (self.busy)() { self.held().hold_shell(card, now); self.log("notice_shell_held", &[("kind", "service_stopped")]); return; }
        self.show_shell(card);
    }
    fn show_shell(&self, card: Value) {
        let kind = card["kind"].as_str().unwrap_or("-").to_owned();
        let data = self.data.clone();
        self.cards.show(card, Box::new(move |result| {
            let result = match result { CardResult::Shown => "shown", CardResult::Dropped => "dropped", CardResult::Unavailable => "unavailable" };
            if let Some(data) = &data { crate::shell_log::fields(data, "notice_shell_card", &[("kind", &kind), ("result", result)]); }
        }));
    }
    fn remember_in_app(&self, spec: &Spec) {
        let mut list = self.in_app.lock().unwrap_or_else(|e| e.into_inner());
        list.retain(|s| s.incident != spec.incident);
        list.push_back(spec.clone());
        while list.len() > IN_APP_CAPACITY { list.pop_front(); }
    }
    fn forget_in_app(&self, incident: &str) -> Option<Spec> {
        let mut list = self.in_app.lock().unwrap_or_else(|e| e.into_inner());
        let at = list.iter().position(|s| s.incident == incident)?;
        list.remove(at)
    }
    /// The page takes down what it shows for `incident` (a banner or toast it was given earlier):
    /// a `host:notify` with the incident's notice in state `closed`. The shell's own id is never
    /// confirmed or receipted.
    fn clear_page(&self, incident: &str, hub: &Mutex<Hub>) {
        let Some(mut spec) = self.forget_in_app(incident) else { return };
        spec.state = State::Closed;
        let id = format!("shell-clear-{}", self.seq.fetch_add(1, Ordering::Relaxed));
        let mut hub = hub.lock().unwrap_or_else(|e| e.into_inner());
        if hub.page_connected() {
            hub.host(json!({"t":"host:notify","delivery_id":id,"severity":"info","title":"","detail":"","target":"overview","notice":spec.to_json()}));
        }
    }
    /// `host_notify_rendered{delivery_id}` from the page connection `page` (the notice is on screen).
    pub fn rendered(&self, id: &str, page: u64) -> Result<(), &'static str> { self.rendered_as(id, page, true) }
    /// `shown: false`: the page confirmed without showing it (the overview's hero already shows
    /// the incident): receipted the same, but no sound.
    pub fn rendered_as(&self, id: &str, page: u64, shown: bool) -> Result<(), &'static str> {
        match self.notifier().rendered(id, page) {
            Ok(sound) => { if sound && shown { (self.sound)(); } Ok(()) },
            Err(reason) => { self.log("notify_render_rejected", &[("delivery", id), ("reason", reason)]); Err(reason) },
        }
    }
    /// Worker tick: expired in-app notices plus every receipt ready to send.
    pub fn receipts(&self, now: Instant) -> Vec<Receipt> { let mut n = self.notifier(); n.expire(now); n.take() }
    /// `ui.language` changed: the card window follows (the page follows `host:language` itself).
    pub fn language(&self, locale: &'static str) { self.cards.language(locale); }
    /// A new sidecar session: the old ids, held frames and remembered incidents are forgotten,
    /// and the old sidecar's cards leave the card window.
    pub fn session(&self) {
        let abandoned = self.notifier().reset();
        self.held().session();
        self.in_app.lock().unwrap_or_else(|e| e.into_inner()).clear();
        self.cards.reset();
        if abandoned > 0 { self.log("notify_abandoned_old_epoch", &[("count", &abandoned.to_string())]); }
    }
}

/// `host:language{language}` for a supported locale (hello.lang or ui.language).
pub fn language_frame(language: &str) -> Option<Value> {
    native_text::supported(language).map(|l| json!({"t":"host:language","language":l}))
}

#[cfg(test)] pub mod tests {
    use super::*;
    use crate::presence::{Probe, Window};
    use std::sync::Arc;
    use tauri::ipc::Channel;
    fn data(id: &str) -> Value {
        json!({"delivery_id":id,"event_ids":["e1"],"severity":"warn","title":"Tier down","detail":"Vanguard offline","members":["e1"],"target":"alerts"})
    }
    pub struct FakeProbe(pub Mutex<Option<Window>>);
    impl Probe for FakeProbe { fn window(&self) -> Option<Window> { *self.0.lock().unwrap() } }
    pub struct Rig { pub desk: Desk, pub hub: Mutex<Hub>, pub balloons: Arc<Mutex<Vec<String>>>, pub frames: Arc<Mutex<Vec<Value>>>, window: Arc<FakeProbe>, tray: Arc<Mutex<Option<Balloon>>> }
    impl Rig {
        fn set(&self, window: Option<Window>) { *self.window.0.lock().unwrap() = window; }
        fn tray(&self, result: Option<Balloon>) { *self.tray.lock().unwrap() = result; }
        /// A page connection that declared `host_notify_ready`.
        fn connect(&self) { self.connect_unready(); self.desk.notify_ready(self.hub.lock().unwrap().page_generation()); }
        fn connect_unready(&self) {
            let out = self.frames.clone();
            self.hub.lock().unwrap().connect(Channel::new(move |body| {
                if let tauri::ipc::InvokeResponseBody::Json(s) = body { out.lock().unwrap().push(serde_json::from_str::<Value>(&s).unwrap()["frame"].clone()); } Ok(())
            }));
        }
        fn notices(&self) -> Vec<Value> { self.frames.lock().unwrap().iter().filter(|f| f["t"] == "host:notify").cloned().collect() }
    }
    /// A card window stand-in: records what it was asked to show / close / reset, and answers
    /// with `result` (or parks the callback when `result` is `None`).
    #[derive(Default)]
    pub struct FakeCards { pub shown: Mutex<Vec<Value>>, pub closed: Mutex<Vec<String>>, pub resets: Mutex<usize>,
        pub result: Mutex<Option<CardResult>>, pub parked: Mutex<Vec<CardDone>> }
    impl Cards for FakeCards {
        fn show(&self, card: Value, done: CardDone) {
            self.shown.lock().unwrap().push(card);
            let result = *self.result.lock().unwrap();
            match result { Some(result) => done(result), None => self.parked.lock().unwrap().push(done) }
        }
        fn close(&self, incident: &str) { self.closed.lock().unwrap().push(incident.to_owned()); }
        fn reset(&self) { *self.resets.lock().unwrap() += 1; }
    }
    pub fn desk(window: Option<Window>, tray: Option<Balloon>) -> (Desk, Arc<FakeProbe>, Arc<Mutex<Option<Balloon>>>, Arc<Mutex<Vec<String>>>) { desk_at(window, tray, None) }
    pub fn desk_at(window: Option<Window>, tray: Option<Balloon>, data: Option<PathBuf>) -> (Desk, Arc<FakeProbe>, Arc<Mutex<Option<Balloon>>>, Arc<Mutex<Vec<String>>>) {
        let probe = Arc::new(FakeProbe(Mutex::new(window)));
        let tray = Arc::new(Mutex::new(tray)); let balloons = Arc::new(Mutex::new(Vec::new()));
        let (t, b) = (tray.clone(), balloons.clone());
        // The fake tray answers at once; production answers later on the tray's UI thread.
        let desk = Desk::new(Box::new(probe.clone()), Box::new(move |n: &Notice, done: BalloonDone| { b.lock().unwrap().push(n.delivery_id.clone()); let r = *t.lock().unwrap(); done(r) }), data);
        (desk, probe, tray, balloons)
    }
    fn rig(window: Option<Window>, tray: Option<Balloon>, page: bool) -> Rig { rig_from(desk(window, tray), page, |d| d) }
    fn rig_from(parts: (Desk, Arc<FakeProbe>, Arc<Mutex<Option<Balloon>>>, Arc<Mutex<Vec<String>>>), page: bool, wrap: impl FnOnce(Desk) -> Desk) -> Rig {
        let (desk, window, tray, balloons) = parts;
        let mut hub = Hub::default(); hub.reset(json!({"epoch":"one"}));
        let rig = Rig { desk: wrap(desk), hub: Mutex::new(hub), balloons, frames: Arc::new(Mutex::new(Vec::new())), window, tray };
        if page { rig.connect(); }
        rig
    }
    /// A rig with a card window stand-in (answers `Shown` unless told otherwise), a busy switch
    /// and a sound counter.
    struct Cardy { rig: Rig, cards: Arc<FakeCards>, busy: Arc<std::sync::atomic::AtomicBool>, sounds: Arc<std::sync::atomic::AtomicUsize> }
    fn cardy(window: Option<Window>, page: bool) -> Cardy {
        use std::sync::atomic::{AtomicBool, AtomicUsize};
        let cards = Arc::new(FakeCards::default()); *cards.result.lock().unwrap() = Some(CardResult::Shown);
        let (busy, sounds) = (Arc::new(AtomicBool::new(false)), Arc::new(AtomicUsize::new(0)));
        let (c, b, s) = (cards.clone(), busy.clone(), sounds.clone());
        let rig = rig_from(desk(window, Some(Balloon::Submitted)), page, move |d| d.with_cards(c)
            .with_busy(Box::new(move || b.load(Ordering::SeqCst))).with_sound(Arc::new(move || { s.fetch_add(1, Ordering::SeqCst); })));
        Cardy { rig, cards, busy, sounds }
    }
    impl Cardy {
        fn sounds(&self) -> usize { self.sounds.load(Ordering::SeqCst) }
        fn shown(&self) -> Vec<Value> { self.cards.shown.lock().unwrap().clone() }
        fn closed(&self) -> Vec<String> { self.cards.closed.lock().unwrap().clone() }
    }
    /// A frame with a notice object (`extra` overrides notice keys; `null` removes one).
    fn noticed(id: &str, incident: &str, state: &str, severity: &str, extra: Value) -> Notice {
        let mut d = data(id); d["severity"] = json!(severity);
        let mut n = json!({"kind":"outage","incident":incident,"state":state,"group":"connection","title_id":"notice.outage.title",
            "detail_id":"notice.outage.detail","params":{"minutes":3},"action":"view","sound":true,"sticky":true,"ttl_ms":0});
        for (k, v) in extra.as_object().unwrap() { if v.is_null() { n.as_object_mut().unwrap().remove(k); } else { n[k] = v.clone(); } }
        d["notice"] = n;
        Notice::parse(&d).unwrap()
    }
    const FRONT: Option<Window> = Some(Window { visible: true, focused: true });
    const BEHIND: Option<Window> = Some(Window { visible: true, focused: false });
    const HIDDEN: Option<Window> = Some(Window { visible: false, focused: false });
    fn notice(id: &str) -> Notice { Notice::parse(&data(id)).unwrap() }
    fn receipt(id: &str, outcome: Outcome) -> Receipt { Receipt { delivery_id: id.into(), outcome } }

    #[test] fn route_table() {
        let free = || false; let busy = || true;
        assert_eq!(route(false, true, true, free), Route::InApp);
        assert_eq!(route(false, true, false, free), Route::Card, "page offline");
        assert_eq!(route(false, false, true, free), Route::Card, "background");
        assert_eq!(route(true, true, true, free), Route::Card, "a forced preview always tries the card window");
        assert_eq!(route(false, false, true, busy), Route::Hold, "busy: held instead of shown");
        assert_eq!(route(true, false, true, busy), Route::Card, "a forced preview is never held");
        assert_eq!(route(false, true, true, || panic!("busy is not asked for an in-app notice")), Route::InApp);
    }
    #[test] fn foreground_goes_in_app_and_page_confirmation_is_delivered_in_app() {
        let rig = rig(FRONT, Some(Balloon::Submitted), true); let now = Instant::now();
        assert_eq!(rig.desk.deliver(&notice("desktop-a"), &rig.hub, now), Handled::InApp);
        let shown = rig.notices();
        assert_eq!(shown.len(), 1);
        assert_eq!(shown[0], json!({"t":"host:notify","epoch":"one","delivery_id":"desktop-a","severity":"warn","title":"Tier down","detail":"Vanguard offline","target":"alerts"}));
        assert!(rig.balloons.lock().unwrap().is_empty(), "no balloon while in front");
        assert!(rig.desk.receipts(now).is_empty(), "shown is not delivered: only the page's confirmation counts");
        let page = rig.hub.lock().unwrap().page_generation();
        assert_eq!(rig.desk.rendered("desktop-a", page), Ok(()));
        assert_eq!(rig.desk.receipts(now), vec![receipt("desktop-a", Outcome::DeliveredInApp)]);
        assert_eq!(rig.desk.rendered("desktop-a", page), Err("already_receipted"), "one receipt per id");
        assert_eq!(rig.desk.rendered("desktop-zzz", page), Err("unknown_delivery"), "the page cannot invent a delivery");
        assert!(rig.desk.receipts(now + RENDER_WAIT * 2).is_empty());
    }
    #[test] fn background_or_hidden_uses_the_balloon() {
        for window in [BEHIND, HIDDEN, None] {
            let rig = rig(window, Some(Balloon::Submitted), true); let now = Instant::now();
            assert_eq!(rig.desk.deliver(&notice("desktop-b"), &rig.hub, now), Handled::Card);
            assert!(rig.notices().is_empty());
            assert_eq!(rig.desk.receipts(now), vec![receipt("desktop-b", Outcome::DeliveredWindows)]);
        }
        // In front, but no page Channel: the balloon.
        let rig = rig(FRONT, Some(Balloon::Submitted), false);
        assert_eq!(rig.desk.deliver(&notice("desktop-c"), &rig.hub, Instant::now()), Handled::Card);
    }
    #[test] fn forced_test_tries_windows_even_in_front() {
        let rig = rig(FRONT, Some(Balloon::Submitted), true); let now = Instant::now();
        let forced = notice("desktop-test-1"); assert!(forced.force);
        assert_eq!(rig.desk.deliver(&forced, &rig.hub, now), Handled::Card);
        assert_eq!(*rig.balloons.lock().unwrap(), vec!["desktop-test-1".to_string()]);
        assert!(rig.notices().is_empty());
        // The explicit field wins over the prefix once the integration ticket sends it.
        let mut d = data("desktop-d"); d["force"] = json!(true);
        assert!(Notice::parse(&d).unwrap().force);
        let mut d = data("desktop-test-2"); d["force"] = json!(false);
        assert!(!Notice::parse(&d).unwrap().force);
        d["force"] = json!("yes"); assert_eq!(Notice::parse(&d), Err("invalid_force"));
    }
    #[test] fn no_tray_is_dropped_and_a_refused_balloon_failed() {
        let rig = rig(HIDDEN, None, true); let now = Instant::now();
        assert_eq!(rig.desk.deliver(&notice("desktop-e"), &rig.hub, now), Handled::Card);
        rig.tray(Some(Balloon::NoIcon));
        assert_eq!(rig.desk.deliver(&notice("desktop-f"), &rig.hub, now), Handled::Card);
        rig.tray(Some(Balloon::Rejected));
        assert_eq!(rig.desk.deliver(&notice("desktop-g"), &rig.hub, now), Handled::Card);
        rig.tray(None); rig.set(FRONT);
        assert_eq!(rig.desk.deliver(&notice("desktop-test-h"), &rig.hub, now), Handled::Card, "forced test without a tray");
        assert_eq!(rig.desk.receipts(now), vec![receipt("desktop-e", Outcome::Dropped), receipt("desktop-f", Outcome::Dropped),
            receipt("desktop-g", Outcome::FailedWindows), receipt("desktop-test-h", Outcome::Dropped)]);
        assert_eq!(Outcome::Dropped.wire(), ("dropped", "none"));
        assert_eq!(Outcome::FailedWindows.wire(), ("failed", "windows"));
        assert_eq!(Outcome::DeliveredInApp.wire(), ("delivered", "in_app"));
        assert_eq!(Outcome::from_balloon(Some(Balloon::Empty)), Outcome::FailedWindows);
    }
    #[test] fn page_timeout_is_dropped_and_a_late_answer_refused() {
        let rig = rig(FRONT, Some(Balloon::Submitted), true); let now = Instant::now();
        assert_eq!(rig.desk.deliver(&notice("desktop-i"), &rig.hub, now), Handled::InApp);
        assert!(rig.desk.receipts(now + RENDER_WAIT - Duration::from_millis(1)).is_empty());
        assert_eq!(rig.desk.receipts(now + RENDER_WAIT), vec![receipt("desktop-i", Outcome::Dropped)]);
        let page = rig.hub.lock().unwrap().page_generation();
        assert_eq!(rig.desk.rendered("desktop-i", page), Err("already_receipted"));
        assert!(rig.desk.receipts(now + RENDER_WAIT * 3).is_empty());
        assert!(rig.balloons.lock().unwrap().is_empty(), "a silent page does not turn into a balloon");
    }
    #[test] fn a_reloaded_page_cannot_confirm_the_old_connection() {
        let rig = rig(FRONT, Some(Balloon::Submitted), true); let now = Instant::now();
        rig.desk.deliver(&notice("desktop-j"), &rig.hub, now);
        let old = rig.hub.lock().unwrap().page_generation();
        rig.connect();
        let new = rig.hub.lock().unwrap().page_generation();
        assert_ne!(old, new);
        assert_eq!(rig.desk.rendered("desktop-j", new), Err("stale_page"));
        assert_eq!(rig.desk.rendered("desktop-j", old), Ok(()));
    }
    #[test] fn each_delivery_id_is_receipted_once() {
        let rig = rig(HIDDEN, Some(Balloon::Submitted), true); let now = Instant::now();
        assert_eq!(rig.desk.deliver(&notice("desktop-k"), &rig.hub, now), Handled::Card);
        assert_eq!(rig.desk.deliver(&notice("desktop-k"), &rig.hub, now), Handled::Duplicate);
        rig.set(FRONT);
        assert_eq!(rig.desk.deliver(&notice("desktop-k"), &rig.hub, now), Handled::Duplicate, "route does not matter for a repeat");
        assert_eq!(rig.desk.deliver(&notice("desktop-l"), &rig.hub, now), Handled::InApp);
        assert_eq!(rig.desk.deliver(&notice("desktop-l"), &rig.hub, now), Handled::Duplicate, "pending in-app is known");
        assert_eq!(rig.notices().len(), 1);
        assert_eq!(rig.balloons.lock().unwrap().len(), 1);
        assert_eq!(rig.desk.receipts(now).len(), 1);
        // A new epoch forgets the old ids and their unsent receipts.
        rig.desk.session();
        assert!(rig.desk.receipts(now + RENDER_WAIT).is_empty(), "the old epoch's pending notice is not receipted to the new sidecar");
        rig.set(HIDDEN);
        assert_eq!(rig.desk.deliver(&notice("desktop-k"), &rig.hub, now), Handled::Card);
    }
    #[test] fn notifier_bounds_and_receipt_frame() {
        let mut n = Notifier::default(); let now = Instant::now();
        for i in 0..PENDING_CAPACITY { assert!(n.await_page(&format!("p{i}"), 1, now)); }
        assert!(!n.await_page("over", 1, now));
        for i in 0..DONE_CAPACITY + 10 { n.finish(&format!("d{i}"), Outcome::Dropped); }
        assert!(!n.known("d0") && n.known(&format!("d{}", DONE_CAPACITY + 9)), "the receipted-id memory is bounded");
        let frame = receipt_frame(12, &receipt("desktop-x", Outcome::FailedWindows));
        assert_eq!(frame["args"], json!({"delivery_id":"desktop-x","outcome":"failed","surface":"windows"}));
        assert_eq!((frame["cmd"].as_str(), frame["from"].as_str(), frame["owner"].as_str(), frame["req_id"].as_str()),
            (Some("alerts.desktopDelivered"), Some("shell"), Some("shell"), Some("shell-notify-12")));
        // A full in-app table drops at once instead of waiting.
        let rig = rig(FRONT, Some(Balloon::Submitted), true);
        for i in 0..PENDING_CAPACITY { rig.desk.notifier().await_page(&format!("q{i}"), 0, now); }
        assert_eq!(rig.desk.deliver(&notice("desktop-m"), &rig.hub, now), Handled::InAppFull);
        assert_eq!(rig.desk.receipts(now), vec![receipt("desktop-m", Outcome::Dropped)]);
    }
    #[test] fn dto_validation() {
        assert!(Notice::parse(&data("desktop-n")).is_ok());
        for (key, value, code) in [("delivery_id", json!(""), "invalid_delivery_id"), ("delivery_id", json!("a b"), "invalid_delivery_id"),
            ("severity", json!("fatal"), "invalid_severity"), ("target", json!("nodes"), "invalid_target"), ("title", json!(3), "invalid_title"),
            ("detail", json!(null), "invalid_detail"), ("members", json!([1]), "invalid_members"), ("event_ids", json!("e"), "invalid_members")] {
            let mut d = data("desktop-n"); d[key] = value; assert_eq!(Notice::parse(&d), Err(code), "{key}");
        }
    }
    #[test] fn shell_topics_are_split_out_of_frames_and_validated() {
        let evt = |topic: &str, rev: u64, kind: &str| json!({"t":"evt","epoch":"one","topic":topic,"rev":rev,"seq":rev,"kind":kind,"produced_at_ms":1,"sent_at_ms":1,"data":{}});
        let mut single = evt(DESKTOP, 1, "edge");
        assert_eq!(take_shell_items(&mut single).len(), 1); assert!(single.is_null());
        let mut page = evt("service.state", 1, "snapshot");
        assert!(take_shell_items(&mut page).is_empty()); assert_eq!(page["topic"], "service.state");
        let mut batch = json!({"t":"batch","items":[evt("service.state",1,"snapshot"),evt(LANGUAGE,1,"edge"),evt("jobs",1,"edge"),evt(DESKTOP,1,"edge")]});
        let shell = take_shell_items(&mut batch);
        assert_eq!(shell.iter().map(|i| i["topic"].as_str().unwrap()).collect::<Vec<_>>(), vec![LANGUAGE, DESKTOP]);
        assert_eq!(batch["items"].as_array().unwrap().iter().map(|i| i["topic"].as_str().unwrap()).collect::<Vec<_>>(), vec!["service.state", "jobs"]);
        let mut cursor = Cursor::default();
        assert_eq!(cursor.validate(&evt(DESKTOP, 1, "edge"), "one"), Ok((DESKTOP, None)));
        assert_eq!(cursor.validate(&evt(DESKTOP, 1, "edge"), "one"), Err("sequence_gap"), "a replayed revision");
        assert_eq!(cursor.validate(&evt(DESKTOP, 2, "snapshot"), "one"), Err("invalid_enum"), "alerts.desktop has no snapshot");
        assert_eq!(cursor.validate(&evt(LANGUAGE, 1, "snapshot"), "one"), Ok((LANGUAGE, None)));
        assert_eq!(cursor.validate(&evt(LANGUAGE, 2, "edge"), "two"), Err("epoch_mismatch"));
        let mut keyed = evt(LANGUAGE, 3, "edge"); keyed["key"] = json!("x");
        assert_eq!(cursor.validate(&keyed, "one"), Err("invalid_enum"));
        let mut stale = evt(LANGUAGE, 4, "edge"); stale.as_object_mut().unwrap().remove("sent_at_ms");
        assert_eq!(cursor.validate(&stale, "one"), Err("missing_field"));
        // The first ui.language edge has seq 4: accepted, the jump from 1 is reported.
        assert_eq!(cursor.validate(&evt(LANGUAGE, 4, "edge"), "one"), Ok((LANGUAGE, Some(Skip { expected: 1, got: 4 }))));
    }
    #[test] fn shell_topic_seq_skips_are_reported_and_gaps_follow_the_hub_rule() {
        let edge = |rev: u64, seq: u64| json!({"t":"evt","epoch":"one","topic":DESKTOP,"rev":rev,"seq":seq,"kind":"edge","produced_at_ms":1,"sent_at_ms":1,"data":{}});
        let gap = |rev: u64, seq: u64, from: u64, to: u64, dropped: u64| json!({"t":"evt","epoch":"one","topic":DESKTOP,"rev":rev,"seq":seq,"kind":"gap",
            "produced_at_ms":1,"sent_at_ms":1,"data":{"from_seq":from,"to_seq":to,"dropped":dropped}});
        let mut c = Cursor::default();
        assert_eq!(c.validate(&edge(1, 1), "one"), Ok((DESKTOP, None)));
        assert_eq!(c.validate(&edge(2, 2), "one"), Ok((DESKTOP, None)));
        assert_eq!(c.validate(&edge(3, 5), "one"), Ok((DESKTOP, Some(Skip { expected: 3, got: 5 }))), "a silent jump is accepted and reported");
        assert_eq!(c.validate(&edge(4, 5), "one"), Err("sequence_gap"), "seq never goes back");
        // Gap: from = previous + 1, seq = to + 1, dropped = to - from + 1 (transport.rs EventCursor).
        assert_eq!(c.validate(&gap(5, 9, 7, 8, 2), "one"), Err("sequence_gap"), "does not start right after the last edge");
        assert_eq!(c.validate(&gap(6, 9, 6, 8, 2), "one"), Err("sequence_gap"), "wrong dropped count");
        assert_eq!(c.validate(&gap(7, 10, 6, 8, 3), "one"), Err("sequence_gap"), "seq must follow to_seq");
        let mut no_from = gap(8, 9, 6, 8, 3); no_from["data"].as_object_mut().unwrap().remove("from_seq");
        assert_eq!(c.validate(&no_from, "one"), Err("invalid_type"));
        assert_eq!(c.validate(&gap(9, 9, 6, 8, 3), "one"), Ok((DESKTOP, None)));
        assert_eq!(c.validate(&edge(10, 10), "one"), Ok((DESKTOP, None)), "the edge after a valid gap is contiguous");
    }
    #[test] fn a_foreign_shell_item_rejects_the_whole_batch() {
        let item = |topic: &str, epoch: &str| json!({"t":"evt","epoch":epoch,"topic":topic,"rev":1,"seq":1,"kind":"edge","produced_at_ms":1,"sent_at_ms":1,"data":{}});
        assert_eq!(check_batch(&json!({"t":"batch","items":[item("jobs","one"),item(DESKTOP,"one")]}), "one"), Ok(()));
        assert_eq!(check_batch(&json!({"t":"batch","items":[item("jobs","one"),item(DESKTOP,"two")]}), "one"), Err("epoch_mismatch"));
        let mut stale = item(LANGUAGE, "one"); stale.as_object_mut().unwrap().remove("produced_at_ms");
        assert_eq!(check_batch(&json!({"t":"batch","items":[stale]}), "one"), Err("missing_field"));
        assert_eq!(check_batch(&item(DESKTOP, "two"), "one"), Ok(()), "a single evt is the Cursor's to check");
    }
    #[test] fn only_a_page_that_declared_ready_gets_in_app_notices() {
        // Online, in front, but the page never called host_notify_ready: the balloon.
        let rig = rig(FRONT, Some(Balloon::Submitted), false); rig.connect_unready(); let now = Instant::now();
        assert_eq!(rig.desk.deliver(&notice("desktop-r1"), &rig.hub, now), Handled::Card);
        assert!(rig.notices().is_empty());
        assert_eq!(rig.desk.receipts(now), vec![receipt("desktop-r1", Outcome::DeliveredWindows)]);
        rig.desk.notify_ready(rig.hub.lock().unwrap().page_generation());
        assert_eq!(rig.desk.deliver(&notice("desktop-r2"), &rig.hub, now), Handled::InApp);
        // A reloaded page is a new connection: it must declare again.
        rig.connect_unready();
        assert_eq!(rig.desk.deliver(&notice("desktop-r3"), &rig.hub, now), Handled::Card);
        assert_eq!(*rig.balloons.lock().unwrap(), vec!["desktop-r1".to_string(), "desktop-r3".to_string()]);
    }
    #[test] fn the_balloon_receipt_waits_for_the_tray_thread() {
        // Production submits on the tray's UI thread: deliver only hands the notice over.
        let parked: Arc<Mutex<Vec<BalloonDone>>> = Arc::new(Mutex::new(Vec::new())); let park = parked.clone();
        let probe = Arc::new(FakeProbe(Mutex::new(HIDDEN)));
        let desk = Desk::new(Box::new(probe), Box::new(move |_: &Notice, done: BalloonDone| park.lock().unwrap().push(done)), None);
        let mut hub = Hub::default(); hub.reset(json!({"epoch":"one"})); let hub = Mutex::new(hub); let now = Instant::now();
        assert_eq!(desk.deliver(&notice("desktop-s1"), &hub, now), Handled::Card);
        assert!(desk.receipts(now).is_empty(), "not submitted yet: no receipt");
        assert_eq!(desk.deliver(&notice("desktop-s1"), &hub, now), Handled::Duplicate, "claimed before the tray answered");
        let done = parked.lock().unwrap().pop().unwrap();
        std::thread::spawn(move || done(Some(Balloon::Rejected))).join().unwrap();
        assert_eq!(desk.receipts(now), vec![receipt("desktop-s1", Outcome::FailedWindows)]);
    }
    #[test] fn subscription_follows_hello_caps() {
        let p1 = json!({"caps":{"topics":TOPICS,"commands":["app.quit"]}});
        assert_eq!(subscription(&p1), TOPICS.to_vec());
        assert!(!supports(&p1, "ui.presence") && supports(&p1, "app.quit"));
        let mut all: Vec<&str> = TOPICS.to_vec(); all.extend([LANGUAGE, DESKTOP]);
        let full = json!({"caps":{"topics":all,"commands":["alerts.desktopDelivered","ui.presence"]}});
        let mut expected = TOPICS.to_vec(); expected.extend([DESKTOP, LANGUAGE]);
        assert_eq!(subscription(&full), expected, "fixed order: P1 topics, then alerts.desktop, ui.language");
        assert!(supports(&full, "ui.presence") && supports(&full, "alerts.desktopDelivered"));
        // T-P3-06: the hand-off topic is subscribed only when declared, and never reaches the page.
        let mut update = TOPICS.to_vec(); update.push(PREPARE_EXIT);
        assert_eq!(subscription(&json!({"caps":{"topics":update}})).last(), Some(&PREPARE_EXIT));
        let mut frame = json!({"t":"evt","topic":PREPARE_EXIT,"kind":"edge","rev":1,"seq":1,"data":{}});
        assert_eq!(take_shell_items(&mut frame).len(), 1); assert!(frame.is_null());
        assert_eq!(Cursor::default().validate(&json!({"t":"evt","epoch":"e","topic":PREPARE_EXIT,"kind":"snapshot","rev":1,"produced_at_ms":1,"sent_at_ms":1,"data":{}}), "e"), Err("invalid_enum"));
        assert_eq!(language_frame("ja_JP"), Some(json!({"t":"host:language","language":"ja_JP"})));
        assert_eq!(language_frame("xx_XX"), None);
    }
    #[test] fn v1_subscription_takes_every_declared_page_topic_but_never_shutdown() {
        let mut declared: Vec<&str> = TOPICS.to_vec();
        declared.extend(["shutdown", PREPARE_EXIT, "nodes.list", DESKTOP, "config.items", LANGUAGE, "no.such.topic"]);
        let topics = subscription(&json!({"caps":{"topics":declared}}));
        let mut expected = TOPICS.to_vec(); expected.extend(["nodes.list", "config.items", DESKTOP, LANGUAGE, PREPARE_EXIT]);
        assert_eq!(topics, expected, "P1, then v1 page topics in V1_PAGE_TOPICS order, then shell topics");
        let mut all: Vec<&str> = TOPICS.to_vec(); all.extend(V1_PAGE_TOPICS); all.extend(SHELL_TOPICS); all.push("shutdown");
        assert_eq!(subscription(&json!({"caps":{"topics":all}})).len(), TOPICS.len() + V1_PAGE_TOPICS.len() + SHELL_TOPICS.len());
        // force=false on a test-prefixed id wins over the legacy prefix.
        let mut data = json!({"delivery_id":"desktop-test-1","event_ids":[],"severity":"info","title":"t","detail":"d","members":[],"target":"alerts","force":false});
        assert!(!Notice::parse(&data).unwrap().force);
        data.as_object_mut().unwrap().remove("force");
        assert!(Notice::parse(&data).unwrap().force, "legacy frame: the prefix still marks a forced test");
        data["force"] = json!("yes");
        assert_eq!(Notice::parse(&data), Err("invalid_force"));
    }

    // ---- 10-30 notice redesign: the notice object, the card route, hold, one place per incident ----

    #[test] fn the_notice_object_parses_and_a_malformed_one_falls_back_to_the_text() {
        let n = noticed("d1", "outage-1", "open", "error", json!({}));
        let spec = n.spec.as_ref().unwrap();
        assert_eq!((spec.kind, spec.incident.as_str(), spec.state, spec.group), ("outage", "outage-1", State::Open, "connection"));
        assert_eq!((spec.detail_id.as_deref(), spec.reason_id.as_deref(), spec.action, spec.sound, spec.sticky, spec.ttl_ms),
            (Some("notice.outage.detail"), None, true, true, true, 0));
        assert_eq!(n.spec_error, None);
        // Optional keys: absent and null alike; an errors.start.* detail is a message id like any other.
        let n = noticed("d2", "i", "open", "warn", json!({"action":null,"detail_id":"errors.start.port_in_use","reason_id":null,"fix_id":null,"params":null}));
        let spec = n.spec.unwrap();
        assert!(!spec.action && spec.params.is_empty() && spec.reason_id.is_none());
        assert_eq!(spec.detail_id.as_deref(), Some("errors.start.port_in_use"));
        // ttl: sticky → 0; not sticky and 0 → the default for the severity / recovered; clamped otherwise.
        let ttl = |state: &str, severity: &str, extra: Value| noticed("t", "i", state, severity, extra).spec.unwrap().ttl_ms;
        assert_eq!(ttl("open", "info", json!({"sticky":false})), 8000);
        assert_eq!(ttl("open", "warn", json!({"sticky":false})), 10000);
        assert_eq!(ttl("resolved", "info", json!({"sticky":false})), 6000);
        assert_eq!(ttl("open", "info", json!({"sticky":false,"ttl_ms":10})), 2000);
        assert_eq!(ttl("open", "info", json!({"sticky":false,"ttl_ms":9_999_999})), 120_000);
        assert_eq!(ttl("open", "info", json!({"sticky":true,"ttl_ms":5000})), 0);
        // Malformed notices never refuse the frame: it is shown with its title / detail.
        for (extra, code) in [(json!({"kind":"service_stopped"}), "notice_kind"), (json!({"kind":"x"}), "notice_kind"),
            (json!({"incident":""}), "notice_incident"), (json!({"incident":"a b"}), "notice_incident"), (json!({"incident":"x".repeat(65)}), "notice_incident"),
            (json!({"state":"gone"}), "notice_state"), (json!({"group":"other"}), "notice_group"), (json!({"title_id":"Notice.Title"}), "notice_title_id"),
            (json!({"title_id":"title"}), "notice_title_id"), (json!({"title_id":null}), "notice_title_id"), (json!({"detail_id":"<b>"}), "notice_message_id"),
            (json!({"params":{"a":true}}), "notice_params"), (json!({"params":{"A":1}}), "notice_params"), (json!({"params":{"a":1.5}}), "notice_params"),
            (json!({"params":{"a":"x".repeat(201)}}), "notice_params"), (json!({"params":[1]}), "notice_params"),
            (json!({"params":{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6,"g":7,"h":8,"i":9}}), "notice_params"),
            (json!({"action":"open"}), "notice_action"), (json!({"sound":"yes"}), "notice_flag"), (json!({"ttl_ms":-1}), "notice_ttl")] {
            let mut d = data("bad"); d["notice"] = json!({"kind":"outage","incident":"i","state":"open","group":"connection","title_id":"notice.outage.title"});
            for (k, v) in extra.as_object().unwrap() { d["notice"][k] = v.clone(); }
            let n = Notice::parse(&d).unwrap_or_else(|e| panic!("{extra}: the frame itself was refused ({e})"));
            assert_eq!((n.spec.is_none(), n.spec_error), (true, Some(code)), "{extra}");
            assert_eq!(n.host_frame().get("notice"), None, "no notice reaches the page");
        }
        let mut d = data("bad"); d["notice"] = json!("text");
        assert_eq!(Notice::parse(&d).unwrap().spec_error, Some("notice_not_object"));
    }
    #[test] fn the_host_frame_and_the_card_carry_the_notice() {
        let n = noticed("d1", "outage-1", "open", "error", json!({"reason_id":"notice.outage.reason.source_supply"}));
        let frame = n.host_frame();
        assert_eq!(frame["notice"], json!({"kind":"outage","incident":"outage-1","state":"open","group":"connection","title_id":"notice.outage.title",
            "detail_id":"notice.outage.detail","reason_id":"notice.outage.reason.source_supply","params":{"minutes":3},"action":"view","sticky":true,"ttl_ms":0}));
        assert_eq!(frame.get("sound"), None, "the sound is the shell's, never the page's");
        let card = n.card();
        assert_eq!((card["delivery_id"].as_str(), card["severity"].as_str(), card["target"].as_str(), card["title"].as_str(), card["shell"].as_bool()),
            (Some("d1"), Some("error"), Some("alerts"), Some("Tier down"), Some(false)));
        assert_eq!((card["incident"].as_str(), card["title_id"].as_str(), card["reason_id"].as_str()), (Some("outage-1"), Some("notice.outage.title"), Some("notice.outage.reason.source_supply")));
        // A frame without a notice is a legacy card of its own text, an incident of its own.
        let legacy = notice("d2").card();
        assert_eq!((legacy["kind"].as_str(), legacy["incident"].as_str(), legacy["state"].as_str(), legacy["action"].as_str(), legacy["sticky"].as_bool(), legacy["ttl_ms"].as_u64()),
            (Some("legacy"), Some("delivery:d2"), Some("open"), Some("view"), Some(false), Some(10000)));
        assert_eq!(legacy.get("title_id"), None);
        let shell = shell_card(4, "summary", "info", "notice.summary.title", "notice.summary.detail", json!({"count":2,"resolved":1}), false);
        assert_eq!((shell["delivery_id"].as_str(), shell["shell"].as_bool(), shell["ttl_ms"].as_u64(), shell["target"].as_str()),
            (Some("shell-summary-4"), Some(true), Some(8000), Some("overview")));
    }
    #[test] fn a_card_on_screen_is_delivered_windows_and_plays_the_sound_once() {
        let c = cardy(HIDDEN, true); let now = Instant::now();
        assert_eq!(c.rig.desk.deliver(&noticed("c1", "outage-1", "open", "error", json!({})), &c.rig.hub, now), Handled::Card);
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("c1", Outcome::DeliveredWindows)]);
        assert_eq!(c.sounds(), 1);
        assert!(c.rig.balloons.lock().unwrap().is_empty(), "no balloon when the card is on screen");
        assert_eq!(c.shown()[0]["incident"], "outage-1");
        // No sound for a notice that does not ask for it.
        c.rig.desk.deliver(&noticed("c2", "fallback-1", "open", "warn", json!({"sound":false,"kind":"fallback","group":"tier_fallback"})), &c.rig.hub, now);
        assert_eq!(c.sounds(), 1);
        // Forced preview in front: still the card window, never in-app.
        c.rig.set(FRONT);
        assert_eq!(c.rig.desk.deliver(&noticed("desktop-test-9", "preview", "open", "info", json!({"kind":"preview","group":"preview","sound":false,"sticky":false})), &c.rig.hub, now), Handled::Card);
        assert!(c.rig.notices().is_empty());
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("c2", Outcome::DeliveredWindows), receipt("desktop-test-9", Outcome::DeliveredWindows)]);
    }
    #[test] fn the_receipt_waits_for_the_card_and_an_unavailable_card_falls_back_to_the_balloon() {
        let c = cardy(HIDDEN, true); let now = Instant::now();
        *c.cards.result.lock().unwrap() = None;
        assert_eq!(c.rig.desk.deliver(&noticed("w1", "i1", "open", "error", json!({})), &c.rig.hub, now), Handled::Card);
        assert!(c.rig.desk.receipts(now).is_empty(), "not on screen yet: no receipt");
        assert_eq!(c.rig.desk.deliver(&noticed("w1", "i1", "open", "error", json!({})), &c.rig.hub, now), Handled::Duplicate, "claimed while waiting");
        let done = c.cards.parked.lock().unwrap().pop().unwrap();
        std::thread::spawn(move || done(CardResult::Shown)).join().unwrap();
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("w1", Outcome::DeliveredWindows)]);
        // Unavailable (no notice.html, the page did not render): the balloon, with its usual outcomes.
        *c.cards.result.lock().unwrap() = Some(CardResult::Unavailable);
        c.rig.desk.deliver(&noticed("w2", "i2", "open", "error", json!({})), &c.rig.hub, now);
        c.rig.tray(Some(Balloon::Rejected));
        c.rig.desk.deliver(&noticed("w3", "i3", "open", "error", json!({})), &c.rig.hub, now);
        c.rig.tray(None);
        c.rig.desk.deliver(&noticed("w4", "i4", "open", "error", json!({})), &c.rig.hub, now);
        assert_eq!(*c.rig.balloons.lock().unwrap(), ["w2", "w3", "w4"]);
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("w2", Outcome::DeliveredWindows), receipt("w3", Outcome::FailedWindows), receipt("w4", Outcome::Dropped)]);
        assert_eq!(c.sounds(), 1, "only the card that was really shown rang");
        // Dropped: an older frame of the incident that never reached the screen.
        *c.cards.result.lock().unwrap() = Some(CardResult::Dropped);
        c.rig.desk.deliver(&noticed("w5", "i5", "open", "error", json!({})), &c.rig.hub, now);
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("w5", Outcome::Dropped)]);
    }
    #[test] fn closed_takes_the_card_or_banner_down_silently_and_is_dropped_at_once() {
        let c = cardy(HIDDEN, true); let now = Instant::now();
        assert_eq!(c.rig.desk.deliver(&noticed("x1", "outage-1", "closed", "error", json!({})), &c.rig.hub, now), Handled::Closed);
        assert_eq!(c.closed(), ["outage-1"]);
        assert!(c.shown().is_empty(), "nothing new is shown for a closed incident");
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("x1", Outcome::Dropped)]);
        // In front: the page gets the closed frame (it dismisses silently) and nothing waits for it.
        c.rig.set(FRONT);
        assert_eq!(c.rig.desk.deliver(&noticed("x2", "outage-2", "closed", "error", json!({})), &c.rig.hub, now), Handled::Closed);
        assert_eq!(c.rig.notices().len(), 1);
        assert_eq!(c.rig.notices()[0]["notice"]["state"], "closed");
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("x2", Outcome::Dropped)]);
        assert_eq!(c.rig.desk.receipts(now + RENDER_WAIT * 2), vec![], "no second receipt after the page wait");
        assert_eq!(c.sounds(), 0);
    }
    #[test] fn one_incident_lives_in_one_place() {
        let c = cardy(FRONT, true); let now = Instant::now();
        // In front: the page shows the outage; the card window lets go of it.
        assert_eq!(c.rig.desk.deliver(&noticed("p1", "outage-1", "open", "error", json!({})), &c.rig.hub, now), Handled::InApp);
        assert_eq!(c.closed(), ["outage-1"]);
        // Hidden: the next frame of the incident goes to the card window, and the page's banner goes.
        c.rig.set(HIDDEN);
        assert_eq!(c.rig.desk.deliver(&noticed("p2", "outage-1", "update", "error", json!({})), &c.rig.hub, now), Handled::Card);
        let clear = c.rig.notices().into_iter().find(|f| f["delivery_id"].as_str().is_some_and(|d| d.starts_with("shell-clear-"))).expect("a clear frame");
        assert_eq!((clear["notice"]["incident"].as_str(), clear["notice"]["state"].as_str()), (Some("outage-1"), Some("closed")));
        let page = c.rig.hub.lock().unwrap().page_generation();
        assert_eq!(c.rig.desk.rendered(clear["delivery_id"].as_str().unwrap(), page), Err("unknown_delivery"), "a clear is never confirmed");
        // Only once: the page no longer holds it.
        c.rig.desk.deliver(&noticed("p3", "outage-1", "update", "error", json!({})), &c.rig.hub, now);
        assert_eq!(c.rig.notices().iter().filter(|f| f["delivery_id"].as_str().is_some_and(|d| d.starts_with("shell-clear-"))).count(), 1);
        // In front again: the recovery shows in the page, and the corner card goes.
        c.rig.set(FRONT);
        assert_eq!(c.rig.desk.deliver(&noticed("p4", "outage-1", "resolved", "info", json!({"sticky":false})), &c.rig.hub, now), Handled::InApp);
        assert_eq!(c.closed(), ["outage-1", "outage-1"]);
        assert!(c.rig.desk.rendered("p1", page).is_ok());
    }
    #[test] fn in_app_sound_only_when_the_page_really_showed_it() {
        let c = cardy(FRONT, true); let now = Instant::now();
        let page = c.rig.hub.lock().unwrap().page_generation();
        c.rig.desk.deliver(&noticed("s1", "outage-1", "open", "error", json!({})), &c.rig.hub, now);
        c.rig.desk.deliver(&noticed("s2", "outage-2", "open", "error", json!({})), &c.rig.hub, now);
        c.rig.desk.deliver(&noticed("s3", "update-1", "open", "info", json!({"sound":false,"kind":"update","group":"update"})), &c.rig.hub, now);
        assert_eq!(c.sounds(), 0, "nothing rings before the page confirmed");
        assert_eq!(c.rig.desk.rendered_as("s1", page, false), Ok(()), "the overview showed it already: receipted, silent");
        assert_eq!(c.sounds(), 0);
        assert_eq!(c.rig.desk.rendered_as("s2", page, true), Ok(()));
        assert_eq!(c.rig.desk.rendered("s3", page), Ok(()));
        assert_eq!(c.sounds(), 1);
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("s1", Outcome::DeliveredInApp), receipt("s2", Outcome::DeliveredInApp), receipt("s3", Outcome::DeliveredInApp)]);
    }
    #[test] fn busy_holds_the_latest_frame_per_incident_and_releases_with_a_summary() {
        use std::sync::atomic::Ordering::SeqCst;
        let c = cardy(HIDDEN, true); let now = Instant::now();
        c.busy.store(true, SeqCst);
        assert_eq!(c.rig.desk.deliver(&noticed("h1", "outage-1", "open", "error", json!({})), &c.rig.hub, now), Handled::Held);
        assert_eq!(c.rig.desk.deliver(&noticed("h1", "outage-1", "open", "error", json!({})), &c.rig.hub, now), Handled::Duplicate, "a held id is known");
        assert_eq!(c.rig.desk.deliver(&noticed("h2", "outage-1", "update", "error", json!({})), &c.rig.hub, now), Handled::Held);
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("h1", Outcome::Dropped)], "superseded by the incident's newer frame");
        c.rig.desk.deliver(&noticed("h3", "fallback-1", "open", "warn", json!({"kind":"fallback","group":"tier_fallback"})), &c.rig.hub, now);
        c.rig.desk.deliver(&noticed("h4", "capture-1", "open", "error", json!({"kind":"capture_lost"})), &c.rig.hub, now);
        c.rig.desk.deliver(&noticed("h5", "capture-1", "resolved", "info", json!({"kind":"capture_lost","sticky":false})), &c.rig.hub, now);
        c.rig.desk.deliver(&noticed("h6", "start-1", "closed", "error", json!({"kind":"start_failed"})), &c.rig.hub, now);
        assert!(c.shown().is_empty() && c.sounds() == 0, "nothing is shown or heard while busy");
        // The preview is never held.
        assert_eq!(c.rig.desk.deliver(&noticed("desktop-test-1", "preview", "open", "info", json!({"kind":"preview","group":"preview"})), &c.rig.hub, now), Handled::Card);
        // Re-checked every 5 s while something is held; still busy: still held.
        c.rig.desk.tick(&c.rig.hub, now + HOLD_RECHECK - Duration::from_millis(1));
        c.rig.desk.tick(&c.rig.hub, now + HOLD_RECHECK);
        assert_eq!(c.shown().len(), 1);
        // Free: the open / update incidents are shown, the others go quietly, one summary follows.
        c.busy.store(false, SeqCst);
        c.rig.desk.tick(&c.rig.hub, now + HOLD_RECHECK * 2);
        let shown = c.shown();
        let ids: Vec<&str> = shown.iter().map(|s| s["delivery_id"].as_str().unwrap()).collect();
        assert_eq!(&ids[1..3], ["h2", "h3"]);
        assert_eq!(shown[3]["kind"], "summary");
        assert_eq!(shown[3]["params"], json!({"count":3,"resolved":1}), "h2, h3 and the recovered h5; the closed h6 ends silently");
        assert_eq!((shown[3]["title_id"].as_str(), shown[3]["shell"].as_bool()), (Some("notice.summary.title"), Some(true)));
        assert!(c.closed().contains(&"capture-1".to_string()) && c.closed().contains(&"start-1".to_string()), "their old cards go quietly");
        let receipts = c.rig.desk.receipts(now);
        for (id, outcome) in [("desktop-test-1", Outcome::DeliveredWindows), ("h2", Outcome::DeliveredWindows), ("h3", Outcome::DeliveredWindows),
            ("h4", Outcome::Dropped), ("h5", Outcome::Dropped), ("h6", Outcome::Dropped)] {
            assert_eq!(receipts.iter().filter(|r| r.delivery_id == id).map(|r| r.outcome).collect::<Vec<_>>(), [outcome], "{id}: {receipts:?}");
        }
        assert_eq!(c.sounds(), 3, "the preview, h2 and h3 asked for it; the summary never rings");
        c.rig.desk.tick(&c.rig.hub, now + HOLD_RECHECK * 3);
        assert_eq!(c.shown().len(), 4, "nothing held: the tick does nothing");
    }
    #[test] fn the_hold_keeps_twenty_incidents_and_released_frames_route_afresh() {
        use std::sync::atomic::Ordering::SeqCst;
        let c = cardy(HIDDEN, true); let now = Instant::now();
        c.busy.store(true, SeqCst);
        for i in 0..HOLD_CAPACITY + 2 { c.rig.desk.deliver(&noticed(&format!("k{i}"), &format!("i{i}"), "open", "error", json!({})), &c.rig.hub, now); }
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("k0", Outcome::Dropped), receipt("k1", Outcome::Dropped)], "the oldest incidents beyond 20");
        // Back in front when the user is free: released frames go in-app now.
        c.busy.store(false, SeqCst); c.rig.set(FRONT);
        c.rig.desk.tick(&c.rig.hub, now + HOLD_RECHECK);
        assert_eq!(c.rig.notices().len(), HOLD_CAPACITY);
        assert!(c.shown().is_empty());
        let summary = summary(&[noticed("a", "x", "closed", "info", json!({}))]);
        assert_eq!(summary, None, "only a closed incident: no summary");
    }
    #[test] fn a_frame_shown_between_two_looks_supersedes_the_held_one() {
        use std::sync::atomic::Ordering::SeqCst;
        let c = cardy(HIDDEN, true); let now = Instant::now();
        c.busy.store(true, SeqCst);
        c.rig.desk.deliver(&noticed("g1", "outage-1", "open", "error", json!({})), &c.rig.hub, now);
        // Free before the next look: the newer frame shows at once, the held one is dropped.
        c.busy.store(false, SeqCst);
        assert_eq!(c.rig.desk.deliver(&noticed("g2", "outage-1", "resolved", "info", json!({"sticky":false})), &c.rig.hub, now), Handled::Card);
        c.rig.desk.tick(&c.rig.hub, now + HOLD_RECHECK);
        let ids: Vec<String> = c.shown().iter().map(|s| s["delivery_id"].as_str().unwrap().to_owned()).collect();
        assert_eq!(ids, ["g2"], "the stale open frame never shows after its recovery");
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("g1", Outcome::Dropped), receipt("g2", Outcome::DeliveredWindows)]);
    }
    #[test] fn the_crash_card_shows_only_behind_the_window_and_waits_out_a_busy_user() {
        use std::sync::atomic::Ordering::SeqCst;
        let c = cardy(FRONT, true); let now = Instant::now();
        c.rig.desk.service_stopped(now);
        assert!(c.shown().is_empty(), "in front, the page's crash banner says it");
        c.rig.set(HIDDEN);
        c.rig.desk.service_stopped(now);
        let card = c.shown()[0].clone();
        assert_eq!((card["kind"].as_str(), card["incident"].as_str(), card["severity"].as_str(), card["sticky"].as_bool(), card["shell"].as_bool(), card["action"].as_str()),
            (Some("service_stopped"), Some("service_stopped"), Some("error"), Some(true), Some(true), Some("view")));
        assert_eq!((card["title_id"].as_str(), card["detail_id"].as_str()), (Some("notice.service_stopped.title"), Some("notice.service_stopped.detail")));
        assert!(c.rig.desk.receipts(now).is_empty(), "the shell's own card is never receipted");
        assert_eq!(c.sounds(), 0);
        // Busy: held, also across the relaunch's new session, and shown once the user is free.
        c.busy.store(true, SeqCst);
        c.rig.desk.service_stopped(now);
        c.rig.desk.session();
        c.busy.store(false, SeqCst);
        c.rig.desk.tick(&c.rig.hub, now + HOLD_RECHECK);
        assert_eq!(c.shown().len(), 2);
        assert_eq!(c.shown()[1]["kind"], "service_stopped");
    }
    #[test] fn a_new_session_resets_the_cards_and_ignores_late_results() {
        use std::sync::atomic::Ordering::SeqCst;
        let c = cardy(HIDDEN, true); let now = Instant::now();
        *c.cards.result.lock().unwrap() = None;
        c.rig.desk.deliver(&noticed("n1", "outage-1", "open", "error", json!({})), &c.rig.hub, now);
        c.busy.store(true, SeqCst);
        c.rig.desk.deliver(&noticed("n2", "fallback-1", "open", "warn", json!({"kind":"fallback","group":"tier_fallback"})), &c.rig.hub, now);
        c.rig.desk.session();
        assert_eq!(*c.cards.resets.lock().unwrap(), 1, "the old sidecar's cards leave the card window");
        // The old card is confirmed after the new epoch began: no receipt reaches the new sidecar.
        let done = c.cards.parked.lock().unwrap().pop().unwrap();
        done(CardResult::Shown);
        assert!(c.rig.desk.receipts(now).is_empty());
        // The held frame of the old sidecar is forgotten.
        c.busy.store(false, SeqCst);
        c.rig.desk.tick(&c.rig.hub, now + HOLD_RECHECK);
        assert_eq!(c.shown().len(), 1);
        // The same id in the new session is a new delivery.
        *c.cards.result.lock().unwrap() = Some(CardResult::Shown);
        assert_eq!(c.rig.desk.deliver(&noticed("n2", "fallback-1", "open", "warn", json!({"kind":"fallback","group":"tier_fallback"})), &c.rig.hub, now), Handled::Card);
        assert_eq!(c.rig.desk.receipts(now), vec![receipt("n2", Outcome::DeliveredWindows)]);
    }
}
