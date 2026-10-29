import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import match_filter_func
from werkzeug.middleware.proxy_fix import ProxyFix
from flask import (Flask, after_this_request, jsonify, request, send_file,
                   send_from_directory)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # baca IP asli di balik proxy
# URL website Vercel Anda, contoh: https://mediaflow.vercel.app
ORIGIN = os.environ.get("ALLOWED_ORIGIN", "").rstrip("/")
BASE = os.path.dirname(os.path.abspath(__file__))
COOKIES = os.path.join(BASE, "cookies.txt")  # opsional, format Netscape
ALLOWED = ("facebook.com", "fb.watch", "instagram.com", "tiktok.com")
IMG_EXT = ("jpg", "jpeg", "png", "webp")
MAX_MB = 150        # batas ukuran file
MAX_SEC = 600       # batas durasi media (detik)
MAX_TIME = 120      # batas waktu proses download (detik)
MAX_PHOTOS = 20     # batas jumlah foto per postingan
RATE = 40           # maks permintaan per IP per menit
MAX_JOBS = 3        # maks download berjalan bersamaan
HITS = defaultdict(deque)
JOBS = threading.BoundedSemaphore(MAX_JOBS)


def err(code, status=400):
    return jsonify(code=code), status


def classify(e, default):
    m = str(e).lower()
    if any(k in m for k in ("private", "login", "log in", "sign in", "cookies",
                            "restricted", "not available", "removed")):
        return "private"
    if any(k in m for k in ("http error 5", "unable to download webpage",
                            "timed out", "connection")):
        return "platform"
    return default


def limited():
    q, now = HITS[request.remote_addr], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE:
        return True
    q.append(now)
    return False


def valid(url):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return len(url) <= 500 and p.scheme in ("http", "https") and any(
        host == d or host.endswith("." + d) for d in ALLOWED)


def body():
    return request.get_json(silent=True) or {}


def ytdl_base():
    o = {"quiet": True, "noplaylist": True, "socket_timeout": 15}
    if os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    return o


# ---------- foto lewat gallery-dl ----------
def gdl(*args, timeout):
    cmd = [sys.executable, "-m", "gallery_dl"]
    if os.path.exists(COOKIES):
        cmd += ["--cookies", COOKIES]
    return subprocess.run(cmd + list(args), capture_output=True, text=True,
                          timeout=timeout)


def text(x):
    return x if isinstance(x, str) else ""


def gdl_info(url):
    """Metadata foto dari gallery-dl. None jika tidak ada foto / gagal."""
    try:
        r = gdl("-j", "--range", f"1-{MAX_PHOTOS}", url, timeout=45)
        data = json.loads(r.stdout or "[]")
    except Exception:
        return None
    files = [e for e in data if isinstance(e, list) and len(e) >= 3
             and e[0] == 3 and isinstance(e[2], dict)]
    # i = nomor urut file di postingan (dipakai sebagai --range saat download)
    imgs = [(i + 1, e) for i, e in enumerate(files)
            if text(e[2].get("extension")).lower() in IMG_EXT]
    if not imgs:
        return None
    m = imgs[0][1][2]
    owner = m.get("owner") if isinstance(m.get("owner"), dict) else {}
    return {
        "title": (text(m.get("description")) or text(m.get("title"))
                  or text(m.get("caption")) or "Foto")[:120],
        "uploader": (text(m.get("username")) or text(owner.get("username"))
                     or text(m.get("uploader")) or text(m.get("author"))),
        "thumbnail": imgs[0][1][1],
        "count": len(imgs),
        "photos": [{"i": i, "u": e[1]} for i, e in imgs],
    }


def gdl_photos(url, tmp, pick=None):
    rng = ",".join(map(str, pick)) if pick else f"1-{MAX_PHOTOS}"
    try:
        gdl("-D", tmp, "--range", rng, "--filesize-max",
            f"{MAX_MB}M", "--no-mtime", url, timeout=MAX_TIME)
    except Exception:
        return []
    return sorted(os.path.join(tmp, f) for f in os.listdir(tmp)
                  if f.lower().endswith(IMG_EXT))


@app.get("/")
def home():
    if os.path.exists(os.path.join(BASE, "index.html")):
        return send_from_directory(BASE, "index.html")
    return jsonify(ok=True, name="mediaflow-api")


@app.get("/api/ping")
def ping():
    return jsonify(ok=True)


