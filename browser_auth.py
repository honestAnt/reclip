"""Pull site login from the local browser so yt-dlp can fetch logged-in pages.

yt-dlp only sends cookies (including HttpOnly). This module:

1. Loads those cookies from Chrome / Safari / Firefox / Edge / etc.
2. On macOS, also reads localStorage / sessionStorage / document.cookie from an
   already-open tab on the same site, and merges token-like keys into a cookie file.

Neither this process nor the ReClip page can read another origin's storage from
JavaScript (same-origin policy). The cookie databases and AppleScript tab hook
are what actually reach Douyin / Xiaohongshu / YouTube sessions.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

SUPPORTED_BROWSERS = (
    "chrome",
    "edge",
    "brave",
    "vivaldi",
    "opera",
    "chromium",
    "firefox",
    "safari",
)

# Prefer Chromium-based browsers: Douyin / Xiaohongshu sessions usually live there.
BROWSER_PRIORITY = SUPPORTED_BROWSERS

BROWSER_LABELS = {
    "chrome": "Chrome",
    "edge": "Edge",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "chromium": "Chromium",
    "firefox": "Firefox",
    "safari": "Safari",
}

# When matching open tabs / extra cookies, include sibling hosts the extractor hits.
RELATED_DOMAINS = {
    "youtube.com": ["youtube.com", "youtu.be", "google.com", "youtube-nocookie.com"],
    "youtu.be": ["youtube.com", "youtu.be", "google.com"],
    "douyin.com": ["douyin.com", "iesdouyin.com"],
    "iesdouyin.com": ["douyin.com", "iesdouyin.com"],
    "xiaohongshu.com": ["xiaohongshu.com", "xhslink.com", "xiaohongshu.cn"],
    "xhslink.com": ["xiaohongshu.com", "xhslink.com"],
    "tiktok.com": ["tiktok.com"],
    "bilibili.com": ["bilibili.com", "biliapi.net", "bilivideo.com"],
    "instagram.com": ["instagram.com", "cdninstagram.com", "facebook.com"],
    "facebook.com": ["facebook.com", "instagram.com"],
}

# localStorage / sessionStorage keys worth turning into cookies or headers.
TOKEN_KEY_RE = re.compile(
    r"(token|session|auth|sid|uid|jwt|login|passport|csrf|mstoken|"
    r"web_session|sessionid|access|refresh|webid|web_id|openid)",
    re.I,
)

COOKIE_NAME_RE = re.compile(r"^[\w!#$%&'*+\-.^`|~]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")

_COOKIE_CACHE_TTL = 14 * 24 * 60 * 60
_cookie_lock = threading.Lock()
_cookie_cache: dict[str, tuple[str, float]] = {}
_AUTH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".auth")

_COOKIE_LOAD_HINTS = (
    "failed to load cookies",
    "could not copy",
    "could not find firefox cookies",
    "could not find chrome cookies",
    "failed to decrypt",
    "keyring",
    "keychain",
    "unknown browser",
    "unsupported browser",
    "invalid cookies from browser",
)


def _home() -> str:
    return os.path.expanduser("~")


def _browser_data_paths(name: str) -> list[str]:
    home = _home()
    darwin = {
        "chrome": [f"{home}/Library/Application Support/Google/Chrome"],
        "edge": [f"{home}/Library/Application Support/Microsoft Edge"],
        "brave": [f"{home}/Library/Application Support/BraveSoftware/Brave-Browser"],
        "vivaldi": [f"{home}/Library/Application Support/Vivaldi"],
        "opera": [f"{home}/Library/Application Support/com.operasoftware.Opera"],
        "chromium": [f"{home}/Library/Application Support/Chromium"],
        "firefox": [f"{home}/Library/Application Support/Firefox"],
        "safari": [
            f"{home}/Library/Cookies/Cookies.binarycookies",
            f"{home}/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
        ],
    }
    linux = {
        "chrome": [f"{home}/.config/google-chrome", f"{home}/.config/google-chrome-stable"],
        "edge": [f"{home}/.config/microsoft-edge"],
        "brave": [f"{home}/.config/BraveSoftware/Brave-Browser"],
        "vivaldi": [f"{home}/.config/vivaldi"],
        "opera": [f"{home}/.config/opera"],
        "chromium": [f"{home}/.config/chromium"],
        "firefox": [f"{home}/.mozilla/firefox"],
        "safari": [],
    }
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    windows = {
        "chrome": [os.path.join(local, "Google", "Chrome")],
        "edge": [os.path.join(local, "Microsoft", "Edge")],
        "brave": [os.path.join(local, "BraveSoftware", "Brave-Browser")],
        "vivaldi": [os.path.join(local, "Vivaldi")],
        "opera": [os.path.join(roaming, "Opera Software", "Opera Stable")],
        "chromium": [os.path.join(local, "Chromium")],
        "firefox": [os.path.join(roaming, "Mozilla", "Firefox")],
        "safari": [],
    }
    if sys.platform == "darwin":
        table = darwin
    elif sys.platform == "win32":
        table = windows
    else:
        table = linux
    return [p for p in table.get(name, []) if p]


def browser_available(name: str) -> bool:
    name = (name or "").split("+")[0].split(":")[0].lower()
    if name == "safari" and sys.platform == "darwin":
        return os.path.isdir("/Applications/Safari.app") or any(os.path.exists(p) for p in _browser_data_paths(name))
    return any(os.path.exists(p) for p in _browser_data_paths(name))


def browser_from_ua(ua: str) -> str:
    """Map the ReClip tab's User-Agent to a yt-dlp browser name."""
    text = (ua or "").lower()
    if not text:
        return "auto"
    if "edg/" in text or "edgios" in text:
        return "edge"
    if "firefox/" in text or "fxios/" in text:
        return "firefox"
    if "opr/" in text or "opera" in text:
        return "opera"
    if "brave/" in text:
        return "brave"
    if "vivaldi" in text:
        return "vivaldi"
    if "chrome/" in text or "crios/" in text:
        return "chrome"
    if "safari/" in text:
        return "safari"
    return "auto"


