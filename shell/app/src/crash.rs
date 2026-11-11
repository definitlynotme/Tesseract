//! T-P3-04 sidecar crash and hang handling (plan §2.2.6): exit classification, the bounded
//! stopped-state restart policy, the pong watchdog, page banners and the pre-hello dialog text.
use serde_json::{json, Value};
use std::{collections::VecDeque, path::Path, time::{Duration, Instant}};

pub const RESTART_WINDOW: Duration = Duration::from_secs(300);
pub const RESTART_LIMIT: usize = 3;
/// Pause before a relaunch, so a sidecar that dies at once cannot spin the CPU.
pub const RESTART_DELAY: Duration = Duration::from_secs(1);
pub const PONG_TIMEOUT: Duration = Duration::from_secs(15);
/// A worker loop gap this long means the machine slept or the process was suspended.
pub const SUSPEND_GAP: Duration = Duration::from_secs(5);
/// Exit code the shell gives a sidecar it terminates on the user's "force restart".
pub const FORCE_RESTART_CODE: u32 = 0xE000_0001;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ExitKind {
    /// 0: the sidecar finished its own shutdown; the shell follows it out.
    Normal,
    /// 74: shutdown ended with a cleanup failure; follow it out, never restart.
    CleanupFailed,
    /// Anything else outside a quit: banner and a bounded stopped-state restart.
    Crash,
}
/// Only for exits outside the quit/session-end flow (that flow is `Machine::sidecar_exited`).
pub fn classify(code: u32) -> ExitKind {
    match code { 0 => ExitKind::Normal, 74 => ExitKind::CleanupFailed, _ => ExitKind::Crash }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Restart { Allowed { attempt: usize }, Exhausted }
/// At most `RESTART_LIMIT` restarts in any `RESTART_WINDOW`; once refused, never again.
#[derive(Default)]
pub struct RestartPolicy { restarts: VecDeque<Instant>, exhausted: bool }
impl RestartPolicy {
    pub fn on_crash(&mut self, now: Instant) -> Restart {
        while self.restarts.front().is_some_and(|at| now.saturating_duration_since(*at) >= RESTART_WINDOW) { self.restarts.pop_front(); }
        if self.exhausted || self.restarts.len() >= RESTART_LIMIT { self.exhausted = true; return Restart::Exhausted; }
        self.restarts.push_back(now);
        Restart::Allowed { attempt: self.restarts.len() }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Watch { Unresponsive, Recovered }
/// 15 s without `pong` raises one banner; the shell never kills on its own (the user may
/// "force restart"). Sleep/resume and long loop gaps restart the count.
pub struct Watchdog { last_pong: Instant, last_tick: Instant, resume_seen: u64, timeout: Duration, pub unresponsive: bool }
impl Watchdog {
    pub fn new(now: Instant, resume_seen: u64, timeout: Duration) -> Self { Self { last_pong: now, last_tick: now, resume_seen, timeout, unresponsive: false } }
    /// `resume` is the process-wide PBT_APMRESUMEAUTOMATIC counter.
    pub fn tick(&mut self, now: Instant, resume: u64) -> Option<Watch> {
        if resume != self.resume_seen || now.saturating_duration_since(self.last_tick) > SUSPEND_GAP {
            self.resume_seen = resume; self.last_pong = now;
        }
        self.last_tick = now;
        if !self.unresponsive && now.saturating_duration_since(self.last_pong) > self.timeout {
            self.unresponsive = true; return Some(Watch::Unresponsive);
        }
        None
    }
    pub fn pong(&mut self, now: Instant) -> Option<Watch> {
        self.last_pong = now;
        if self.unresponsive { self.unresponsive = false; return Some(Watch::Recovered); }
        None
    }
}

/// `host:banner` frames for the page (shell → page only, never sent to the sidecar).
pub mod banner {
    use super::*;
    pub const CRASHED: &str = "sidecar_crashed";
    pub const UNRESPONSIVE: &str = "sidecar_unresponsive";
    /// state: restarting | restarted | unrecoverable. `restarts` counts restarts in the window.
    /// `restarted` is a one-shot notice (`active:false`): it is delivered live, replaces the
    /// cached `restarting` banner and is never replayed to a reconnecting page.
    pub fn crashed(state: &str, exit_code: Option<u32>, restarts: usize) -> Value {
        json!({"t":"host:banner","kind":CRASHED,"active":state != "restarted","state":state,"exit_code":exit_code,
            "restarts":restarts,"limit":RESTART_LIMIT})
    }
    pub fn unresponsive(active: bool) -> Value {
        json!({"t":"host:banner","kind":UNRESPONSIVE,"active":active,"actions":if active {json!(["force_restart"])} else {json!([])}})
    }
}

// ---------------------------------------------------------------- pre-hello dialog text

/// Pre-hello dialog wording: the sidecar process ended, or the shell stopped it because the
/// handshake did not arrive in time or did not match the contract (never "it exited").
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum PreHello { Exited, Handshake }
/// `start_worker` error text (`sidecar_start_failed_<code>` / `sidecar_start_timeout`) → wording.
pub fn prehello_kind(error: &str) -> PreHello {
    const HANDSHAKE: [&str; 4] = ["hello_timeout", "sidecar_start_timeout", "hello_contract_mismatch", "epoch_missing"];
    if HANDSHAKE.iter().any(|code| error.ends_with(code)) { PreHello::Handshake } else { PreHello::Exited }
}

pub const TAIL_LINES: usize = 20;
const LINE_UNITS: usize = 200;
/// Key names whose value is hidden. A key matches when its normalized name (lowercase, `-` →
/// `_`) ends with one of these, so `api_key`, `obfs-password`, `pre-shared-key`, `auth_token`,
/// `webhook_url`, `vless_link`, `account_sid` are covered. `secret_keys_cover_bridge_vocab`
/// keeps this in step with `bridge_vocab.SECRET_RECORDS` (the one definition of secret fields).
const SECRET_KEYS: &[&str] = &["password", "passwd", "pwd", "psk", "secret", "token", "uuid", "key", "auth", "authorization",
    "sid", "cookie", "credentials", "url", "urls", "uri", "link", "subscription", "subscriptions", "webhook", "username", "user",
    "topic", "value"];
/// `bridge_vocab.SECRET_RECORDS` names that are deliberately not value-masked here, with why.
#[cfg(test)]
const VOCAB_EXEMPT: &[(&str, &str)] = &[
    ("server", "an ntfy/gotify host; any URL value is still URL-masked"),
    ("headers", "a mapping; its credential members (authorization, cookie, x-api-key, *token*) are keys themselves"),
    ("path", "source identity rank only, not a credential; tracebacks need file paths"),
    ("channel", "source identity rank only, not a credential"),
];
/// Proxy share links carry the whole credential in the URL body: never show any of it.
const PROXY_SCHEMES: &[&str] = &["vmess", "vless", "ss", "ssr", "trojan", "hysteria", "hysteria2", "hy2", "tuic", "wireguard", "wg",
    "socks", "socks4", "socks5", "snell", "anytls", "naive", "naive+https", "juicity", "mieru", "shadowtls"];
/// Quote/bracket characters around a word or a value (`{'password': 'x',}` style dumps).
const WRAP: &str = "'\"{}[](),;<>`";
fn bare(word: &str) -> &str { word.trim_matches(|c: char| WRAP.contains(c)) }
fn base64ish(s: &str) -> bool { s.chars().all(|c| c.is_ascii_alphanumeric() || "+/=_-".contains(c)) }
fn secret_key(key: &str) -> bool {
    let key = bare(key).to_ascii_lowercase().replace('-', "_");
    let key = key.trim_matches(|c: char| !c.is_ascii_alphanumeric() && c != '_');
    !key.is_empty() && SECRET_KEYS.iter().any(|k| key.ends_with(k))
}
/// `Bearer`/`Basic`/`Digest`: the next word is the credential, whatever it looks like.
fn auth_scheme(word: &str) -> bool { matches!(bare(word).to_ascii_lowercase().as_str(), "bearer" | "basic" | "digest") }
/// Long opaque tokens (≥24 base64 characters with a digit) and JWT-shaped `xxx.yyy.zzz`.
fn opaque(word: &str) -> bool {
    let word = bare(word);
    if word.len() >= 24 && base64ish(word) && word.chars().any(|c| c.is_ascii_digit()) { return true; }
    let parts: Vec<&str> = word.split('.').collect();
    parts.len() == 3 && parts.iter().all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_alphanumeric() || "_-=".contains(c)))
        && (parts[0].starts_with("eyJ") && parts[0].len() >= 10 || parts.iter().all(|p| p.len() >= 16))
}
/// Replaces the value, keeping the quotes/brackets/punctuation around it: `'hunter2',` → `'***',`.
fn mask_value(value: &str) -> String {
    let start = value.len() - value.trim_start_matches(|c: char| WRAP.contains(c)).len();
    let body = &value[start..];
    let core = body.trim_end_matches(|c: char| WRAP.contains(c) || c == '.' || c == ':');
    if core.is_empty() { return value.to_owned(); }
    format!("{}***{}", &value[..start], &body[core.len()..])
}
fn masked_url(word: &str) -> Option<String> {
    let at = word.find("://")?;
    let (prefix, rest) = word.split_at(at + 3);
    let scheme = word[..at].rsplit(|c: char| !(c.is_ascii_alphanumeric() || "+-.".contains(c))).next().unwrap_or("").to_ascii_lowercase();
    if PROXY_SCHEMES.contains(&scheme.as_str()) { return Some(format!("{prefix}***")); }
    let body = rest.trim_end_matches(|c: char| WRAP.contains(c) || c == '.');
    // No host-like shape (no dot, no port) and a long base64 body: an opaque link, hide it.
    if !body.contains('@') && body.len() >= 16 && base64ish(body) && !body.contains('.') { return Some(format!("{prefix}***")); }
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let host = rest[..end].rsplit('@').next().unwrap_or("");
    let creds = if rest[..end].contains('@') { "***@" } else { "" };
    // Path segments carry bot tokens, webhook ids, device keys and subscription tokens
    // (`/bot<id>:<token>/…`, `/hook/<uuid>`, `/<device_key>/…`): like
    // `alert_delivery._redacted_history_url`, any path other than `` or `/` becomes `/***`.
    // Trailing quotes/brackets/punctuation around the URL are kept.
    let tail = &rest[end..];
    let trail = &tail[tail.trim_end_matches(|c: char| WRAP.contains(c) || c == '.').len()..];
    let path = tail.split(['?', '#']).next().unwrap_or("");
    let masked_path = !matches!(path.trim_end_matches(|c: char| WRAP.contains(c) || c == '.'), "" | "/");
    let masked_query = tail.contains(['?', '#']);
    let path = if masked_path { "/***" } else if masked_query { path } else { tail };
    let query = if masked_query { "?***" } else { "" };
    let trail = if masked_path || masked_query { trail } else { "" };
    Some(format!("{prefix}{creds}{host}{path}{query}{trail}"))
}
/// One word that is not a pending value. Sets `pending` when the *next* word is the secret.
fn redact_word(word: &str, pending: &mut bool) -> String {
    if auth_scheme(word) { *pending = true; return word.to_owned(); }
    // `password:` / `'token':` / `{"uuid"=` — the value is the next word.
    if let Some(key) = bare(word).strip_suffix([':', '=']) {
        if secret_key(key) { *pending = true; return word.to_owned(); }
    }
    let kv = word.find(['=', ':']).filter(|&i| i > 0 && i + 1 < word.len());
    if let Some(eq) = kv.filter(|&eq| secret_key(&word[..eq])) {
        let value = &word[eq + 1..];
        if auth_scheme(value) { *pending = true; return word.to_owned(); }
        if bare(value).is_empty() { *pending = true; return word.to_owned(); }
        return format!("{}{}", &word[..=eq], mask_value(value));
    }
    if let Some(url) = masked_url(word) { return url; }
    if opaque(word) { return mask_value(word); }
    if let Some(eq) = kv.filter(|&eq| opaque(&word[eq + 1..])) { return format!("{}{}", &word[..=eq], mask_value(&word[eq + 1..])); }
    word.to_owned()
}
/// Display-only redaction of one sidecar stderr line: secret `key=value` / `key: value` /
/// `'key': 'value'` pairs, `Bearer`/`Basic` credentials, proxy share links, URL credentials and
/// queries, opaque and JWT tokens, control characters. The result is never written to shell.log.
pub fn redact(line: &str) -> String {
    let mut words = Vec::new();
    let mut pending = false;
    let input: Vec<&str> = line.split(' ').collect();
    for (index, &word) in input.iter().enumerate() {
        // `password = hunter2`: a bare secret key followed by a lone separator word.
        if !pending && !word.contains([':', '=']) && secret_key(word)
            && input.get(index + 1).is_some_and(|next| matches!(bare(next), ":" | "=")) {
            pending = true; words.push(word.to_owned()); continue;
        }
        if pending {
            // A lone separator or an auth scheme sits between the key and its value.
            if word.is_empty() || matches!(bare(word), "" | ":" | "=") || auth_scheme(word) { words.push(word.to_owned()); continue; }
            pending = false;
            words.push(mask_value(word));
            continue;
        }
        words.push(redact_word(word, &mut pending));
    }
    let joined: String = words.join(" ").chars().map(|c| if c.is_control() && c != '\t' { ' ' } else { c }).collect();
    crate::tray::fit(&joined, LINE_UNITS)
}
/// Last `n` non-empty lines of the sidecar's own stderr log (§2.2.1 step 5.3), redacted.
pub fn stderr_tail(data: &Path, n: usize) -> Option<Vec<String>> {
    let bytes = std::fs::read(data.join("logs").join("sidecar-stderr.log")).ok()?;
    // Bounded: only the last 64 KiB are ever decoded.
    let start = bytes.len().saturating_sub(64 * 1024);
    let text = String::from_utf8_lossy(&bytes[start..]);
    let lines: Vec<String> = text.lines().filter(|l| !l.trim().is_empty()).map(redact).collect();
    Some(lines[lines.len().saturating_sub(n)..].to_vec())
}

#[cfg(test)] mod tests {
    use super::*;
    fn clock() -> impl Fn(u64) -> Instant { let base = Instant::now(); move |s| base + Duration::from_secs(s) }
    #[test] fn exit_classification() {
        assert_eq!(classify(0), ExitKind::Normal);
        assert_eq!(classify(74), ExitKind::CleanupFailed, "74 follows the shutdown flow, never restarts");
        for code in [1, 2, 4, 0xFFFF_FFFF, FORCE_RESTART_CODE] { assert_eq!(classify(code), ExitKind::Crash); }
    }
    #[test] fn restart_limit_is_three_per_five_minutes_then_stops_for_good() {
        let t = clock(); let mut p = RestartPolicy::default();
        assert_eq!(p.on_crash(t(0)), Restart::Allowed { attempt: 1 });
        assert_eq!(p.on_crash(t(100)), Restart::Allowed { attempt: 2 });
        assert_eq!(p.on_crash(t(200)), Restart::Allowed { attempt: 3 });
        assert_eq!(p.on_crash(t(299)), Restart::Exhausted);
        assert_eq!(p.on_crash(t(10_000)), Restart::Exhausted, "after giving up the shell never relaunches");
        // Old restarts age out of the 5-minute window.
        let mut p = RestartPolicy::default();
        for s in [0, 10, 20] { assert!(matches!(p.on_crash(t(s)), Restart::Allowed{..})); }
        assert_eq!(p.on_crash(t(300)), Restart::Allowed { attempt: 3 });
        assert_eq!(p.on_crash(t(301)), Restart::Exhausted);
    }
    #[test] fn pong_timeout_only_raises_a_banner_and_resume_resets() {
        let t = clock(); let mut w = Watchdog::new(t(0), 0, PONG_TIMEOUT);
        for s in 1..=15 { assert_eq!(w.tick(t(s), 0), None, "{s}"); }
        assert_eq!(w.tick(t(16), 0), Some(Watch::Unresponsive));
        assert_eq!(w.tick(t(17), 0), None, "one banner, no repeated actions, no kill");
        assert_eq!(w.pong(t(18)), Some(Watch::Recovered));
        assert_eq!(w.pong(t(19)), None);
        // PBT_APMRESUMEAUTOMATIC: the count restarts from the resume.
        let mut w = Watchdog::new(t(0), 0, PONG_TIMEOUT);
        for s in 1..=14 { w.tick(t(s), 0); }
        assert_eq!(w.tick(t(15), 1), None);
        for s in 16..=30 { assert_eq!(w.tick(t(s), 1), None, "{s}"); }
        assert_eq!(w.tick(t(31), 1), Some(Watch::Unresponsive));
        // A suspended loop (gap > 5 s) is not counted as sidecar silence.
        let mut w = Watchdog::new(t(0), 0, PONG_TIMEOUT);
        assert_eq!(w.tick(t(40), 0), None);
        assert_eq!(w.tick(t(41), 0), None);
    }
    #[test] fn banners_are_host_frames() {
        let b = banner::crashed("restarting", Some(1), 1);
        assert_eq!((b["t"].as_str(), b["kind"].as_str(), b["limit"].as_u64()), (Some("host:banner"), Some("sidecar_crashed"), Some(3)));
        assert_eq!(banner::unresponsive(true)["actions"], json!(["force_restart"]));
        assert_eq!(banner::unresponsive(false)["active"], false);
        // `restarted` is a one-shot notice; only restarting/unrecoverable stay active for replay.
        assert_eq!(banner::crashed("restarting", Some(3), 1)["active"], true);
        assert_eq!(banner::crashed("restarted", Some(3), 1)["active"], false);
        assert_eq!(banner::crashed("unrecoverable", Some(3), 3)["active"], true);
    }
    #[test] fn prehello_wording_separates_exit_from_handshake_failures() {
        for error in ["sidecar_start_failed_hello_timeout", "sidecar_start_timeout", "sidecar_start_failed_hello_contract_mismatch", "sidecar_start_failed_epoch_missing"] {
            assert_eq!(prehello_kind(error), PreHello::Handshake, "{error}");
        }
        for error in ["sidecar_start_failed_identity_mismatch", "sidecar_start_failed_os_2", "sidecar_start_failed_child_wait_handle_failed"] {
            assert_eq!(prehello_kind(error), PreHello::Exited, "{error}");
        }
    }
    /// P2 rule: `bridge_vocab.py` is the one definition of secret fields. Every name it lists is
    /// either value-masked here or exempt for a written reason; a new vocab entry fails this test.
    #[test] fn secret_keys_cover_bridge_vocab() {
        let vocab = include_str!("../../../bridge_vocab.py");
        let start = vocab.find("SECRET_RECORDS = MappingProxyType({").expect("SECRET_RECORDS block");
        let block = &vocab[start..start + vocab[start..].find("\n})").expect("block end")];
        let names: Vec<&str> = block.lines().skip(1).filter_map(|l| l.trim().strip_prefix('"')).filter_map(|l| l.split('"').next()).collect();
        assert!(names.len() >= 30 && names.contains(&"password") && names.contains(&"preshared_key"), "{names:?}");
        for name in names {
            let exempt = VOCAB_EXEMPT.iter().any(|(n, _)| *n == name);
            assert!(secret_key(name) != exempt, "{name}: masked={} exempt={exempt}", secret_key(name));
        }
        for extra in ["obfs-password", "pre-shared-key", "psk", "url", "subscription", "x-api-key", "auth_token", "proxy-authorization"] {
            assert!(secret_key(extra), "{extra}");
        }
    }
    #[test] fn stderr_tail_is_redacted_and_bounded() {
        let line = "GET https://user:pa55@sub.example.net/api/v1?token=abc#x password=hunter2 Authorization:Bearer tok3nvalue uuid=1234 aGVsbG8gd29ybGQgdGhpcyBpczEyMzQ1Ng== ok";
        let r = redact(line);
        assert!(r.contains("https://***@sub.example.net/***?***"), "{r}");
        for secret in ["pa55", "hunter2", "abc", "aGVsbG8", "1234", "tok3nvalue"] { assert!(!r.contains(secret), "{secret} in {r}"); }
        assert!(r.contains("password=***") && r.contains("uuid=***") && r.contains("Authorization:Bearer ***") && r.ends_with(" ok"), "{r}");
        // Review MUST-1 probes: every one of these used to pass through unchanged.
        // Split so the source never carries a whole token literal (public-release audit).
        let jwt = concat!("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", ".eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U");
        let probes: [(String, &[&str]); 9] = [
            ("yaml error at line 3: password: hunter2".into(), &["hunter2"]),
            ("{'password': 'hunter2', 'uuid': 'b831381d-6324-4d53-ad4f-8cda48b30811'}".into(), &["hunter2", "b831381d", "8cda48b30811"]),
            ("{\"token\": \"abcdef\", \"obfs-password\": \"qwerty\"}".into(), &["abcdef", "qwerty"]),
            ("vmess://eyJ2IjoiMiIsInBzIjoiZGVtbyIsImFkZCI6IjE5Mi4wLjIuNCJ9 failed".into(), &["eyJ2", "ZGVtbyIs"]),
            ("ssr://c2VydmVyOjQ0MzphdXRoX2FlczEyOF9tZDU6YWVzLTI1Ni1jZmI6dGxzMS4yX3RpY2tldF9hdXRoOmNHRnpjdw".into(), &["c2VydmVy", "cGFzcw", "cGFzc3"]),
            (format!("Authorization: Bearer {jwt}"), &["eyJhbGci", "dozjg", "eyJzdWIi"]),
            ("header Authorization: Basic dXNlcjpw".into(), &["dXNlcjpw"]),
            (format!("token={jwt}; next"), &["eyJhbGci"]),
            ("trojan://pw@203.0.113.4:443#name hy2://secretpass@h.example:443 opaque://QWxhZGRpbjpvcGVuIHNlc2FtZQ".into(), &["pw@", "203.0.113.4", "secretpass", "QWxhZGRpbjpv"]),
        ];
        for (input, secrets) in &probes {
            let out = redact(input);
            for secret in *secrets { assert!(!out.contains(secret), "{secret} leaked: {input} -> {out}"); }
        }
        assert_eq!(redact("yaml error at line 3: password: hunter2"), "yaml error at line 3: password: ***");
        assert_eq!(redact("{'password': 'hunter2', 'uuid': 'b831381d-6324'}"), "{'password': '***', 'uuid': '***'}");
        assert_eq!(redact("{\"token\": \"abcdef\"}"), "{\"token\": \"***\"}");
        assert_eq!(redact("vmess://eyJ2IjoiMiJ9 x"), "vmess://*** x");
        assert_eq!(redact("password = hunter2 ok"), "password = *** ok", "a lone separator between key and value");
        assert_eq!(redact(&format!("Authorization: Bearer {jwt}")), "Authorization: Bearer ***");
        assert_eq!(redact(jwt), "***", "a bare JWT is an opaque token");
        // Review round-2 MUST-1: credentials in a URL path (bot token, webhook uuid, device key,
        // subscription token). Any non-root path becomes `/***`, as `_redacted_history_url` does.
        let path_probes: [(&str, &str, &[&str]); 4] = [
            ("POST https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage failed",
                "POST https://api.telegram.org/*** failed", &["123456789", "AAHdq", "sendMessage"]),
            ("POST https://open.feishu.cn/open-apis/bot/v2/hook/b831381d-6324-4d53-ad4f-8cda48b30811 503",
                "POST https://open.feishu.cn/*** 503", &["b831381d", "8cda48b30811", "hook"]),
            ("GET https://api.day.app/Xk2pQ7mN9vR4tY8w/title?group=t",
                "GET https://api.day.app/***?***", &["Xk2pQ7mN9vR4tY8w", "title", "group"]),
            ("fetch 'https://sub.example.com/api/v1/client/subscribe/0123456789abcdef0123456789abcdef', timeout",
                "fetch 'https://sub.example.com/***', timeout", &["0123456789abcdef", "subscribe"]),
        ];
        for (input, expected, secrets) in &path_probes {
            let out = redact(input);
            for secret in *secrets { assert!(!out.contains(secret), "{secret} leaked: {input} -> {out}"); }
            assert_eq!(out, *expected);
        }
        assert_eq!(redact("https://example.com/docs/page"), "https://example.com/***");
        assert_eq!(redact("(see https://example.com/a/b)."), "(see https://example.com/***).");
        // Ordinary diagnostics stay readable.
        for plain in ["File \"C:\\app\\bridge_main.py\", line 42, in main", "ValueError: invalid literal for int() with base 10: 'x'",
            "https://example.com", "https://example.com/", "connect https://api.example.com:8443/ failed",
            "version 1.2.3 of sub.example.net", "KeyError: 'service'"] {
            assert_eq!(redact(plain), plain);
        }
        assert!(redact(&"z".repeat(500)).encode_utf16().count() <= 200);
        assert_eq!(redact("Traceback (most recent call last):"), "Traceback (most recent call last):");
        assert_eq!(redact("bad\u{1b}[31mansi"), "bad [31mansi");
        let root = std::env::temp_dir().join(format!("tesseract-tail-{}-{}", std::process::id(), std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        struct Cleanup(std::path::PathBuf);
        impl Drop for Cleanup { fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); } }
        let _c = Cleanup(root.clone());
        assert!(stderr_tail(&root, TAIL_LINES).is_none());
        std::fs::create_dir_all(root.join("logs")).unwrap();
        let body: String = (0..30).map(|n| format!("line {n} secret=s{n}\r\n\r\n")).collect();
        std::fs::write(root.join("logs/sidecar-stderr.log"), body).unwrap();
        let tail = stderr_tail(&root, TAIL_LINES).unwrap();
        assert_eq!(tail.len(), 20); assert_eq!(tail[0], "line 10 secret=***"); assert_eq!(tail[19], "line 29 secret=***");
    }
}
