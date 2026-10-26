import os
import re
import json
import time
import uuid
import html
import shutil
import asyncio
import tempfile
import subprocess
import ssl
import urllib.parse
import urllib.request

import yt_dlp
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ============================== الإعدادات ==============================
# هات API_ID و API_HASH من https://my.telegram.org  (قسم API development tools)
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

MAX_FILE_SIZE = int(1.95 * 1024 * 1024 * 1024)  # ~1.95 جيجا (حد تيليجرام للبوتات 2 جيجا)
FORMAT = "best[ext=mp4]/best"
UPDATE_EVERY = 3  # تحديث رسالة التقدم كل كام ثانية
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
SSL_CTX = ssl._create_unverified_context()  # بعض المواقع شهادتها ناقصة
MAX_QUALITY = 1080   # أعلى جودة بتظهر في قايمة Dailymotion / VK
JOB_TTL = 6 * 3600   # طلبات الجودات اللي ماتكمّلتش بتتمسح بعد 6 ساعات

app = Client(
    "box_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=ParseMode.HTML,
    max_concurrent_transmissions=4,
)

JOBS = {}  # job_id -> بيانات الطلب
SEP = "━━━━━━━━━━━━━━━━━━━━"


class Cancelled(Exception):
    pass


# ============================== أدوات مساعدة ==============================
def human_size(n):
    if not n:
        return "غير معروف"
    n = float(n)
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB" if n >= 1 else f"{n * 1024:.1f} MB"


def human_time(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} ثانية"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} دقيقة و {s} ثانية"
    h, m = divmod(m, 60)
    return f"{h} ساعة و {m} دقيقة"


def fmt_duration(sec):
    if not sec:
        return ""
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def green_bar(p, n=10):
    filled = max(0, min(n, int(p / 100 * n)))
    return "🟩" * filled + "⬜" * (n - filled)


def shade_bar(p, n=10):
    filled = max(0, min(n, int(p / 100 * n)))
    return "[" + "▓" * filled + "░" * (n - filled) + "]"


def header(title, label=None):
    text = f"<b>{html.escape(title)}</b>\n"
    if label:
        text += f"{html.escape(label)}\n"
    return text + f"{SEP}\n\n"


def download_text(title, done, total, speed):
    p = (done / total * 100) if total else 0
    speed_txt = f"{human_size(speed)}/s" if speed else "..."
    return (
        header(title)
        + "📥 <b>جاري تحميل فيديو...</b>\n\n"
        + f"{green_bar(p)}  <b>{p:.1f}%</b>\n"
        + f"📊 الحجم: {human_size(done)} / {human_size(total)}\n"
        + f"🚀 السرعة: {speed_txt}"
    )


def upload_text(title, done, total, speed, eta):
    p = (done / total * 100) if total else 0
    speed_txt = f"{human_size(speed)}/s" if speed else "..."
    eta_txt = human_time(eta) if eta is not None else "..."
    return (
        header(title)
        + "📤 <b>جاري رفع فيديو إلى تيليجرام...</b>\n\n"
        + f"{shade_bar(p)} <b>{p:.0f}%</b>\n"
        + f"📦 {human_size(done)} / {human_size(total)}\n\n"
        + f"🚀 السرعة: {speed_txt}\n"
        + f"⏱️ المتبقي تقريباً: {eta_txt}"
    )


async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except Exception:
        pass


# ============================== yt-dlp ==============================
def probe_size(info):
    """يحاول يعرف حجم الملف من الرابط المباشر لو yt-dlp ما عرفوش."""
    try:
        direct = info.get("url")
        if not direct:
            return None
        headers = dict(info.get("http_headers") or {})
        headers.setdefault("User-Agent", BROWSER_UA)
        headers["Range"] = "bytes=0-0"
        req = urllib.request.Request(direct, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                total = cr.rsplit("/", 1)[1]
                if total.isdigit():
                    return int(total)
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and r.status == 200:
                return int(cl)
    except Exception:
        pass
    return None


def ytdlp_base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "http_headers": {"User-Agent": BROWSER_UA},
    }


