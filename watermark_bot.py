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
import urllib.error
import urllib.request

import yt_dlp
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ============================== الإعدادات ==============================
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("WM_BOT_TOKEN", "")   # توكن بوت جديد من BotFather

MAX_FILE_SIZE = int(1.95 * 1024 * 1024 * 1024)   # ~1.95 جيجا
UPDATE_EVERY = 3                                  # تحديث رسالة التقدم كل كام ثانية
DATA_DIR = "wm_data"                              # هنا بيتحفظ الشعارات والإعدادات
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
SEP = "━━━━━━━━━━━━━━━━━━━━"

os.makedirs(DATA_DIR, exist_ok=True)

app = Client(
    "wm_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=ParseMode.HTML,
    max_concurrent_transmissions=4,
)

JOBS = {}                      # job_id -> بيانات
ACTIVE = set()                 # المستخدمين اللي عندهم ملف شغال
STATE = {}                     # user_id -> "logo" أو "text" (بنستنى منه حاجة)
PROC_LOCK = asyncio.Lock()     # ملف واحد بيتعالج في الوقت (عشان الموبايل)

# ---------- خيارات الضبط ----------
POS_NAMES = {
    "tl": "أعلى يسار", "tc": "أعلى وسط", "tr": "أعلى يمين",
    "ml": "وسط يسار", "mc": "المنتصف", "mr": "وسط يمين",
    "bl": "أسفل يسار", "bc": "أسفل وسط", "br": "أسفل يمين",
}
POS_ARROWS = {
    "tl": "↖️", "tc": "⬆️", "tr": "↗️",
    "ml": "⬅️", "mc": "⏺️", "mr": "➡️",
    "bl": "↙️", "bc": "⬇️", "br": "↘️",
}
POS_GRID = [["tl", "tc", "tr"], ["ml", "mc", "mr"], ["bl", "bc", "br"]]
SIZES = [5, 8, 10, 15, 20, 25, 30]
OPACITIES = [100, 80, 60, 40, 20]
MARGINS = [0, 1, 2, 3, 5, 8]
# الاسم ، preset ، crf ، أقصى ارتفاع (None = زي الأصل)
SPEEDS = {
    "turbo": ("⚡ سريع جداً (720p)", "ultrafast", 29, 720),
    "fast": ("🚀 سريع (جودة الأصل)", "ultrafast", 27, None),
    "quality": ("🎞️ جودة أعلى", "veryfast", 23, None),
}
DEFAULTS = {
    "kind": "image", "text": "", "size": 15, "opacity": 80,
    "margin": 2, "pos": "tr", "speed": "turbo",
}


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
    return f"{n:.2f} GB"


def human_time(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} ثانية"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} دقيقة و {s} ثانية"
    h, m = divmod(m, 60)
    return f"{h} ساعة و {m} دقيقة"


