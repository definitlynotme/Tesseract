//! Single lock serializes replay, responses and live events onto one Channel.
//!
//! T-P2-31b: the hub caches every v1 page topic (PROTOCOL §11) for the page: state topics as their
//! latest complete DTO, `bastion.sources`/`config.items` and the keyed `nodes.list`/
//! `subscriptions.list` as list slots (one per `(topic,key)`) that apply patches only on
//! `rev == base_rev`, assemble paged snapshots (pulling later pages with `snapshot_req{cursor}`)
//! and request a replacement snapshot on a mismatch or `snapshot_required`. The shell is the
//! subscriber; the page only ever sees complete, assembled snapshots and in-order patches.
//!
//! T-P4-C3: every page connection starts its replay with `host:caps` (the invokes the shell
//! registers and the frames it generates) and ends it with `rt:replayed{epoch}`; a new epoch ends
//! its subscription seed with `rt:replayed` too (all expected seed slots arrived, or
//! [`SEED_GRACE`]). `rt_resync{topic,key?}` re-delivers one cached complete snapshot, or asks the
//! sidecar for a replacement when the cache is not current (at most once per slot per second).
//! H commands (none in v1) pass only with both keys (`hgate.rs`).
//!
//! T-P3-H2 (31b-5): a slot whose cache went stale is announced to the page with the shell's own
//! `rt:stale{topic,key,reason}`: `resync` when the shell starts fetching a replacement after a
//! mismatch, `exhausted` when its [`RESYNC_ATTEMPTS`] attempts ran out (the slot then stays stale
//! until a fresh snapshot or the page's `rt_resync`). No timed retry, no forged sidecar gap.
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::time::{Duration, Instant};
use tauri::ipc::Channel;
use tauri::Emitter;
use crate::hgate;

/// P1 topics: required in every hello (contract check) and always subscribed.
pub const TOPICS: [&str; 8] = ["service.state", "route.published", "tiers.summary", "traffic.tick",
    "traffic.history", "core.info", "logs.batch", "jobs"];
/// v1 page topics beyond P1 (PROTOCOL §11), subscribed when `hello.caps.topics` declares them.
/// `ui.language` is consumed by the shell (`host:language`); the shell-only topics never reach
/// the page.
pub const V1_PAGE_TOPICS: [&str; 17] = ["service.collect", "tier.transition", "exit.identity",
    "protocols.mix", "network.mode", "killswitch.state", "bastion.pool", "bastion.sources",
    "nodes.list", "subscriptions.list", "gateway.current", "config.changed", "config.items",
    "logs.feed", "alerts.history", "update.state", "geo.status"];
/// Event-only topics: edges and gaps, no snapshot.
const EVENT_TOPICS: [&str; 2] = ["tier.transition", "config.changed"];
/// ID-list topics (snapshot / patch / gap); the keyed ones carry `key` = a layer.
pub const LIST_TOPICS: [&str; 4] = ["bastion.sources", "config.items", "nodes.list", "subscriptions.list"];
pub const KEYED_TOPICS: [&str; 2] = ["nodes.list", "subscriptions.list"];
pub const LIST_KEYS: [&str; 4] = ["vanguard", "sentinel", "bastion", "gateway"];
/// PROTOCOL §10 「页面运行时命令（23）」 and 「页面查询（8）」 (host.* are host.rs's).
pub const PAGE_COMMANDS: [&str; 23] = ["alerts.test", "app.update.check",
    "app.update.install", "exit.verify", "gateway.select", "geo.update", "import.apply",
    "import.preview", "logs.exportBundle", "logs.exportFiltered", "logs.setMihomoLevel", "mode.set",
    "nodes.cancelDiagnose", "nodes.delete", "nodes.diagnose", "nodes.reveal", "nodes.update",
    "service.restart", "service.start", "service.stop", "settings.apply", "settings.validate",
    "subscriptions.refresh"];
pub const PAGE_QUERIES: [&str; 8] = ["app.info", "autostart.state",
    "config.snapshot", "geo.status", "import.specs", "logs.query", "logs.tail", "nodes.detail"];
/// A replacement-snapshot request that fails this often in a row stops (the slot stays stale).
const RESYNC_ATTEMPTS: u32 = 3;
/// T-P4-C3 `host:caps.invokes`: the page invokes this shell registers (`dev_quit` is a debug-only
/// harness hook, not a page capability). main.rs registers exactly these (a unit test checks it).
/// 11-05: `host_window` (the page's window buttons; the window has no system caption).
pub const HOST_INVOKES: [&str; 10] = ["rt", "rt_connect", "rt_resync", "host_notify_ready", "host_notify_rendered",
    "host_confirm_quit", "host_set_page", "host_force_restart", "host_ready", "host_window"];
/// T-P4-C3 `host:caps.events`: the shell → page frames the shell itself generates.
/// 11-05: `host:window{maximized}` (which glyph the page's maximize button shows).
pub const HOST_EVENTS: [&str; 12] = ["host:caps", "host:confirmQuit", "host:banner", "host:notify", "host:navigate",
    "host:visibility", "host:language", "host:window", "rt:reset", "rt:log-gap", "rt:replayed", "rt:stale"];
/// `rt_resync`: at most one request per `(topic,key)` within this interval (else `rate_limited`).
pub const RESYNC_INTERVAL: Duration = Duration::from_secs(1);
/// A new epoch's subscription seed ends with `rt:replayed` once every expected slot delivered its
/// first frame, or after this long (a topic without an initial DTO keeps its barrier closed).
/// The sidecar sends one seed frame per publication tick: the real sidecar's 29 v1 slots took
/// 7.1 s on the development machine (T-P4-C3 report), hence the margin.
pub const SEED_GRACE: Duration = Duration::from_secs(15);
/// Bytes the writer may still add to a frame `Hub::request` built (the `epoch` stamp, the
/// newline); a request whose frame plus this margin exceeds `MAX_FRAME` is refused at once with
/// `frame_too_large` (PROTOCOL §13 承载), never handed to the pipe writer.
pub const FRAME_ENVELOPE_MARGIN: usize = 256;
/// Whether `frame`, serialized, fits one NDJSON frame with the envelope margin to spare.
pub fn frame_fits(frame: &Value) -> bool {
    serde_json::to_vec(frame).map(|bytes| bytes.len().saturating_add(FRAME_ENVELOPE_MARGIN) <= bridge_host::MAX_FRAME)
        .unwrap_or(false)
}

pub fn page_topic(topic: &str) -> bool { TOPICS.contains(&topic) || V1_PAGE_TOPICS.contains(&topic) }
/// State topics the tray reads (D20, `tray.rs` `Status::observe`). The hub hands them over only
/// once its cache accepted them (`Hub::take_facts`), so the tray never adopts an older rev the
/// cache refused, and replacement snapshots from `rt_resync` reach it too.
pub const FACT_TOPICS: [&str; 3] = ["service.state", "tiers.summary", "killswitch.state"];
/// Page topics with a complete baseline: what a subscription seed delivers and `rt_resync` can
/// deliver again (not the event-only topics, `jobs` or `logs.batch`).
pub fn resyncable(topic: &str) -> bool {
    page_topic(topic) && topic != "jobs" && topic != "logs.batch" && !EVENT_TOPICS.contains(&topic)
}
/// The `host:caps` frame of every page connection (T-P4-C3).
pub fn host_caps(epoch: &str) -> Value {
    json!({"t":"host:caps","epoch":epoch,"invokes":HOST_INVOKES,"events":HOST_EVENTS})
}
/// Seed slots of a new epoch: every resyncable page topic `hello.caps.topics` declares (keyed
/// list topics once per layer).
fn seed_slots(hello: &Value) -> BTreeSet<String> {
    let declared = |t: &str| hello["caps"]["topics"].as_array().is_some_and(|v| v.iter().any(|x| x == t));
    let mut slots = BTreeSet::new();
    for topic in TOPICS.iter().chain(V1_PAGE_TOPICS.iter()).copied().filter(|t| resyncable(t) && declared(t)) {
        if KEYED_TOPICS.contains(&topic) { for key in LIST_KEYS { slots.insert(slot_name(topic, Some(key))); } }
        else { slots.insert(topic.to_owned()); }
    }
    slots
}
/// A new epoch's subscription seed still being delivered (`rt:replayed` follows it).
struct Seed { waiting: BTreeSet<String>, started: Instant, deadline: Instant, total: usize }
/// Page requests of the control domain go through the shell's control lane.
pub fn control_command(cmd: &str) -> bool {
    matches!(cmd, "service.stop" | "cancel" | "nodes.cancelDiagnose")
}
fn slot_name(topic: &str, key: Option<&str>) -> String {
    match key { Some(key) => format!("{topic}#{key}"), None => topic.to_owned() }
}
fn frame_slot(frame: &Value) -> String { slot_name(frame["topic"].as_str().unwrap_or(""), frame["key"].as_str()) }
/// `(topic, key)` of a slot name.
fn slot_parts(slot: &str) -> (&str, Option<&str>) {
    match slot.split_once('#') { Some((topic, key)) => (topic, Some(key)), None => (slot, None) }
}
/// T-P3-H2 `rt:stale` reasons: the shell started fetching a replacement snapshot after a
/// mismatch, or its bounded attempts ran out.
pub const STALE_RESYNC: &str = "resync";
pub const STALE_EXHAUSTED: &str = "exhausted";

// Only validation metadata is staged; retained job/log payloads are never cloned.
struct EventCursor {
    revisions: HashMap<String,u64>, edge_seq: HashMap<String,u64>,
    jobs_seq: u64, jobs_count: usize, jobs_bytes: usize,
}
impl EventCursor {
    fn validate(&mut self, frame: &Value) -> Result<(), &'static str> {
        let topic = frame["topic"].as_str().ok_or("missing_field")?;
        let kind = frame["kind"].as_str().ok_or("missing_field")?;
        let rev = frame["rev"].as_u64().filter(|r| *r > 0).ok_or("invalid_type")?;
        if !page_topic(topic) || !frame["data"].is_object() { return Err("invalid_enum"); }
        match frame.get("key") {
            Some(key) if KEYED_TOPICS.contains(&topic) => {
                if !key.as_str().is_some_and(|k| LIST_KEYS.contains(&k)) { return Err("invalid_enum"); }
            },
            Some(_) => return Err("invalid_enum"),
            None if KEYED_TOPICS.contains(&topic) => return Err("missing_field"),
            None => (),
        }
        let kinds: &[&str] = if topic == "jobs" { &["edge"] } else if topic == "logs.batch" { &["edge", "gap"] }
            else if topic.starts_with("traffic.") { &["snapshot"] } else if EVENT_TOPICS.contains(&topic) { &["edge", "gap"] }
            else if LIST_TOPICS.contains(&topic) { &["snapshot", "patch", "gap"] } else { &["snapshot", "edge", "gap"] };
        if !kinds.contains(&kind) { return Err("invalid_enum"); }
        let list = LIST_TOPICS.contains(&topic);
        if kind == "patch" {
            frame["base_rev"].as_u64().ok_or("missing_field")?;
            if !frame["data"]["changed"].is_array() || !frame["data"]["removed"].is_array() { return Err("invalid_type"); }
        }
        if kind == "snapshot" && list && !frame["data"]["items"].is_array() { return Err("invalid_type"); }
        // List slots are not rebuild material: a stale or out-of-order list frame only costs a
        // replacement snapshot (absorb_list), never the whole session.
        if !list && rev <= *self.revisions.get(topic).unwrap_or(&0) { return Err("sequence_gap"); }
        if kind != "snapshot" && kind != "patch" && frame["seq"].as_u64().filter(|s| *s > 0).is_none() { return Err("invalid_type"); }
        if kind != "snapshot" && kind != "patch" && topic != "jobs" {
            let seq=frame["seq"].as_u64().unwrap();
            let previous=*self.edge_seq.get(topic).unwrap_or(&0);
            if kind == "gap" {
                let data=&frame["data"];
                let from=data["from_seq"].as_u64().filter(|v|*v>0).ok_or("invalid_type")?;
                let to=data["to_seq"].as_u64().ok_or("invalid_type")?;
                if from!=previous+1 || to<from || seq!=to.checked_add(1).ok_or("invalid_type")?
                    || data["dropped"].as_u64()!=Some(to-from+1) { return Err("sequence_gap"); }
            } else if seq!=previous+1 { return Err("sequence_gap"); }
            self.edge_seq.insert(topic.into(),seq);
        }
        if topic == "jobs" {
            let seq = frame["seq"].as_u64().unwrap();
            if seq != self.jobs_seq + 1 { return Err("sequence_gap"); }
            let bytes = serde_json::to_vec(frame).map_err(|_|"invalid_json")?.len();
            if self.jobs_count >= 65536 || self.jobs_bytes + bytes > 32*1024*1024 { return Err("job_capacity"); }
            self.jobs_seq = seq; self.jobs_count += 1; self.jobs_bytes += bytes;
        }
        if !list { self.revisions.insert(topic.into(), rev); }
        Ok(())
    }
}
pub fn rebuild_required(code: &str) -> bool {
    matches!(code, "sequence_gap" | "job_capacity")
}

#[derive(Clone, Debug, Serialize)]
pub struct Delivery { pub order: u64, pub frame: Value }

/// A paged snapshot being assembled (PROTOCOL §11: buffer, then install atomically).
struct Assembly { snapshot_id: String, index: u64, rev: u64, frame: Value, items: Vec<Value> }

