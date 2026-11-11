//! Explicit test harness: isolated scope, scripted requests, sanitized JSONL.
use bridge_host::{BridgeHost, Event, PrivatePipe};
use serde_json::{json, Value};
use std::{env, fs::File, io::{self, Write}, path::Path, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};

fn send(host: &mut BridgeHost, value: Value) -> io::Result<()> {
    let mut line = serde_json::to_vec(&value)?;
    line.push(b'\n');
    let deadline = Instant::now() + Duration::from_secs(5);
    while !host.poll_write()? {
        if Instant::now() > deadline { return Err(io::Error::other("write_timeout")); }
        std::thread::sleep(Duration::from_millis(2));
    }
    if !host.send(&line)? { return Err(io::Error::other("write_busy")); }
    Ok(())
}
fn stamp(mut value: Value, epoch: &str) -> Value {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64;
    value["epoch"] = json!(epoch);
    value["produced_at_ms"] = json!(now);
    value["sent_at_ms"] = json!(now);
    value
}
fn run() -> io::Result<()> {
    let args: Vec<String> = env::args().skip(1).collect();
    let value = |key: &str| args.windows(2).find(|v| v[0] == key).map(|v| v[1].clone())
        .ok_or_else(|| io::Error::other(format!("missing {key}")));
    if env::var("TESSERACT_INSTANCE_SCOPE").unwrap_or_default().is_empty() {
        return Err(io::Error::other("isolated_scope_required"));
    }
    let config = std::fs::canonicalize(env::var("TESSERACT_CONFIG_DIR").map_err(io::Error::other)?)?;
    let data = std::fs::canonicalize(env::var("TESSERACT_DATA_DIR").map_err(io::Error::other)?)?;
    let scope_id = bridge_host::scope::custom_id(&config.to_string_lossy(), &data.to_string_lossy())?;
    let exe = value("--exe")?;
    let child_args = if args.iter().any(|v| v == "--source") { vec![value("--entry")?] } else { vec![] };
    let script: Vec<String> = if args.iter().any(|v| v == "--script") {
        serde_json::from_slice(&std::fs::read(value("--script")?)?)?
    } else { vec!["service.start".into(), "service.stop".into(), "app.quit".into()] };
    if script.last().map(String::as_str) != Some("app.quit") || script.iter().any(|c| !["service.start", "service.stop", "app.quit"].contains(&c.as_str())) {
        return Err(io::Error::other("invalid_script"));
    }
    let mut output = File::create(value("--output")?)?;
    let mut host = PrivatePipe::create()?.launch(Path::new(&exe), &child_args)?;
    let hello = host.hello().clone();
    if hello["t"] != "hello" || hello["proto"] != 1 || hello["runtime_pid"] != host.child_pid()
        || hello["scope"]["kind"] != "custom" || hello["scope"]["id"] != scope_id {
        return Err(io::Error::other("hello_contract_mismatch"));
    }
    let epoch = hello["epoch"].as_str().ok_or_else(|| io::Error::other("epoch_missing"))?.to_owned();
    writeln!(output, "{hello}")?;
    send(&mut host, stamp(json!({"t":"hello_ack", "proto":1, "accepted":true}), &epoch))?;
    let topics = ["service.state", "route.published", "tiers.summary", "traffic.tick", "traffic.history", "core.info", "logs.batch", "jobs"];
    send(&mut host, stamp(json!({"t":"subscribe", "id":1, "topics":topics}), &epoch))?;
    let mut snapshots = std::collections::HashSet::new();
    let mut stage = 0;
    let mut active = false;
    let mut bye = false;
    let mut child_exit = None;
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        for event in host.poll_event()? {
            match event {
                Event::Frame(line) => {
                    let frame: Value = serde_json::from_str(&line)?;
                    if frame["epoch"] != epoch { return Err(io::Error::other("epoch_mismatch")); }
                    writeln!(output, "{frame}")?;
                    output.flush()?;
                    let items = if frame["t"] == "batch" { frame["items"].as_array().cloned().unwrap_or_default() } else { vec![frame.clone()] };
                    for item in items {
                        if item["t"] == "evt" && item["kind"] == "snapshot" {
                            if let Some(topic) = item["topic"].as_str() { snapshots.insert(topic.to_owned()); }
                        }
                        if item["topic"] == "jobs" && item["data"]["name"] == script.get(stage).map(String::as_str).unwrap_or("")
                            && ["done", "failed", "cancelled", "superseded"].contains(&item["data"]["state"].as_str().unwrap_or("")) {
                            stage += 1;
                            active = false;
                        }
                    }
                    if frame["t"] == "bye" { bye = true; }
                    if frame["t"] == "ack" && frame["accepted"] == false { return Err(io::Error::other("command_rejected")); }
                },
                Event::ChildExited(code) => {
                    // Process signaling may race the final pipe read. Consume
                    // every event from this poll before judging the bye receipt.
                    child_exit = Some(code);
                },
                Event::Disconnected if !bye => return Err(io::Error::other("unexpected_disconnect")),
                Event::Disconnected => (),
                _ => return Err(io::Error::other("invalid_frame")),
            }
        }
        if let Some(code) = child_exit {
            if bye {
                if code != 0 || stage != script.len() { return Err(io::Error::other(format!("unexpected_exit:{code}"))); }
                return Ok(());
            }
        }
        if snapshots.len() == 6 && !active && stage < script.len() {
            send(&mut host, stamp(json!({"t":"req", "id":stage+2, "cmd":script[stage], "args":{}, "from":"shell"}), &epoch))?;
            active = true;
        }
        if Instant::now() > deadline { return Err(io::Error::other("script_timeout")); }
        std::thread::sleep(Duration::from_millis(5));
    }
}
fn main() {
    if let Err(error) = run() { eprintln!("headless_shell: {error}"); std::process::exit(1); }
}