def clock(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def green_bar(p, n=10):
    filled = max(0, min(n, int(p / 100 * n)))
    return "🟩" * filled + "⬜" * (n - filled)


def shade_bar(p, n=15):
    filled = max(0, min(n, int(p / 100 * n)))
    return "[" + "█" * filled + "░" * (n - filled) + "]"


def header(title):
    return f"<b>{html.escape(title)}</b>\n{SEP}\n\n"


def dl_text(title, done, total, speed):
    p = (done / total * 100) if total else 0
    speed_txt = f"{human_size(speed)}/s" if speed else "..."
    return (
        header(title)
        + "📥 <b>جاري تحميل الفيديو...</b>\n\n"
        + f"{green_bar(p)}  <b>{p:.1f}%</b>\n"
        + f"📊 الحجم: {human_size(done)} / {human_size(total)}\n"
        + f"🚀 السرعة: {speed_txt}"
    )


def proc_text(title, cur, total, speed, eta):
    p = (cur / total * 100) if total else 0
    speed_txt = f"{speed:.2f}x" if speed else "..."
    eta_txt = human_time(eta) if eta is not None else "..."
    return (
        header(title)
        + "🎨 <b>جاري إضافة العلامة المائية...</b>\n\n"
        + f"{green_bar(p)}  <b>{p:.1f}%</b>\n"
        + f"🎞️ {clock(cur)} / {clock(total)}\n"
        + f"⚙️ سرعة المعالجة: {speed_txt}\n"
        + f"⏳ المتبقي تقريباً: {eta_txt}"
    )


def up_text(title, done, total, speed, eta):
    p = (done / total * 100) if total else 0
    speed_txt = f"{human_size(speed)}/s" if speed else "..."
    eta_txt = human_time(eta) if eta is not None else "..."
    return (
        header(title)
        + "📤 <b>جاري رفع الفيديو إلى تيليجرام...</b>\n\n"
        + f"{shade_bar(p)} <b>{p:.0f}%</b>\n"
        + f"📦 {human_size(done)} / {human_size(total)}\n\n"
        + f"🚀 السرعة: {speed_txt}\n"
        + f"⏱️ المتبقي تقريباً: {eta_txt}"
    )


# ============================== أزرار ملونة (Bot API 9.4) ==============================
# style: "success" = أخضر ، "danger" = أحمر ، "primary" = أزرق ، None = عادي
def btn(text, data, style=None):
    b = {"text": text, "callback_data": data}
    if style:
        b["style"] = style
    return b


def plain_markup(rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row] for row in rows]
    )


def tg_api(method, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


async def edit_with_buttons(msg, text, rows=None):
    """بيعدّل الرسالة بأزرار ملونة، ولو فشل بيرجع للأزرار العادية."""
    payload = {
        "chat_id": msg.chat.id,
        "message_id": msg.id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if rows:
        payload["reply_markup"] = {"inline_keyboard": rows}
    try:
        await asyncio.to_thread(tg_api, "editMessageText", payload)
        return
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        if "not modified" in body or e.code == 429:
            return
    except Exception:
        pass
    await msg.edit_text(text, reply_markup=plain_markup(rows) if rows else None)


async def safe_edit(msg, text, kb=None):
    try:
        await edit_with_buttons(msg, text, kb)
    except Exception:
        pass


# ============================== الإعدادات (بتتحفظ لكل مستخدم) ==============================
try:
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        SETTINGS = json.load(f)
except Exception:
    SETTINGS = {}


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, ensure_ascii=False)
    except Exception:
        pass


def get_user(uid):
    u = SETTINGS.setdefault(str(uid), {})
    for k, v in DEFAULTS.items():
        u.setdefault(k, v)
    return u


def logo_path(uid):
    return os.path.join(DATA_DIR, f"logo_{uid}")


def text_png_path(uid):
    return os.path.join(DATA_DIR, f"text_{uid}.png")


def active_wm(uid, u):
    p = text_png_path(uid) if u["kind"] == "text" else logo_path(uid)
    return p if os.path.exists(p) else None


def cycle(values, current):
    try:
        return values[(values.index(current) + 1) % len(values)]
    except ValueError:
        return values[0]


# ============================== علامة نصية (Pillow) ==============================
def find_font(text):
    arabic = any("\u0600" <= c <= "\u06FF" for c in text)
    dirs = ["/system/fonts", "/system/font", "/usr/share/fonts", os.path.expandvars("$PREFIX/share/fonts")]
    names = (
        ["NotoNaskhArabic-Regular.ttf", "NotoSansArabic-Regular.ttf", "NotoSansArabicUI-Regular.ttf",
         "DroidSansArabic.ttf", "NotoKufiArabic-Regular.ttf"]
        if arabic else ["Roboto-Regular.ttf", "DroidSans.ttf", "NotoSans-Regular.ttf", "DejaVuSans.ttf"]
    )
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    # بحث عام عن أي خط يناسب
    key = "arabic" if arabic else ""
    for d in dirs:
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    if f.lower().endswith((".ttf", ".otf")) and key in f.lower():
                        return os.path.join(root, f)
    return None


def render_text_png(text, out_path):
    from PIL import Image, ImageDraw, ImageFont

    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        text = get_display(arabic_reshaper.reshape(text))
    except Exception:
        pass
    font_path = find_font(text)
    if not font_path:
        raise RuntimeError("ملقتش خط مناسب على الجهاز.")
    font = ImageFont.truetype(font_path, 140)
    stroke = 6
    probe_img = Image.new("RGBA", (10, 10))
    bbox = ImageDraw.Draw(probe_img).textbbox((0, 0), text, font=font, stroke_width=stroke)
    w, h = bbox[2] - bbox[0] + 24, bbox[3] - bbox[1] + 24
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text(
        (12 - bbox[0], 12 - bbox[1]), text, font=font,
        fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255),
    )
    img.save(out_path)