#[derive(Default)]
pub struct Hub {
    pub epoch: String,
    pub hello: Value,
    pub ready: bool,
    channel: Option<Channel<Delivery>>,
    pub app: Option<tauri::AppHandle>,
    order: u64,
    snapshots: BTreeMap<String, Value>,
    jobs: Vec<Value>,
    jobs_bytes: usize,
    logs: VecDeque<Value>,
    logs_bytes: usize,
    logs_evicted: u64,
    records_evicted: u64,
    record_first: Option<u64>,
    record_last: Option<u64>,
    jobs_seq: u64,
    tick_seq: u64,
    edge_seq: HashMap<String, u64>,
    revisions: HashMap<String, u64>,
    // A page may cancel only an operation accepted for its exact owner.
    pending: HashMap<u64, (String, String, bool)>,
    authorized: HashMap<String, String>,
    pub next_id: u64,
    // P3-04 `host:banner` frames by kind. They describe the shell's view of the sidecar, so a
    // new epoch keeps them; a reconnecting page gets every active one after the replay.
    banners: BTreeMap<String, Value>,
    // T-P3-05 latest `host:language` / `host:visibility` (11-05: and `host:window`) by key: replayed to every reconnecting
    // page (re-stamped with the current epoch) so it never starts from a stale language or state.
    sticky: BTreeMap<String, Value>,
    // Incremented by every `connect`; an in-app notice is confirmable only by the same connection.
    page_generation: u64,
    // T-P2-31b list slots: installed snapshots (complete DTO, rev = cache rev), page assemblies,
    // slots needing a replacement snapshot / next page, and the shell's own snapshot_req ids.
    lists: BTreeMap<String, Value>,
    assembly: HashMap<String, Assembly>,
    wanted: BTreeMap<String, Option<String>>,
    requested: HashMap<u64, String>,
    attempts: HashMap<String, u32>,
    // In-flight request ids that ask for a replacement snapshot (no cursor), and ids a later
    // need superseded: their late reply is absorbed silently and never counts as a failure.
    replacing: BTreeSet<u64>,
    superseded: BTreeSet<u64>,
    // T-P4-C3: the new epoch's subscription seed (None once `rt:replayed` was published), the
    // last page `rt_resync` per slot (rate limit), and how the last seed ended (for shell.log).
    seed: Option<Seed>,
    resynced: HashMap<String, Instant>,
    seed_outcome: Option<Vec<(&'static str, String)>>,
    // T-P4-C3 fix round 1: [`FACT_TOPICS`] frames the hub accepted into its cache (stream or
    // replacement snapshot) since the last `take_facts`; the tray reads only these.
    facts: Vec<Value>,
    // T-P3-H2 (31b-5): slots whose cache is stale and the reason the page last heard
    // ([`STALE_RESYNC`] / [`STALE_EXHAUSTED`]); a slot the page itself asked to resync is entered
    // silently as `resync`. Replayed to a reconnecting page; a fresh snapshot or a new epoch
    // clears it. `stale_log` keeps the published notices for shell.log (`take_stale_log`).
    stale: BTreeMap<String, &'static str>,
    stale_log: Vec<Vec<(&'static str, String)>>,
    // Bastion trace (CLAUDE_CHANGES §8r, diagnostic, temporary): shell.log fields (`rt_pool`) for
    // each bastion.pool frame the hub received, refused or forwarded, each replacement request
    // and reply, and each page resync of it; capped per process (`take_pool_log`).
    pool_log: Vec<Vec<(&'static str, String)>>,
    pool_log_lines: u32,
}
/// Bastion trace: at most this many `rt_pool` lines per process.
const POOL_TRACE_MAX: u32 = 600;
impl Hub {
    /// Bastion trace (§8r): one `rt_pool` entry; numbers and fixed codes only.
    fn trace_pool(&mut self, fields: Vec<(&'static str, String)>) {
        if self.pool_log_lines >= POOL_TRACE_MAX { return; }
        self.pool_log_lines += 1;
        self.pool_log.push(fields);
    }
    /// Bastion trace (§8r): a bastion.pool event the hub took in (`fwd` 1 when it reached the
    /// page) or refused (`err`).
    fn trace_pool_frame(&mut self, frame: &Value, fwd: Option<bool>, err: Option<&'static str>) {
        if frame["topic"] != "bastion.pool" { return; }
        let num = |v: &Value| v.as_u64().map_or_else(|| "-".to_owned(), |n| n.to_string());
        self.trace_pool(vec![("via", "stream".to_owned()), ("kind", frame["kind"].as_str().unwrap_or("-").to_owned()),
            ("rev", num(&frame["rev"])), ("seq", num(&frame["seq"])), ("alive", num(&frame["data"]["runtime_alive"])),
            ("fwd", fwd.map_or("-", |f| if f { "1" } else { "0" }).to_owned()), ("err", err.unwrap_or("-").to_owned())]);
    }
    /// shell.log fields (`rt_pool`) of the Bastion trace since the last call.
    pub fn take_pool_log(&mut self) -> Vec<Vec<(&'static str, String)>> { std::mem::take(&mut self.pool_log) }
    /// Publishes a `host:*` frame and keeps the latest one per `key` for page replay. A value
    /// equal to the kept one (epoch aside) is not published again: the page already has it, and a
    /// reconnecting page gets it from the replay. Returns whether it was published.
    pub fn host_sticky(&mut self, key: &str, mut frame: Value) -> bool {
        frame["epoch"] = json!(self.epoch);
        if self.sticky.get(key).is_some_and(|kept| { let mut kept = kept.clone(); kept["epoch"] = json!(self.epoch); kept == frame }) { return false; }
        self.sticky.insert(key.to_owned(), frame.clone());
        self.publish(frame);
        true
    }
    fn sticky_frames(&self) -> Vec<Value> {
        self.sticky.values().map(|f| { let mut f = f.clone(); f["epoch"] = json!(self.epoch); f }).collect()
    }
    pub fn page_generation(&self) -> u64 { self.page_generation }
    /// A batch whose items were all shell-only events (notify.rs): only its tick is consumed.
    pub fn shell_only_batch(&mut self, frame: &Value) -> Result<(), &'static str> {
        if frame["epoch"] != self.epoch { return Err("epoch_mismatch"); }
        if frame["produced_at_ms"].as_u64().is_none() || frame["sent_at_ms"].as_u64().is_none() { return Err("missing_field"); }
        let tick = frame["tick_seq"].as_u64().filter(|t| *t > 0).ok_or("invalid_type")?;
        if tick <= self.tick_seq { return Err("sequence_gap"); }
        self.tick_seq = tick;
        Ok(())
    }
    /// Publishes a shell → page `host:*` frame stamped with the current epoch.
    pub fn host(&mut self, mut frame: Value) {
        frame["epoch"] = json!(self.epoch);
        self.publish(frame);
    }
    /// Publishes a shell → page `host:banner` and remembers it for replay while active.
    pub fn banner(&mut self, mut frame: Value) {
        frame["epoch"] = json!(self.epoch);
        if let Some(kind) = frame["kind"].as_str() { self.banners.insert(kind.to_owned(), frame.clone()); }
        self.publish(frame);
    }
    /// A page Channel is attached (a failed send drops it).
    pub fn page_connected(&self) -> bool { self.channel.is_some() }
    fn active_banners(&self) -> Vec<Value> { self.banners.values().filter(|b| b["active"] == true).cloned().collect() }
    fn delivery(&mut self, frame: Value) -> Delivery {
        self.order += 1;
        Delivery { order: self.order, frame }
    }
    pub fn publish(&mut self, frame: Value) {
        let reset = (frame["t"] == "rt:reset").then(||frame.clone());
        let d = self.delivery(frame);
        if let Some(channel) = &self.channel {
            if channel.send(d).is_err() { self.channel = None; }
        }
        if let (Some(app),Some(reset))=(&self.app,reset) { let _=app.emit("rt:reset",reset); }
    }
    pub fn reset(&mut self, hello: Value) {
        self.epoch = hello["epoch"].as_str().unwrap_or_default().into();
        self.hello = hello;
        self.ready = true;
        self.snapshots.clear(); self.jobs.clear(); self.logs.clear();
        self.jobs_bytes=0; self.logs_bytes=0; self.logs_evicted=0; self.records_evicted=0;
        self.record_first=None; self.record_last=None; self.tick_seq=0;
        self.revisions.clear(); self.edge_seq.clear(); self.jobs_seq = 0; self.pending.clear(); self.authorized.clear();
        self.lists.clear(); self.assembly.clear(); self.wanted.clear(); self.requested.clear(); self.attempts.clear();
        self.replacing.clear(); self.superseded.clear(); self.resynced.clear(); self.facts.clear();
        self.stale.clear(); self.stale_log.clear();
        self.next_id = 2; // subscribe is id 1
        self.publish(json!({"t":"rt:reset", "epoch":self.epoch, "reason":"runtime_gone"}));
        self.publish(self.hello.clone());
        // T-P4-C3: the seed that follows `subscribed` is this epoch's replay; rt:replayed ends it.
        let waiting = seed_slots(&self.hello);
        let total = waiting.len();
        let started = Instant::now();
        self.seed = Some(Seed { waiting, started, deadline: started + SEED_GRACE, total });
        self.seed_tick(Instant::now());
    }
    pub fn gone(&mut self) {
        self.ready = false;
        self.pending.clear(); self.authorized.clear();
        self.seed = None; self.facts.clear();
        self.publish(json!({"t":"rt:reset", "epoch":self.epoch, "reason":"runtime_gone"}));
    }
    /// [`FACT_TOPICS`] frames the cache accepted since the last call, in order (the tray's feed).
    pub fn take_facts(&mut self) -> Vec<Value> { std::mem::take(&mut self.facts) }
    fn replayed(&self) -> Value { json!({"t":"rt:replayed","epoch":self.epoch}) }
    /// A seed slot delivered its first frame (or will only be resynced later).
    fn seeded(&mut self, slot: &str) {
        if let Some(seed) = &mut self.seed { seed.waiting.remove(slot); }
    }
    /// Ends the new epoch's seed with `rt:replayed` once every expected slot arrived or the grace
    /// ran out. Called after the frame that completed it was published (worker loop: every pass).
    pub fn seed_tick(&mut self, now: Instant) {
        let Some(seed) = &self.seed else { return };
        if !seed.waiting.is_empty() && now < seed.deadline { return; }
        let seed = self.seed.take().unwrap();
        let reason = if seed.waiting.is_empty() { "all" } else { "deadline" };
        // shell.log tokens: counts and the first missing slot (`#` written as `.`).
        let first = seed.waiting.iter().next().map_or("-".to_owned(), |s| s.replace('#', "."));
        self.seed_outcome = Some(vec![("reason", reason.to_owned()), ("slots", seed.total.to_string()),
            ("missing", seed.waiting.len().to_string()), ("first_missing", first),
            ("ms", now.saturating_duration_since(seed.started).as_millis().to_string())]);
        let frame = self.replayed();
        self.publish(frame);
    }
    /// shell.log fields of the last finished seed, once.
    pub fn take_seed_outcome(&mut self) -> Option<Vec<(&'static str, String)>> { self.seed_outcome.take() }
    fn replay(&self) -> Vec<Value> {
        let mut frames = vec![json!({"t":"rt:reset", "epoch":self.epoch, "reason":"reconnect"}), host_caps(&self.epoch), self.hello.clone()];
        if !self.ready {
            let mut gone = vec![json!({"t":"rt:reset","epoch":self.epoch,"reason":"runtime_gone"}), host_caps(&self.epoch)];
            gone.extend(self.active_banners()); gone.extend(self.sticky_frames()); gone.push(self.replayed()); return gone;
        }
        frames.extend(self.snapshots.values().cloned());
        frames.extend(self.lists.values().cloned());
        // T-P3-H2: a slot still stale is announced again (its cache is not in the replay).
        frames.extend(self.stale.iter().map(|(slot, reason)| self.stale_frame(slot, reason)));
        frames.extend(self.jobs.iter().cloned());
        if let Some(first) = self.logs.front() {
            let seq = first["seq"].as_u64().unwrap_or(1);
            if seq > 1 { frames.push(json!({"t":"rt:log-gap","epoch":self.epoch,
                "data":{"from_seq":1,"to_seq":seq-1,"dropped":self.logs_evicted,
                    "record_from_seq":self.record_first,"record_to_seq":self.record_last,"record_dropped":self.records_evicted}})); }
        } else if self.logs_evicted>0 {
            frames.push(json!({"t":"rt:log-gap","epoch":self.epoch,
                "data":{"from_seq":1,"to_seq":self.edge_seq.get("logs.batch"),"dropped":self.logs_evicted,
                    "record_from_seq":self.record_first,"record_to_seq":self.record_last,"record_dropped":self.records_evicted}}));
        }
        frames.extend(self.logs.iter().cloned());
        frames.extend(self.active_banners());
        frames.extend(self.sticky_frames());
        // While the new epoch's seed is still arriving, its end publishes rt:replayed instead.
        if self.seed.is_none() { frames.push(self.replayed()); }
        frames
    }
    pub fn connect(&mut self, channel: Channel<Delivery>) {
        self.channel = Some(channel);
        self.page_generation += 1;
        for frame in self.replay() { self.publish(frame); }
    }
    pub fn request(&mut self, req_id: &str, cmd: &str, args: Value, owner: Option<String>) -> Result<Value, String> {
        self.request_gated(req_id, cmd, args, owner, hgate::H_COMMANDS, &hgate::build_features())
    }
    /// [`Hub::request`] with an explicit H command table and build key (unit tests).
    fn request_gated(&mut self, req_id: &str, cmd: &str, args: Value, owner: Option<String>,
        h_table: &[(&str, &str)], h_build: &[&str]) -> Result<Value, String> {
        if !self.ready { return Err("runtime_unavailable".into()); }
        if req_id.is_empty() || req_id.len() > 128 || !args.is_object()
            || owner.as_ref().is_some_and(|s| s.is_empty() || s.len() > 128) { return Err("invalid_type".into()); }
        let owner = owner.unwrap_or_default();
        let keys: Vec<_> = args.as_object().unwrap().keys().map(String::as_str).collect();
        // P1 commands keep their exact argument checks; every other v1 page command/query is
        // name-whitelisted here and shape-checked by the sidecar (PROTOCOL §10: a malformed
        // request with a usable id is answered with a structured error, never executed).
        match cmd {
            "service.start" | "service.restart" => if !keys.is_empty() { return Err("not_allowed".into()); },
            "service.stop" => if !(keys.is_empty() || (keys == ["expected_generation"] && args["expected_generation"].as_u64().is_some())) { return Err("not_allowed".into()); },
            "cancel" => if !(keys == ["op"] && args["op"].as_str().is_some_and(|op| self.authorized.get(op) == Some(&owner))) { return Err("not_allowed".into()); },
            _ if PAGE_COMMANDS.contains(&cmd) || PAGE_QUERIES.contains(&cmd) => (),
            // T-P4-C3: an H command passes only with both keys (hello.caps.features and the
            // build's h_features); v1 has none.
            _ if hgate::allowed(cmd, h_table, h_build, &self.hello) == Some(true) => (),
            _ => return Err("not_allowed".into()),
        }
        if cmd != "cancel" && !self.hello["caps"]["commands"].as_array()
            .is_some_and(|v| v.iter().any(|c| c == cmd)) { return Err("not_allowed".into()); }
        let control=control_command(cmd);
        let count=self.pending.values().filter(|p|p.2==control).count();
        if count>=if control {32}else{64} { return Err("job_capacity".into()); }
        let id = self.next_id;
        let frame = if cmd == "cancel" { json!({"t":"cancel","id":id,"op":args["op"]}) }
            else { json!({"t":"req","id":id,"cmd":cmd,"args":args,"from":"page","req_id":req_id,"owner":owner}) };
        // T-P2-31b fix round 1 (M2): page arguments are unbounded (import text, settings); a frame
        // the pipe would refuse must never reach the writer, where it would fail the session.
        if !frame_fits(&frame) { return Err("frame_too_large".into()); }
        self.next_id += 1;
        self.pending.insert(id, (req_id.into(), owner, control));
        Ok(frame)
    }
    /// Writer backstop: a page frame that turned out too large for the pipe is dropped and its
    /// request answered with a structured `frame_too_large` error (the session goes on).
    pub fn reject_oversized(&mut self, id: u64) -> bool {
        let Some((req_id, owner, _)) = self.pending.remove(&id) else { return false };
        self.publish(json!({"t":"error","epoch":self.epoch,"id":id,"req_id":req_id,"owner":owner,
            "error":{"code":"frame_too_large","message_key":"errors.bridge.frame_too_large","params":{}}}));
        true
    }
    pub fn reject_queued(&mut self, id: u64, req_id: &str) {
        self.pending.remove(&id);
        self.publish(json!({"t":"error","epoch":self.epoch,"id":id,"req_id":req_id,
            "error":{"code":"job_capacity","message_key":"errors.bridge.job_capacity","params":{}}}));
    }
    /// One validated page-topic event: updates the caches and returns what the page receives
    /// (`None`: nothing yet, e.g. a partial page group or a patch that needs a resync).
    fn absorb(&mut self, frame: &Value) -> Option<Value> {
        // Called only after the entire incoming frame has passed validation.
        let topic=frame["topic"].as_str().unwrap();
        let kind=frame["kind"].as_str().unwrap();
        let rev=frame["rev"].as_u64().unwrap();
        if LIST_TOPICS.contains(&topic) { return self.absorb_list(frame); }
        self.seeded(topic);
        if kind != "snapshot" && topic != "jobs" {
            self.edge_seq.insert(topic.into(),frame["seq"].as_u64().unwrap());
        }
        if topic == "jobs" {
            let seq = frame["seq"].as_u64().unwrap();
            let bytes=serde_json::to_vec(frame).unwrap().len();
            self.jobs_seq = seq;
            self.jobs_bytes+=bytes;
            self.jobs.push(frame.clone());
            if let Some(op) = frame["data"]["op"].as_str() {
                if ["done","failed","cancelled","superseded"].contains(&frame["data"]["state"].as_str().unwrap_or("")) { self.authorized.remove(op); }
            }
        } else if topic == "logs.batch" {
            self.logs_bytes+=serde_json::to_vec(frame).unwrap().len();
            self.logs.push_back(frame.clone());
            while self.logs.len()>256 || self.logs_bytes>1024*1024 {
                if let Some(old)=self.logs.pop_front() {
                    self.logs_bytes-=serde_json::to_vec(&old).unwrap().len(); self.logs_evicted+=1;
                    if let Some(records)=old["data"]["records"].as_array() {
                        for record in records {
                            if let Some(seq)=record["seq"].as_u64() {
                                self.record_first.get_or_insert(seq); self.record_last=Some(seq); self.records_evicted+=1;
                            }
                        }
                    }
                }
            }
        } else if kind != "gap" && !EVENT_TOPICS.contains(&topic) {
            // T-P4-C3: the cache never steps back to an older rev (defensive: the sidecar gives a
            // replacement snapshot a fresh wire rev from the stream's own counter, so a later
            // stream frame is always newer). The tray takes only what the cache took.
            if self.snapshots.get(topic).and_then(|f| f["rev"].as_u64()).map_or(true, |cached| rev >= cached) {
                self.snapshots.insert(topic.into(), frame.clone());
                if FACT_TOPICS.contains(&topic) { self.facts.push(frame.clone()); }
                self.stale.remove(topic);
            }
        } else if kind == "gap" && FACT_TOPICS.contains(&topic) {
            self.facts.push(frame.clone());
        }
        self.revisions.insert(topic.into(), rev);
        Some(frame.clone())
    }
    /// List slots (PROTOCOL §11): install complete snapshots, assemble pages, merge patches only
    /// on `rev == base_rev`; anything else asks the sidecar for a replacement snapshot.
    fn absorb_list(&mut self, frame: &Value) -> Option<Value> {
        let slot = frame_slot(frame);
        let kind = frame["kind"].as_str().unwrap_or("");
        match kind {
            "snapshot" if frame.get("page").is_some() => self.begin_pages(&slot, frame),
            "snapshot" => Some(self.install(&slot, frame.clone())),
            // A page group is being assembled: the sidecar pauses this slot's increments, and a
            // patch it still sent cannot apply to half a table (the installed rev then decides).
            "patch" if self.assembly.contains_key(&slot) => None,
            "patch" => {
                let base = frame["base_rev"].as_u64();
                let merged = self.lists.get(&slot)
                    .filter(|cached| cached["rev"].as_u64() == base && frame["rev"].as_u64() > base)
                    .map(|cached| merge_patch(cached, frame));
                match merged {
                    Some(merged) => { self.lists.insert(slot.clone(), merged); self.revisions.insert(slot, frame["rev"].as_u64().unwrap_or(0)); Some(frame.clone()) },
                    None => { self.lists.remove(&slot); self.want(&slot, None); None },
                }
            },
            _ => { self.lists.remove(&slot); self.want(&slot, None); Some(frame.clone()) },
        }
    }
    fn install(&mut self, slot: &str, mut frame: Value) -> Value {
        if let Some(map) = frame.as_object_mut() { map.remove("page"); }
        frame["kind"] = json!("snapshot");
        self.revisions.insert(slot.to_owned(), frame["rev"].as_u64().unwrap_or(0));
        self.assembly.remove(slot); self.wanted.remove(slot); self.attempts.remove(slot);
        self.stale.remove(slot);
        self.seeded(slot);
        // Requests still in flight for this slot are answered by this snapshot already.
        let stale: Vec<u64> = self.requested.iter().filter(|(_, s)| s.as_str() == slot).map(|(id, _)| *id).collect();
        for id in stale { self.requested.remove(&id); self.replacing.remove(&id); self.superseded.insert(id); }
        self.lists.insert(slot.to_owned(), frame.clone());
        frame
    }
    fn begin_pages(&mut self, slot: &str, frame: &Value) -> Option<Value> {
        let page = &frame["page"];
        if page["index"] != 0 { self.want(slot, None); return None; }
        if page["complete"] == true { return Some(self.install(slot, frame.clone())); }
        let (Some(id), Some(cursor)) = (page["snapshot_id"].as_str(), page["next_cursor"].as_str()) else { self.want(slot, None); return None; };
        let items = frame["data"]["items"].as_array().cloned().unwrap_or_default();
        self.assembly.insert(slot.to_owned(), Assembly { snapshot_id: id.into(), index: 0, rev: frame["rev"].as_u64().unwrap_or(0), frame: frame.clone(), items });
        self.lists.remove(slot);
        self.want(slot, Some(cursor.to_owned()));
        None
    }
    /// The next page of an assembly, or `None` when it does not continue that group.
    fn continue_pages(&mut self, slot: &str, frame: &Value) -> Option<Value> {
        let page = &frame["page"];
        let Some(group) = self.assembly.get_mut(slot) else { return self.begin_pages(slot, frame) };
        if page["snapshot_id"].as_str() != Some(group.snapshot_id.as_str()) || page["index"].as_u64() != Some(group.index + 1)
            || frame["rev"].as_u64() != Some(group.rev) {
            if page["index"] == 0 { return self.begin_pages(slot, frame); }
            self.assembly.remove(slot); self.want(slot, None); return None;
        }
        group.index += 1;
        group.items.extend(frame["data"]["items"].as_array().cloned().unwrap_or_default());
        if page["complete"] == true {
            let group = self.assembly.remove(slot).unwrap();
            let mut assembled = group.frame;
            assembled["data"]["items"] = Value::Array(group.items);
            return Some(self.install(slot, assembled));
        }
        match page["next_cursor"].as_str() {
            Some(cursor) => { let cursor = cursor.to_owned(); self.want(slot, Some(cursor)); },
            None => { self.assembly.remove(slot); self.want(slot, None); },
        }
        None
    }
    fn want(&mut self, slot: &str, cursor: Option<String>) {
        let in_flight: Vec<u64> = self.requested.iter().filter(|(_, s)| s.as_str() == slot).map(|(id, _)| *id).collect();
        // T-P2-31b fix round 1: a replacement already in flight answers a further mismatch too
        // (its snapshot is newer than every patch the pipe delivered before it). Superseding it
        // would count as a failed attempt and could leave the slot stale for the whole epoch.
        // A slot waiting for a replacement is no longer part of the epoch's seed: its snapshot
        // reaches the page later as a live frame.
        if cursor.is_none() { self.seeded(slot); }
        if cursor.is_none() && in_flight.iter().any(|id| self.replacing.contains(id)) { return; }
        for id in in_flight {
            self.requested.remove(&id); self.replacing.remove(&id);
            self.superseded.insert(id);
        }
        self.wanted.insert(slot.to_owned(), cursor);
    }
    /// `snapshot_req` frames the shell must send (replacement snapshots and next pages), each
    /// with a reserved id; a frame the caller cannot queue goes back with `request_unsent`.
    pub fn take_requests(&mut self) -> Vec<Value> {
        let wanted = std::mem::take(&mut self.wanted);
        let mut frames = Vec::new();
        for (slot, cursor) in wanted {
            if cursor.is_none() {
                // T-P3-H2: the first replacement request of a stale episode tells the page
                // (`resync`); a slot out of attempts tells it once (`exhausted`) and stays stale.
                let attempts = self.attempts.entry(slot.clone()).or_insert(0);
                if *attempts >= RESYNC_ATTEMPTS { self.announce_stale(&slot, STALE_EXHAUSTED); continue; }
                *attempts += 1;
                if !self.stale.contains_key(&slot) { self.announce_stale(&slot, STALE_RESYNC); }
                if slot == "bastion.pool" {
                    let n = self.attempts.get(&slot).copied().unwrap_or(0);
                    self.trace_pool(vec![("via", "snapshot_req".to_owned()), ("attempt", n.to_string())]);
                }
            }
            let (topic, key) = match slot.split_once('#') { Some((t, k)) => (t.to_owned(), Some(k.to_owned())), None => (slot.clone(), None) };
            let id = self.next_id; self.next_id += 1;
            let mut frame = json!({"t":"snapshot_req","id":id,"topic":topic});
            if let Some(key) = key { frame["key"] = json!(key); }
            match cursor { Some(cursor) => frame["cursor"] = json!(cursor), None => { self.replacing.insert(id); } }
            self.requested.insert(id, slot);
            frames.push(frame);
        }
        frames
    }
    /// T-P3-H2 `rt:stale{topic,key,reason}` for one slot (`key` null on unkeyed topics).
    fn stale_frame(&self, slot: &str, reason: &str) -> Value {
        let (topic, key) = slot_parts(slot);
        json!({"t":"rt:stale","epoch":self.epoch,"topic":topic,"key":key,"reason":reason})
    }
    /// Records `reason` for `slot` and publishes `rt:stale` unless the page already heard it.
    fn announce_stale(&mut self, slot: &str, reason: &'static str) {
        if self.stale.get(slot) == Some(&reason) { return; }
        self.stale.insert(slot.to_owned(), reason);
        let (topic, key) = slot_parts(slot);
        self.stale_log.push(vec![("topic", topic.to_owned()), ("key", key.unwrap_or("-").to_owned()), ("reason", reason.to_owned())]);
        let frame = self.stale_frame(slot, reason);
        self.publish(frame);
    }
    /// shell.log fields (`rt_stale topic key reason`) of the `rt:stale` notices published since
    /// the last call.
    pub fn take_stale_log(&mut self) -> Vec<Vec<(&'static str, String)>> { std::mem::take(&mut self.stale_log) }
    pub fn request_unsent(&mut self, frame: &Value) {
        if let Some(slot) = frame["id"].as_u64().and_then(|id| self.requested.remove(&id)) {
            if let Some(id) = frame["id"].as_u64() { self.replacing.remove(&id); }
            let cursor = frame["cursor"].as_str().map(str::to_owned);
            if cursor.is_none() { if let Some(n) = self.attempts.get_mut(&slot) { *n = n.saturating_sub(1); } }
            self.wanted.insert(slot, cursor);
        }
    }
    /// Whether `id` is one of the shell's own `snapshot_req` ids.
    pub fn owns_snapshot_request(&self, frame: &Value) -> bool {
        frame["t"] == "snapshot_res" && frame["id"].as_u64().is_some_and(|id| self.requested.contains_key(&id) || self.superseded.contains(&id))
    }
    /// The reply to the shell's own `snapshot_req`: installs (or continues assembling) the slot
    /// and publishes the complete snapshot to the page; an error asks again (bounded).
    pub fn snapshot_res(&mut self, frame: &Value) -> Result<(), &'static str> {
        let result = self.snapshot_reply(frame);
        self.seed_tick(Instant::now());
        result
    }
    fn snapshot_reply(&mut self, frame: &Value) -> Result<(), &'static str> {
        if frame["epoch"] != self.epoch { return Err("epoch_mismatch"); }
        // A superseded request's late reply is absorbed: a newer request owns the slot now.
        if frame["id"].as_u64().is_some_and(|id| self.superseded.remove(&id)) { return Ok(()); }
        if let Some(id) = frame["id"].as_u64() { self.replacing.remove(&id); }
        let slot = frame["id"].as_u64().and_then(|id| self.requested.remove(&id)).ok_or("unknown_request")?;
        if !LIST_TOPICS.contains(&slot.split('#').next().unwrap_or_default()) { return self.state_snapshot_res(slot, frame); }
        let snapshot = &frame["snapshot"];
        if !snapshot.is_object() { self.want(&slot, None); return Ok(()); }
        if snapshot["t"] != "evt" || snapshot["epoch"] != self.epoch || snapshot["kind"] != "snapshot"
            || frame_slot(snapshot) != slot || !snapshot["data"]["items"].is_array() || snapshot["rev"].as_u64().is_none() {
            self.want(&slot, None); return Err("invalid_snapshot");
        }
        let published = if snapshot.get("page").is_some() { self.continue_pages(&slot, snapshot) } else { Some(self.install(&slot, snapshot.clone())) };
        if let Some(out) = published { self.publish(out); }
        Ok(())
    }
    /// T-P4-C3: the reply to a `snapshot_req` for a state topic (asked by `rt_resync` because the
    /// shell's cached snapshot was not current). Only a snapshot at least as new as the latest
    /// rev the stream delivered is cached and published. The sidecar stamps it with a fresh wire
    /// rev from the counter the stream shares (`bridge_publish._submit`), newer than any rev the
    /// stream carried so far. Since T-P2-H2 the topic's still-queued edges and gaps that this
    /// snapshot covers are dropped, never sent (`bridge_publish._drop_covered`): the stream shows a
    /// value of the topic again only when a newer edge comes (a larger rev again). The page
    /// keeps the newest by rev. `revisions` stays the stream's: the
    /// cursor only needs a rising rev, so this is defensive, not a reuse of revs.
    fn state_snapshot_res(&mut self, slot: String, frame: &Value) -> Result<(), &'static str> {
        let pool = slot == "bastion.pool";
        let result = self.state_snapshot_apply(slot, frame);
        if pool {
            // Bastion trace (§8r): what became of a replacement reply (`none`: no snapshot in it)
            let snap = &frame["snapshot"];
            let num = |v: &Value| v.as_u64().map_or_else(|| "-".to_owned(), |n| n.to_string());
            let outcome = match result { Ok(()) if snap.is_object() => "installed", Ok(()) => "none", Err(e) => e };
            self.trace_pool(vec![("via", "snapshot_res".to_owned()), ("outcome", outcome.to_owned()),
                ("rev", num(&snap["rev"])), ("alive", num(&snap["data"]["runtime_alive"])),
                ("err", frame["error"]["code"].as_str().unwrap_or("-").to_owned())]);
        }
        result
    }
    fn state_snapshot_apply(&mut self, slot: String, frame: &Value) -> Result<(), &'static str> {
        let snapshot = &frame["snapshot"];
        if !snapshot.is_object() { self.want(&slot, None); return Ok(()); }
        let rev = snapshot["rev"].as_u64().filter(|r| *r > 0);
        if snapshot["t"] != "evt" || snapshot["epoch"] != self.epoch || snapshot["kind"] != "snapshot" || snapshot["topic"] != slot.as_str()
            || snapshot.get("key").is_some() || !snapshot["data"].is_object() || rev.is_none() {
            self.want(&slot, None); return Err("invalid_snapshot");
        }
        let rev = rev.unwrap();
        if rev < *self.revisions.get(&slot).unwrap_or(&0)
            || self.snapshots.get(&slot).and_then(|f| f["rev"].as_u64()).is_some_and(|cached| cached > rev) {
            // T-P3-H2 fix 1: never leave a stale mark nobody acts on. A cache that is current
            // anyway (the stream caught up) ends the episode; otherwise this counts as a failed
            // attempt and the shell asks again (bounded: the third failure says `exhausted`).
            if self.state_current(&slot) { self.attempts.remove(&slot); self.stale.remove(&slot); } else { self.want(&slot, None); }
            return Err("stale_snapshot");
        }
        self.attempts.remove(&slot);
        if FACT_TOPICS.contains(&slot.as_str()) { self.facts.push(snapshot.clone()); }
        self.stale.remove(&slot);
        self.snapshots.insert(slot, snapshot.clone());
        self.publish(snapshot.clone());
        Ok(())
    }
    /// Whether the cached snapshot of a state topic is the latest the stream delivered (a gap
    /// or an unseen topic makes it not current).
    fn state_current(&self, topic: &str) -> bool {
        self.snapshots.get(topic).and_then(|f| f["rev"].as_u64())
            .is_some_and(|cached| cached >= *self.revisions.get(topic).unwrap_or(&0))
    }
    /// T-P4-C3 `invoke('rt_resync', {topic, key?})` from the page. Re-delivers the shell's cached
    /// complete snapshot of one page topic (keyed list topics by key, paged lists assembled, a
    /// state topic cached from an edge as `kind:"snapshot"` without `seq`), or
    /// asks the sidecar for a replacement when the cache is not current. Errors: `runtime_unavailable`,
    /// `not_allowed` (not a resyncable page topic, or not declared in hello.caps.topics),
    /// `invalid_type` (a key on an unkeyed topic, a missing or unknown key), `rate_limited`
    /// (another request for the same slot within [`RESYNC_INTERVAL`]).
    pub fn resync(&mut self, topic: &str, key: Option<&str>, now: Instant) -> Result<Value, &'static str> {
        if !self.ready { return Err("runtime_unavailable"); }
        if !resyncable(topic) || !self.hello["caps"]["topics"].as_array().is_some_and(|v| v.iter().any(|t| t == topic)) {
            return Err("not_allowed");
        }
        let keyed = KEYED_TOPICS.contains(&topic);
        match key {
            Some(k) if keyed && LIST_KEYS.contains(&k) => (),
            None if !keyed => (),
            _ => return Err("invalid_type"),
        }
        let slot = slot_name(topic, key);
        if slot == "bastion.pool" {
            // Bastion trace (§8r): the page asked for the topic again (after a gap or a mismatch)
            let current = if self.state_current(topic) { "1" } else { "0" };
            self.trace_pool(vec![("via", "page_resync".to_owned()), ("cache_current", current.to_owned())]);
        }
        if self.resynced.get(&slot).is_some_and(|at| now.saturating_duration_since(*at) < RESYNC_INTERVAL) { return Err("rate_limited"); }
        self.resynced.insert(slot.clone(), now);
        let cached = if LIST_TOPICS.contains(&topic) {
            self.lists.get(&slot).filter(|_| !self.assembly.contains_key(&slot)).cloned()
        } else if self.state_current(topic) { self.snapshots.get(topic).cloned().map(as_snapshot) } else { None };
        if let Some(frame) = cached {
            self.publish(frame);
            return Ok(json!({"source":"cache"}));
        }
        // Already being fetched (a replacement or the next page): that snapshot answers this too.
        // T-P3-H2 fix 1: a replacement still queued for a slot out of attempts is never sent
        // (`take_requests` drops it and says `exhausted`), so it does not count as in flight.
        let out_of_attempts = self.attempts.get(&slot).is_some_and(|n| *n >= RESYNC_ATTEMPTS);
        let queued = self.wanted.get(&slot).is_some_and(|cursor| cursor.is_some() || !out_of_attempts);
        if queued || self.assembly.contains_key(&slot) || self.requested.values().any(|s| *s == slot) {
            return Ok(json!({"source":"pending"}));
        }
        // An explicit page request may restart a slot whose bounded retries had stopped. The page
        // asked, so the slot turns stale silently (`resync`); running out again says `exhausted`.
        self.attempts.remove(&slot);
        self.stale.insert(slot.clone(), STALE_RESYNC);
        self.want(&slot, None);
        Ok(json!({"source":"request"}))
    }
    /// One sidecar frame for the page (after its own publish, a completed seed adds rt:replayed).
    pub fn receive(&mut self, frame: Value) -> Result<(), &'static str> {
        let result = self.receive_frame(frame);
        self.seed_tick(Instant::now());
        result
    }
    fn receive_frame(&mut self, mut frame: Value) -> Result<(), &'static str> {
        if !frame.is_object() { return Err("invalid_type"); }
        if frame["epoch"] != self.epoch { return Err("epoch_mismatch"); }
        if frame["produced_at_ms"].as_u64().is_none() || frame["sent_at_ms"].as_u64().is_none() { return Err("missing_field"); }
        let mut cursor=EventCursor { revisions:self.revisions.clone(), edge_seq:self.edge_seq.clone(),
            jobs_seq:self.jobs_seq, jobs_count:self.jobs.len(), jobs_bytes:self.jobs_bytes };
        match frame["t"].as_str().ok_or("invalid_type")? {
            "evt" => {
                if let Err(e) = cursor.validate(&frame) { self.trace_pool_frame(&frame, None, Some(e)); return Err(e); }
                let out = self.absorb(&frame);
                self.trace_pool_frame(&frame, Some(out.is_some()), None);
                match out { Some(out) => frame = out, None => return Ok(()) }
            },
            "batch" => {
                let tick=frame["tick_seq"].as_u64().filter(|t|*t>0).ok_or("invalid_type")?;
                if tick<=self.tick_seq { return Err("sequence_gap"); }
                let items = frame["items"].as_array().filter(|i| !i.is_empty()).ok_or("invalid_type")?;
                for item in items {
                    if item["t"] != "evt" || item["epoch"] != self.epoch { return Err("epoch_mismatch"); }
                    if item["produced_at_ms"].as_u64().is_none() || item["sent_at_ms"].as_u64().is_none() { return Err("missing_field"); }
                    if let Err(e) = cursor.validate(item) { let item = item.clone(); self.trace_pool_frame(&item, None, Some(e)); return Err(e); }
                }
                let items = items.clone();
                let forwarded: Vec<Value> = items.iter().filter_map(|item| {
                    let out = self.absorb(item);
                    self.trace_pool_frame(item, Some(out.is_some()), None);
                    out
                }).collect();
                self.tick_seq=tick;
                if forwarded.is_empty() { return Ok(()); }
                frame["items"] = Value::Array(forwarded);
            },
            "ack" | "res" | "error" => {
                if frame["t"] == "error" && frame.get("id").is_none() && frame["error"]["code"] == "snapshot_required" {
                    // PROTOCOL §11: an abandoned page group / revoked list baseline. The shell is
                    // the subscriber: it asks for a replacement snapshot itself.
                    if let Some(topic) = frame["error"]["params"]["topic"].as_str().filter(|t| LIST_TOPICS.contains(t)) {
                        // T-P3-H2 fix 1: the slot name becomes the page's `rt:stale{topic,key}`, so
                        // `tier` must be one of LIST_KEYS on a keyed topic and absent elsewhere;
                        // anything else is dropped (shell.log `snapshot_required_invalid_slot`).
                        let tier = &frame["error"]["params"]["tier"];
                        let valid = if KEYED_TOPICS.contains(&topic) { tier.as_str().is_some_and(|k| LIST_KEYS.contains(&k)) } else { tier.is_null() };
                        if !valid { return Err("snapshot_required_invalid_slot"); }
                        let slot = slot_name(topic, tier.as_str());
                        self.assembly.remove(&slot); self.lists.remove(&slot); self.want(&slot, None);
                        return Ok(());
                    }
                }
                if let Some(id) = frame["id"].as_u64() {
                    if let Some((req_id, owner, _)) = self.pending.remove(&id) {
                        frame["req_id"] = json!(req_id); frame["owner"] = json!(owner);
                        if frame["t"] == "ack" && frame["accepted"] == true {
                            if let Some(op) = frame["op"].as_str() { self.authorized.insert(op.into(), owner); }
                        }
                    }
                }
            },
            "subscribed" | "pong" | "bye" | "fatal" => (),
            _ => return Err("invalid_enum"),
        }
        self.publish(frame);
        Ok(())
    }
}

