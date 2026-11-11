//! The page, served from the binary on `http://setup.localhost/`. Design tokens and fonts are
//! the app's own (ui/css, ui/fonts), so the installer can never drift from the app's look.
use std::borrow::Cow;
use wry::http::Response;

const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

fn file(path: &str) -> Option<(&'static [u8], &'static str)> {
    Some(match path {
        "/" | "/index.html" => (include_bytes!("../ui/index.html"), "text/html; charset=utf-8"),
        "/setup.css" => (include_bytes!("../ui/setup.css"), "text/css; charset=utf-8"),
        "/setup.js" => (include_bytes!("../ui/setup.js"), "text/javascript; charset=utf-8"),
        "/strings.js" => (include_bytes!("../ui/strings.js"), "text/javascript; charset=utf-8"),
        "/css/tokens.css" => (include_bytes!("../../../ui/css/tokens.css"), "text/css; charset=utf-8"),
        "/css/fonts.css" => (include_bytes!("../../../ui/css/fonts.css"), "text/css; charset=utf-8"),
        "/fonts/instrument-sans-latin-wght-normal.woff2" => (include_bytes!("../../../ui/fonts/instrument-sans-latin-wght-normal.woff2"), "font/woff2"),
        "/fonts/instrument-sans-latin-ext-wght-normal.woff2" => (include_bytes!("../../../ui/fonts/instrument-sans-latin-ext-wght-normal.woff2"), "font/woff2"),
        "/fonts/geist-mono-latin-400-normal.woff2" => (include_bytes!("../../../ui/fonts/geist-mono-latin-400-normal.woff2"), "font/woff2"),
        "/fonts/geist-mono-latin-ext-400-normal.woff2" => (include_bytes!("../../../ui/fonts/geist-mono-latin-ext-400-normal.woff2"), "font/woff2"),
        "/fonts/geist-mono-latin-500-normal.woff2" => (include_bytes!("../../../ui/fonts/geist-mono-latin-500-normal.woff2"), "font/woff2"),
        "/fonts/geist-mono-latin-ext-500-normal.woff2" => (include_bytes!("../../../ui/fonts/geist-mono-latin-ext-500-normal.woff2"), "font/woff2"),
        "/license.txt" => (include_bytes!("../../../LICENSE"), "text/plain; charset=utf-8"),
        _ => return None,
    })
}

pub fn serve(path: &str) -> Response<Cow<'static, [u8]>> {
    match file(path) {
        Some((body, mime)) => Response::builder()
            .header("Content-Type", mime)
            .header("Content-Security-Policy", CSP)
            .header("Cache-Control", "no-store")
            .body(Cow::Borrowed(body))
            .unwrap(),
        None => Response::builder().status(404).body(Cow::Borrowed(&b""[..])).unwrap(),
    }
}