# ============================== ffmpeg ==============================
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
        duration = float((data.get("format") or {}).get("duration") or 0)
    except Exception:
        pass
    return duration, width, height


def make_thumb(path, duration, out_path):
    try:
        t = 3 if duration > 10 else 0
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", path, "-frames:v", "1",
             "-vf", "scale=320:-2", "-q:v", "5", out_path],
            capture_output=True, timeout=60,
        )
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception:
        pass
    return None


def build_filter(src_w, src_h, u):
    """بيحسب الفلتر: تصغير اختياري + العلامة بالحجم والشفافية والموضع."""
    _, _, _, max_h = SPEEDS.get(u["speed"], SPEEDS["turbo"])
    out_w, out_h = src_w, src_h
    if max_h and src_h > max_h:
        out_h = max_h
        out_w = int(round(src_w * max_h / src_h / 2)) * 2
        scale = f"scale=-2:{max_h},"
    elif src_w % 2 or src_h % 2:
        out_w, out_h = src_w - src_w % 2, src_h - src_h % 2
        scale = f"scale={out_w}:{out_h},"
    else:
        scale = ""
    wm_w = max(16, int(out_w * u["size"] / 100))
    wm_w -= wm_w % 2
    mx = int(out_w * u["margin"] / 100)
    my = int(out_h * u["margin"] / 100)
    xs = {"l": f"{mx}", "c": "(W-w)/2", "r": f"W-w-{mx}"}
    ys = {"t": f"{my}", "m": "(H-h)/2", "b": f"H-h-{my}"}
    pos = u["pos"] if u["pos"] in POS_NAMES else "tr"
    x, y = xs[pos[1]], ys[pos[0]]
    alpha = max(0.05, min(1.0, u["opacity"] / 100))
    return (
        f"[0:v]{scale}format=yuv420p[bg];"
        f"[1:v]format=rgba,scale={wm_w}:-1,colorchannelmixer=aa={alpha:.2f}[wm];"
        f"[bg][wm]overlay=x={x}:y={y}:format=auto[out]"
    )


def build_cmd(in_path, wm_path, out_path, u, src_w, src_h):
    _, preset, crf, _ = SPEEDS.get(u["speed"], SPEEDS["turbo"])
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        "-progress", "pipe:1",
        "-i", in_path, "-i", wm_path,
        "-filter_complex", build_filter(src_w, src_h, u),
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-sn",
        "-movflags", "+faststart", out_path,
    ]


async def run_ffmpeg(job, cmd, total_sec, on_progress):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    job["proc"] = proc
    err_task = asyncio.create_task(proc.stderr.read())
    cur, speed = 0.0, None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        line = line.decode(errors="ignore").strip()
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                cur = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                pass
        elif line.startswith("speed="):
            m = re.match(r"speed=\s*([\d.]+)x", line)
            if m:
                speed = float(m.group(1))
        elif line.startswith("progress="):
            await on_progress(cur, speed)
    rc = await proc.wait()
    err = (await err_task).decode(errors="ignore")
    job["proc"] = None
    if job["cancel"]:
        raise Cancelled()
    if rc != 0:
        raise RuntimeError("ffmpeg: " + err[-600:])


