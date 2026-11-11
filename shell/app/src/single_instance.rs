//! Win32 ownership is confined to the acquiring (UI) thread. Legacy is never waited on.
use std::{io, marker::PhantomData, path::{Path, PathBuf}, rc::Rc,
    sync::{Arc, atomic::{AtomicBool, Ordering}}, time::{Duration, Instant}};

type Handle = usize;
pub const ACCESS_DENIED: u32 = 5;
const NOT_FOUND: u32 = 2;
const ALREADY_EXISTS: u32 = 183;
const LIMIT: u32 = 30_000;
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Wait { Owned, Abandoned, Timeout }
pub trait Api: Clone {
    fn mutex(&self, name: &str) -> Result<(Handle, bool), u32>;
    fn open_mutex(&self, name: &str) -> Result<Option<Handle>, u32>;
    fn event(&self, name: &str, manual: bool) -> Result<Handle, u32>;
    fn open_event(&self, name: &str) -> Result<Option<Handle>, u32>;
    fn wait(&self, h: Handle, ms: u32) -> Result<Wait, u32>;
    fn set(&self, h: Handle) -> Result<(), u32>;
    fn reset(&self, h: Handle) -> Result<(), u32>;
    fn release(&self, h: Handle) -> Result<(), u32>;
    fn close(&self, h: Handle) -> Result<(), u32>;
    fn foreground(&self) -> Result<(), u32>;
    fn log(&self, code: &str);
    fn pause(&self) { std::thread::sleep(Duration::from_millis(10)); }
}
struct Object<A: Api> { api: A, handle: Handle, owned: bool, _thread: PhantomData<Rc<()>> }
impl<A: Api> Object<A> {
    fn new(api: &A, handle: Handle) -> Self { Self{api:api.clone(),handle,owned:false,_thread:PhantomData} }
    fn wait(&mut self, ms:u32) -> Result<Wait,u32> {
        let result=self.api.wait(self.handle,ms)?;
        if result != Wait::Timeout {self.owned=true;}
        if result == Wait::Abandoned {self.api.log("instance_mutex_abandoned");}
        Ok(result)
    }
    fn finish(mut self) -> Result<(),u32> {
        if self.owned {self.api.release(self.handle)?; self.owned=false;}
        self.api.close(self.handle)?; self.handle=0; Ok(())
    }
}
impl<A:Api> Drop for Object<A> {
    fn drop(&mut self) {
        if self.owned && self.api.release(self.handle).is_err() {self.api.log("instance_release_failed");}
        if self.handle!=0 && self.api.close(self.handle).is_err() {self.api.log("instance_close_failed");}
    }
}
pub struct Lease<A:Api> {
    // Drop events and legacy before releasing shell. All fields stay on this thread.
    restore:Object<A>, quitting:Object<A>, _legacy:Object<A>, _shell:Object<A>,
}
impl<A:Api> Lease<A> {
    pub fn mark_quitting(&self) -> Result<(),u32> { self.quitting.api.set(self.quitting.handle) }
}
pub enum Outcome<A:Api> { Start(Lease<A>), Exit(i32) }
#[derive(Debug, PartialEq)]
pub enum Failure { Win32(u32), QuittingTimeout, RuntimeTimeout }
impl From<u32> for Failure {fn from(e:u32)->Self {Self::Win32(e)}}
impl Failure {
    pub fn code(&self)->String {match self {
        Self::Win32(e)=>format!("instance_win32_error_{e}"),
        Self::QuittingTimeout=>"instance_quitting_timeout".into(),
        Self::RuntimeTimeout=>"instance_runtime_timeout".into(),
    }}
}
fn remaining(deadline:Instant)->u32 {deadline.saturating_duration_since(Instant::now()).as_millis().min(LIMIT as u128) as u32}
fn signal<A:Api>(api:&A,names:&serde_json::Value)->Result<(),Failure> {
    // The winner may still be between the legacy gate and event creation.
    let deadline=Instant::now()+Duration::from_secs(5);
    let event=loop {
        if let Some(h)=api.open_event(names["restore_event"].as_str().unwrap())? {break Object::new(api,h);}
        if remaining(deadline)==0 {return Err(Failure::Win32(NOT_FOUND));}
        api.pause();
    };
    api.set(event.handle)?; event.finish()?; Ok(())
}
/// `cli` is decided before parsing scope directories or querying Python.
pub fn acquire<A:Api>(api:A,names:&serde_json::Value,cli:bool)->Result<Outcome<A>,Failure> {
    if cli {return Ok(Outcome::Exit(0));}
    let name=|key:&str| names[key].as_str().unwrap();
    let shell_deadline=Instant::now()+Duration::from_millis(LIMIT.into());
    let shell=loop {
        let (h,_)=api.mutex(name("shell_mutex"))?; let mut shell=Object::new(&api,h);
        if shell.wait(0)?!=Wait::Timeout {break shell;}
        let quitting=api.open_event(name("quitting_event"))?.map(|h|Object::new(&api,h));
        let is_quitting=match &quitting {Some(e)=>api.wait(e.handle,0)?!=Wait::Timeout,None=>false};
        if let Some(event)=quitting {event.finish()?;}
        if !is_quitting {
            signal(&api,names)?;
            // The restore signal is already delivered. Windows refuses foreground
            // rights to launches that do not own the foreground (autostart, other
            // apps), so a refusal is logged, never shown as an instance failure.
            if let Err(code)=api.foreground() {api.log(&format!("instance_foreground_not_granted_{code}"));}
            shell.finish()?; return Ok(Outcome::Exit(0));
        }
        api.log("instance_waiting_for_quitting_shell");
        let ms=remaining(shell_deadline);
        if ms==0 || shell.wait(ms)?==Wait::Timeout {return Err(Failure::QuittingTimeout);}
        // Wait acquired ownership; release it before retry, never recurse ownership.
        shell.finish()?;
    };
    let runtime_deadline=Instant::now()+Duration::from_millis(LIMIT.into());
    let mut draining=false;
    let legacy=loop {
        let (h,exists)=api.mutex(name("legacy_mutex"))?; let gate=Object::new(&api,h);
        if !exists {break gate;}
        let runtime=api.open_mutex(name("runtime_mutex"))?.map(|h|Object::new(&api,h));
        if let Some(mut runtime)=runtime {
            if runtime.wait(0)?==Wait::Timeout {
                api.log("instance_waiting_for_runtime_cleanup");
                draining=true; gate.finish()?;
                let ms=remaining(runtime_deadline);
                if ms==0 || runtime.wait(ms)?==Wait::Timeout {return Err(Failure::RuntimeTimeout);}
                runtime.finish()?; continue;
            }
            runtime.finish()?;
        }
        gate.finish()?;
        // Runtime release precedes Python's last legacy close. Do not misidentify
        // that tail window as Qt, and do not keep our own gate handle alive.
        if draining {
            if remaining(runtime_deadline)==0 {return Err(Failure::RuntimeTimeout);}
            api.pause(); continue;
        }
        signal(&api,names)?; shell.finish()?; return Ok(Outcome::Exit(0));
    };
    let quitting=Object::new(&api,api.event(name("quitting_event"),true)?);
    api.reset(quitting.handle)?; // Only the new shell owner clears a prior generation.
    let restore=Object::new(&api,api.event(name("restore_event"),false)?);
    Ok(Outcome::Start(Lease{restore,quitting,_legacy:legacy,_shell:shell}))
}