def fetch_info_ytdlp(url):
    opts = ytdlp_base_opts()
    opts["format"] = FORMAT
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("entries"):
            info = next(iter(info["entries"]))
        ext = info.get("ext") or "mp4"
        title = info.get("title") or "video"
        if not title.lower().endswith("." + ext.lower()):
            title = f"{title}.{ext}"
        size = info.get("filesize") or info.get("filesize_approx") or probe_size(info)
        return {"title": title, "size": size, "direct": False}


def direct_info(url):
    """للروابط المباشرة اللي yt-dlp مش بيعرف يقراها (SSL ناقص / 403)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Range": "bytes=0-0"}
    )
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            return None
        size = None
        cr = r.headers.get("Content-Range")
        cl = r.headers.get("Content-Length") or ""
        if cr and "/" in cr and cr.rsplit("/", 1)[1].isdigit():
            size = int(cr.rsplit("/", 1)[1])
        elif cl.isdigit() and r.status == 200:
            size = int(cl)
        name = None
        cd = r.headers.get("Content-Disposition") or ""
        m = re.search(r"filename\*?=(?:UTF-8\'\')?\"?([^\";]+)", cd)
        if m:
            name = urllib.parse.unquote(m.group(1))
        if not name:
            name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
        name = name or "video"
        if not os.path.splitext(name)[1]:
            name += ".mp4"
        return {"title": name, "size": size, "direct": True}


def fetch_info(url):
    try:
        return fetch_info_ytdlp(url)
    except Exception as first_error:
        try:
            info = direct_info(url)
        except Exception:
            info = None
        if info:
            return info
        raise first_error


def direct_download(url, tmp_dir, title):
    ext = os.path.splitext(title)[1] or ".mp4"
    name = "video" + ext
    path = os.path.join(tmp_dir, name)
    if shutil.which("aria2c"):
        r = subprocess.run(
            [
                "aria2c", "-x16", "-s16", "-k1M", "--file-allocation=none",
                "--max-tries=3", "--check-certificate=false",
                f"--user-agent={BROWSER_UA}",
                "--console-log-level=error", "--summary-interval=0",
                "--download-result=hide",
                "-d", tmp_dir, "-o", name, url,
            ],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not os.path.exists(path):
            raise RuntimeError("aria2c: " + (r.stderr or r.stdout or "failed")[-300:])
        return path
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f, 1024 * 1024)
    return path


def download(url, tmp_dir, hook, direct=False, title="video.mp4"):
    if direct:
        return direct_download(url, tmp_dir, title)
    opts = ytdlp_base_opts()
    opts.update({
        "format": FORMAT,
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
    })
    if shutil.which("aria2c"):
        opts["external_downloader"] = {"default": "aria2c"}
        opts["external_downloader_args"] = {
            "aria2c": ["-x16", "-s16", "-k1M", "--file-allocation=none"]
        }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [
        os.path.join(tmp_dir, f)
        for f in os.listdir(tmp_dir)
        if not f.endswith((".aria2", ".part", ".ytdl"))
    ]
    if not files:
        raise RuntimeError("لم يتم العثور على الملف بعد التنزيل.")
    return max(files, key=os.path.getsize)


def dir_bytes(tmp_dir):
    """حجم اللي اتنزّل فعلاً (بيتجاهل المساحات الفاضية) عشان شريط التقدم."""
    total = 0
    try:
        for f in os.listdir(tmp_dir):
            if f.endswith(".aria2"):
                continue
            st = os.stat(os.path.join(tmp_dir, f))
            total += st.st_blocks * 512 if st.st_blocks else st.st_size
    except Exception:
        pass
    return total


async def monitor_download(msg, job, tmp_dir, kb):
    """بيتابع حجم الملف كل كام ثانية ويحدّث الرسالة، ويوقف aria2 لو اتضغط إلغاء."""
    title = job["title"]
    last_bytes, last_t = 0, time.time()
    while True:
        await asyncio.sleep(UPDATE_EVERY)
        if job["cancel"]:
            subprocess.run(["pkill", "-f", tmp_dir], capture_output=True)
            return
        done = dir_bytes(tmp_dir)
        now = time.time()
        speed = (done - last_bytes) / (now - last_t) if now > last_t else 0
        last_bytes, last_t = done, now
        total = job["size"]
        if total and done > total:
            done = total
        await safe_edit(msg, download_text(title, done, total, speed), kb)


# ============================== ffprobe / ffmpeg ==============================
def probe(path):
    duration = width = height = 0
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=60,
        ).stdout
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = int(float((data.get("format") or {}).get("duration") or 0))
    except Exception:
        pass
    return duration, width, height


def make_thumb(path, duration, out_path):
    try:
        t = 3 if duration > 10 else 0
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(t), "-i", path,
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", out_path,
            ],
            capture_output=True, timeout=60,
        )
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception:
        pass
    return None


# ============================== Dailymotion / VK (اختيار الجودات) ==============================
QUALITY_SITES = ("dailymotion.com", "dai.ly", "vk.com", "vk.ru", "vkvideo.ru")


def is_quality_site(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in QUALITY_SITES)


def quality_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
    }


def fetch_info_q(url):
    with yt_dlp.YoutubeDL(quality_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("مفيش فيديو في الرابط ده.")
        info = entries[0]
    return info


def q_download_text(title, label, kind_ar, done, total, speed):
    p = (done / total * 100) if total else 0
    speed_txt = f"{human_size(speed)}/s" if speed else "..."
    return (
        header(title, label)
        + f"📥 <b>جاري تحميل {kind_ar}...</b>\n\n"
        + f"{green_bar(p)}  <b>{p:.1f}%</b>\n"
        + f"📊 الحجم: {human_size(done)} / {human_size(total)}\n"
        + f"🚀 السرعة: {speed_txt}"
    )


def q_upload_text(title, label, kind_ar, done, total, speed, eta):
    p = (done / total * 100) if total else 0
    speed_txt = f"{human_size(speed)}/s" if speed else "..."
    eta_txt = human_time(eta) if eta is not None else "..."
    return (
        header(title, label)
        + f"📤 <b>جاري رفع {kind_ar} إلى تيليجرام...</b>\n\n"
        + f"{shade_bar(p)} <b>{p:.0f}%</b>\n"
        + f"📦 {human_size(done)} / {human_size(total)}\n\n"
        + f"🚀 السرعة: {speed_txt}\n"
        + f"⏱️ المتبقي تقريباً: {eta_txt}"
    )


def fsize(f, duration):
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        return int(size)
    tbr = f.get("tbr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def build_options(info):
    """بيطلّع قايمة الجودات (فيديو وصوت) مع الأحجام التقريبية."""
    formats = info.get("formats") or []
    duration = info.get("duration") or 0

    audio_only = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    video_only = [
        f for f in formats
        if f.get("acodec") == "none" and f.get("vcodec") != "none" and f.get("height")
    ]
    combined = [
        f for f in formats
        if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("height")
    ]

    # أحسن صوت للدمج مع الفيديو (m4a أفضل للدمج في mp4)
    best_audio = None
    if audio_only:
        best_audio = max(
            audio_only, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or f.get("tbr") or 0)
        )

    # أحسن فورمات لكل جودة (بنفضّل H.264 / mp4 عشان يشتغل في تيليجرام)
    by_q = {}
    for f in video_only + combined:
        w, h = f.get("width"), f.get("height")
        q = min(w, h) if w and h else h
        if not q or q > MAX_QUALITY:
            continue
        vc = f.get("vcodec") or ""
        pref = (
            1 if vc.startswith(("avc1", "h264")) else 0,
            1 if f.get("ext") == "mp4" else 0,
            f.get("tbr") or 0,
        )
        cur = by_q.get(q)
        if cur is None or pref > cur[0]:
            by_q[q] = (pref, f)

    options = []
    for q in sorted(by_q, reverse=True):
        f = by_q[q][1]
        size = fsize(f, duration)
        if f.get("acodec") == "none" and best_audio:
            fmt = f"{f['format_id']}+{best_audio['format_id']}"
            a_size = fsize(best_audio, duration)
            size = (size or 0) + (a_size or 0) or None
        else:
            fmt = f["format_id"]
        options.append({"kind": "video", "label": f"{q}p", "fmt": fmt, "size": size})

    # لو مفيش جودات واضحة (بعض المنصات) نعرض خيار واحد "الأفضل"
    if not options and formats:
        options.append({"kind": "video", "label": "الأفضل", "fmt": "bv*+ba/b", "size": info.get("filesize") or info.get("filesize_approx")})

    # الصوت: أعلى جودة، وسط، أقل جودة (بحد أقصى 3)
    by_abr = {}
    for f in audio_only:
        abr = f.get("abr") or f.get("tbr")
        if not abr:
            continue
        key = round(abr)
        size = fsize(f, duration)
        cur = by_abr.get(key)
        if cur is None or (size or 0) > (cur[1] or 0):
            by_abr[key] = (f, size)
    abrs = sorted(by_abr, reverse=True)
    if len(abrs) > 3:
        abrs = [abrs[0], abrs[len(abrs) // 2], abrs[-1]]
    for key in abrs:
        f, size = by_abr[key]
        options.append(
            {"kind": "audio", "label": f"{key} kbps", "fmt": f["format_id"], "size": size, "abr": key}
        )
    return options


def download_one(url, opt, out_dir, hook):
    opts = quality_opts()
    opts.update(
        {
            "format": opt["fmt"],
            "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
            "progress_hooks": [hook],
            "concurrent_fragment_downloads": 4,
        }
    )
    if opt["kind"] == "video":
        opts["merge_output_format"] = "mp4"
    else:
        quality = str(min(max(int(opt.get("abr") or 192), 64), 320))
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}
        ]
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if not f.endswith((".part", ".ytdl", ".temp"))
    ]
    if not files:
        raise RuntimeError("لم يتم العثور على الملف بعد التنزيل.")
    return max(files, key=os.path.getsize)


def list_text(job):
    lines = [f"<b>{html.escape(job['title'])}</b>"]
    if job.get("duration"):
        lines.append(f"⏱️ {fmt_duration(job['duration'])}")
    lines.append("")
    lines.append("<b>الجودات والأحجام المتاحة:</b>")
    for o in job["options"]:
        icon = "🎬" if o["kind"] == "video" else "🎵"
        if o["size"]:
            size = ("≈ " if o["kind"] == "video" else "") + human_size(o["size"])
        else:
            size = "حجم غير معروف"
        lines.append(f"{icon} {o['label']} — {size}")
    lines.append("")
    lines.append("<b>اضغط على الجودات لتحديدها (يمكنك اختيار أكثر من جودة)، ثم اضغط زر التحميل:</b>")
    return "\n".join(lines)


def build_kb(job_id, job):
    rows, row = [], []
    for i, o in enumerate(job["options"]):
        icon = "🎬" if o["kind"] == "video" else "🎵"
        mark = "✅" if i in job["selected"] else icon
        row.append(InlineKeyboardButton(f"{mark} {o['label']}", callback_data=f"q:{job_id}:{i}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(f"⬇️ تحميل الجودات المحددة ({len(job['selected'])})", callback_data=f"go:{job_id}")]
    )
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data=f"x:{job_id}")])
    return InlineKeyboardMarkup(rows)



async def handle_quality_link(message, url):
    now = time.time()
    for jid in [j for j, v in JOBS.items() if now - v.get("created", now) > JOB_TTL]:
        JOBS.pop(jid, None)

    status = await message.reply_text("⏳ <b>جارِ جلب الجودات المتاحة...</b>", quote=True)

    try:
        info = await asyncio.to_thread(fetch_info_q, url)
        options = build_options(info)
    except Exception as e:
        err = html.escape(str(e))[:3000]
        await status.edit_text(f"❌ <b>تعذّر قراءة الرابط:</b>\n<code>{err}</code>")
        return

    if not options:
        await status.edit_text("❌ <b>ماقدرتش ألاقي جودات متاحة للرابط ده.</b>")
        return

    job_id = uuid.uuid4().hex[:10]
    job = {
        "url": url,
        "title": info.get("title") or "video",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "options": options,
        "selected": set(),
        "user_id": message.from_user.id,
        "cancel": False,
        "started": False,
        "created": now,
    }
    JOBS[job_id] = job
    await status.edit_text(list_text(job), reply_markup=build_kb(job_id, job))


@app.on_callback_query(filters.regex(r"^(q|go|x):"))
async def on_quality_button(client, cq):
    parts = cq.data.split(":")
    action, job_id = parts[0], parts[1]
    job = JOBS.get(job_id)

    if not job:
        await cq.answer("انتهت صلاحية الطلب، ابعت الرابط تاني.", show_alert=True)
        return
    if cq.from_user.id != job["user_id"]:
        await cq.answer("ده مش طلبك.", show_alert=True)
        return

    if action == "q":
        if job["started"]:
            await cq.answer("التحميل بدأ بالفعل.")
            return
        idx = int(parts[2])
        if idx in job["selected"]:
            job["selected"].discard(idx)
        else:
            job["selected"].add(idx)
        try:
            await cq.message.edit_reply_markup(build_kb(job_id, job))
        except Exception:
            pass
        await cq.answer()
        return

    if action == "x":
        job["cancel"] = True
        if not job["started"]:
            JOBS.pop(job_id, None)
            await safe_edit(cq.message, "❌ <b>تم الإلغاء.</b>")
        await cq.answer("جارِ الإلغاء...")
        return

    # action == "go"
    if job["started"]:
        await cq.answer("التحميل بدأ بالفعل.")
        return
    if not job["selected"]:
        await cq.answer("اختار جودة واحدة على الأقل.", show_alert=True)
        return
    job["started"] = True
    await cq.answer()
    asyncio.create_task(q_run_job(client, cq.message, job_id, job))


async def q_process_one(client, msg, job, opt, idx, n, work_dir, loop, cancel_kb):
    title = job["title"]
    icon = "🎬" if opt["kind"] == "video" else "🎵"
    label = f"{icon} {opt['label']}  ({idx}/{n})"
    kind_ar = "فيديو" if opt["kind"] == "video" else "صوت"
    os.makedirs(work_dir, exist_ok=True)

    await safe_edit(msg, q_download_text(title, label, kind_ar, 0, opt["size"], None), cancel_kb)

    state = {"last": 0, "files": {}}

    def hook(d):
        if job["cancel"]:
            raise Cancelled()
        state["files"][d.get("filename") or ""] = d.get("downloaded_bytes") or 0
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - state["last"] < UPDATE_EVERY:
            return
        state["last"] = now
        done = sum(state["files"].values())
        total = opt["size"] or d.get("total_bytes") or d.get("total_bytes_estimate")
        if total and done > total:
            done = total
        text = q_download_text(title, label, kind_ar, done, total, d.get("speed"))
        asyncio.run_coroutine_threadsafe(safe_edit(msg, text, cancel_kb), loop)

    path = await asyncio.to_thread(download_one, job["url"], opt, work_dir, hook)
    if job["cancel"]:
        raise Cancelled()

    real_size = os.path.getsize(path)
    if real_size > MAX_FILE_SIZE:
        return "too_big"

    up = {"start": time.time(), "last": 0}

    async def upload_progress(current, total):
        if job["cancel"]:
            client.stop_transmission()
        now = time.time()
        if now - up["last"] < UPDATE_EVERY and current < total:
            return
        up["last"] = now
        elapsed = now - up["start"]
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed else None
        await safe_edit(msg, q_upload_text(title, label, kind_ar, current, total, speed, eta), cancel_kb)

    await safe_edit(msg, q_upload_text(title, label, kind_ar, 0, real_size, None, None), cancel_kb)

    short = title[:150]
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", title[:100]).strip() or "video"
    caption = f"<b>{html.escape(short)}</b>\n{icon} {html.escape(opt['label'])}"

    if opt["kind"] == "audio":
        duration, _, _ = await asyncio.to_thread(probe, path)
        sent = await client.send_audio(
            chat_id=msg.chat.id,
            audio=path,
            caption=caption,
            title=short,
            performer=job.get("uploader") or None,
            duration=duration,
            file_name=f"{safe_name}.mp3",
            progress=upload_progress,
        )
    else:
        duration, width, height = await asyncio.to_thread(probe, path)
        thumb = await asyncio.to_thread(
            make_thumb, path, duration, os.path.join(work_dir, "thumb.jpg")
        )
        if path.lower().endswith((".mp4", ".mov", ".m4v")):
            sent = await client.send_video(
                chat_id=msg.chat.id,
                video=path,
                caption=caption,
                duration=duration,
                width=width,
                height=height,
                thumb=thumb,
                file_name=f"{safe_name}.mp4",
                supports_streaming=True,
                progress=upload_progress,
            )
        else:
            sent = await client.send_document(
                chat_id=msg.chat.id,
                document=path,
                caption=caption,
                thumb=thumb,
                progress=upload_progress,
            )

    if sent is None or job["cancel"]:
        raise Cancelled()
    return "ok"


async def q_run_job(client, msg, job_id, job):
    loop = asyncio.get_running_loop()
    tmp_root = tempfile.mkdtemp(prefix="q_")
    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ إلغاء التحميل", callback_data=f"x:{job_id}")]]
    )
    chosen = [job["options"][i] for i in sorted(job["selected"])]
    n = len(chosen)
    skipped, errors = [], []

    try:
        for idx, opt in enumerate(chosen, 1):
            if job["cancel"]:
                raise Cancelled()
            work_dir = os.path.join(tmp_root, str(idx))
            try:
                if opt["size"] and opt["size"] > MAX_FILE_SIZE:
                    skipped.append(opt["label"])
                    continue
                result = await q_process_one(client, msg, job, opt, idx, n, work_dir, loop, cancel_kb)
                if result == "too_big":
                    skipped.append(opt["label"])
            except Cancelled:
                raise
            except Exception as e:
                if job["cancel"]:
                    raise Cancelled()
                errors.append((opt["label"], str(e)))
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

        try:
            await msg.delete()
        except Exception:
            pass

        notes = []
        if skipped:
            notes.append("⚠️ الجودات دي أكبر من 2 جيجا واتخطّيتها: " + "، ".join(skipped))
        for label, err in errors:
            notes.append(f"❌ فشل تحميل {label}:\n<code>{html.escape(err)[:500]}</code>")
        if notes:
            await client.send_message(msg.chat.id, "\n\n".join(notes))

    except Cancelled:
        await safe_edit(msg, "❌ <b>تم الإلغاء.</b>")
    except Exception as e:
        err = html.escape(str(e))[:3000]
        await safe_edit(msg, f"❌ <b>حدث خطأ أثناء المعالجة:</b>\n<code>{err}</code>")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        JOBS.pop(job_id, None)


# ============================== الهاندلرز ==============================
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    name = html.escape(message.from_user.first_name or "")
    await message.reply_text(
        f"أهلاً بيك يا {name} في بوت التحميلات 🎬⚡\n"
        "ابعت لي رابط الفيديو (أقل من 2 جيجا) وهحمله وأرفعه لك.\n"
        "ولو الرابط من Dailymotion أو VK Video هعرض لك الجودات المتاحة تختار منها."
    )


@app.on_message(filters.text & filters.private & ~filters.command(["start"]))
async def on_link(client, message):
    url = message.text.strip()
    if not url.lower().startswith(("http://", "https://")):
        await message.reply_text("⚠️ من فضلك ابعت رابط صحيح يبدأ بـ http أو https.")
        return

    # روابط Dailymotion و VK بتعرض قايمة جودات تختار منها
    if is_quality_site(url):
        await handle_quality_link(message, url)
        return

    status = await message.reply_text("⏳ <b>جارِ التحقق من الرابط...</b>", quote=True)

    try:
        info = await asyncio.to_thread(fetch_info, url)
    except Exception as e:
        err = html.escape(str(e))[:3000]
        await status.edit_text(f"❌ <b>تعذّر قراءة الرابط:</b>\n<code>{err}</code>")
        return

    size = info["size"]
    if size and size > MAX_FILE_SIZE:
        await status.edit_text(
            "❌ <b>حجم الملف أكبر من 2 جيجا!</b>\n"
            "تيليجرام لا يسمح للبوتات برفع ملفات بالحجم ده."
        )
        return

    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {
        "url": url,
        "title": info["title"],
        "size": size,
        "user_id": message.from_user.id,
        "cancel": False,
        "started": False,
        "direct": info.get("direct", False),
    }

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"⬇️ 🎬 فيديو — {human_size(size)}", callback_data=f"dl:{job_id}")],
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel:{job_id}")],
        ]
    )
    await status.edit_text(
        f"<b>{html.escape(info['title'])}</b>\n\n"
        f"<b>الحجم:</b> {human_size(size)}\n"
        "<b>اختر لبدء التنزيل:</b>",
        reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^(dl|cancel):"))
async def on_button(client, cq):
    action, job_id = cq.data.split(":", 1)
    job = JOBS.get(job_id)

    if not job:
        await cq.answer("انتهت صلاحية الطلب.", show_alert=True)
        return
    if cq.from_user.id != job["user_id"]:
        await cq.answer("ده مش طلبك.", show_alert=True)
        return

    if action == "cancel":
        job["cancel"] = True
        if not job["started"]:
            JOBS.pop(job_id, None)
            await safe_edit(cq.message, "❌ <b>تم الإلغاء.</b>")
        await cq.answer("جارِ الإلغاء...")
        return

    if job["started"]:
        await cq.answer("العملية بدأت بالفعل.")
        return

    job["started"] = True
    await cq.answer()
    asyncio.create_task(run_job(client, cq.message, job_id, job))


async def run_job(client, msg, job_id, job):
    loop = asyncio.get_running_loop()
    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    title = job["title"]
    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ إلغاء التحميل", callback_data=f"cancel:{job_id}")]]
    )

    try:
        await safe_edit(msg, download_text(title, 0, job["size"], None), cancel_kb)

        # ---------- التحميل ----------
        state = {"last": 0}

        def hook(d):
            if job["cancel"]:
                raise Cancelled()
            if d.get("status") != "downloading":
                return
            now = time.time()
            if now - state["last"] < UPDATE_EVERY:
                return
            state["last"] = now
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or job["size"]
            text = download_text(title, done, total, d.get("speed"))
            asyncio.run_coroutine_threadsafe(safe_edit(msg, text, cancel_kb), loop)

        monitor = asyncio.create_task(monitor_download(msg, job, tmp_dir, cancel_kb))
        try:
            path = await asyncio.to_thread(
                download, job["url"], tmp_dir, hook, job["direct"], title
            )
        finally:
            monitor.cancel()

        if job["cancel"]:
            raise Cancelled()

        real_size = os.path.getsize(path)
        if real_size > MAX_FILE_SIZE:
            await safe_edit(msg, "❌ <b>الملف المنزّل أكبر من 2 جيجا، مش هينفع يترفع.</b>")
            return

        # ---------- بيانات الفيديو والصورة المصغرة ----------
        duration, width, height = await asyncio.to_thread(probe, path)
        thumb = await asyncio.to_thread(
            make_thumb, path, duration, os.path.join(tmp_dir, "thumb.jpg")
        )

        # ---------- الرفع ----------
        up = {"start": time.time(), "last": 0}

        async def upload_progress(current, total):
            if job["cancel"]:
                client.stop_transmission()
            now = time.time()
            if now - up["last"] < UPDATE_EVERY and current < total:
                return
            up["last"] = now
            elapsed = now - up["start"]
            speed = current / elapsed if elapsed > 0 else 0
            eta = (total - current) / speed if speed else None
            await safe_edit(msg, upload_text(title, current, total, speed, eta), cancel_kb)

        await safe_edit(msg, upload_text(title, 0, real_size, None, None), cancel_kb)

        sent = await client.send_video(
            chat_id=msg.chat.id,
            video=path,
            caption=f"<b>{html.escape(title)}</b>",
            duration=duration,
            width=width,
            height=height,
            thumb=thumb,
            file_name=title,
            supports_streaming=True,
            progress=upload_progress,
        )

        if sent is None or job["cancel"]:
            await safe_edit(msg, "❌ <b>تم إلغاء الرفع.</b>")
        else:
            await msg.delete()

    except Cancelled:
        await safe_edit(msg, "❌ <b>تم إلغاء التحميل.</b>")
    except Exception as e:
        if job["cancel"]:
            await safe_edit(msg, "❌ <b>تم الإلغاء.</b>")
        else:
            err = html.escape(str(e))[:3000]
            await safe_edit(msg, f"❌ <b>حدث خطأ أثناء المعالجة:</b>\n<code>{err}</code>")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        JOBS.pop(job_id, None)


if __name__ == "__main__":
    print("Bot is running...")
    app.run()