# ============================== لوحة الإعدادات ==============================
def panel_text(uid, u):
    kind = "نص 📝" if u["kind"] == "text" else "صورة / شعار 🖼️"
    has = "✅ جاهز" if active_wm(uid, u) else "❌ لسه ما اترفعش"
    speed_name = SPEEDS.get(u["speed"], SPEEDS["turbo"])[0]
    lines = [
        "💡 <b>اضبط مظهر علامتك المائية بدقة لتناسب فيديوهاتك:</b>",
        "",
        f"• النوع الحالي: {kind}",
        f"• العلامة: {has}",
    ]
    if u["kind"] == "text" and u["text"]:
        lines.append(f"• النص: {html.escape(u['text'])}")
    lines += [
        f"• الموضع: {POS_NAMES[u['pos']]}",
        f"• الشفافية: {u['opacity']}%",
        f"• الحجم: {u['size']}%",
        f"• الهامش: {u['margin']}%",
        f"• نمط المعالجة: {speed_name}",
        "",
        "📩 <b>ابعت الفيديو (لحد 2 جيجا) أو رابط مباشر وهضيف عليه العلامة.</b>",
    ]
    return "\n".join(lines)


def panel_rows(u):
    return [
        [btn("📝 علامة نصية", "s:text", "primary"), btn("🖼️ رفع الشعار", "s:logo", "primary")],
        [btn(f"📐 الحجم: {u['size']}%", "s:size", "primary"), btn(f"🌗 الشفافية: {u['opacity']}%", "s:opacity", "primary")],
        [btn(f"📍 الموضع: {POS_NAMES[u['pos']]}", "s:pos", "primary"), btn(f"↔️ الهامش: {u['margin']}%", "s:margin", "primary")],
        [btn(f"🚀 نمط المعالجة: {SPEEDS.get(u['speed'], SPEEDS['turbo'])[0]}", "s:speed", "primary")],
        [btn("👁️ معاينة العلامة المائية", "s:preview", "success")],
    ]


def pos_rows(u):
    rows = []
    for line in POS_GRID:
        rows.append([
            btn(("✅ " if k == u["pos"] else "") + f"{POS_ARROWS[k]} {POS_NAMES[k]}", f"s:pos:{k}", "success" if k == u["pos"] else "primary")
            for k in line
        ])
    rows.append([btn("🔙 رجوع", "s:back", "danger")])
    return rows


async def send_panel(message):
    u = get_user(message.from_user.id)
    m = await message.reply_text(panel_text(message.from_user.id, u))
    await safe_edit(m, panel_text(message.from_user.id, u), panel_rows(u))


@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    STATE.pop(message.from_user.id, None)
    await send_panel(message)


@app.on_callback_query(filters.regex(r"^s:"))
async def on_settings(client, cq):
    uid = cq.from_user.id
    u = get_user(uid)
    parts = cq.data.split(":")
    action = parts[1]
    msg = cq.message

    if action == "size":
        u["size"] = cycle(SIZES, u["size"])
    elif action == "opacity":
        u["opacity"] = cycle(OPACITIES, u["opacity"])
    elif action == "margin":
        u["margin"] = cycle(MARGINS, u["margin"])
    elif action == "speed":
        keys = list(SPEEDS)
        u["speed"] = keys[(keys.index(u["speed"]) + 1) % len(keys)] if u["speed"] in keys else keys[0]
    elif action == "pos":
        if len(parts) == 3 and parts[2] in POS_NAMES:
            u["pos"] = parts[2]
            save_settings()
            await edit_with_buttons(msg, panel_text(uid, u), panel_rows(u))
            await cq.answer("تم")
            return
        await edit_with_buttons(msg, "📍 <b>اختار موضع العلامة:</b>", pos_rows(u))
        await cq.answer()
        return
    elif action == "back":
        await edit_with_buttons(msg, panel_text(uid, u), panel_rows(u))
        await cq.answer()
        return
    elif action == "logo":
        STATE[uid] = "logo"
        await cq.answer()
        await msg.reply_text(
            "🖼️ <b>ابعت صورة الشعار دلوقتي.</b>\n"
            "الأفضل تبعتها <b>كملف (File/Document)</b> وبصيغة PNG عشان الخلفية الشفافة تفضل شفافة."
        )
        return
    elif action == "text":
        STATE[uid] = "text"
        await cq.answer()
        await msg.reply_text("📝 <b>اكتب النص اللي عايزه يظهر كعلامة مائية:</b>")
        return
    elif action == "preview":
        wm = active_wm(uid, u)
        if not wm:
            await cq.answer("ارفع شعار أو اكتب نص الأول.", show_alert=True)
            return
        await cq.answer("جارِ تجهيز المعاينة...")
        asyncio.create_task(send_preview(client, msg.chat.id, uid, u, wm))
        return

    save_settings()
    await edit_with_buttons(msg, panel_text(uid, u), panel_rows(u))
    await cq.answer()