def list_available_browsers(ua: str | None = None) -> dict:
    browsers = []
    for name in BROWSER_PRIORITY:
        if browser_available(name):
            browsers.append({"id": name, "label": BROWSER_LABELS.get(name, name.title())})
    cookies_file = os.environ.get("RECLIP_COOKIES_FILE", "").strip()
    detected = browser_from_ua(ua or "")
    env_default = os.environ.get("RECLIP_BROWSER", "").strip()
    default = env_default
    if not default or default.lower() == "auto":
        if detected != "auto" and any(b["id"] == detected for b in browsers):
            default = detected
        else:
            default = browsers[0]["id"] if browsers else ""
    return {
        "default": default,
        "detected": detected if detected != "auto" else default,
        "browsers": browsers,
        "cookies_file": bool(cookies_file and os.path.isfile(cookies_file)),
    }


def browser_family(spec: str | None) -> str | None:
    if not spec:
        return None
    return spec.split("+")[0].split(":")[0].strip().lower() or None


def resolve_browser(choice: str | None, ua: str | None = None) -> str | None:
    """Return a yt-dlp --cookies-from-browser spec, or None to skip."""
    raw = (choice or "").strip() or os.environ.get("RECLIP_BROWSER", "").strip() or "auto"
    if raw.lower() in ("none", "off", "no", "0"):
        return None
    if raw.lower() in ("auto", ""):
        env_browser = os.environ.get("RECLIP_BROWSER", "").strip()
        if env_browser and env_browser.lower() not in ("auto", "none", "off"):
            raw = env_browser
        else:
            detected = browser_from_ua(ua or "")
            if detected != "auto" and browser_available(detected):
                return detected
            available = list_available_browsers()["browsers"]
            return available[0]["id"] if available else None
    family = browser_family(raw)
    if family not in SUPPORTED_BROWSERS:
        return None
    return raw


