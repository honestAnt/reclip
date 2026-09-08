import os
import uuid
import glob
import json
import tempfile
import threading
import time
from subprocess import TimeoutExpired
from flask import Flask, request, jsonify, send_file, render_template

from browser_auth import list_available_browsers, login_status, resolve_browser, run_ytdlp, cached_cookie_file
from media_urls import normalize_media_url, unsupported_profile_message

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
_info_lock = threading.Lock()
_info_cache = {}
_INFO_TTL = 10 * 60


def request_browser(data=None):
    """Prefer an explicit UI choice, otherwise the browser that opened localhost:8899."""
    data = data or {}
    return (data.get("browser") or "").strip() or None


def request_ua():
    return request.headers.get("User-Agent") or ""


def ytdlp_stderr(result):
    return (result.stderr or "").strip().split("\n")[-1] if result.stderr else "yt-dlp failed"


def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output.

    With ``-j`` yt-dlp prints one JSON object per line. Some extractors
    emit multiple videos even with ``--no-playlist``, so stdout contains
    several objects and a plain ``json.loads`` raises "Extra data".
    Return the first valid object.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


def remember_info(url, info):
    with _info_lock:
        _info_cache[url] = (info, time.time())


def cached_info(url):
    with _info_lock:
        hit = _info_cache.get(url)
    if not hit:
        return None
    info, ts = hit
    if time.time() - ts > _INFO_TTL:
        return None
    return info


def run_download(job_id, url, format_choice, format_id, browser=None, ua=None, info=None):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    extra = ["--no-playlist", "-o", out_template]
    info_path = None
    pass_url = True

    if info:
        fd, info_path = tempfile.mkstemp(prefix="reclip-info-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(info, handle)
        extra += ["--load-info-json", info_path]
        pass_url = False

    if format_choice == "audio":
        extra += ["-x", "--audio-format", "mp3"]
    elif format_id:
        extra += ["-f", f"{format_id}/{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        extra += ["-f", "bestvideo+bestaudio/best/best", "--merge-output-format", "mp4"]

    try:
        result = run_ytdlp(
            extra, url, browser=browser, timeout=300, ua=ua, pass_url=pass_url,
        )
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = ytdlp_stderr(result)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:100].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        if info_path:
            try:
                os.remove(info_path)
            except OSError:
                pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/auth")
def auth_status():
    ua = request_ua()
    info = list_available_browsers(ua)
    using = resolve_browser(request.args.get("browser") or "auto", ua=ua)
    info["using"] = using
    info["login"] = login_status(using)
    return jsonify(info)


@app.route("/api/auth/refresh", methods=["POST"])
def auth_refresh():
    data = request.json or {}
    ua = request_ua()
    spec = resolve_browser(request_browser(data) or "auto", ua=ua)
    if not spec:
        return jsonify({"error": "No local browser profile found"}), 400
    path = cached_cookie_file(spec, force=True)
    if not path:
        return jsonify({"error": "Could not read browser login. If macOS asked for Keychain access, click Always Allow and retry."}), 400
    status = login_status(spec)
    status["ok"] = True
    return jsonify(status)


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json or {}
    url = normalize_media_url(data.get("url", "").strip())
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    profile_error = unsupported_profile_message(url)
    if profile_error:
        return jsonify({"error": profile_error}), 400

    browser = request_browser(data)
    ua = request_ua()
    try:
        result = run_ytdlp(["--no-playlist", "-j"], url, browser=browser, timeout=60, ua=ua)
        if result.returncode != 0:
            return jsonify({"error": ytdlp_stderr(result)}), 400

        info = parse_ytdlp_json(result.stdout)
        remember_info(url, info)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
            "auth_browser": resolve_browser(browser, ua=ua) or "",
        })
    except TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json or {}
    url = normalize_media_url(data.get("url", "").strip())
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    profile_error = unsupported_profile_message(url)
    if profile_error:
        return jsonify({"error": profile_error}), 400

    try:
        result = run_ytdlp(
            ["--flat-playlist", "-J"],
            url,
            browser=request_browser(data),
            timeout=60,
            ua=request_ua(),
        )
        if result.returncode != 0:
            return jsonify({"error": ytdlp_stderr(result)}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries", [])
        urls = [entry.get("url") for entry in entries if entry.get("url")]
        return jsonify({"urls": urls})
    except TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json or {}
    url = normalize_media_url(data.get("url", "").strip())
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    browser = request_browser(data)
    ua = request_ua()

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    profile_error = unsupported_profile_message(url)
    if profile_error:
        return jsonify({"error": profile_error}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    info = cached_info(url)
    if info is None:
        try:
            result = run_ytdlp(["--no-playlist", "-j"], url, browser=browser, timeout=60, ua=ua)
            if result.returncode != 0:
                return jsonify({"error": ytdlp_stderr(result)}), 400
            info = parse_ytdlp_json(result.stdout)
            remember_info(url, info)
        except TimeoutExpired:
            return jsonify({"error": "Timed out fetching video info"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, format_choice, format_id, browser, ua, info),
    )
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
