import os, io, tempfile, subprocess
from PIL import Image, ImageEnhance
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter

from telegram import Update, InputFile, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ===== SETTINGS =====
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "8015286256:AAGXMSdculR-87FjJXyHAhck2IVLDi6rUsY")
COVER_PATH = os.environ.get("COVER_PATH", "D:/programs/thubmnail bot v1/leademy_cover.png")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@leademy_edu")      # e.g. -1001234567890 or @yourchannel
WATERMARK_OPACITY = float(os.environ.get("WATERMARK_OPACITY", "0.28"))
MAX_CAPTION_LEN = 1024

# Conversation states
CHOOSING_TYPE, CHOOSING_MODE, WAITING_FILE, WAITING_CAPTION = range(4)

# ---------- helpers ----------
def sanitize_caption(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_CAPTION_LEN else text[:MAX_CAPTION_LEN-1] + "…"

def temp_file(suffix): fd, p = tempfile.mkstemp(suffix=suffix); os.close(fd); return p

def ensure_cover_exists():
    if not os.path.exists(COVER_PATH):
        raise FileNotFoundError("Cover image not found. Put 'leademy_cover.png' next to the script.")

def make_tg_thumb(cover_path: str) -> str:
    """Make a Telegram-compliant thumbnail (JPEG, <=320x320, <=200KB)."""
    img = Image.open(cover_path).convert("RGB")
    img.thumbnail((320, 320))
    out = temp_file(".jpg")
    for q in (85, 80, 75, 70):
        img.save(out, "JPEG", quality=q, optimize=True, progressive=True)
        if os.path.getsize(out) <= 200_000:
            break
    return out

# ---------- PDF cover ----------
def make_cover_pdf_for_size(img_bytes: bytes, page_width: float, page_height: float) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    iw, ih = img.size
    pr, ir = page_width/page_height, iw/ih
    if ir > pr:
        dw, dh = page_width, page_width/ir; x, y = 0, (page_height-dh)/2
    else:
        dh, dw = page_height, page_height*ir; x, y = (page_width-dw)/2, 0
    c.drawImage(ImageReader(img), x, y, width=dw, height=dh, preserveAspectRatio=True, mask='auto')
    c.showPage(); c.save(); buf.seek(0)
    return buf.read()

def prepend_cover_to_pdf(original_pdf_bytes: bytes, cover_img_path: str) -> bytes:
    with open(cover_img_path, "rb") as f: cover_img_bytes = f.read()
    try:
        r = PdfReader(io.BytesIO(original_pdf_bytes))
        mb = r.pages[0].mediabox; pw, ph = float(mb.width), float(mb.height)
    except Exception:
        pw, ph = 595.2756, 841.8898  # A4
    cover_pdf = make_cover_pdf_for_size(cover_img_bytes, pw, ph)

    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(cover_pdf)).pages[0])
    for p in PdfReader(io.BytesIO(original_pdf_bytes)).pages: writer.add_page(p)
    out = io.BytesIO(); writer.write(out); out.seek(0); return out.read()

# ---------- Audio (FFmpeg) ----------
def run_ffmpeg(cmd: list):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="ignore")[:2000])

def add_cover_to_audio(in_audio_path: str, cover_img_path: str, out_audio_path: str):
    # try stream copy
    try:
        run_ffmpeg([
            "ffmpeg","-y","-i",in_audio_path,"-i",cover_img_path,
            "-map","0","-map","1","-c","copy","-id3v2_version","3",
            "-metadata:s:v","title=Album cover","-metadata:s:v","comment=Cover (front)",
            out_audio_path
        ]); return
    except Exception:
        pass
    # fallback re-encode
    run_ffmpeg([
        "ffmpeg","-y","-i",in_audio_path,"-i",cover_img_path,
        "-map","0:a:0","-map","1:v:0","-c:a","libmp3lame","-q:a","2",
        "-c:v","mjpeg","-disposition:v","attached_pic", out_audio_path
    ])