def registrable_domain(host: str) -> str:
    host = (host or "").lower().lstrip(".")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "edu"):
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def related_hosts(host: str) -> list[str]:
    host = (host or "").lower().lstrip(".")
    root = registrable_domain(host)
    extra = []
    for key, values in RELATED_DOMAINS.items():
        if host == key or host.endswith("." + key) or root == key:
            extra = values
            break
    seen = []
    for item in [host, root, *extra]:
        item = item.lower().lstrip(".")
        if item and item not in seen:
            seen.append(item)
    return seen


def cookies_file_from_env() -> str | None:
    path = os.environ.get("RECLIP_COOKIES_FILE", "").strip()
    if path and os.path.isfile(path):
        return path
    return None


def _ensure_auth_dir() -> str:
    os.makedirs(_AUTH_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(_AUTH_DIR, 0o700)
    except OSError:
        pass
    return _AUTH_DIR


def cookie_store_path(spec: str) -> str:
    family, profile = _browser_spec_parts(spec)
    slug = family or "browser"
    if profile:
        safe = re.sub(r"[^\w.-]+", "_", profile)[:40]
        slug = f"{family}-{safe}"
    return os.path.join(_ensure_auth_dir(), f"{slug}.txt")


def _browser_spec_parts(spec: str) -> tuple[str | None, str | None]:
    family = browser_family(spec)
    if not spec:
        return family, None
    match = re.fullmatch(
        r"(?P<name>[^+:]+)(?:\+(?P<keyring>[^:]+))?(?::(?P<profile>.+?))?(?:::(?P<container>.+))?",
        spec.strip(),
    )
    profile = match.group("profile") if match else None
    return family, profile


def dump_browser_cookies(spec: str) -> str | None:
    family, profile = _browser_spec_parts(spec)
    if not family:
        return None
    try:
        from yt_dlp.cookies import extract_cookies_from_browser

        jar = extract_cookies_from_browser(family, profile)
        path = cookie_store_path(spec)
        tmp = path + ".tmp"
        jar.save(tmp, ignore_discard=True, ignore_expires=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def login_status(spec: str | None) -> dict:
    if not spec:
        return {"saved": False}
    path = cookie_store_path(spec)
    if not os.path.isfile(path):
        return {"saved": False, "browser": spec}
    age = max(0, time.time() - os.path.getmtime(path))
    return {
        "saved": True,
        "browser": spec,
        "age_seconds": int(age),
    }


def cached_cookie_file(spec: str | None, force: bool = False) -> str | None:
    env_file = cookies_file_from_env()
    if not spec:
        return env_file
    path = cookie_store_path(spec)
    now = time.time()
    with _cookie_lock:
        if not force and os.path.isfile(path):
            age = now - os.path.getmtime(path)
            if age < _COOKIE_CACHE_TTL:
                _cookie_cache[spec] = (path, now)
                return path
        dumped = dump_browser_cookies(spec)
        if dumped:
            _cookie_cache[spec] = (dumped, now)
            return dumped
        if os.path.isfile(path):
            return path
    return env_file


def site_header_args(url: str) -> list[str]:
    host = (urlparse(url).hostname or "").lower()
    if "douyin.com" in host or "iesdouyin.com" in host:
        return ["--add-header", "Referer:https://www.douyin.com/"]
    if "xiaohongshu.com" in host:
        return ["--add-header", "Referer:https://www.xiaohongshu.com/"]
    return []


def _tab_needles(host: str) -> list[str]:
    return related_hosts(host)


def _run_osascript(script: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    text = (result.stdout or "").strip()
    if not text or text in ("missing value", "null", "undefined"):
        return ""
    return text


_CHROMIUM_APPS = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "brave": "Brave Browser",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "chromium": "Chromium",
}

_STORAGE_JS = (
    "JSON.stringify({cookie:document.cookie,"
    "localStorage:Object.fromEntries(Object.entries(localStorage)),"
    "sessionStorage:Object.fromEntries(Object.entries(sessionStorage))})"
)


def _applescript_chromium(app_name: str, process_name: str, needle: str) -> str:
    return f'''
tell application "System Events"
  if not (exists process "{process_name}") then return ""
end tell
set js to "{_STORAGE_JS}"
set needle to "{needle}"
tell application "{app_name}"
  repeat with w in windows
    repeat with t in tabs of w
      try
        set u to URL of t
        if u contains needle then
          try
            return execute t javascript js
          end try
        end if
      end try
    end repeat
  end repeat
end tell
return ""
'''


def _applescript_safari(needle: str) -> str:
    return f'''
tell application "System Events"
  if not (exists process "Safari") then return ""
end tell
set js to "{_STORAGE_JS}"
set needle to "{needle}"
tell application "Safari"
  repeat with w in windows
    repeat with t in tabs of w
      try
        set u to URL of t
        if u contains needle then
          try
            return do JavaScript js in t
          end try
        end if
      end try
    end repeat
  end repeat
end tell
return ""
'''


def extract_open_tab_storage(url: str, browser_spec: str | None) -> dict:
    """Best-effort localStorage / sessionStorage / document.cookie from an open tab.

    Only looks at the selected browser, and never launches it. HttpOnly cookies
    still come from the on-disk cookie DB via yt-dlp, not from this hook.
    """
    if sys.platform != "darwin":
        return {}
    if os.environ.get("RECLIP_TAB_STORAGE", "").lower() not in ("1", "true", "yes"):
        return {}
    host = (urlparse(url).hostname or "").lower()
    if not host or not HOST_RE.match(host):
        return {}
    family = browser_family(browser_spec)
    needles = [n for n in _tab_needles(host) if HOST_RE.match(n)]
    if not needles:
        return {}
    needle = needles[0]
    if family == "safari":
        raw = _run_osascript(_applescript_safari(needle))
    elif family in _CHROMIUM_APPS:
        raw = _run_osascript(
            _applescript_chromium(_CHROMIUM_APPS[family], _CHROMIUM_APPS[family], needle)
        )
    else:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _cookie_tuples_from_document_cookie(raw: str, domain: str) -> list[tuple[str, str, str]]:
    out = []
    if not raw:
        return out
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if COOKIE_NAME_RE.match(name) and value:
            out.append((domain, name, value))
    return out


def _cookie_tuples_from_storage(storage: dict, domain: str) -> list[tuple[str, str, str]]:
    out = []
    blob = {}
    for key in ("localStorage", "sessionStorage"):
        value = storage.get(key) or {}
        if isinstance(value, dict):
            blob.update(value)
    for name, value in blob.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if not COOKIE_NAME_RE.match(name) or not TOKEN_KEY_RE.search(name):
            continue
        if not value or len(value) > 3000 or "\t" in value or "\n" in value or "\r" in value:
            continue
        out.append((domain, name, value))
    return out


def _header_args_from_storage(storage: dict) -> list[str]:
    blob = {}
    for key in ("localStorage", "sessionStorage"):
        value = storage.get(key) or {}
        if isinstance(value, dict):
            blob.update(value)
    for name, value in blob.items():
        if not isinstance(name, str) or not isinstance(value, str) or not value:
            continue
        lowered = name.lower()
        if lowered in ("authorization", "authorizationtoken", "access_token", "accessToken"):
            header = value if value.lower().startswith("bearer ") else (
                f"Bearer {value}" if value.count(".") >= 2 else value
            )
            if "\n" in header or "\r" in header:
                continue
            return ["--add-header", f"Authorization:{header}"]
    return []


def write_netscape_cookies(cookies: list[tuple[str, str, str]], dest: str) -> None:
    expires = str(int(time.time()) + 30 * 86400)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("# Netscape HTTP Cookie File\n")
        handle.write("# Generated by ReClip from the local browser session.\n")
        for domain, name, value in cookies:
            domain = domain if domain.startswith(".") else domain
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            handle.write(f"{domain}\t{flag}\t/\tTRUE\t{expires}\t{name}\t{value}\n")


def extra_cookie_file(url: str, browser_spec: str | None) -> tuple[str | None, list[str]]:
    """Write extra cookies from an open tab; return (path, extra yt-dlp args)."""
    storage = extract_open_tab_storage(url, browser_spec)
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None, []
    domain = "." + registrable_domain(host)
    cookies = _cookie_tuples_from_document_cookie(storage.get("cookie") or "", domain)
    cookies += _cookie_tuples_from_storage(storage, domain)
    headers = _header_args_from_storage(storage)
    env_file = cookies_file_from_env()
    if not cookies and not env_file:
        return None, headers
    handle = tempfile.NamedTemporaryFile(
        prefix="reclip-auth-",
        suffix=".txt",
        delete=False,
    )
    path = handle.name
    handle.close()
    try:
        if env_file:
            with open(env_file, "r", encoding="utf-8", errors="replace") as src, open(
                path, "w", encoding="utf-8"
            ) as dest:
                dest.write(src.read())
            os.chmod(path, 0o600)
            if cookies:
                expires = str(int(time.time()) + 30 * 86400)
                with open(path, "a", encoding="utf-8") as dest:
                    for domain_name, name, value in cookies:
                        flag = "TRUE" if domain_name.startswith(".") else "FALSE"
                        dest.write(f"{domain_name}\t{flag}\t/\tTRUE\t{expires}\t{name}\t{value}\n")
        else:
            write_netscape_cookies(cookies, path)
    except OSError:
        _cleanup(path)
        return None, headers
    return path, headers


def ytdlp_auth_args(url: str, browser: str | None, ua: str | None = None, force_cookies: bool = False) -> tuple[list[str], str | None]:
    spec = resolve_browser(browser, ua=ua)
    extra_path, header_args = extra_cookie_file(url, spec)
    cached = cached_cookie_file(spec, force=force_cookies)
    env_file = cookies_file_from_env()
    args: list[str] = []
    if extra_path:
        args += ["--cookies", extra_path]
    elif cached:
        args += ["--cookies", cached]
    elif env_file:
        args += ["--cookies", env_file]
    elif spec:
        args += ["--cookies-from-browser", spec]
    args += site_header_args(url)
    args += header_args
    return args, extra_path


def is_cookie_load_error(stderr: str) -> bool:
    text = (stderr or "").lower()
    if "fresh cookies" in text:
        return False
    if "cookie" not in text and "keychain" not in text and "keyring" not in text:
        return False
    return any(hint in text for hint in _COOKIE_LOAD_HINTS)


def is_impersonate_error(stderr: str) -> bool:
    text = (stderr or "").lower()
    return "impersonat" in text or "curl_cffi" in text


def impersonate_args() -> list[str]:
    if os.environ.get("RECLIP_NO_IMPERSONATE", "").lower() in ("1", "true", "yes"):
        return []
    return ["--impersonate", "chrome"]


def _cleanup(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def run_ytdlp(
    extra_args: list[str],
    url: str,
    browser: str | None = None,
    timeout: int = 60,
    ua: str | None = None,
    pass_url: bool = True,
) -> subprocess.CompletedProcess:
    """Run yt-dlp with site cookies from the local browser. extra_args should not include the URL."""
    auth_args, extra_file = ytdlp_auth_args(url, browser, ua=ua)
    net_args = impersonate_args()
    refreshed = False
    try:
        cmd = ["yt-dlp", *extra_args, *auth_args, *net_args]
        if pass_url:
            cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0 and net_args and is_impersonate_error(result.stderr):
            cmd = ["yt-dlp", *extra_args, *auth_args]
            if pass_url:
                cmd.append(url)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0 and auth_args and is_cookie_load_error(result.stderr):
            cmd = ["yt-dlp", *extra_args, *net_args]
            if pass_url:
                cmd.append(url)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stderr = (result.stderr or "").lower()
        if result.returncode != 0 and not refreshed and "fresh cookies" in stderr:
            spec = resolve_browser(browser, ua=ua)
            if spec:
                cached_cookie_file(spec, force=True)
                refreshed = True
                auth_args, extra_file = ytdlp_auth_args(url, browser, ua=ua)
                cmd = ["yt-dlp", *extra_args, *auth_args, *net_args]
                if pass_url:
                    cmd.append(url)
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result
    finally:
        _cleanup(extra_file)
