//! Fixed native acceptance probe for the scoped P3 shell, including release.
//! No arbitrary scripts, page commands, remote debugging or production scope.
use serde_json::{json, Value};
use std::{fs, path::{Path,PathBuf}, sync::mpsc, time::{Duration,Instant}};
use tauri::WebviewWindow;
use windows::core::{Interface,HSTRING,BOOL};
use webview2_com::Microsoft::Web::WebView2::Win32::*;

fn evaluate(window:&WebviewWindow, script:String) -> Result<Value,String> {
    let (tx,rx)=mpsc::channel(); let failure=tx.clone();
    window.with_webview(move |view| unsafe {
        let handler=webview2_com::ExecuteScriptCompletedHandler::create(Box::new(move |status,result| {
            let parsed=status.map_err(|_|"script_failed".to_string())
                .and_then(|_|serde_json::from_str(&result).map_err(|_|"script_invalid_json".to_string()));
            let _=tx.send(parsed); Ok(())
        }));
        let result=view.controller().CoreWebView2().and_then(|core|core.ExecuteScript(&HSTRING::from(script),&handler));
        if result.is_err() {let _=failure.send(Err("execute_script_failed".into()));}
    }).map_err(|_|"webview_dispatch_failed")?;
    rx.recv_timeout(Duration::from_secs(8)).map_err(|_|"script_timeout")?
}
fn settings(window:&WebviewWindow) -> Result<Value,String> {
    let (tx,rx)=mpsc::channel();
    window.with_webview(move |view| {
        let result=(|| -> windows::core::Result<Value> {unsafe {
            let controller=view.controller(); let core=controller.CoreWebView2()?; let s=core.Settings()?;
            let s3:ICoreWebView2Settings3=s.cast()?; let s4:ICoreWebView2Settings4=s.cast()?;
            let s5:ICoreWebView2Settings5=s.cast()?; let s6:ICoreWebView2Settings6=s.cast()?;
            let c4:ICoreWebView2Controller4=controller.cast()?;
            let mut result=serde_json::Map::new();
            macro_rules! read {($name:expr,$object:expr,$method:ident)=>{{
                let mut b=BOOL(0); $object.$method(&mut b)?; result.insert($name.into(),json!(b.as_bool()));
            }};}
            read!("devtools",s,AreDevToolsEnabled); read!("context_menu",s,AreDefaultContextMenusEnabled);
            read!("status_bar",s,IsStatusBarEnabled); read!("zoom",s,IsZoomControlEnabled);
            read!("browser_keys",s3,AreBrowserAcceleratorKeysEnabled);
            read!("password_autosave",s4,IsPasswordAutosaveEnabled); read!("autofill",s4,IsGeneralAutofillEnabled);
            read!("pinch",s5,IsPinchZoomEnabled); read!("swipe",s6,IsSwipeNavigationEnabled);
            read!("external_drop",c4,AllowExternalDrop);
            let mut zoom=0.0; controller.ZoomFactor(&mut zoom)?; result.insert("zoom_factor".into(),json!(zoom));
            Ok(Value::Object(result))
        }})();
        let _=tx.send(result.map_err(|_|"settings_readback_failed".to_string()));
    }).map_err(|_|"settings_dispatch_failed")?;
    rx.recv_timeout(Duration::from_secs(8)).map_err(|_|"settings_timeout")?
}
fn write(data:&Path,name:&str,value:&Value) -> Result<(),String> {
    let pending=data.join(format!("{name}.pending"));
    fs::write(&pending,serde_json::to_vec_pretty(value).unwrap()).and_then(|_|fs::rename(pending,data.join(name))).map_err(|_|"evidence_write_failed".into())
}
fn verify(window:&WebviewWindow,data:&Path,port:u16) -> Result<Value,String> {
    let deadline=Instant::now()+Duration::from_secs(45);
    while evaluate(window,"!!window.__TAURI__?.core && !!window.T?.sim".into())? != true {
        if Instant::now()>deadline {return Err("prototype_not_ready".into());} std::thread::sleep(Duration::from_millis(100));
    }
    let before=settings(window)?;
    if before.as_object().unwrap().iter().any(|(k,v)|k!="zoom_factor" && v!=false) {return Err("release_settings_enabled".into());}
    evaluate(window,r#"(() => {
        window.__p3Probe={token:'p3-release-sentinel',keyboard:0,done:false};
        document.addEventListener('keydown',e=>{ if (e.key.toLowerCase()==='f5' || (e.ctrlKey && ['r','+','-','=','0'].includes(e.key.toLowerCase()))) window.__p3Probe.keyboard++; },true);
        (async()=>{
            const p=window.__p3Probe;
            p.devPage=(await fetch('/dev-rt.html')).status;
            p.devScript=(await fetch('/dev-rt.js')).status;
            try {await window.__TAURI__.core.invoke('dev_quit'); p.devCommand='accepted';} catch(e) {p.devCommand=String(e);}
            const keys=[['F5',false,false],['r',true,false],['r',true,true],['+',true,false],['-',true,false],['0',true,false]];
            p.keys=keys.map(([key,ctrlKey,shiftKey])=>{
                const e=new KeyboardEvent('keydown',{key,ctrlKey,shiftKey,bubbles:true,cancelable:true});
                document.dispatchEvent(e); return e.defaultPrevented;
            });
            const transfer=new DataTransfer(); transfer.items.add(new File(['p3'],'p3-file.html',{type:'text/html'}));
            const drop=new DragEvent('drop',{dataTransfer:transfer,bubbles:true,cancelable:true});
            document.dispatchEvent(drop); p.fileDrop=drop.defaultPrevented; p.done=true;
        })(); return true;
    })()"#.into())?;
    loop {
        if evaluate(window,"!!window.__p3Probe?.done".into())? == true {break;}
        if Instant::now()>deadline {return Err("page_acceptance_timeout".into());} std::thread::sleep(Duration::from_millis(100));
    }
    let page=evaluate(window,"window.__p3Probe".into())?;
    if page["devPage"]!=404 || page["devScript"]!=404 || !page["devCommand"].as_str().unwrap_or("").contains("not found")
        || page["keys"].as_array().is_none_or(|v|v.iter().any(|v|v!=true)) || page["fileDrop"]!=true {
        write(data,"acceptance-page-failure.json",&page)?; return Err("release_page_restrictions_failed".into());
    }
    // T-P3-07 (fix round 1, B-S3): the physical-input driver starts on this stage file. The window
    // stays DWM-cloaked until host_ready or the 1.5 s timeout; input sent before the reveal could
    // land on whatever is behind it, so the stage is only announced once the window is shown.
    let hwnd=window.hwnd().map_err(|_|"window_handle_failed")?.0 as isize;
    while crate::webview::app_cloaked(hwnd) {
        if Instant::now()>deadline {return Err("window_still_cloaked".into());} std::thread::sleep(Duration::from_millis(50));
    }
    write(data,"acceptance-stage.json",&json!({"stage":"physical_input","before":before,"page":page}))?;
    while !data.join("acceptance-input.done").is_file() {
        if Instant::now()>deadline {return Err("physical_input_timeout".into());} std::thread::sleep(Duration::from_millis(100));
    }
    let after=settings(window)?; let sentinel=evaluate(window,"({token:window.__p3Probe?.token,keyboard:window.__p3Probe?.keyboard,url:location.href})".into())?;
    if sentinel["token"]!="p3-release-sentinel" || after["zoom_factor"]!=before["zoom_factor"] || sentinel["keyboard"]!=0 {
        return Err("physical_input_changed_document_or_zoom".into());
    }
    // These real renderer navigation/new-window attempts exercise the normal handlers.
    evaluate(window,format!("(() => {{ const a=document.createElement('a'); a.href='http://127.0.0.1:{port}/p3-external'; a.target='_blank'; document.body.append(a); a.click(); return true; }})()"))?;
    std::thread::sleep(Duration::from_millis(500));
    evaluate(window,format!("(() => {{ location.href='http://127.0.0.1:{port}/p3-navigation'; return true; }})()"))?;
    std::thread::sleep(Duration::from_millis(1000));
    let navigation=evaluate(window,"({token:window.__p3Probe?.token,url:location.href})".into())?;
    if navigation["token"]!="p3-release-sentinel" || !navigation["url"].as_str().unwrap_or("").starts_with("http://tauri.localhost/") {return Err("external_navigation_entered_webview".into());}
    Ok(json!({"passed":true,"release":!cfg!(debug_assertions),"settings_before":before,"settings_after":after,
        "page":page,"physical_input":sentinel,"external_navigation":navigation,
        "position":window.outer_position().ok().map(|p|json!({"x":p.x,"y":p.y})),"scale":window.scale_factor().ok()}))
}
/// `quit` is the shell's quit entry (`QuitReason::Acceptance`: no page confirmation).
pub fn start(window:WebviewWindow,data:PathBuf,quit:Box<dyn FnOnce()+Send>,port:u16) {
    std::thread::spawn(move || {
        let result=verify(&window,&data,port).unwrap_or_else(|e|json!({"passed":false,"error":e}));
        let _=write(&data,"acceptance-result.json",&result);
        quit();
    });
}