async def send_preview(client, chat_id, uid, u, wm):
    out = os.path.join(DATA_DIR, f"preview_{uid}.jpg")
    fg = build_filter(1280, 720, {**u, "speed": "fast"})
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x33475b:s=1280x720:d=1",
        "-i", wm, "-filter_complex", fg, "-map", "[out]", "-frames:v", "1", out,
    ]
    try:
        r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(r.stderr[-300:])
        await client.send_photo(chat_id, out, caption="👁️ <b>معاينة العلامة المائية بالإعدادات الحالية</b>")
    except Exception as e:
        await client.send_message(chat_id, f"❌ تعذّر تجهيز المعاينة:\n<code>{html.escape(str(e))[:500]}</code>")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


# ============================== استقبال الشعار والنص ==============================
async def save_logo(client, message):
    uid = message.from_user.id
    path = logo_path(uid)
    await client.download_media(message, file_name=path)
    u = get_user(uid)
    u["kind"] = "image"
    save_settings()
    STATE.pop(uid, None)
    await message.reply_text("✅ <b>تم حفظ الشعار.</b>")
    await send_panel(message)


@app.on_message(filters.photo & filters.private)
async def on_photo(client, message):
    if STATE.get(message.from_user.id) == "logo":
        await save_logo(client, message)
    else:
        await message.reply_text("لو عايز تغيّر الشعار اضغط /start ثم 🖼️ رفع الشعار.")


@app.on_message(filters.document & filters.private)
async def on_document(client, message):
    mime = (message.document.mime_type or "").lower()
    uid = message.from_user.id
    if mime.startswith("image/"):
        if STATE.get(uid) == "logo":
            await save_logo(client, message)
        else:
            await message.reply_text("لو عايز تغيّر الشعار اضغط /start ثم 🖼️ رفع الشعار.")
        return
    name = (message.document.file_name or "").lower()
    if mime.startswith("video/") or name.endswith((".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts")):
        await launch_job(client, message, "tg", message)
    else:
        await message.reply_text("⚠️ ابعت فيديو أو صورة الشعار بس.")


@app.on_message(filters.video & filters.private)
async def on_video(client, message):
    await launch_job(client, message, "tg", message)


@app.on_message(filters.text & filters.private & ~filters.command(["start"]))
async def on_text(client, message):
    uid = message.from_user.id
    text = message.text.strip()

    if STATE.get(uid) == "text":
        try:
            await asyncio.to_thread(render_text_png, text, text_png_path(uid))
        except ImportError:
            await message.reply_text(
                "⚠️ محتاج مكتبة Pillow للنص. نفّذ في Termux:\n"
                "<code>pkg install python-pillow</code>\n"
                "<code>pip install arabic-reshaper python-bidi</code>"
            )
            return
        except Exception as e:
            await message.reply_text(f"❌ تعذّر عمل العلامة النصية:\n<code>{html.escape(str(e))[:500]}</code>")
            return
        u = get_user(uid)
        u["kind"], u["text"] = "text", text[:60]
        save_settings()
        STATE.pop(uid, None)
        await message.reply_text("✅ <b>تم حفظ العلامة النصية.</b>")
        await send_panel(message)
        return

    if text.lower().startswith(("http://", "https://")):
        await launch_job(client, message, "url", text)
        return

    await message.reply_text("📩 ابعت فيديو أو رابط مباشر، أو اضغط /start لضبط العلامة.")


