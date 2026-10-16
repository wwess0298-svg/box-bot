import telebot
import yt_dlp
import os

# حط التوكن بتاع بوتك هنا بين العلامتين
TOKEN = "8793171859:AAEc0jHrdsB95A2b5ISKD6bR2-6syntzuKE"
bot = telebot.TeleBot(TOKEN)

MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # حد أقصى 2 جيجابايت

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "أهلاً بك! أرسل لي رابط الفيديو وسأقوم بتحميله لك.")

@bot.message_handler(func=lambda message: True)
def download_media(message):
    url = message.text.strip()
    if not url.startswith("http"):
        bot.reply_to(message, "من فضلك ابعت رابط صحيح يبدأ بـ http أو https")
        return

    msg = bot.reply_to(message, "⏳ جاري التحقق من الرابط وتحميل الملف...")
    output_filename = "download_media.mp4"

    ydl_opts = {
        'format': 'best',
        'outtmpl': output_filename,
        'noplaylist': True
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            filesize = info.get('filesize') or info.get('filesize_approx', 0)
            
            if filesize and filesize > MAX_FILE_SIZE_BYTES:
                bot.reply_to(message, "عذراً، هذا الملف أكبر من 2 جيجابايت ولا يمكن للبوت تحميله.")
                return

            ydl.download([url])

        if os.path.exists(output_filename):
            with open(output_filename, 'rb') as video:
                bot.send_video(message.chat.id, video, caption="إليك الفيديو الخاص بك!")
            os.remove(output_filename)
        else:
            bot.reply_to(message, "حدث خطأ أثناء تحميل الملف، حاول مرة أخرى.")
            
    except Exception as e:
        bot.reply_to(message, f"حدث خطأ: {str(e)}")

if name == "main":
    print("Bot is running...")
    bot.infinity_polling()