// Small raw FFI boundary: no new dependency/features or lockfile changes.
#[link(name="kernel32")]
extern "system" {
    fn CreateMutexW(attrs:*const u8,owner:i32,name:*const u16)->Handle;
    fn OpenMutexW(access:u32,inherit:i32,name:*const u16)->Handle;
    fn CreateEventW(attrs:*const u8,manual:i32,initial:i32,name:*const u16)->Handle;
    fn OpenEventW(access:u32,inherit:i32,name:*const u16)->Handle;
    fn WaitForSingleObject(handle:Handle,ms:u32)->u32;
    fn ReleaseMutex(handle:Handle)->i32;
    fn CloseHandle(handle:Handle)->i32;
    fn SetEvent(handle:Handle)->i32;
    fn ResetEvent(handle:Handle)->i32;
    fn GetLastError()->u32;
    fn GetCurrentProcess()->Handle;
}
#[link(name="advapi32")]
extern "system" {
    fn OpenProcessToken(process:Handle,access:u32,token:*mut Handle)->i32;
    fn GetTokenInformation(token:Handle,class:u32,out:*mut u32,len:u32,returned:*mut u32)->i32;
}
#[link(name="user32")]
extern "system" {fn AllowSetForegroundWindow(pid:u32)->i32;}
fn wide(s:&str)->Vec<u16> {s.encode_utf16().chain(Some(0)).collect()}
fn ok(v:i32)->Result<(),u32> {if v!=0 {Ok(())} else {Err(unsafe{GetLastError()})}}
fn opened(h:Handle)->Result<Option<Handle>,u32> {
    if h!=0 {Ok(Some(h))} else {let e=unsafe{GetLastError()}; if e==NOT_FOUND {Ok(None)} else {Err(e)}}
}
#[derive(Clone)]
pub struct Win32 {pub data:PathBuf}
impl Api for Win32 {
    fn mutex(&self,name:&str)->Result<(Handle,bool),u32> {unsafe {
        let h=CreateMutexW(std::ptr::null(),0,wide(name).as_ptr()); let e=GetLastError();
        if h==0 {Err(e)} else if e==0 || e==ALREADY_EXISTS {Ok((h,e==ALREADY_EXISTS))}
        else {CloseHandle(h);Err(e)}
    }}
    fn open_mutex(&self,name:&str)->Result<Option<Handle>,u32> {opened(unsafe{OpenMutexW(0x00100001,0,wide(name).as_ptr())})}
    fn event(&self,name:&str,manual:bool)->Result<Handle,u32> {unsafe {
        let h=CreateEventW(std::ptr::null(),manual.into(),0,wide(name).as_ptr());
        if h==0 {Err(GetLastError())} else {Ok(h)}
    }}
    fn open_event(&self,name:&str)->Result<Option<Handle>,u32> {opened(unsafe{OpenEventW(0x00100002,0,wide(name).as_ptr())})}
    fn wait(&self,h:Handle,ms:u32)->Result<Wait,u32> {match unsafe{WaitForSingleObject(h,ms)} {
        0=>Ok(Wait::Owned),0x80=>Ok(Wait::Abandoned),0x102=>Ok(Wait::Timeout),
        0xffffffff=>Err(unsafe{GetLastError()}),other=>Err(other),
    }}
    fn set(&self,h:Handle)->Result<(),u32>{ok(unsafe{SetEvent(h)})}
    fn reset(&self,h:Handle)->Result<(),u32>{ok(unsafe{ResetEvent(h)})}
    fn release(&self,h:Handle)->Result<(),u32>{ok(unsafe{ReleaseMutex(h)})}
    fn close(&self,h:Handle)->Result<(),u32>{ok(unsafe{CloseHandle(h)})}
    fn foreground(&self)->Result<(),u32>{ok(unsafe{AllowSetForegroundWindow(u32::MAX)})}
    fn log(&self,code:&str){crate::log_code(&self.data,code);}
}
/// Elevation, not merely administrator-group membership, selects the profile.
pub fn elevated()->io::Result<bool> {unsafe {
    let mut token=0; ok(OpenProcessToken(GetCurrentProcess(),8,&mut token)).map_err(|e|io::Error::from_raw_os_error(e as i32))?;
    let mut value=0;let mut returned=0;
    let result=ok(GetTokenInformation(token,20,&mut value,4,&mut returned));
    let closed=ok(CloseHandle(token));
    result.and(closed).map_err(|e|io::Error::from_raw_os_error(e as i32))?; Ok(value!=0)
}}
pub fn profile(data:&Path,id:&str,admin:bool)->PathBuf {data.join(if admin {"WebView2-admin"} else {"WebView2"}).join(id)}

