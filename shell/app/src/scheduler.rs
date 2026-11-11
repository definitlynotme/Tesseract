//! One host-owned overlapped write; independent, bounded control scheduling.
use serde_json::Value;
use std::{collections::VecDeque, sync::mpsc::{Receiver,TryRecvError}};
use crate::{Control,current_frame};
pub struct SendQueue {
    handshake: VecDeque<Value>, control: VecDeque<Value>, ordinary: VecDeque<Value>, ping: Option<Value>,
    /// T-P3-05 shell commands: `alerts.desktopDelivered` receipts (bounded FIFO) and the latest
    /// `ui.presence` (a newer value replaces an unsent one). After control, before page work.
    shell: VecDeque<Value>, presence: Option<Value>,
}
/// Receipts waiting for the pipe; the sidecar's own pending table holds 64 notices.
pub const SHELL_CAPACITY: usize = 64;
impl SendQueue {
    pub fn new<const N: usize>(handshake: [Value;N]) -> Self {
        Self {handshake:handshake.into(),control:VecDeque::new(),ordinary:VecDeque::new(),ping:None,shell:VecDeque::new(),presence:None}
    }
    pub fn pump(&mut self, ordinary: &Receiver<Control>, control: &Receiver<Control>, epoch: &str) -> bool {
        // Never condition control intake on ordinary capacity; preserve FIFO within its domain.
        while self.control.len()<32 {
            match control.try_recv() {
                Ok(c)=>if let Some(v)=current_frame(c,epoch) {self.control.push_back(v);},
                Err(_)=>break,
            }
        }
        while self.ordinary.len()<64 {
            match ordinary.try_recv() {
                Ok(c)=>if let Some(v)=current_frame(c,epoch) {self.ordinary.push_back(v);},
                Err(TryRecvError::Disconnected)=>return true,
                Err(TryRecvError::Empty)=>break,
            }
        }
        false
    }
    pub fn ping(&mut self, frame:Value) {self.ping=Some(frame);}
    /// False when the receipt lane is full (the caller logs). Nothing redelivers that notice:
    /// a runtime alert just expires unreceipted in the sidecar; an update notice is offered
    /// again at the next start.
    pub fn shell(&mut self, frame:Value) -> bool {
        if self.shell.len()>=SHELL_CAPACITY {return false;}
        self.shell.push_back(frame); true
    }
    /// Returns the request id of an unsent presence value this one replaced.
    pub fn presence(&mut self, frame:Value) -> Option<u64> {
        self.presence.replace(frame).and_then(|old|old["id"].as_u64())
    }
    pub fn quit(&mut self, frame:Value) {
        self.ordinary.clear(); self.control.clear(); self.ping=None; self.shell.clear(); self.presence=None; self.control.push_back(frame);
    }
    /// `app.sessionEnd`: ahead of every queued control frame (only the handshake precedes it);
    /// page work and the heartbeat are dropped as for a quit.
    pub fn urgent(&mut self, frame:Value) {
        self.ordinary.clear(); self.ping=None; self.shell.clear(); self.presence=None; self.control.push_front(frame);
    }
    pub fn next(&mut self) -> Option<Value> {
        self.handshake.pop_front().or_else(||self.control.pop_front())
            .or_else(||self.shell.pop_front()).or_else(||self.presence.take())
            .or_else(||self.ping.take()).or_else(||self.ordinary.pop_front())
    }
}
#[cfg(test)] mod tests {
    use super::*;
    use serde_json::json;
    use std::{sync::mpsc,time::{Duration,Instant}};
    fn message(frame:Value)->Control {Control::Frame{epoch:"one".into(),frame}}
    #[test] fn full_queue_keeps_control_fifo_and_coalesces_ping() {
        let (tx,rx)=mpsc::sync_channel(64); let (ctx,crx)=mpsc::sync_channel(16);
        let mut q=SendQueue::new([json!({"t":"hello_ack"}),json!({"t":"subscribe"})]);
        for n in 0..64 {tx.send(message(json!({"cmd":"service.start","id":n}))).unwrap();}
        q.pump(&rx,&crx,"one"); assert_eq!(q.ordinary.len(),64);
        ctx.send(message(json!({"cmd":"service.stop"}))).unwrap();
        ctx.send(message(json!({"t":"cancel"}))).unwrap();
        for n in 0..10000 {q.ping(json!({"t":"ping","n":n}));}
        q.pump(&rx,&crx,"one"); assert_eq!(q.control.len(),2); assert_eq!(q.ordinary.len(),64);
        assert_eq!(q.next().unwrap()["t"],"hello_ack"); assert_eq!(q.next().unwrap()["t"],"subscribe");
        assert_eq!(q.next().unwrap()["cmd"],"service.stop"); assert_eq!(q.next().unwrap()["t"],"cancel");
        assert_eq!(q.next().unwrap()["n"],9999); assert!(q.ping.is_none());
        assert_eq!(q.next().unwrap()["id"],0);
    }
    #[test] fn session_end_jumps_every_queue_but_the_handshake() {
        let (tx,rx)=mpsc::sync_channel(64); let (ctx,crx)=mpsc::sync_channel(16);
        let mut q=SendQueue::new([json!({"t":"hello_ack"})]);
        for n in 0..64 {tx.send(message(json!({"cmd":"service.start","id":n}))).unwrap();}
        ctx.send(message(json!({"cmd":"service.stop"}))).unwrap();
        q.pump(&rx,&crx,"one"); q.ping(json!({"t":"ping","n":1}));
        q.urgent(json!({"cmd":"app.sessionEnd"}));
        assert_eq!(q.next().unwrap()["t"],"hello_ack");
        assert_eq!(q.next().unwrap()["cmd"],"app.sessionEnd");
        assert_eq!(q.next().unwrap()["cmd"],"service.stop");
        assert!(q.next().is_none(),"page work and the heartbeat are dropped");
    }
    #[test] fn shell_lane_after_control_before_ping_and_page_work() {
        let (tx,rx)=mpsc::sync_channel(64); let (ctx,crx)=mpsc::sync_channel(16);
        let mut q=SendQueue::new([json!({"t":"hello_ack"})]);
        tx.send(message(json!({"cmd":"service.start","id":1}))).unwrap();
        ctx.send(message(json!({"cmd":"service.stop"}))).unwrap();
        q.pump(&rx,&crx,"one"); q.ping(json!({"t":"ping","n":1}));
        assert!(q.shell(json!({"cmd":"alerts.desktopDelivered","id":5})));
        assert_eq!(q.presence(json!({"cmd":"ui.presence","id":6})),None);
        assert_eq!(q.presence(json!({"cmd":"ui.presence","id":7})),Some(6),"an unsent presence is replaced, not queued");
        let order:Vec<Value>=std::iter::from_fn(||q.next()).collect();
        let names:Vec<&str>=order.iter().map(|f|f["cmd"].as_str().or(f["t"].as_str()).unwrap()).collect();
        assert_eq!(names,["hello_ack","service.stop","alerts.desktopDelivered","ui.presence","ping","service.start"]);
        assert_eq!(order[3]["id"],7);
        for n in 0..SHELL_CAPACITY {assert!(q.shell(json!({"id":n})));}
        assert!(!q.shell(json!({"id":"over"})),"bounded");
        q.presence(json!({"cmd":"ui.presence"}));
        q.quit(json!({"cmd":"app.quit"}));
        assert_eq!(q.next().unwrap()["cmd"],"app.quit"); assert!(q.next().is_none(),"a quit drops pending shell reports");
    }
    #[test] fn stopped_pipe_resumes_inflight_then_stop_cancel_before_full_normal_queue() {
        use bridge_host::{PrivatePipe,Event,MAX_FRAME};
        let exe=std::env::current_exe().unwrap().parent().unwrap().parent().unwrap().join("echo_sidecar.exe");
        let mut host=PrivatePipe::create().unwrap().launch(&exe,&["--pause-read".into()]).unwrap();
        let mut inflight=vec![b'x';MAX_FRAME-1]; inflight.push(b'\n');
        assert!(host.send(&inflight).unwrap()); assert!(!host.poll_write().unwrap());
        let (tx,rx)=mpsc::sync_channel(64); let (ctx,crx)=mpsc::sync_channel(16);
        let mut q=SendQueue::new([]);
        for n in 0..64 {tx.send(message(json!({"cmd":"service.start","id":n}))).unwrap();}
        q.pump(&rx,&crx,"one");
        ctx.send(message(json!({"cmd":"service.stop"}))).unwrap(); ctx.send(message(json!({"t":"cancel"}))).unwrap();
        for n in 0..100 {q.ping(json!({"t":"ping","n":n})); q.pump(&rx,&crx,"one");}
        let deadline=Instant::now()+Duration::from_secs(15); let mut sent=0; let mut received=Vec::new();
        while Instant::now()<deadline && received.len()<5 {
            if host.poll_write().unwrap() && sent<4 {
                let mut bytes=serde_json::to_vec(&q.next().unwrap()).unwrap(); bytes.push(b'\n');
                assert!(host.send(&bytes).unwrap()); sent+=1;
            }
            for event in host.poll_event().unwrap() {if let Event::Frame(line)=event {received.push(line);}}
            std::thread::sleep(Duration::from_millis(2));
        }
        assert_eq!(received.len(),5); assert_eq!(received[0].as_bytes(),inflight);
        let frames:Vec<Value>=received[1..].iter().map(|s|serde_json::from_str(s).unwrap()).collect();
        assert_eq!(frames[0]["cmd"],"service.stop"); assert_eq!(frames[1]["t"],"cancel");
        assert_eq!(frames[2]["n"],99); assert_eq!(frames[3]["id"],0);
    }
}
