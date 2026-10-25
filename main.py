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

app = Client(
    "box_bot",
    API_ID = ...
API_HASH = ...
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
...
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


def green_bar(p, n=10):
    filled = max(0, min(n, int(p / 100 * n)))
    return "🟩" * filled + "⬜" * (n - filled)


def shade_bar(p, n=10):
    filled = max(0, min(n, int(p / 100 * n)))
    return "[" + "▓" * filled + "░" * (n - filled) + "]"


def header(title):
    return f"<b>{html.escape(title)}</b>\n{SEP}\n\n"


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
def fetch_info(url):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": FORMAT}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("entries"):
            info = next(iter(info["entries"]))
        ext = info.get("ext") or "mp4"
        title = info.get("title") or "video"
        if not title.lower().endswith("." + ext.lower()):
            title = f"{title}.{ext}"
        size = info.get("filesize") or info.get("filesize_approx")
        return {"title": title, "size": size}


def download(url, tmp_dir, hook):
    opts = {
        "format": FORMAT,
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)]
    if not files:
        raise RuntimeError("لم يتم العثور على الملف بعد التنزيل.")
    return max(files, key=os.path.getsize)


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


# ============================== الهاندلرز ==============================
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    name = html.escape(message.from_user.first_name or "")
    await message.reply_text(
        f"أهلاً بيك يا {name} في بوت التحميلات 🎬⚡\n"
        "ابعت لي رابط الفيديو (أقل من 2 جيجا) وهحمله وأرفعه لك."
    )


@app.on_message(filters.text & filters.private & ~filters.command(["start"]))
async def on_link(client, message):
    url = message.text.strip()
    if not url.lower().startswith(("http://", "https://")):
        await message.reply_text("⚠️ من فضلك ابعت رابط صحيح يبدأ بـ http أو https.")
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

        path = await asyncio.to_thread(download, job["url"], tmp_dir, hook)

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