/// Cloneable marker for P3-03/P3-04. Owns a separate event handle, never a mutex.
pub struct Quitting {api:Win32,handle:Handle}
impl Quitting {
    pub fn new(api:Win32,names:&serde_json::Value)->Result<Self,u32> {
        let handle=api.open_event(names["quitting_event"].as_str().unwrap())?.ok_or(NOT_FOUND)?;
        Ok(Self{api,handle})
    }
    pub fn mark_quitting(&self)->Result<(),u32>{self.api.set(self.handle)}
    /// P3-04: the user cancelled the quit at the unsaved-changes prompt.
    pub fn clear(&self)->Result<(),u32>{self.api.reset(self.handle)}
}
impl Drop for Quitting {fn drop(&mut self){if self.api.close(self.handle).is_err(){self.api.log("instance_close_failed");}}}

pub struct RestoreListener {stop:Arc<AtomicBool>,thread:Option<std::thread::JoinHandle<()>>}
impl RestoreListener {
    pub fn start(lease:&Lease<Win32>,app:tauri::AppHandle)->Result<Self,u32> {
        use tauri::Manager;
        // Independently opened handle belongs to listener, closed after its wait loop.
        let api=lease.restore.api.clone();
        let scope=app.state::<ScopeNames>();
        let h=api.open_event(scope.0["restore_event"].as_str().unwrap())?.ok_or(NOT_FOUND)?;
        let stop=Arc::new(AtomicBool::new(false)); let flag=stop.clone();
        let thread=std::thread::spawn(move || {
            while !flag.load(Ordering::Acquire) {
                match api.wait(h,200) {
                    Ok(Wait::Owned)=>{
                        api.log("restore_event_received"); let handle=app.clone(); let data=api.data.clone();
                        if app.run_on_main_thread(move || {
                            if let Some(window)=handle.get_webview_window("main") {
                                if window.show().and_then(|_|window.unminimize()).and_then(|_|window.set_focus()).is_err() {
                                    crate::log_code(&data,"restore_window_failed");
                                }
                            }
                        }).is_err() {api.log("restore_dispatch_failed");}
                    },
                    Ok(Wait::Timeout)=>(),
                    _=>{api.log("restore_wait_failed");break;},
                }
            }
            if api.close(h).is_err(){api.log("instance_close_failed");}
        });
        Ok(Self{stop,thread:Some(thread)})
    }
}
impl Drop for RestoreListener {fn drop(&mut self){self.stop.store(true,Ordering::Release);if let Some(t)=self.thread.take(){let _=t.join();}}}
pub struct ScopeNames(pub serde_json::Value);