/// The `rt_resync` copy of a cached state frame: the cache keeps the latest complete DTO, which
/// may have arrived as an `edge`; the page gets it as what §13 promises, a complete snapshot
/// (`kind:"snapshot"`, no edge `seq`), so it is never projected as a live edge. The cache itself
/// is unchanged.
fn as_snapshot(mut frame: Value) -> Value {
    if frame["kind"] != "snapshot" {
        frame["kind"] = json!("snapshot");
        if let Some(map) = frame.as_object_mut() { map.remove("seq"); }
    }
    frame
}

/// PROTOCOL §4 patch: `changed` rows replace the row with the same id (or are appended),
/// `removed` ids are dropped; the result carries the patch's rev.
fn merge_patch(baseline: &Value, patch: &Value) -> Value {
    let mut merged = baseline.clone();
    let mut items: Vec<Value> = baseline["data"]["items"].as_array().cloned().unwrap_or_default();
    let removed: BTreeSet<&str> = patch["data"]["removed"].as_array().into_iter().flatten().filter_map(Value::as_str).collect();
    items.retain(|row| !row["id"].as_str().is_some_and(|id| removed.contains(id)));
    for row in patch["data"]["changed"].as_array().into_iter().flatten() {
        match items.iter_mut().find(|old| old["id"] == row["id"]) {
            Some(old) => *old = row.clone(),
            None => items.push(row.clone()),
        }
    }
    merged["data"]["items"] = Value::Array(items);
    merged["rev"] = patch["rev"].clone();
    merged["produced_at_ms"] = patch["produced_at_ms"].clone();
    merged["sent_at_ms"] = patch["sent_at_ms"].clone();
    merged
}

