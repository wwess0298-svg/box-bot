import os
import time
import telebot
import yt_dlp
TOKEN = "8852120082:AAGwIcax_Cc625HuBe7TIgAYH5Z2ADlb6dk"
bot = telebot.TeleBot(TOKEN)
MAX_FILE_SIZE_BYTES = 1.9 * 1024 * 1024 * 1024
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "أهلاً بيك يا أحمد في بوت التحميل، ابعت لي الرابط فوراً.")
@bot.message_handler(func=lambda message: True)
def download_media(message):
    url = message.text.strip()
if not url.startswith("http"):
    bot.reply_to(message, "من فضلك ابعت رابط صحيح يبدأ بـ http أو https")
    return
msg = bot.reply_to(message, "جاري التحقق من الرابط وتحميل الملف...")
output_filename = "downloaded_media.mp4"
ydl_opts = {
'format': 'best',
'outtmpl': output_filename,
'noplaylist': True,
'progress_hooks': [lambda d: progress_hook(d, msg, message.chat.id)],
}
try:
    with yt_dlp.YoutubeDL({'format': 'best', 'noplaylist': True}) as ydl:
        info = ydl.extract_info(url, download=False)
        filesize = info.get('filesize') or info.get('filesize_approx')
        if filesize and filesize > MAX_FILE_SIZE_BYTES:
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg.message_id,
                text="عذراً، هذا الملف أكبر من 2 جيجابايت!",
"تيليجرام لا حمسي للبوتات عفرب ملفات تتجاوز هذا الحجم.",
message.chat.id,
msg.message_id,
parse_mode="HTML"
)
return
bot.edit_message_text("📥 <b>االنتظار برجاء ...(الكبيرة لألحجام) اآلن التنزيل ةيلمع بدأت.>/b>", message.chat.id, msg.message_id, parse_mode="HTML")
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
ydl.download([url])
# التحقق يئاهنلا من حجم الملف المنزل ىلع السيرفر
if os.path.exists(output_filename):
actual_size = os.path.getsize(output_filename)
if actual_size > MAX_FILE_SIZE_BYTES:
bot.edit_message_text("عذراً، الملف أكبر من 2 جيجابايت!", chat_id=message.chat.id, message_id=msg.message_id)
os.remove(output_filename)
return
bot.edit_message_text("تم الانتهاء من التنزيل بنجاح!", chat_id=message.chat.id, message_id=msg.message_id)
# إرسال الفيديو للمستخدم
with open(output_filename, 'rb') as video:
bot.send_video(
message.chat.id,
video,
caption="🎬 <b>بنجاح عفرلا تم>!/b>\n⚡ بوت ةطساوب: BOX",
parse_mode="HTML",
ةلهم ةينمز أطول للملفات الكبيرة عشان ميفصلش أثناء عفرلا # 300=timeout
)
bot.delete_message(message.chat.id, msg.message_id)
except Exception as e:
try:
bot.edit_message_text(f"❌ <b>التحميل أو ةجلاعملا أثناء أطخ حدث:>/b>\n<code>{str(e)}</code>", message.chat.id, msg.message_id,
parse_mode="HTML")
except:
bot.send_message(message.chat.id, f"❌ أطخ حدث:} str(e)}")
finally:
# حسم الملف من السيرفر فوراً عشان نحافظ ىلع ةحاسملا
if os.path.exists(output_filename):
os.remove(output_filename)
# ةلاد عبتت التقدم ةعرسلاو
last_update_time = 0
def progress_hook(d, msg, chat_id):
global last_update_time
if d['status'] == 'downloading':
current_time = time.time()
if current_time - last_update_time > 4: # يناوث 4 كل تحديث
try:
percent = d.get('_percent_str', '0%').strip()
speed = d.get('_speed_str', 'N/A').strip()
eta = d.get('_eta_str', 'N/A').strip()
text = (
f"📥 <b>الكبير الملف تنزيل جاري...>/b>\n"
f"━━━━━━━━━━━━━━━\n"
f"📊 ةبسنلا:> b>{percent}</b>\n"
f"🚀 ةعرسلا:> code>{speed}</code>\n"
f"⏱️ يقبتملا:> code>{eta}</code>\n"
f"━━━━━━━━━━━━━━━"
)
bot.edit_message_text(text, chat_id, msg.message_id, parse_mode="HTML")
last_update_time = current_time
except Exception:
pass
print("Bot is running and ready for large files...")
bot.infinity_polling()