#[cfg(test)] mod tests {
    use super::*;
    use std::{cell::RefCell,collections::{HashMap,VecDeque}};
    #[derive(Clone,Debug)] enum Reply {Create(bool),Open(bool),Wait(Wait),Unit,Error(u32)}
    #[derive(Default)] struct Trace {
        steps:VecDeque<(String,Reply)>,handles:HashMap<Handle,(String,bool)>,next:usize,
        sets:Vec<String>,logs:Vec<String>,opens:usize,releases:usize,
    }
    #[derive(Clone,Default)] struct Fake(Rc<RefCell<Trace>>);
    fn suffix(name:&str)->&str {name.rsplit('-').next().unwrap()}
    impl Fake {
        fn new(steps:Vec<(&str,Reply)>)->Self {
            let fake=Self::default();fake.0.borrow_mut().steps=steps.into_iter().map(|(s,r)|(s.into(),r)).collect();fake
        }
        fn call(&self,operation:String)->Result<Reply,u32> {
            let (expected,reply)=self.0.borrow_mut().steps.pop_front().unwrap_or_else(||panic!("unexpected {operation}"));
            assert_eq!(operation,expected);match reply {Reply::Error(e)=>Err(e),r=>Ok(r)}
        }
        fn handle(&self,name:&str)->Handle {
            let mut trace=self.0.borrow_mut();trace.next+=1;let h=trace.next;
            trace.handles.insert(h,(suffix(name).into(),false));trace.opens+=1;h
        }
        fn named(&self,h:Handle)->String {self.0.borrow().handles[&h].0.clone()}
    }
    impl Api for Fake {
        fn mutex(&self,n:&str)->Result<(Handle,bool),u32> {
            let Reply::Create(exists)=self.call(format!("mutex:{}",suffix(n)))? else {panic!()}; Ok((self.handle(n),exists))
        }
        fn open_mutex(&self,n:&str)->Result<Option<Handle>,u32> {
            let Reply::Open(exists)=self.call(format!("open:{}",suffix(n)))? else {panic!()};Ok(exists.then(||self.handle(n)))
        }
        fn event(&self,n:&str,manual:bool)->Result<Handle,u32> {
            self.call(format!("event:{}:{manual}",suffix(n)))?; Ok(self.handle(n))
        }
        fn open_event(&self,n:&str)->Result<Option<Handle>,u32> {self.open_mutex(n)}
        fn wait(&self,h:Handle,ms:u32)->Result<Wait,u32> {
            let n=self.named(h);
            let Reply::Wait(w)=self.call(format!("wait:{n}:{}",if ms==0 {"probe"} else {"bounded"}))? else {panic!()};
            if ["shell","runtime"].contains(&n.as_str()) && w!=Wait::Timeout {
                let mut trace=self.0.borrow_mut();assert!(!trace.handles[&h].1,"recursive ownership");trace.handles.get_mut(&h).unwrap().1=true;
            } Ok(w)
        }
        fn set(&self,h:Handle)->Result<(),u32>{let n=self.named(h);self.call(format!("set:{n}"))?;self.0.borrow_mut().sets.push(n);Ok(())}
        fn reset(&self,h:Handle)->Result<(),u32>{self.call(format!("reset:{}",self.named(h)))?;Ok(())}
        fn release(&self,h:Handle)->Result<(),u32>{
            let mut trace=self.0.borrow_mut();assert!(trace.handles[&h].1,"released unowned/legacy");
            trace.handles.get_mut(&h).unwrap().1=false;trace.releases+=1;Ok(())
        }
        fn close(&self,h:Handle)->Result<(),u32>{let entry=self.0.borrow_mut().handles.remove(&h).unwrap();assert!(!entry.1,"closed owned mutex");Ok(())}
        fn foreground(&self)->Result<(),u32>{self.call("foreground".into())?;Ok(())}
        fn log(&self,c:&str){self.0.borrow_mut().logs.push(c.into());}
        fn pause(&self){}
    }
    fn start()->Vec<(&'static str,Reply)> {vec![
        ("mutex:shell",Reply::Create(false)),("wait:shell:probe",Reply::Wait(Wait::Owned)),
        ("mutex:instance",Reply::Create(false)),("event:quitting:true",Reply::Unit),
        ("reset:quitting",Reply::Unit),("event:restore:false",Reply::Unit)]}
    fn restore()->Vec<(&'static str,Reply)> {vec![
        ("open:restore",Reply::Open(true)),("set:restore",Reply::Unit)]}
    fn occupied(quitting:bool)->Vec<(&'static str,Reply)> {vec![
        ("mutex:shell",Reply::Create(true)),("wait:shell:probe",Reply::Wait(Wait::Timeout)),
        ("open:quitting",Reply::Open(quitting))]}
    fn check(steps:Vec<(&str,Reply)>,exit:i32,set:bool,cli:bool)->Fake {
        let fake=Fake::new(steps);
        let names=bridge_host::scope::names("custom","0123456789abcdef01234567").unwrap();
        let result=acquire(fake.clone(),&names,cli);
        let code=match result {Ok(Outcome::Start(lease))=>{drop(lease);0},Ok(Outcome::Exit(code))=>code,Err(_)=>1};
        assert_eq!(code,exit);let trace=fake.0.borrow();
        assert_eq!(trace.sets.contains(&"restore".into()),set);assert!(trace.handles.is_empty(),"leaked handles");
        assert!(trace.steps.is_empty(),"unconsumed steps {:?}",trace.steps);drop(trace);fake
    }
    #[test] fn first_instance_and_profile_separation(){check(start(),0,false,false);assert_ne!(profile(Path::new("data"),"id",false),profile(Path::new("data"),"id",true));}
    #[test] fn second_shell_restores(){let mut s=occupied(false);s.extend(restore());s.push(("foreground",Reply::Unit));check(s,0,true,false);}
    #[test] fn second_shell_foreground_refusal_is_logged_after_signal(){
        for e in [ACCESS_DENIED,87] {
            let mut s=occupied(false);s.extend(restore());s.push(("foreground",Reply::Error(e)));
            let f=check(s,0,true,false);
            assert!(f.0.borrow().logs.contains(&format!("instance_foreground_not_granted_{e}")));
        }
    }
    #[test] fn quitting_wait_success_releases_before_retry(){
        let mut s=occupied(true);s.extend([("wait:quitting:probe",Reply::Wait(Wait::Owned)),("wait:shell:bounded",Reply::Wait(Wait::Owned))]);s.extend(start());
        let f=check(s,0,false,false);assert_eq!(f.0.borrow().releases,2);
    }
    #[test] fn quitting_wait_timeout(){let mut s=occupied(true);s.extend([("wait:quitting:probe",Reply::Wait(Wait::Owned)),("wait:shell:bounded",Reply::Wait(Wait::Timeout))]);check(s,1,false,false);}
    fn legacy()->Vec<(&'static str,Reply)> {vec![
        ("mutex:shell",Reply::Create(false)),("wait:shell:probe",Reply::Wait(Wait::Owned)),
        ("mutex:instance",Reply::Create(true)),("open:runtime",Reply::Open(true))]}
    #[test] fn old_qt_unowned_runtime_still_blocks(){let mut s=legacy();s.push(("wait:runtime:probe",Reply::Wait(Wait::Owned)));s.extend(restore());check(s,0,true,false);}
    #[test] fn old_qt_absent_runtime(){let mut s=legacy();s[3].1=Reply::Open(false);s.extend(restore());check(s,0,true,false);}
    #[test] fn residual_runtime_wait_success_including_legacy_tail(){
        let mut s=legacy();s.extend([("wait:runtime:probe",Reply::Wait(Wait::Timeout)),("wait:runtime:bounded",Reply::Wait(Wait::Owned)),
            ("mutex:instance",Reply::Create(true)),("open:runtime",Reply::Open(true)),("wait:runtime:probe",Reply::Wait(Wait::Owned))]);
        s.extend(start().into_iter().skip(2));check(s,0,false,false);
    }
    #[test] fn residual_runtime_wait_timeout(){let mut s=legacy();s.extend([("wait:runtime:probe",Reply::Wait(Wait::Timeout)),("wait:runtime:bounded",Reply::Wait(Wait::Timeout))]);check(s,1,false,false);}
    #[test] fn abandonment_is_owned_and_logged(){let mut s=start();s[1].1=Reply::Wait(Wait::Abandoned);let f=check(s,0,false,false);assert!(f.0.borrow().logs.contains(&"instance_mutex_abandoned".into()));}
    #[test] fn access_denied_and_unknown_at_every_start_step(){
        for error in [ACCESS_DENIED,87] {for i in 0..start().len() {let mut s=start();s[i].1=Reply::Error(error);s.truncate(i+1);check(s,1,false,false);}}
    }
    #[test] fn denied_open_and_set_never_fail_open(){
        for e in [ACCESS_DENIED,87] {
            let mut s=occupied(false);s[2].1=Reply::Error(e);check(s,1,false,false);
            let mut s=legacy();s[3].1=Reply::Error(e);check(s,1,false,false);
            let mut s=occupied(false);s.extend(restore().into_iter().take(2));s.last_mut().unwrap().1=Reply::Error(e);check(s,1,false,false);
        }
    }
    #[test] fn cli_takes_no_handles(){let f=check(vec![],0,false,true);assert_eq!(f.0.borrow().opens,0);assert_eq!(crate::cli_mode(&["--version".into()]),Some(0));}
    #[test] fn quitting_marker_is_manual_reset_only(){
        let mut steps=start();steps.push(("set:quitting",Reply::Unit));let fake=Fake::new(steps);
        let names=bridge_host::scope::names("custom","0123456789abcdef01234567").unwrap();
        let Outcome::Start(lease)=acquire(fake.clone(),&names,false).unwrap() else {panic!()};
        lease.mark_quitting().unwrap();drop(lease);assert!(fake.0.borrow().handles.is_empty());assert_eq!(fake.0.borrow().sets,vec!["quitting"]);
    }
}