# ---------- Video watermark ----------
def make_transparent_watermark(src_path: str, out_path: str, opacity: float = 0.28):
    img = Image.open(src_path).convert("RGBA")
    r,g,b,a = img.split()
    a = ImageEnhance.Brightness(a).enhance(opacity)
    Image.merge("RGBA",(r,g,b,a)).save(out_path,"PNG")

def watermark_video(in_video_path: str, watermark_png_path: str, out_video_path: str):
    # scale watermark to 256px width; place bottom-right with 24px margin
    filter_complex = "[1]scale=256:-1[wm];[0][wm]overlay=W-w-24:H-h-24:format=auto"
    run_ffmpeg([
        "ffmpeg","-y","-i",in_video_path,"-i",watermark_png_path,
        "-filter_complex", filter_complex,
        "-c:v","libx264","-preset","veryfast","-crf","22","-c:a","copy",
        out_video_path
    ])

def kb_types():
    return ReplyKeyboardMarkup([["📄 PDF","🎵 Audio","🎬 Video"]],
                               resize_keyboard=True, one_time_keyboard=True)

def kb_modes():
    return ReplyKeyboardMarkup([["🧰 Full Post","⚡ Thumb Only"]],
                               resize_keyboard=True, one_time_keyboard=True)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Choose what you want to send:",
        reply_markup=kb_types()
    )
    return CHOOSING_TYPE

async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").lower()
    if "pdf" in t:   context.user_data["type"]="PDF"
    elif "audio" in t: context.user_data["type"]="Audio"
    elif "video" in t: context.user_data["type"]="Video"
    else:
        await update.message.reply_text("Please tap a button.", reply_markup=kb_types()); return CHOOSING_TYPE
    await update.message.reply_text("Select processing mode:", reply_markup=kb_modes())
    return CHOOSING_MODE

async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").lower()
    if "full" in t: context.user_data["mode"]="full"
    elif "thumb" in t: context.user_data["mode"]="thumb"
    else:
        await update.message.reply_text("Please choose a mode.", reply_markup=kb_modes()); return CHOOSING_MODE
    await update.message.reply_text("Great. Now send your file.", reply_markup=ReplyKeyboardRemove())
    return WAITING_FILE

async def recv_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_cover_exists()
    doc = update.message.document or update.message.audio or update.message.video
    if not doc:
        await update.message.reply_text("Please send a file."); return WAITING_FILE
    tfile = await doc.get_file()

    # guess suffix
    if update.message.document:
        suffix = os.path.splitext(update.message.document.file_name or "file.pdf")[1] or ".pdf"
    elif update.message.audio:
        suffix = ".mp3"
    else:
        suffix = ".mp4"

    in_path = temp_file(suffix)
    await tfile.download_to_drive(in_path)
    context.user_data["in_path"] = in_path
    context.user_data["orig_name"] = getattr(doc, "file_name", "file")
    await update.message.reply_text("Now send your caption:")
    return WAITING_CAPTION