#[cfg(test)]
mod tests {
    use super::*;
    fn hub() -> Hub {
        let mut hub = Hub::default();
        hub.reset(json!({"epoch":"one","caps":{"commands":["service.start","service.stop","service.restart"]}})); hub
    }
    fn evt(topic: &str, rev: u64, kind: &str) -> Value {
        json!({"t":"evt","epoch":"one","topic":topic,"rev":rev,"kind":kind,"seq":rev,
            "produced_at_ms":1,"sent_at_ms":1,"data":{"state":"running","op":"op1"}})
    }
    #[test] fn whitelist_and_cancel_owner() {
        let mut h = hub();
        for c in ["app.quit","settings.apply","getattr","subscribe","unknown"] { assert!(h.request("r",c,json!({}),None).is_err()); }
        assert!(h.request("r","service.start",json!({"path":"x"}),None).is_err());
        assert!(h.request("r","service.stop",json!({"expected_generation":-1}),None).is_err());
        assert!(h.request("restart","service.restart",json!({}),None).is_ok());
        assert!(h.request("c","cancel",json!({"op":"op1"}),Some("a".into())).is_err());
        let r = h.request("r","service.start",json!({}),Some("a".into())).unwrap();
        h.receive(json!({"t":"ack","epoch":"one","id":r["id"],"accepted":true,"op":"op1","produced_at_ms":1,"sent_at_ms":1})).unwrap();
        assert!(h.request("c","cancel",json!({"op":"op1"}),Some("b".into())).is_err());
        assert!(h.request("c","cancel",json!({"op":"op1"}),Some("a".into())).is_ok());
        let mut terminal=evt("jobs",1,"edge"); terminal["data"]["state"]=json!("done");
        h.receive(terminal).unwrap();
        assert!(h.request("c","cancel",json!({"op":"op1"}),Some("a".into())).is_err());
    }
    #[test] fn cache_replay_epoch_and_jobs_gap() {
        let mut h = hub();
        h.receive(evt("service.state",1,"snapshot")).unwrap();
        let mut edge=evt("service.state",2,"edge"); edge["seq"]=json!(1);
        h.receive(edge).unwrap();
        h.receive(evt("jobs",1,"edge")).unwrap();
        assert_eq!(h.replay().iter().filter(|f| f["topic"] == "service.state").count(),1);
        assert_eq!(h.replay()[3]["rev"],2);
        assert_eq!(h.receive(evt("jobs",3,"edge")),Err("sequence_gap"));
        h.reset(json!({"epoch":"two"}));
        assert!(h.snapshots.is_empty() && h.jobs.is_empty() && h.authorized.is_empty());
        assert_eq!(h.replay()[0]["t"],"rt:reset");
        assert_eq!(h.receive(evt("service.state",3,"snapshot")),Err("epoch_mismatch"));
    }
    #[test] fn channel_is_ordered_including_replay() {
        let captured = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let output = captured.clone();
        let channel = Channel::new(move |body| {
            if let tauri::ipc::InvokeResponseBody::Json(body) = body {
                output.lock().unwrap().push(serde_json::from_str::<Value>(&body).unwrap());
            } Ok(())
        });
        let mut h = hub(); h.connect(channel);
        h.receive(evt("service.state",1,"snapshot")).unwrap();
        h.receive(evt("jobs",1,"edge")).unwrap();
        h.receive(json!({"t":"ack","epoch":"one","id":7,"accepted":false,"produced_at_ms":1,"sent_at_ms":1})).unwrap();
        h.receive(json!({"t":"res","epoch":"one","id":8,"ok":true,"data":{},"produced_at_ms":1,"sent_at_ms":1})).unwrap();
        let frames = captured.lock().unwrap();
        assert_eq!(frames.len(),8);
        assert!(frames.windows(2).all(|v| v[0]["order"].as_u64().unwrap()+1 == v[1]["order"].as_u64().unwrap()));
        let replay:Vec<_>=frames[..4].iter().map(|f|f["frame"]["t"].as_str().unwrap_or("hello").to_owned()).collect();
        assert_eq!(replay,["rt:reset","host:caps","hello","rt:replayed"]);
        assert_eq!(frames[4]["frame"]["topic"],"service.state");
        assert_eq!(frames[5]["frame"]["topic"],"jobs");
        assert_eq!(frames[6]["frame"]["t"],"ack"); assert_eq!(frames[7]["frame"]["t"],"res");
    }
    #[test] fn edge_gaps_and_bounded_log_replay_are_explicit() {
        let mut h=hub();
        h.receive(evt("core.info",1,"edge")).unwrap();
        assert_eq!(h.receive(evt("core.info",3,"edge")),Err("sequence_gap"));
        let mut gap=evt("core.info",2,"gap");
        gap["seq"]=json!(4); gap["data"]=json!({"from_seq":2,"to_seq":3,"dropped":2});
        h.receive(gap).unwrap();
        for seq in 1..=257 {
            let mut log=evt("logs.batch",seq,"edge");
            log["data"]=json!({"from_seq":seq*10,"records":[{"seq":seq*10}]});
            h.receive(log).unwrap();
        }
        let replay=h.replay();
        let gap=replay.iter().find(|v|v["t"]=="rt:log-gap").unwrap();
        assert_eq!(gap["data"]["dropped"],1);
        assert_eq!(gap["data"]["record_from_seq"],10);
        assert_eq!(gap["data"]["record_dropped"],1);
        let mut big=evt("logs.batch",258,"edge");
        big["data"]=json!({"records":[{"seq":2580,"raw":"x".repeat(1024*1024)}]});
        h.receive(big).unwrap();
        assert!(h.logs.is_empty());
        assert!(h.replay().iter().any(|v|v["t"]=="rt:log-gap" && v["data"]["to_seq"]==258));
        h.gone(); assert_eq!(h.replay().iter().map(|f|f["t"].as_str().unwrap()).collect::<Vec<_>>(),["rt:reset","host:caps","rt:replayed"]);
    }
    #[test] fn host_banners_survive_a_new_epoch_and_replay_only_while_active() {
        let mut h=hub();
        h.banner(json!({"t":"host:banner","kind":"sidecar_crashed","active":true,"state":"restarting"}));
        h.gone();
        let replay=h.replay(); assert_eq!(replay.len(),4); assert_eq!(replay[2]["state"],"restarting");
        h.reset(json!({"epoch":"two"}));
        h.banner(json!({"t":"host:banner","kind":"sidecar_crashed","active":true,"state":"restarted"}));
        h.banner(json!({"t":"host:banner","kind":"sidecar_unresponsive","active":false}));
        let banners:Vec<_>=h.replay().into_iter().filter(|f|f["t"]=="host:banner").collect();
        assert_eq!(banners.len(),1); assert_eq!(banners[0]["state"],"restarted");
        assert!(!h.page_connected());
    }
    #[test] fn sticky_host_frames_replay_with_the_current_epoch() {
        let mut h=hub();
        assert!(h.host_sticky("language",json!({"t":"host:language","language":"zh_CN"})));
        assert!(!h.host_sticky("language",json!({"t":"host:language","language":"zh_CN"})),"hello.lang then the same ui.language snapshot: published once");
        let order=h.order;
        h.reset(json!({"epoch":"one"}));
        assert!(!h.host_sticky("language",json!({"t":"host:language","language":"zh_CN"})),"unchanged across a new epoch as well");
        assert_eq!(h.order,order+3,"only rt:reset, hello and rt:replayed (an empty seed) were published");
        h.host_sticky("visibility",json!({"t":"host:visibility","visible":false}));
        assert!(h.host_sticky("language",json!({"t":"host:language","language":"ja_JP"})));
        let replay=h.replay();
        let sticky:Vec<_>=replay.iter().filter(|f|f["t"].as_str().is_some_and(|t|t=="host:language"||t=="host:visibility")).collect();
        assert_eq!(sticky.len(),2,"the latest per key only");
        assert_eq!(sticky[0]["language"],"ja_JP"); assert_eq!(sticky[1]["visible"],false);
        h.gone(); assert_eq!(h.replay().len(),5,"a gone sidecar still replays the shell's own state (and host:caps, rt:replayed)");
        h.reset(json!({"epoch":"two"}));
        assert!(h.replay().iter().filter(|f|f["t"]=="host:visibility").all(|f|f["epoch"]=="two"));
        assert_eq!(h.page_generation(),0);
        h.connect(Channel::new(|_|Ok(()))); h.connect(Channel::new(|_|Ok(())));
        assert_eq!(h.page_generation(),2);
    }
    #[test] fn shell_only_batch_consumes_the_tick() {
        let mut h=hub();
        let batch=|tick:u64|json!({"t":"batch","epoch":"one","tick_seq":tick,"items":[],"produced_at_ms":1,"sent_at_ms":1});
        h.shell_only_batch(&batch(2)).unwrap();
        assert_eq!(h.shell_only_batch(&batch(2)),Err("sequence_gap"));
        assert_eq!(h.receive(json!({"t":"batch","epoch":"one","tick_seq":1,"items":[evt("service.state",1,"snapshot")],"produced_at_ms":1,"sent_at_ms":1})),Err("sequence_gap"));
        let mut foreign=batch(3); foreign["epoch"]=json!("x"); assert_eq!(h.shell_only_batch(&foreign),Err("epoch_mismatch"));
        h.receive(json!({"t":"batch","epoch":"one","tick_seq":3,"items":[evt("service.state",1,"snapshot")],"produced_at_ms":1,"sent_at_ms":1})).unwrap();
    }
    #[test] fn stop_has_capacity_when_normal_admission_is_full() {
        let mut h=hub();
        for n in 0..64 { h.request(&format!("r{n}"),"service.start",json!({}),None).unwrap(); }
        assert_eq!(h.request("over","service.start",json!({}),None),Err("job_capacity".into()));
        assert!(h.request("stop","service.stop",json!({}),None).is_ok());
    }
    #[test] fn isolated_bad_frame_preserves_live_session_and_delivery_order() {
        let mut h=hub();
        h.receive(evt("service.state",1,"snapshot")).unwrap();
        let order=h.order;
        let mut bad=evt("service.state",2,"snapshot"); bad.as_object_mut().unwrap().remove("sent_at_ms");
        let code=h.receive(bad).unwrap_err();
        assert!(!rebuild_required(code)); assert!(h.ready); assert_eq!(h.order,order);
        h.receive(evt("service.state",2,"snapshot")).unwrap();
        assert_eq!(h.snapshots["service.state"]["rev"],2); assert_eq!(h.order,order+1);
        assert!(rebuild_required(h.receive(evt("jobs",2,"edge")).unwrap_err()));
    }
    #[test] fn invalid_batch_tail_is_atomic_for_all_caches_and_sequences() {
        let mut h=hub(); h.receive(evt("service.state",1,"snapshot")).unwrap();
        let request=h.request("r","service.start",json!({}),Some("owner".into())).unwrap();
        h.receive(json!({"t":"ack","epoch":"one","id":request["id"],"accepted":true,"op":"op1","produced_at_ms":1,"sent_at_ms":1})).unwrap();
        let mut terminal=evt("jobs",1,"edge"); terminal["data"]["state"]=json!("done");
        let good=vec![evt("service.state",2,"snapshot"),terminal,evt("logs.batch",1,"edge")];
        let mut bad=evt("core.info",1,"snapshot"); bad["data"]=json!(null);
        let mut items=good.clone(); items.push(bad);
        let before=h.replay(); let order=h.order;
        assert_eq!(h.receive(json!({"t":"batch","epoch":"one","tick_seq":1,"items":items,"produced_at_ms":1,"sent_at_ms":1})),Err("invalid_enum"));
        assert_eq!(h.replay(),before); assert_eq!(h.order,order); assert_eq!(h.tick_seq,0);
        assert_eq!(h.jobs_bytes,0); assert_eq!(h.logs_bytes,0); assert_eq!(h.jobs_seq,0);
        assert_eq!(h.authorized.get("op1").unwrap(),"owner"); assert!(h.ready);
        h.receive(json!({"t":"batch","epoch":"one","tick_seq":1,"items":good,"produced_at_ms":1,"sent_at_ms":1})).unwrap();
        assert_eq!(h.jobs.len(),1); assert_eq!(h.logs.len(),1); assert_eq!(h.tick_seq,1);
        assert!(!h.authorized.contains_key("op1"));
    }
    // ---- T-P2-31b: v1 page whitelist, topic caches and list slots ----
    fn v1_hub() -> Hub {
        let mut hub = Hub::default();
        let mut commands: Vec<&str> = PAGE_COMMANDS.to_vec(); commands.extend(PAGE_QUERIES);
        commands.extend(["alerts.desktopDelivered", "app.quit", "shell.saveGrant"]);
        hub.reset(json!({"epoch":"one","caps":{"commands":commands}})); hub
    }
    fn list(topic: &str, key: Option<&str>, rev: u64, ids: &[&str]) -> Value {
        let items: Vec<Value> = ids.iter().map(|id| json!({"id":id,"v":0})).collect();
        let mut frame = json!({"t":"evt","epoch":"one","topic":topic,"rev":rev,"kind":"snapshot","produced_at_ms":1,"sent_at_ms":1,"data":{"items":items}});
        if let Some(key) = key { frame["key"] = json!(key); }
        frame
    }
    fn patch(topic: &str, key: Option<&str>, base: u64, rev: u64, changed: Value, removed: Value) -> Value {
        let mut frame = json!({"t":"evt","epoch":"one","topic":topic,"rev":rev,"base_rev":base,"kind":"patch","produced_at_ms":2,"sent_at_ms":2,
            "data":{"changed":changed,"removed":removed}});
        if let Some(key) = key { frame["key"] = json!(key); }
        frame
    }
    fn ids(frame: &Value) -> Vec<String> {
        frame["data"]["items"].as_array().unwrap().iter().map(|i| i["id"].as_str().unwrap().to_owned()).collect()
    }
    #[test] fn v1_page_commands_and_queries_pass_only_when_declared() {
        let mut h = v1_hub();
        for cmd in ["settings.apply", "nodes.update", "logs.tail", "app.info", "alerts.test", "geo.update"] {
            let frame = h.request("r", cmd, json!({"x":1}), Some("page-1".into())).unwrap();
            assert_eq!((frame["t"].as_str(), frame["cmd"].as_str(), frame["from"].as_str()), (Some("req"), Some(cmd), Some("page")));
        }
        for cmd in ["alerts.desktopDelivered", "app.quit", "shell.saveGrant", "ui.presence", "host.saveFile", "app.sessionEnd", "update.exitResponse"] {
            assert_eq!(h.request("r", cmd, json!({}), Some("page-1".into())), Err("not_allowed".into()), "{cmd} is not a page command");
        }
        let mut p1 = hub();
        assert_eq!(p1.request("r", "settings.apply", json!({}), None), Err("not_allowed".into()), "undeclared in caps");
        assert!(control_command("nodes.cancelDiagnose") && !control_command("nodes.diagnose"));
        assert_eq!(PAGE_COMMANDS.len() + PAGE_QUERIES.len(), 31);
    }
    #[test] fn v1_state_topics_cache_and_event_topics_only_forward() {
        let mut h = v1_hub();
        let mut mode = evt("network.mode", 1, "snapshot"); mode["data"] = json!({"mode":"system"});
        h.receive(mode).unwrap();
        let mut changed = evt("config.changed", 1, "edge"); changed["seq"] = json!(1);
        h.receive(changed).unwrap();
        let replay = h.replay();
        assert!(replay.iter().any(|f| f["topic"] == "network.mode"));
        assert!(!replay.iter().any(|f| f["topic"] == "config.changed"), "edge-only topics have no baseline");
        assert_eq!(h.receive(evt("config.changed", 2, "snapshot")), Err("invalid_enum"));
        for shell_only in ["alerts.desktop", "update.prepareExit", "shutdown", "ui.language"] {
            assert_eq!(h.receive(evt(shell_only, 1, "edge")), Err("invalid_enum"), "{shell_only} never reaches the page hub");
        }
        let mut keyed = evt("network.mode", 2, "snapshot"); keyed["key"] = json!("vanguard");
        assert_eq!(h.receive(keyed), Err("invalid_enum"));
        assert_eq!(h.receive(list("nodes.list", None, 1, &[])), Err("missing_field"), "keyed lists need a key");
    }
    #[test] fn list_patches_merge_only_on_base_rev_and_resync_otherwise() {
        let mut h = v1_hub();
        h.receive(list("config.items", None, 3, &["a", "b"])).unwrap();
        h.receive(patch("config.items", None, 3, 4, json!([{"id":"b","v":1},{"id":"c","v":1}]), json!(["a"]))).unwrap();
        let cached = &h.lists["config.items"];
        assert_eq!((ids(cached), cached["rev"].as_u64()), (vec!["b".to_owned(), "c".to_owned()], Some(4)));
        assert!(h.take_requests().is_empty());
        let order = h.order;
        h.receive(patch("config.items", None, 9, 10, json!([]), json!(["b"]))).unwrap();
        assert_eq!(h.order, order, "a mismatched patch never reaches the page");
        assert!(!h.lists.contains_key("config.items"), "stale: no replay of a baseline that missed a patch");
        let requests = h.take_requests();
        assert_eq!(requests.len(), 1);
        assert_eq!((requests[0]["t"].as_str(), requests[0]["topic"].as_str()), (Some("snapshot_req"), Some("config.items")));
        assert!(requests[0].get("cursor").is_none() && requests[0].get("key").is_none());
        let id = requests[0]["id"].as_u64().unwrap();
        let reply = json!({"t":"snapshot_res","epoch":"one","id":id,"topic":"config.items","produced_at_ms":3,"sent_at_ms":3,
            "snapshot":list("config.items", None, 11, &["c"])});
        assert!(h.owns_snapshot_request(&reply));
        h.snapshot_res(&reply).unwrap();
        // T-P3-H2: rt:stale{resync} when the request went out, then the replacement snapshot.
        assert_eq!(h.order, order + 2, "the replacement snapshot is published to the page");
        assert_eq!(ids(&h.lists["config.items"]), ["c"]);
        h.receive(patch("config.items", None, 11, 12, json!([{"id":"d","v":0}]), json!([]))).unwrap();
        assert_eq!(ids(&h.lists["config.items"]), ["c", "d"]);
        // An out-of-order list frame is not session-fatal.
        assert_eq!(h.receive(patch("config.items", None, 11, 11, json!([]), json!([]))), Ok(()));
    }
    #[test] fn keyed_paged_snapshots_are_assembled_before_the_page_sees_them() {
        let mut h = v1_hub();
        let mut first = list("nodes.list", Some("vanguard"), 5, &["n1", "n2"]);
        first["page"] = json!({"snapshot_id":"s1","index":0,"complete":false,"next_cursor":"c1"});
        let order = h.order;
        h.receive(first).unwrap();
        assert_eq!(h.order, order, "half a table is never shown");
        h.receive(list("nodes.list", Some("sentinel"), 1, &[])).unwrap();
        assert_eq!(h.order, order + 1, "other keys are independent");
        let requests = h.take_requests();
        assert_eq!(requests.len(), 1);
        assert_eq!((requests[0]["key"].as_str(), requests[0]["cursor"].as_str()), (Some("vanguard"), Some("c1")));
        let mut last = list("nodes.list", Some("vanguard"), 5, &["n3"]);
        last["page"] = json!({"snapshot_id":"s1","index":1,"complete":true,"next_cursor":null});
        h.snapshot_res(&json!({"t":"snapshot_res","epoch":"one","id":requests[0]["id"],"topic":"nodes.list","key":"vanguard",
            "produced_at_ms":3,"sent_at_ms":3,"snapshot":last})).unwrap();
        let installed = &h.lists["nodes.list#vanguard"];
        assert_eq!(ids(installed), ["n1", "n2", "n3"]);
        assert!(installed.get("page").is_none(), "page metadata is dropped after assembly");
        assert_eq!(h.replay().iter().filter(|f| f["topic"] == "nodes.list").count(), 2);
        h.receive(patch("nodes.list", Some("vanguard"), 5, 6, json!([]), json!(["n1"]))).unwrap();
        assert_eq!(ids(&h.lists["nodes.list#vanguard"]), ["n2", "n3"]);
    }
    #[test] fn snapshot_required_resyncs_a_list_with_bounded_attempts() {
        let mut h = v1_hub();
        h.receive(list("bastion.sources", None, 1, &["x"])).unwrap();
        let order = h.order;
        let required = json!({"t":"error","epoch":"one","produced_at_ms":1,"sent_at_ms":1,"owner":"shell",
            "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":{"topic":"bastion.sources"}}});
        h.receive(required).unwrap();
        assert_eq!(h.order, order, "the shell is the subscriber: the page never sees this error");
        for attempt in 0..3 {
            let requests = h.take_requests();
            assert_eq!(requests.len(), 1, "attempt {attempt}");
            let failed = json!({"t":"snapshot_res","epoch":"one","id":requests[0]["id"],"topic":"bastion.sources","produced_at_ms":1,"sent_at_ms":1,
                "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":{}}});
            h.snapshot_res(&failed).unwrap();
        }
        assert!(h.take_requests().is_empty(), "bounded: the slot stays stale");
        h.reset(json!({"epoch":"two"}));
        assert!(h.lists.is_empty() && h.take_requests().is_empty());
    }
    #[test] fn oversized_page_requests_are_refused_and_the_session_goes_on() {
        let mut h = v1_hub();
        let next = h.next_id;
        let big = "x".repeat(5 * 1024 * 1024);
        assert_eq!(h.request("r-big", "import.preview", json!({"kind":"text","text":big}), Some("page-1".into())),
            Err("frame_too_large".into()));
        let edge = "x".repeat(bridge_host::MAX_FRAME - FRAME_ENVELOPE_MARGIN);
        assert_eq!(h.request("r-edge", "settings.apply", json!({"v":edge}), Some("page-1".into())), Err("frame_too_large".into()),
            "the envelope margin is reserved for the writer's stamp");
        assert!(h.pending.is_empty() && h.next_id == next, "a refused request reserves nothing");
        let frame = h.request("r-ok", "import.preview", json!({"kind":"text","text":"vmess://x"}), Some("page-1".into())).unwrap();
        assert!(frame_fits(&frame));
        // Writer backstop: the page gets a structured error for the dropped frame, once.
        let (id, order) = (frame["id"].as_u64().unwrap(), h.order);
        assert!(h.reject_oversized(id));
        assert_eq!(h.order, order + 1);
        assert!(!h.reject_oversized(id) && h.pending.is_empty());
        assert!(h.request("r-next", "app.info", json!({}), Some("page-1".into())).is_ok(), "the hub keeps serving");
    }
    #[test] fn a_replacement_in_flight_is_not_superseded_by_further_mismatches() {
        let mut h = v1_hub();
        h.receive(list("config.items", None, 3, &["a"])).unwrap();
        h.receive(patch("config.items", None, 9, 10, json!([]), json!([]))).unwrap();
        let requests = h.take_requests();
        assert_eq!(requests.len(), 1);
        for rev in 11..15u64 {
            h.receive(patch("config.items", None, rev, rev + 1, json!([]), json!([]))).unwrap();
            assert!(h.take_requests().is_empty(), "the in-flight replacement answers this mismatch too");
        }
        let reply = json!({"t":"snapshot_res","epoch":"one","id":requests[0]["id"],"topic":"config.items","produced_at_ms":3,"sent_at_ms":3,
            "snapshot":list("config.items", None, 15, &["z"])});
        assert!(h.owns_snapshot_request(&reply));
        h.snapshot_res(&reply).unwrap();
        assert_eq!(ids(&h.lists["config.items"]), ["z"], "installed after three further mismatches");
        // A superseded request (a page continuation replaced by a fresh snapshot) is absorbed silently.
        let mut first = list("nodes.list", Some("gateway"), 2, &["n1"]);
        first["page"] = json!({"snapshot_id":"s1","index":0,"complete":false,"next_cursor":"c1"});
        h.receive(first).unwrap();
        let next_page = h.take_requests();
        assert_eq!(next_page[0]["cursor"].as_str(), Some("c1"));
        h.receive(list("nodes.list", Some("gateway"), 3, &["n9"])).unwrap();
        let mut stale_page = list("nodes.list", Some("gateway"), 2, &["n2"]);
        stale_page["page"] = json!({"snapshot_id":"s1","index":1,"complete":true,"next_cursor":null});
        let late = json!({"t":"snapshot_res","epoch":"one","id":next_page[0]["id"],"topic":"nodes.list","key":"gateway","produced_at_ms":3,"sent_at_ms":3,
            "snapshot":stale_page});
        assert!(h.owns_snapshot_request(&late));
        assert_eq!(h.snapshot_res(&late), Ok(()));
        assert_eq!(ids(&h.lists["nodes.list#gateway"]), ["n9"], "the late page never overwrites the newer snapshot");
        assert!(!h.owns_snapshot_request(&late));
    }
    // ---- T-P4-C3: host:caps, rt:replayed, the epoch seed, rt_resync, H gate ----
    type Captured = std::sync::Arc<std::sync::Mutex<Vec<Value>>>;
    fn capture(h: &mut Hub) -> Captured {
        let captured: Captured = Default::default();
        let output = captured.clone();
        h.connect(Channel::new(move |body| {
            if let tauri::ipc::InvokeResponseBody::Json(body) = body {
                output.lock().unwrap().push(serde_json::from_str::<Value>(&body).unwrap()["frame"].clone());
            } Ok(())
        }));
        captured
    }
    fn kinds(frames: &[Value]) -> Vec<String> {
        frames.iter().map(|f| match f["topic"].as_str() {
            Some(topic) => match f["key"].as_str() { Some(key) => format!("{topic}#{key}"), None => topic.to_owned() },
            None => f["t"].as_str().unwrap_or("?").to_owned(),
        }).collect()
    }
    fn c3_hub(topics: &[&str]) -> Hub {
        let mut hub = Hub::default();
        let mut commands: Vec<&str> = PAGE_COMMANDS.to_vec(); commands.extend(PAGE_QUERIES);
        hub.reset(json!({"t":"hello","epoch":"one","caps":{"commands":commands,"topics":topics,"features":[]}}));
        hub.seed_tick(Instant::now() + SEED_GRACE * 2);
        hub
    }
    fn state(topic: &str, rev: u64) -> Value {
        json!({"t":"evt","epoch":"one","topic":topic,"rev":rev,"kind":"snapshot","produced_at_ms":1,"sent_at_ms":1,"data":{"rev":rev}})
    }
    fn edge(topic: &str, rev: u64, seq: u64) -> Value {
        json!({"t":"evt","epoch":"one","topic":topic,"rev":rev,"seq":seq,"kind":"edge","produced_at_ms":1,"sent_at_ms":1,
            "data":{"from":"vanguard","to":"sentinel","observed_at_ms":1,"authority":"observed"}})
    }
    fn state_gap(topic: &str, rev: u64, seq: u64) -> Value {
        json!({"t":"evt","epoch":"one","topic":topic,"rev":rev,"seq":seq,"kind":"gap","produced_at_ms":1,"sent_at_ms":1,
            "data":{"from_seq":seq-1,"to_seq":seq-1,"dropped":1}})
    }
    fn required(epoch: &str, topic: &str) -> Value {
        json!({"t":"error","epoch":epoch,"produced_at_ms":1,"sent_at_ms":1,"owner":"shell",
            "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":{"topic":topic}}})
    }
    fn failed_reply(id: &Value, topic: &str) -> Value {
        json!({"t":"snapshot_res","epoch":"one","id":id,"topic":topic,"produced_at_ms":1,"sent_at_ms":1,
            "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":{}}})
    }
    #[test] fn host_caps_lists_the_registered_invokes_and_generated_frames() {
        let caps = host_caps("e");
        assert_eq!((caps["t"].as_str(), caps["epoch"].as_str()), (Some("host:caps"), Some("e")));
        let invokes: Vec<&str> = caps["invokes"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
        for name in ["rt", "rt_connect", "host_notify_ready", "host_notify_rendered", "host_confirm_quit", "host_set_page",
            "host_force_restart", "host_ready", "rt_resync", "host_window"] { assert!(invokes.contains(&name), "{name}"); }
        assert_eq!(invokes.len(), 10);
        assert!(!invokes.contains(&"dev_quit"), "debug-only harness hook, not a page capability");
        let events: Vec<&str> = caps["events"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
        for name in ["rt:replayed", "rt:reset", "rt:log-gap", "host:caps", "host:confirmQuit", "host:banner", "host:notify",
            "host:navigate", "host:visibility", "host:language", "host:window", "rt:stale"] { assert!(events.contains(&name), "{name}"); }
        assert_eq!(events.len(), 12);
        assert!(events.iter().all(|e| e.starts_with("host:") || e.starts_with("rt:")));
    }
    #[test] fn every_connect_replays_caps_first_and_ends_with_rt_replayed() {
        let mut h = c3_hub(&["service.state", "network.mode", "jobs", "logs.batch", "tier.transition"]);
        h.receive(state("service.state", 1)).unwrap();
        h.receive(state("network.mode", 1)).unwrap();
        h.receive(evt("jobs", 1, "edge")).unwrap();
        let mut log = evt("logs.batch", 1, "edge"); log["data"] = json!({"records":[{"seq":1}]}); h.receive(log).unwrap();
        h.host_sticky("visibility", json!({"t":"host:visibility","visible":true}));
        h.banner(json!({"t":"host:banner","kind":"sidecar_unresponsive","active":true,"actions":["force_restart"]}));
        let frames = capture(&mut h);
        h.receive(edge("tier.transition", 1, 1)).unwrap();
        let got = kinds(&frames.lock().unwrap());
        assert_eq!(got, ["rt:reset", "host:caps", "hello", "network.mode", "service.state", "jobs", "logs.batch",
            "host:banner", "host:visibility", "rt:replayed", "tier.transition"],
            "02b section 9 item 5: the first live tier.transition comes after rt:replayed, so the page treats it as live");
        // A second page connection (reload) gets the whole replay again, rt:replayed last.
        let again = capture(&mut h);
        let got = kinds(&again.lock().unwrap());
        assert_eq!((got.first().map(String::as_str), got.get(1).map(String::as_str), got.last().map(String::as_str)),
            (Some("rt:reset"), Some("host:caps"), Some("rt:replayed")));
        assert!(!got.contains(&"tier.transition".to_owned()), "event-only topics have no baseline to replay");
        // Not ready (sidecar gone): the shell's own state still replays between caps and rt:replayed.
        h.gone();
        let gone = capture(&mut h);
        assert_eq!(kinds(&gone.lock().unwrap()), ["rt:reset", "host:caps", "host:banner", "host:visibility", "rt:replayed"]);
    }
    #[test] fn a_new_epoch_ends_its_seed_with_rt_replayed() {
        let mut h = Hub::default();
        let frames = capture(&mut h);
        h.reset(json!({"t":"hello","epoch":"one","caps":{"topics":["service.state","network.mode","nodes.list","jobs","tier.transition","alerts.desktop"]}}));
        assert_eq!(kinds(&frames.lock().unwrap())[3..], ["rt:reset", "hello"], "seed pending: no rt:replayed yet");
        let expected: Vec<String> = ["network.mode", "nodes.list#bastion", "nodes.list#gateway", "nodes.list#sentinel", "nodes.list#vanguard", "service.state"]
            .iter().map(|s| s.to_string()).collect();
        assert_eq!(h.seed.as_ref().unwrap().waiting.iter().cloned().collect::<Vec<_>>(), expected, "resyncable declared slots only");
        h.receive(state("service.state", 1)).unwrap();
        h.receive(edge("tier.transition", 1, 1)).unwrap();
        h.receive(state("network.mode", 1)).unwrap();
        // A page connecting mid-seed gets no rt:replayed in its replay: the seed's end sends it.
        let mid = capture(&mut h);
        assert!(!kinds(&mid.lock().unwrap()).contains(&"rt:replayed".to_owned()));
        for key in ["vanguard", "sentinel", "bastion"] { h.receive(list("nodes.list", Some(key), 1, &[])).unwrap(); }
        assert!(h.seed.is_some());
        h.receive(list("nodes.list", Some("gateway"), 1, &[])).unwrap();
        let got = kinds(&mid.lock().unwrap());
        assert_eq!(got[got.len() - 2..], ["nodes.list#gateway", "rt:replayed"], "rt:replayed right after the last seed frame");
        let outcome = h.take_seed_outcome().unwrap();
        assert_eq!(outcome[..4], [("reason", "all".to_owned()), ("slots", "6".to_owned()), ("missing", "0".to_owned()), ("first_missing", "-".to_owned())]);
        assert_eq!(outcome[4].0, "ms");
        let after = capture(&mut h);
        assert_eq!(kinds(&after.lock().unwrap()).last().map(String::as_str), Some("rt:replayed"), "a later replay ends with it again");
        // A seed slot that never arrives: the grace ends the seed.
        h.reset(json!({"t":"hello","epoch":"two","caps":{"topics":["service.state","traffic.tick"]}}));
        let mut s2 = state("service.state", 1); s2["epoch"] = json!("two"); h.receive(s2).unwrap();
        let order = h.order;
        h.seed_tick(Instant::now());
        assert_eq!(h.order, order, "still within the grace");
        h.seed_tick(Instant::now() + SEED_GRACE + Duration::from_millis(1));
        assert_eq!(h.order, order + 1);
        assert_eq!(h.take_seed_outcome().unwrap()[2..4], [("missing", "1".to_owned()), ("first_missing", "traffic.tick".to_owned())]);
        // A list slot answered by snapshot_required is resynced later: it no longer holds the seed.
        h.reset(json!({"t":"hello","epoch":"three","caps":{"topics":["bastion.sources"]}}));
        assert!(h.seed.is_some());
        h.receive(required("three", "bastion.sources")).unwrap();
        assert!(h.seed.is_none());
        // gone() drops a pending seed: the next hello starts a new one.
        h.reset(json!({"t":"hello","epoch":"four","caps":{"topics":["service.state"]}}));
        h.gone(); assert!(h.seed.is_none());
    }
    #[test] fn rt_resync_whitelist_keys_and_rate_limit() {
        let t0 = Instant::now();
        let mut h = c3_hub(&["service.state", "network.mode", "nodes.list", "jobs", "logs.batch", "tier.transition", "config.changed", "alerts.desktop", "ui.language"]);
        for topic in ["jobs", "logs.batch", "tier.transition", "config.changed", "alerts.desktop", "ui.language", "update.prepareExit", "shutdown", "bogus"] {
            assert_eq!(h.resync(topic, None, t0), Err("not_allowed"), "{topic}");
        }
        assert_eq!(h.resync("killswitch.state", None, t0), Err("not_allowed"), "a page topic hello did not declare");
        assert_eq!(h.resync("nodes.list", None, t0), Err("invalid_type"), "keyed topics need a key");
        assert_eq!(h.resync("nodes.list", Some("tier9"), t0), Err("invalid_type"));
        assert_eq!(h.resync("service.state", Some("vanguard"), t0), Err("invalid_type"), "unkeyed topics take no key");
        h.receive(state("service.state", 1)).unwrap();
        let frames = capture(&mut h);
        let before = frames.lock().unwrap().len();
        assert_eq!(h.resync("service.state", None, t0), Ok(json!({"source":"cache"})));
        {
            let got = frames.lock().unwrap();
            assert_eq!(got.len(), before + 1);
            assert_eq!(got[before], h.snapshots["service.state"], "the cached complete snapshot, unchanged");
        }
        assert_eq!(h.resync("service.state", None, t0 + Duration::from_millis(999)), Err("rate_limited"));
        assert_eq!(h.resync("network.mode", None, t0), Ok(json!({"source":"request"})), "slots are limited independently");
        assert_eq!(h.resync("service.state", None, t0 + RESYNC_INTERVAL), Ok(json!({"source":"cache"})));
        // A rate-limited call changes nothing (no frame, no request).
        let n = frames.lock().unwrap().len();
        let requests = h.take_requests().len();
        assert_eq!(h.resync("service.state", None, t0 + RESYNC_INTERVAL), Err("rate_limited"));
        assert_eq!(h.resync("network.mode", None, t0), Err("rate_limited"));
        assert_eq!((frames.lock().unwrap().len(), h.take_requests().len(), requests), (n, 0, 1));
        h.gone();
        assert_eq!(h.resync("service.state", None, t0 + Duration::from_secs(9)), Err("runtime_unavailable"));
    }
    #[test] fn rt_resync_requests_a_replacement_when_the_cache_is_not_current() {
        let t0 = Instant::now();
        let mut h = c3_hub(&["service.state", "network.mode"]);
        h.receive(state("network.mode", 1)).unwrap();
        h.receive(state_gap("network.mode", 2, 2)).unwrap();
        let frames = capture(&mut h);
        let n = frames.lock().unwrap().len();
        assert_eq!(h.resync("network.mode", None, t0), Ok(json!({"source":"request"})), "a gap made the cached snapshot old");
        assert_eq!(frames.lock().unwrap().len(), n, "nothing re-delivered from a stale cache");
        let requests = h.take_requests();
        assert_eq!(requests.len(), 1);
        assert_eq!((requests[0]["t"].as_str(), requests[0]["topic"].as_str()), (Some("snapshot_req"), Some("network.mode")));
        assert!(requests[0].get("key").is_none() && requests[0].get("cursor").is_none());
        assert_eq!(h.resync("network.mode", None, t0 + RESYNC_INTERVAL), Ok(json!({"source":"pending"})), "one request in flight answers both");
        assert!(h.take_requests().is_empty());
        let reply = json!({"t":"snapshot_res","epoch":"one","id":requests[0]["id"],"topic":"network.mode","produced_at_ms":3,"sent_at_ms":3,
            "snapshot":state("network.mode", 3)});
        assert!(h.owns_snapshot_request(&reply));
        h.snapshot_res(&reply).unwrap();
        assert_eq!(frames.lock().unwrap().last().unwrap()["rev"], 3, "the replacement reaches the page");
        assert_eq!(h.snapshots["network.mode"]["rev"], 3);
        // The sidecar gives a replacement a fresh wire rev, so the stream never repeats it; a
        // frame with that rev would still be accepted (defensive: no session rebuild).
        h.receive(state("network.mode", 3)).unwrap();
        assert_eq!(h.resync("network.mode", None, t0 + RESYNC_INTERVAL * 2), Ok(json!({"source":"cache"})));
        // Never seen: nothing cached, the shell asks; a failed reply retries (bounded), a malformed one is refused.
        assert_eq!(h.resync("service.state", None, t0), Ok(json!({"source":"request"})));
        let req = h.take_requests();
        h.snapshot_res(&failed_reply(&req[0]["id"], "service.state")).unwrap();
        let retry = h.take_requests();
        assert_eq!(retry.len(), 1, "retried");
        let foreign = json!({"t":"snapshot_res","epoch":"one","id":retry[0]["id"],"topic":"service.state","produced_at_ms":1,"sent_at_ms":1,
            "snapshot":{"t":"evt","epoch":"other","topic":"service.state","rev":5,"kind":"snapshot","data":{}}});
        assert_eq!(h.snapshot_res(&foreign), Err("invalid_snapshot"));
        assert!(!h.snapshots.contains_key("service.state"));
        // A replacement older than what the stream already delivered is not cached or shown.
        h.receive(state("service.state", 4)).unwrap();
        h.receive(state_gap("service.state", 5, 2)).unwrap();
        let again = h.take_requests();
        let old = json!({"t":"snapshot_res","epoch":"one","id":again[0]["id"],"topic":"service.state","produced_at_ms":1,"sent_at_ms":1,
            "snapshot":state("service.state", 2)});
        let n = frames.lock().unwrap().len();
        assert_eq!(h.snapshot_res(&old), Err("stale_snapshot"));
        assert_eq!((frames.lock().unwrap().len(), h.snapshots["service.state"]["rev"].as_u64()), (n, Some(4)));
    }
    #[test] fn rt_resync_redelivers_keyed_and_paged_lists_by_key() {
        let t0 = Instant::now();
        let mut h = c3_hub(&["nodes.list", "config.items"]);
        h.receive(list("nodes.list", Some("vanguard"), 4, &["a", "b"])).unwrap();
        h.receive(list("nodes.list", Some("sentinel"), 2, &["s"])).unwrap();
        let mut first = list("config.items", None, 7, &["c1"]);
        first["page"] = json!({"snapshot_id":"p","index":0,"complete":false,"next_cursor":"k"});
        h.receive(first).unwrap();
        let frames = capture(&mut h);
        let n = frames.lock().unwrap().len();
        assert_eq!(h.resync("nodes.list", Some("sentinel"), t0), Ok(json!({"source":"cache"})));
        {
            let got = frames.lock().unwrap();
            assert_eq!(got.len(), n + 1);
            assert_eq!((got[n]["key"].as_str(), got[n]["kind"].as_str()), (Some("sentinel"), Some("snapshot")));
            assert_eq!(ids(&got[n]), ["s"]);
        }
        assert_eq!(h.resync("nodes.list", Some("vanguard"), t0), Ok(json!({"source":"cache"})), "keys are separate slots");
        assert_eq!(h.resync("config.items", None, t0), Ok(json!({"source":"pending"})), "half a table is never re-delivered");
        assert_eq!(h.resync("nodes.list", Some("bastion"), t0), Ok(json!({"source":"request"})), "no baseline yet: the shell asks");
        let requests = h.take_requests();
        assert!(requests.iter().any(|r| r["topic"] == "nodes.list" && r["key"] == "bastion" && r.get("cursor").is_none()));
        // Fix round 1: once the page group is assembled, rt_resync re-delivers the complete
        // snapshot from the cache (no `page`, every page's items, the group's rev).
        let next = requests.iter().find(|r| r["topic"] == "config.items").expect("next-page request");
        assert_eq!(next["cursor"], "k");
        let mut second = list("config.items", None, 7, &["c2"]);
        second["page"] = json!({"snapshot_id":"p","index":1,"complete":true});
        h.snapshot_res(&json!({"t":"snapshot_res","epoch":"one","id":next["id"],"topic":"config.items","produced_at_ms":1,"sent_at_ms":1,
            "snapshot":second})).unwrap();
        let n = frames.lock().unwrap().len();
        assert_eq!(h.resync("config.items", None, t0 + RESYNC_INTERVAL), Ok(json!({"source":"cache"})));
        {
            let got = frames.lock().unwrap();
            assert_eq!(got.len(), n + 1);
            assert!(got[n].get("page").is_none(), "the assembled snapshot carries no page");
            assert_eq!((got[n]["kind"].as_str(), got[n]["rev"].as_u64()), (Some("snapshot"), Some(7)));
            assert_eq!(ids(&got[n]), ["c1", "c2"]);
        }
        // A slot whose bounded retries stopped is restarted by an explicit page request.
        let mut h = c3_hub(&["bastion.sources"]);
        h.receive(required("one", "bastion.sources")).unwrap();
        for _ in 0..3 {
            let r = h.take_requests();
            h.snapshot_res(&failed_reply(&r[0]["id"], "bastion.sources")).unwrap();
        }
        assert!(h.take_requests().is_empty(), "stopped after 3 attempts");
        assert_eq!(h.resync("bastion.sources", None, t0), Ok(json!({"source":"request"})));
        assert_eq!(h.take_requests().len(), 1);
    }
    /// Fix round 1: an edge-cached state is re-delivered as a complete snapshot; the tray's facts
    /// are exactly what the cache accepted (stream, gap, replacement), never a refused older rev.
    #[test] fn resync_sends_edge_cached_state_as_snapshot_and_facts_follow_the_cache() {
        let t0 = Instant::now();
        let revs = |facts: Vec<Value>| facts.iter().map(|f| (f["kind"].as_str().unwrap().to_owned(), f["rev"].as_u64().unwrap())).collect::<Vec<_>>();
        let mut h = c3_hub(&["service.state", "network.mode"]);
        let mut first = state("service.state", 1); first["kind"] = json!("edge"); first["seq"] = json!(1);
        h.receive(first.clone()).unwrap();
        h.receive(state("network.mode", 1)).unwrap();
        assert_eq!(revs(h.take_facts()), [("edge".to_owned(), 1)], "only the tray's topics");
        let frames = capture(&mut h);
        let n = frames.lock().unwrap().len();
        assert_eq!(h.resync("service.state", None, t0), Ok(json!({"source":"cache"})));
        {
            let got = frames.lock().unwrap();
            assert_eq!(got.len(), n + 1);
            assert_eq!((got[n]["kind"].as_str(), got[n]["rev"].as_u64(), got[n].get("seq")), (Some("snapshot"), Some(1), None));
            assert_eq!(got[n]["data"], first["data"]);
        }
        assert_eq!(h.snapshots["service.state"], first, "the cache itself is unchanged");
        // A gap is a fact (the tray drops the kill-switch fact on one); the cache is then not current.
        h.receive(state_gap("service.state", 2, 3)).unwrap();
        assert_eq!(revs(h.take_facts()), [("gap".to_owned(), 2)]);
        assert_eq!(h.resync("service.state", None, t0 + RESYNC_INTERVAL), Ok(json!({"source":"request"})));
        let req = h.take_requests();
        h.snapshot_res(&json!({"t":"snapshot_res","epoch":"one","id":req[0]["id"],"topic":"service.state","produced_at_ms":1,"sent_at_ms":1,
            "snapshot":state("service.state", 5)})).unwrap();
        assert_eq!(revs(h.take_facts()), [("snapshot".to_owned(), 5)], "the replacement reaches the tray");
        h.receive(state("service.state", 3)).unwrap();
        assert!(h.take_facts().is_empty(), "an older rev the cache refused is not a tray fact");
        assert_eq!(h.snapshots["service.state"]["rev"], 5);
        h.receive(state("service.state", 6)).unwrap();
        assert_eq!(revs(h.take_facts()), [("snapshot".to_owned(), 6)]);
        h.receive(state("service.state", 7)).unwrap();
        h.reset(json!({"t":"hello","epoch":"two","caps":{"topics":["service.state"]}}));
        assert!(h.take_facts().is_empty(), "a new epoch drops facts not yet taken");
    }
    #[test] fn h_commands_pass_only_with_both_keys() {
        let table: &[(&str, &str)] = &[("nodes.reorder", "B-31")];
        let mut commands: Vec<&str> = PAGE_COMMANDS.to_vec(); commands.push("nodes.reorder");
        let mut h = Hub::default();
        for (features, build, ok) in [(json!(["B-31"]), &["B-31"][..], true), (json!(["B-31"]), &[][..], false),
            (json!([]), &["B-31"][..], false), (json!([]), &[][..], false)] {
            h.reset(json!({"epoch":"one","caps":{"commands":commands,"features":features}}));
            let result = h.request_gated("r", "nodes.reorder", json!({}), Some("p".into()), table, build);
            assert_eq!(result.is_ok(), ok, "{features} {build:?}");
            if !ok { assert_eq!(result, Err("not_allowed".into())); }
        }
        // The compiled table (v1: empty) keeps every unknown command out, whatever the keys say.
        h.reset(json!({"epoch":"one","caps":{"commands":commands,"features":["B-31"]}}));
        assert_eq!(h.request("r", "nodes.reorder", json!({}), Some("p".into())), Err("not_allowed".into()));
    }
    // ---- T-P3-H2 (31b-5): rt:stale{topic,key,reason} ----
    fn stale_notices(frames: &Captured) -> Vec<(String, Value, String)> {
        frames.lock().unwrap().iter().filter(|f| f["t"] == "rt:stale")
            .map(|f| (f["topic"].as_str().unwrap().to_owned(), f["key"].clone(), f["reason"].as_str().unwrap().to_owned())).collect()
    }
    fn stale(topic: &str, key: Option<&str>, reason: &str) -> (String, Value, String) { (topic.to_owned(), json!(key), reason.to_owned()) }
    fn keyed_patch(key: &str, base: u64, rev: u64) -> Value { patch("nodes.list", Some(key), base, rev, json!([]), json!([])) }
    #[test] fn a_mismatch_announces_resync_once_when_the_refetch_starts() {
        let mut h = c3_hub(&["nodes.list"]);
        h.receive(list("nodes.list", Some("vanguard"), 3, &["a"])).unwrap();
        let frames = capture(&mut h);
        let n = frames.lock().unwrap().len();
        h.receive(keyed_patch("vanguard", 7, 8)).unwrap();
        assert!(stale_notices(&frames).is_empty(), "nothing yet: the refetch starts with the request");
        let requests = h.take_requests();
        assert_eq!(requests.len(), 1);
        assert_eq!(stale_notices(&frames), [stale("nodes.list", Some("vanguard"), "resync")]);
        {
            let got = frames.lock().unwrap();
            assert_eq!(got.len(), n + 1, "the mismatched patch itself never reaches the page");
            assert_eq!(got[n]["epoch"], "one");
            assert!(got[n].get("seq").is_none() && got[n].get("rev").is_none(), "a shell notice, not a forged sidecar gap");
        }
        assert_eq!(h.take_stale_log(), [vec![("topic", "nodes.list".to_owned()), ("key", "vanguard".to_owned()), ("reason", "resync".to_owned())]]);
        // Further mismatches while the replacement is in flight, and an unsent request that goes
        // out again, announce nothing more.
        h.receive(keyed_patch("vanguard", 9, 10)).unwrap();
        assert!(h.take_requests().is_empty());
        h.request_unsent(&requests[0]);
        assert_eq!(h.take_requests().len(), 1, "re-sent");
        assert_eq!(stale_notices(&frames).len(), 1);
        assert!(h.take_stale_log().is_empty());
        // A reconnecting page hears it again (the slot's cache is not in the replay); other keys are untouched.
        let again = capture(&mut h);
        assert_eq!(stale_notices(&again), [stale("nodes.list", Some("vanguard"), "resync")]);
        {
            let got = again.lock().unwrap();
            assert!(!got.iter().any(|f| f["t"] == "evt" && f["key"] == "vanguard"), "no baseline that missed a patch");
            let at = |t: &str| got.iter().position(|f| f["t"] == t).unwrap();
            assert!(at("hello") < at("rt:stale") && at("rt:stale") < at("rt:replayed"), "part of the replay");
        }
        // Unkeyed list topics carry key null.
        let mut h = c3_hub(&["config.items"]);
        h.receive(list("config.items", None, 1, &["x"])).unwrap();
        let frames = capture(&mut h);
        h.receive(required("one", "config.items")).unwrap();
        h.take_requests();
        assert_eq!(stale_notices(&frames), [stale("config.items", None, "resync")]);
        assert!(frames.lock().unwrap().iter().filter(|f| f["t"] == "rt:stale").all(|f| f.get("key") == Some(&Value::Null)));
    }
    #[test] fn a_successful_refetch_clears_the_notice_and_a_later_mismatch_starts_over() {
        let mut h = c3_hub(&["nodes.list"]);
        h.receive(list("nodes.list", Some("sentinel"), 3, &["a"])).unwrap();
        let frames = capture(&mut h);
        h.receive(keyed_patch("sentinel", 5, 6)).unwrap();
        let requests = h.take_requests();
        let reply = json!({"t":"snapshot_res","epoch":"one","id":requests[0]["id"],"topic":"nodes.list","key":"sentinel","produced_at_ms":3,"sent_at_ms":3,
            "snapshot":list("nodes.list", Some("sentinel"), 9, &["b"])});
        h.snapshot_res(&reply).unwrap();
        {
            let got = frames.lock().unwrap();
            let last = got.last().unwrap();
            assert_eq!((last["t"].as_str(), last["key"].as_str(), last["rev"].as_u64()), (Some("evt"), Some("sentinel"), Some(9)), "the replacement follows the notice");
        }
        assert!(h.stale.is_empty(), "fresh again");
        let replay = capture(&mut h);
        assert!(stale_notices(&replay).is_empty(), "a fresh slot is not announced on reconnect");
        assert_eq!(ids(&replay.lock().unwrap().iter().find(|f| f["key"] == "sentinel").unwrap().clone()), ["b"]);
        // Patches apply again; the next mismatch is a new episode: resync once more.
        h.receive(keyed_patch("sentinel", 9, 10)).unwrap();
        assert!(h.take_requests().is_empty());
        h.receive(keyed_patch("sentinel", 20, 21)).unwrap();
        assert_eq!(h.take_requests().len(), 1);
        assert_eq!(stale_notices(&replay), [stale("nodes.list", Some("sentinel"), "resync")]);
        // A stream snapshot also ends the episode (no notice on its own).
        h.receive(list("nodes.list", Some("sentinel"), 30, &["c"])).unwrap();
        assert!(h.stale.is_empty());
        assert!(stale_notices(&capture(&mut h)).is_empty());
    }
    #[test] fn running_out_of_attempts_announces_exhausted_once_and_the_slot_stays_stale() {
        let mut h = c3_hub(&["bastion.sources", "nodes.list"]);
        h.receive(list("bastion.sources", None, 1, &["x"])).unwrap();
        let frames = capture(&mut h);
        h.receive(required("one", "bastion.sources")).unwrap();
        for attempt in 0..RESYNC_ATTEMPTS {
            let r = h.take_requests();
            assert_eq!(r.len(), 1, "attempt {attempt}");
            h.snapshot_res(&failed_reply(&r[0]["id"], "bastion.sources")).unwrap();
        }
        assert_eq!(stale_notices(&frames), [stale("bastion.sources", None, "resync")], "one resync for the three attempts");
        assert!(h.take_requests().is_empty(), "no fourth attempt, no timed retry");
        assert_eq!(stale_notices(&frames), [stale("bastion.sources", None, "resync"), stale("bastion.sources", None, "exhausted")]);
        let log = h.take_stale_log();
        assert_eq!(log.last().unwrap()[2], ("reason", "exhausted".to_owned()));
        // Further mismatches stay quiet and send nothing.
        h.receive(required("one", "bastion.sources")).unwrap();
        assert!(h.take_requests().is_empty());
        h.receive(patch("bastion.sources", None, 4, 5, json!([]), json!([]))).unwrap();
        assert!(h.take_requests().is_empty());
        assert_eq!(stale_notices(&frames).len(), 2);
        // A reconnect replays the exhausted state; a new epoch clears it.
        assert_eq!(stale_notices(&capture(&mut h)), [stale("bastion.sources", None, "exhausted")]);
        h.reset(json!({"t":"hello","epoch":"two","caps":{"topics":["bastion.sources"]}}));
        assert!(h.stale.is_empty() && h.take_stale_log().is_empty());
        assert!(stale_notices(&capture(&mut h)).is_empty());
    }
    #[test] fn an_explicit_resync_resets_the_count_without_a_resync_notice() {
        let t0 = Instant::now();
        let mut h = c3_hub(&["nodes.list", "network.mode"]);
        let frames = capture(&mut h);
        h.receive(list("nodes.list", Some("bastion"), 1, &["x"])).unwrap();
        h.receive(keyed_patch("bastion", 5, 6)).unwrap();
        for _ in 0..RESYNC_ATTEMPTS {
            let r = h.take_requests();
            h.snapshot_res(&json!({"t":"snapshot_res","epoch":"one","id":r[0]["id"],"topic":"nodes.list","key":"bastion","produced_at_ms":1,"sent_at_ms":1,
                "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":{}}})).unwrap();
        }
        assert!(h.take_requests().is_empty());
        assert_eq!(stale_notices(&frames).len(), 2, "resync, exhausted");
        // The page's rt_resync clears the count: three fresh attempts, and no resync notice (the page asked).
        assert_eq!(h.resync("nodes.list", Some("bastion"), t0), Ok(json!({"source":"request"})));
        assert_eq!(h.attempts.get("nodes.list#bastion"), None);
        for attempt in 0..RESYNC_ATTEMPTS {
            let r = h.take_requests();
            assert_eq!(r.len(), 1, "fresh attempt {attempt}");
            if attempt + 1 < RESYNC_ATTEMPTS {
                h.snapshot_res(&json!({"t":"snapshot_res","epoch":"one","id":r[0]["id"],"topic":"nodes.list","key":"bastion","produced_at_ms":1,"sent_at_ms":1,
                    "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":{}}})).unwrap();
            } else {
                h.snapshot_res(&json!({"t":"snapshot_res","epoch":"one","id":r[0]["id"],"topic":"nodes.list","key":"bastion","produced_at_ms":1,"sent_at_ms":1,
                    "snapshot":list("nodes.list", Some("bastion"), 9, &["y"])})).unwrap();
            }
        }
        assert_eq!(stale_notices(&frames).len(), 2, "the page-initiated refetch announced nothing");
        assert!(h.stale.is_empty(), "the last attempt succeeded: fresh");
        assert_eq!(frames.lock().unwrap().last().unwrap()["t"], "evt", "the replacement reached the page");
        // Out of attempts again after an explicit resync: exhausted is said again; a state topic
        // the page asked for (cache not current) follows the same rule, key null.
        h.receive(keyed_patch("bastion", 1, 2)).unwrap();
        for _ in 0..RESYNC_ATTEMPTS {
            let r = h.take_requests();
            h.snapshot_res(&json!({"t":"snapshot_res","epoch":"one","id":r[0]["id"],"topic":"nodes.list","key":"bastion","produced_at_ms":1,"sent_at_ms":1,
                "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":{}}})).unwrap();
        }
        assert!(h.take_requests().is_empty());
        assert_eq!(h.resync("nodes.list", Some("bastion"), t0 + RESYNC_INTERVAL), Ok(json!({"source":"request"})));
        for _ in 0..RESYNC_ATTEMPTS {
            let r = h.take_requests();
            h.snapshot_res(&json!({"t":"snapshot_res","epoch":"one","id":r[0]["id"],"topic":"nodes.list","key":"bastion","produced_at_ms":1,"sent_at_ms":1,
                "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":{}}})).unwrap();
        }
        assert!(h.take_requests().is_empty());
        let bastion: Vec<String> = stale_notices(&frames).into_iter().map(|(_, _, reason)| reason).collect();
        assert_eq!(bastion, ["resync", "exhausted", "resync", "exhausted", "exhausted"]);
        assert_eq!(h.resync("network.mode", None, t0), Ok(json!({"source":"request"})), "never seen: not current");
        for _ in 0..RESYNC_ATTEMPTS {
            let r = h.take_requests();
            h.snapshot_res(&failed_reply(&r[0]["id"], "network.mode")).unwrap();
        }
        assert!(h.take_requests().is_empty());
        assert_eq!(stale_notices(&frames).last().unwrap(), &stale("network.mode", None, "exhausted"));
        // The stream's first network.mode makes it current: the notice is gone from the replay.
        h.receive(state("network.mode", 4)).unwrap();
        assert!(!stale_notices(&capture(&mut h)).iter().any(|(topic, _, _)| topic == "network.mode"));
    }
    // ---- T-P3-H2 fix round 1 ----
    #[test] fn a_stale_state_replacement_asks_again_within_the_bound_or_ends_the_episode() {
        let t0 = Instant::now();
        let mut h = c3_hub(&["service.state"]);
        h.receive(state("service.state", 4)).unwrap();
        h.receive(state_gap("service.state", 5, 2)).unwrap();
        h.take_requests();
        let frames = capture(&mut h);
        let old = |id: &Value| json!({"t":"snapshot_res","epoch":"one","id":id,"topic":"service.state","produced_at_ms":1,"sent_at_ms":1,
            "snapshot":state("service.state", 2)});
        // The cache is not current: a replacement older than the stream counts as a failed attempt
        // and the shell asks again, never leaving a silent `resync` mark without a request.
        assert_eq!(h.resync("service.state", None, t0), Ok(json!({"source":"request"})));
        for attempt in 0..RESYNC_ATTEMPTS {
            let r = h.take_requests();
            assert_eq!(r.len(), 1, "attempt {attempt}");
            assert_eq!(h.snapshot_res(&old(&r[0]["id"])), Err("stale_snapshot"));
        }
        assert!(h.take_requests().is_empty(), "bounded");
        assert_eq!(stale_notices(&frames), [stale("service.state", None, "exhausted")], "the page asked: no resync, then exhausted");
        assert_eq!(h.snapshots["service.state"]["rev"], 4, "nothing older was cached");
        // The cache became current meanwhile (the stream caught up): the episode ends, no request.
        assert_eq!(h.resync("service.state", None, t0 + RESYNC_INTERVAL), Ok(json!({"source":"request"})));
        let r = h.take_requests();
        h.receive(state("service.state", 6)).unwrap();
        assert_eq!(h.snapshot_res(&old(&r[0]["id"])), Err("stale_snapshot"));
        assert!(h.take_requests().is_empty() && h.stale.is_empty() && !h.attempts.contains_key("service.state"));
        assert!(stale_notices(&capture(&mut h)).is_empty(), "nothing stale in the replay");
    }
    #[test] fn snapshot_required_with_an_invalid_slot_is_dropped() {
        let mut h = c3_hub(&["nodes.list", "config.items"]);
        h.receive(list("nodes.list", Some("vanguard"), 3, &["a"])).unwrap();
        h.receive(list("config.items", None, 1, &["x"])).unwrap();
        let frames = capture(&mut h);
        let n = frames.lock().unwrap().len();
        let error = |params: Value| json!({"t":"error","epoch":"one","produced_at_ms":1,"sent_at_ms":1,"owner":"shell",
            "error":{"code":"snapshot_required","message_key":"errors.bridge.snapshot_required","params":params}});
        for params in [json!({"topic":"nodes.list"}), json!({"topic":"nodes.list","tier":"tier9"}), json!({"topic":"nodes.list","tier":7}),
                       json!({"topic":"config.items","tier":"vanguard"})] {
            assert_eq!(h.receive(error(params.clone())), Err("snapshot_required_invalid_slot"), "{params}");
        }
        assert!(!rebuild_required("snapshot_required_invalid_slot"), "logged and ignored, the session goes on");
        assert!(h.take_requests().is_empty());
        assert_eq!(frames.lock().unwrap().len(), n, "nothing reaches the page: no rt:stale with an arbitrary key");
        assert!(h.lists.contains_key("nodes.list#vanguard") && h.lists.contains_key("config.items"), "baselines kept");
        h.receive(error(json!({"topic":"nodes.list","tier":"vanguard"}))).unwrap();
        assert_eq!(h.take_requests().len(), 1);
        assert_eq!(stale_notices(&frames), [stale("nodes.list", Some("vanguard"), "resync")]);
    }
    #[test] fn a_page_resync_right_after_the_last_failure_restarts_the_refetch() {
        let t0 = Instant::now();
        let mut h = c3_hub(&["bastion.sources"]);
        h.receive(list("bastion.sources", None, 1, &["x"])).unwrap();
        let frames = capture(&mut h);
        h.receive(required("one", "bastion.sources")).unwrap();
        for _ in 0..RESYNC_ATTEMPTS {
            let r = h.take_requests();
            h.snapshot_res(&failed_reply(&r[0]["id"], "bastion.sources")).unwrap();
        }
        // The third failure queued a replacement that would never be sent; the page asks before
        // the shell drops it: a real request, not `pending` with nothing behind it.
        assert_eq!(h.resync("bastion.sources", None, t0), Ok(json!({"source":"request"})));
        assert_eq!(h.take_requests().len(), 1, "three fresh attempts");
        assert_eq!(stale_notices(&frames), [stale("bastion.sources", None, "resync")], "restarted in time: no exhausted");
    }
    // ---- Bastion trace (CLAUDE_CHANGES §8r, diagnostic) ----
    #[test] fn the_bastion_trace_logs_pool_frames_requests_replies_and_resyncs_only() {
        let t0 = Instant::now();
        let mut h = c3_hub(&["bastion.pool", "network.mode"]);
        let pool = |rev: u64, alive: u64| json!({"t":"evt","epoch":"one","topic":"bastion.pool","rev":rev,"kind":"snapshot",
            "produced_at_ms":1,"sent_at_ms":1,"data":{"runtime_alive":alive}});
        let s = |v: &str| v.to_owned();
        h.receive(pool(1, 5)).unwrap();
        h.receive(state("network.mode", 1)).unwrap();
        assert_eq!(h.take_pool_log(), [vec![("via", s("stream")), ("kind", s("snapshot")), ("rev", s("1")), ("seq", s("-")),
            ("alive", s("5")), ("fwd", s("1")), ("err", s("-"))]], "only bastion.pool is traced");
        // a gap; the page asks again; the shell asks the sidecar, whose reply has no snapshot
        h.receive(state_gap("bastion.pool", 2, 2)).unwrap();
        assert_eq!(h.resync("bastion.pool", None, t0), Ok(json!({"source":"request"})));
        let r = h.take_requests();
        h.snapshot_res(&failed_reply(&r[0]["id"], "bastion.pool")).unwrap();
        let log = h.take_pool_log();
        let via: Vec<&str> = log.iter().map(|f| f[0].1.as_str()).collect();
        assert_eq!(via, ["stream", "page_resync", "snapshot_req", "snapshot_res"]);
        assert_eq!((log[0][1].1.as_str(), log[1][1].1.as_str(), log[2][1].1.as_str()), ("gap", "0", "1"), "gap, cache not current, attempt 1");
        assert_eq!((log[3][1].1.as_str(), log[3][4].1.as_str()), ("none", "snapshot_required"));
        // a later stream value reaches the page with its count; a refused frame says why
        h.receive(pool(3, 9)).unwrap();
        assert_eq!(h.receive(pool(3, 9)), Err("sequence_gap"));
        let log = h.take_pool_log();
        assert_eq!((log[0][4].1.as_str(), log[0][5].1.as_str()), ("9", "1"));
        assert_eq!((log[1][5].1.as_str(), log[1][6].1.as_str()), ("-", "sequence_gap"));
        // capped per process
        for rev in 4..(4 + POOL_TRACE_MAX as u64) { let _ = h.receive(pool(rev, 9)); }
        assert!(h.take_pool_log().len() < POOL_TRACE_MAX as usize);
        assert!(h.take_pool_log().is_empty());
    }
}