# ============================== تشغيل الطلب ==============================
@app.on_callback_query(filters.regex(r"^c:"))
async def on_cancel(client, cq):
    job_id = cq.data.split(":", 1)[1]
    job = JOBS.get(job_id)
    if not job:
        await cq.answer("انتهى الطلب.", show_alert=True)
        return
    if cq.from_user.id != job["user_id"]:
        await cq.answer("ده مش طلبك.", show_alert=True)
        return
    job["cancel"] = True
    proc = job.get("proc")
    if proc:
        try:
            proc.kill()
        except Exception:
            pass
    await cq.answer("جارِ الإلغاء...")


async def launch_job(client, message, kind, payload):
    uid = message.from_user.id
    u = get_user(uid)
    if not active_wm(uid, u):
        await message.reply_text("⚠️ الأول ارفع شعار أو اكتب نص: اضغط /start.")
        return
    if uid in ACTIVE:
        await message.reply_text("⏳ عندك ملف قيد المعالجة، استنى لحد ما يخلص.")
        return
    ACTIVE.add(uid)
    asyncio.create_task(run_job(client, message, kind, payload))


def download_url(url, out_dir, hook):
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "nocheckcertificate": True, "format": "best",
        "outtmpl": os.path.join(out_dir, "in.%(ext)s"),
        "progress_hooks": [hook],
        "http_headers": {"User-Agent": BROWSER_UA},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if not f.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("لم يتم العثور على الملف بعد التنزيل.")
    title = info.get("title") or "video"
    ext = info.get("ext") or "mp4"
    return max(files, key=os.path.getsize), f"{title}.{ext}"