async def recv_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = sanitize_caption(update.message.text)
    in_path = context.user_data.get("in_path")
    kind    = context.user_data.get("type")
    mode    = context.user_data.get("mode")     # "full" or "thumb"
    orig    = context.user_data.get("orig_name", "file")

    thumb_path = make_tg_thumb(COVER_PATH)

    try:
        # ---------- PDF ----------
        if kind == "PDF":
            if mode == "full":
                with open(in_path,"rb") as f:
                    payload = prepend_cover_to_pdf(f.read(), COVER_PATH)
                filename = f"{os.path.splitext(orig)[0]}_leademy.pdf"
                doc_input = InputFile(io.BytesIO(payload), filename=filename)
            else:
                # thumb only: keep original file unchanged
                doc_input = InputFile(open(in_path,"rb"), filename=orig)

            with open(thumb_path,"rb") as th:
                await update.message.reply_document(
                    document=doc_input, caption=caption,
                    thumbnail=InputFile(th, filename="thumb.jpg")
                )
            if CHANNEL_ID:
                with open(thumb_path,"rb") as th:
                    # re-open if we used a BytesIO previously
                    if mode == "full":
                        await context.bot.send_document(
                            chat_id=CHANNEL_ID, document=InputFile(io.BytesIO(payload), filename=filename),
                            caption=caption, thumbnail=InputFile(th, filename="thumb.jpg")
                        )
                    else:
                        await context.bot.send_document(
                            chat_id=CHANNEL_ID, document=InputFile(open(in_path,"rb"), filename=orig),
                            caption=caption, thumbnail=InputFile(th, filename="thumb.jpg")
                        )

        # ---------- Audio ----------
        elif kind == "Audio":
            if mode == "full":
                out_audio = temp_file(".mp3")
                add_cover_to_audio(in_path, COVER_PATH, out_audio)
                send_path, send_name = out_audio, f"{os.path.splitext(orig)[0]}_leademy.mp3"
            else:
                send_path, send_name = in_path, orig

            with open(send_path,"rb") as f, open(thumb_path,"rb") as th:
                await update.message.reply_audio(
                    audio=InputFile(f, filename=send_name),
                    caption=caption,
                    thumbnail=InputFile(th, filename="thumb.jpg")
                )
            if CHANNEL_ID:
                with open(send_path,"rb") as f, open(thumb_path,"rb") as th:
                    await context.bot.send_audio(
                        chat_id=CHANNEL_ID,
                        audio=InputFile(f, filename=send_name),
                        caption=caption,
                        thumbnail=InputFile(th, filename="thumb.jpg")
                    )
            if mode == "full":
                os.remove(out_audio)

        # ---------- Video ----------
        elif kind == "Video":
            if mode == "full":
                wm_png = temp_file(".png"); make_transparent_watermark(COVER_PATH, wm_png, WATERMARK_OPACITY)
                out_video = temp_file(".mp4"); watermark_video(in_path, wm_png, out_video)
                send_path, send_name = out_video, f"{os.path.splitext(orig)[0]}_wm.mp4"
            else:
                send_path, send_name = in_path, orig

            with open(send_path,"rb") as f, open(thumb_path,"rb") as th:
                await update.message.reply_video(
                    video=InputFile(f, filename=send_name),
                    caption=caption,
                    thumbnail=InputFile(th, filename="thumb.jpg")
                )
            if CHANNEL_ID:
                with open(send_path,"rb") as f, open(thumb_path,"rb") as th:
                    await context.bot.send_video(
                        chat_id=CHANNEL_ID,
                        video=InputFile(f, filename=send_name),
                        caption=caption,
                        thumbnail=InputFile(th, filename="thumb.jpg")
                    )
            if mode == "full":
                os.remove(out_video); os.remove(wm_png)

        else:
            await update.message.reply_text("Mode/type not set. Use /start."); return ConversationHandler.END

        await update.message.reply_text("✅ Done. Choose another:", reply_markup=kb_types())
        return CHOOSING_TYPE

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return ConversationHandler.END
    finally:
        try: os.remove(in_path)
        except Exception: pass
        try: os.remove(thumb_path)
        except Exception: pass

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_ID:
        await update.message.reply_text("CHANNEL_ID not set.")
        return
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text="✅ Bot can post to this channel.")
        await update.message.reply_text("Ping sent to channel.")
    except Exception as e:
        await update.message.reply_text(f"Channel post failed ({CHANNEL_ID}): {e}")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_type)],
            CHOOSING_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_mode)],
            WAITING_FILE: [MessageHandler(filters.ALL & ~filters.COMMAND, recv_file)],
            WAITING_CAPTION:[MessageHandler(filters.TEXT & ~filters.COMMAND, recv_caption)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("ping", ping))
    app.run_polling()

if __name__ == "__main__":
    main()