@app.post("/api/info")
def info():
    if limited():
        return err("rate", 429)
    url = (body().get("url") or "").strip()
    if not valid(url):
        return err("bad_link")
    try:
        with yt_dlp.YoutubeDL({**ytdl_base(), "skip_download": True}) as y:
            d = y.extract_info(url, download=False)
    except Exception as e:
        # Postingan foto biasanya gagal di yt-dlp, coba lewat gallery-dl.
        g = gdl_info(url)
        if g:
            return jsonify(title=g["title"], uploader=g["uploader"],
                           thumbnail=g["thumbnail"], heights=[], duration=None,
                           size=None, direct_url=None, photo_only=True,
                           count=g["count"], photos=g["photos"])
        return err(classify(e, "read_fail"))
    if (d.get("duration") or 0) > MAX_SEC:
        return err("too_long")
    fm = d.get("formats") or []
    vids = [f for f in fm if f.get("vcodec") not in (None, "none")]
    heights = sorted({f["height"] for f in vids if f.get("height")})
    muxed = [f for f in vids if f.get("url") and f.get("acodec") not in (None, "none")]
    best = muxed[-1] if muxed else None
    direct = best["url"] if best else d.get("url")
    size = None
    if best:
        size = best.get("filesize") or best.get("filesize_approx")
    size = size or d.get("filesize") or d.get("filesize_approx")
    return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"),
                   uploader=d.get("uploader"), heights=heights,
                   duration=d.get("duration"), size=size, direct_url=direct)


@app.post("/api/download")
def download():
    if limited():
        return err("rate", 429)
    b = body()
    url = (b.get("url") or "").strip()
    kind = b.get("kind", "video")
    try:
        h = int(b.get("height") or 0)
    except (TypeError, ValueError):
        h = 0
    if not valid(url) or kind not in ("video", "audio", "photo"):
        return err("bad_link")
    raw = b.get("pick") if isinstance(b.get("pick"), list) else []
    pick = sorted({int(x) for x in raw
                   if str(x).isdigit() and 1 <= int(x) <= MAX_PHOTOS})
    if not JOBS.acquire(blocking=False):
        return err("busy", 503)

    start = time.time()

    def hook(_):
        if time.time() - start > MAX_TIME:
            raise Exception("timeout")

    tmp = tempfile.mkdtemp()
    opts = {**ytdl_base(), "retries": 1,
            "max_filesize": MAX_MB * 1024 * 1024,
            "match_filter": match_filter_func(f"duration <= {MAX_SEC}"),
            "progress_hooks": [hook],
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s")}
    if kind == "audio":
        opts.update(format="bestaudio/best", postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": "192"}])
    elif kind == "video":
        opts.update(format=(f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
                            if h else "best[ext=mp4]/best"))

    try:
        if kind == "photo":
            files = gdl_photos(url, tmp, pick)
            if not files:  # cadangan: gambar sampul lewat yt-dlp
                opts.update(skip_download=True, writethumbnail=True)
                with yt_dlp.YoutubeDL(opts) as y:
                    y.extract_info(url, download=True)
                files = sorted(os.path.join(tmp, f) for f in os.listdir(tmp))
            if len(files) > 1:
                path = os.path.join(tmp, "mediaflow-foto.zip")
                with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
                    for f in files:
                        z.write(f, os.path.basename(f))
            else:
                path = files[0]
        else:
            with yt_dlp.YoutubeDL(opts) as y:
                y.extract_info(url, download=True)
            path = os.path.join(tmp, os.listdir(tmp)[0])
        if os.path.getsize(path) > MAX_MB * 1024 * 1024:
            raise Exception("too big")
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return err(classify(e, "dl_fail"))
    finally:
        JOBS.release()

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(tmp, ignore_errors=True)
        return resp

    return send_file(path, as_attachment=True)


@app.post("/api/feedback")
def feedback():
    if limited():
        return err("rate", 429)
    b = body()
    msg = (b.get("message") or "").strip()[:1000]
    contact = (b.get("contact") or "").strip()[:100]
    if len(msg) < 5:
        return err("short")
    # Hanya menyimpan pesan & kontak opsional. URL pengguna tidak disimpan.
    with open(os.path.join(BASE, "feedback.txt"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')} | {contact} | "
                f"{msg.replace(chr(10), ' ')}\n")
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