async def run_job(client, message, kind, payload):
    uid = message.from_user.id
    u = dict(get_user(uid))
    wm = active_wm(uid, u)
    job_id = uuid.uuid4().hex[:10]
    job = {"user_id": uid, "cancel": False, "proc": None}
    JOBS[job_id] = job
    cancel_rows = [[btn("❌ إلغاء", f"c:{job_id}", "danger")]]
    work_dir = tempfile.mkdtemp(prefix="wm_")
    loop = asyncio.get_running_loop()
    status = await message.reply_text("⏳ <b>جارِ التجهيز...</b>", quote=True)

    try:
        if PROC_LOCK.locked():
            await safe_edit(status, "⏳ <b>فيه ملف بيتعالج قبلك، هبدأ أول ما يخلص...</b>", cancel_rows)
        async with PROC_LOCK:
            if job["cancel"]:
                raise Cancelled()

            # ---------- 1) التحميل ----------
            if kind == "tg":
                media = message.video or message.document
                title = getattr(media, "file_name", None) or "video.mp4"
                size = getattr(media, "file_size", 0) or 0
                if size > MAX_FILE_SIZE:
                    await safe_edit(status, "❌ <b>الملف أكبر من 2 جيجا.</b>")
                    return
                in_path = os.path.join(work_dir, "in" + (os.path.splitext(title)[1] or ".mp4"))
                st = {"last": 0.0, "start": time.time()}

                async def dl_progress(current, total):
                    if job["cancel"]:
                        client.stop_transmission()
                    now = time.time()
                    if now - st["last"] < UPDATE_EVERY and current < total:
                        return
                    st["last"] = now
                    el = now - st["start"]
                    await safe_edit(status, dl_text(title, current, total, current / el if el > 0 else 0), cancel_rows)

                await safe_edit(status, dl_text(title, 0, size, None), cancel_rows)
                got = await client.download_media(message, file_name=in_path, progress=dl_progress)
                if not got or job["cancel"]:
                    raise Cancelled()
                in_path = got
            else:
                title = payload.rsplit("/", 1)[-1].split("?")[0] or "video.mp4"
                st = {"last": 0.0}

                def hook(d):
                    if job["cancel"]:
                        raise Cancelled()
                    if d.get("status") != "downloading":
                        return
                    now = time.time()
                    if now - st["last"] < UPDATE_EVERY:
                        return
                    st["last"] = now
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    text = dl_text(title, d.get("downloaded_bytes") or 0, total, d.get("speed"))
                    asyncio.run_coroutine_threadsafe(safe_edit(status, text, cancel_rows), loop)

                await safe_edit(status, dl_text(title, 0, None, None), cancel_rows)
                in_path, title = await asyncio.to_thread(download_url, payload, work_dir, hook)
                if job["cancel"]:
                    raise Cancelled()

            # ---------- 2) المعالجة ----------
            duration, w, h = await asyncio.to_thread(probe, in_path)
            if not w or not h:
                raise RuntimeError("الملف مش فيديو صالح أو ماقدرتش أقراه.")
            out_name = os.path.splitext(title)[0] or "video"
            out_path = os.path.join(work_dir, "out.mp4")
            pr = {"last": 0.0, "start": time.time()}

            async def on_progress(cur, speed):
                now = time.time()
                if now - pr["last"] < UPDATE_EVERY:
                    return
                pr["last"] = now
                eta = (duration - cur) / speed if speed and duration else None
                await safe_edit(status, proc_text(title, cur, duration, speed, eta), cancel_rows)

            await safe_edit(status, proc_text(title, 0, duration, None, None), cancel_rows)
            await run_ffmpeg(job, build_cmd(in_path, wm, out_path, u, w, h), duration, on_progress)
            os.remove(in_path)  # نفضّي مساحة قبل الرفع

            out_size = os.path.getsize(out_path)
            if out_size > MAX_FILE_SIZE:
                await safe_edit(
                    status,
                    "❌ <b>الفيديو بعد المعالجة أكبر من 2 جيجا.</b>\nجرّب نمط ⚡ سريع جداً (720p) من /start.",
                )
                return

            # ---------- 3) الرفع ----------
            out_dur, out_w, out_h = await asyncio.to_thread(probe, out_path)
            thumb = await asyncio.to_thread(make_thumb, out_path, out_dur, os.path.join(work_dir, "thumb.jpg"))
            up = {"start": time.time(), "last": 0.0}

            async def up_progress(current, total):
                if job["cancel"]:
                    client.stop_transmission()
                now = time.time()
                if now - up["last"] < UPDATE_EVERY and current < total:
                    return
                up["last"] = now
                el = now - up["start"]
                speed = current / el if el > 0 else 0
                eta = (total - current) / speed if speed else None
                await safe_edit(status, up_text(title, current, total, speed, eta), cancel_rows)

            await safe_edit(status, up_text(title, 0, out_size, None, None), cancel_rows)
            sent = await client.send_video(
                chat_id=message.chat.id,
                video=out_path,
                caption=f"<b>{html.escape(out_name[:200])}</b>",
                duration=int(out_dur),
                width=out_w,
                height=out_h,
                thumb=thumb,
                file_name=re.sub(r'[\\/:*?"<>|]', "_", out_name[:100]) + ".mp4",
                supports_streaming=True,
                progress=up_progress,
            )
            if sent is None or job["cancel"]:
                raise Cancelled()
            try:
                await status.delete()
            except Exception:
                pass

    except Cancelled:
        await safe_edit(status, "❌ <b>تم الإلغاء.</b>")
    except Exception as e:
        if job["cancel"]:
            await safe_edit(status, "❌ <b>تم الإلغاء.</b>")
        else:
            err = html.escape(str(e))[:3000]
            await safe_edit(status, f"❌ <b>حدث خطأ أثناء المعالجة:</b>\n<code>{err}</code>")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        ACTIVE.discard(uid)
        JOBS.pop(job_id, None)


if __name__ == "__main__":
    print("Watermark bot is running...")
    app.run()
