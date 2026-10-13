import asyncio
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import fitz
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("image-pdf-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "20"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "20"))
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "15")) * 1024 * 1024
MAX_IMAGE_EDGE = int(os.getenv("MAX_IMAGE_EDGE", "2000"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "72"))
PDF_RENDER_DPI = int(os.getenv("PDF_RENDER_DPI", "110"))
IMAGE_OUTPUT_QUALITY = int(os.getenv("IMAGE_OUTPUT_QUALITY", "70"))

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not WEBHOOK_SECRET or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", WEBHOOK_SECRET):
    raise RuntimeError("Set WEBHOOK_SECRET to a random 20-256 character token using only letters, numbers, _ or -.")
if not RENDER_EXTERNAL_URL.startswith("https://"):
    logger.warning("RENDER_EXTERNAL_URL is not set to an HTTPS URL.")

sessions = {}
session_root = Path(tempfile.gettempdir()) / "image-pdf-telegram-bot"
session_root.mkdir(parents=True, exist_ok=True)
application = Application.builder().token(BOT_TOKEN).build()


def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Images to PDF", callback_data="mode_images")],
        [InlineKeyboardButton("📄 PDF to Images", callback_data="mode_pdf")],
        [
            InlineKeyboardButton("🗜️ Reduce PDF Size", callback_data="mode_pdf_compress"),
            InlineKeyboardButton("🪄 Reduce Image Size", callback_data="mode_image_compress"),
        ],
        [
            InlineKeyboardButton("📘 Help", callback_data="show_help"),
            InlineKeyboardButton("❌ Cancel Task", callback_data="cancel"),
        ],
    ])


def new_session(user_id: int, mode: str):
    old = sessions.pop(user_id, None)
    if old and old.get("dir"):
        shutil.rmtree(old["dir"], ignore_errors=True)
    user_dir = session_root / str(user_id)
    shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir(parents=True, exist_ok=True)
    sessions[user_id] = {
        "mode": mode, "dir": str(user_dir), "images": [],
        "filename": "converted", "compress": True,
    }
    return sessions[user_id]


def cleanup_session(user_id: int):
    item = sessions.pop(user_id, None)
    if item and item.get("dir"):
        shutil.rmtree(item["dir"], ignore_errors=True)


def safe_filename(name: str) -> str:
    name = Path(name.strip()).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "", name).strip(" .")
    name = re.sub(r"\s+", "_", name)
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return (name[:70] or "converted")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_session(update.effective_user.id)
    await update.effective_message.reply_text(
        "✨ IMAGE PDF STUDIO\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Convert images and PDFs, or reduce file sizes.\n\n"
        "Choose a tool below:",
        reply_markup=home_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📘 HELP CENTER\n\n"
        "🖼️ Images to PDF: choose the option, send images, then /done.\n"
        "📄 PDF to Images: choose the option, then send a PDF document.\n"
        "🗜️ Reduce PDF Size: choose the option, then send a PDF document.\n"
        "🪄 Reduce Image Size: choose the option, then send a photo/image file.\n\n"
        "Commands:\n"
        "/done — create PDF\n"
        "/filename my_document — set PDF filename\n"
        "/order 3,1,2 — reorder images (list every index)\n"
        "/compress on|off — optimize images when making a PDF\n"
        "/cancel — cancel current task"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_session(update.effective_user.id)
    await update.effective_message.reply_text("❌ Task cancelled. Choose a tool:", reply_markup=home_keyboard())


async def filename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = sessions.get(update.effective_user.id)
    if not data or data["mode"] != "images":
        await update.effective_message.reply_text("Choose Images to PDF first.")
        return
    value = " ".join(context.args).strip()
    if not value:
        await update.effective_message.reply_text("Usage: /filename school_notes")
        return
    data["filename"] = safe_filename(value)
    await update.effective_message.reply_text(f"PDF filename set to {data['filename']}.pdf")


async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = sessions.get(update.effective_user.id)
    if not data or data["mode"] != "images":
        await update.effective_message.reply_text("Choose Images to PDF first.")
        return
    paths = data["images"]
    value = "".join(context.args).replace(" ", "")
    try:
        indices = [int(x) for x in value.split(",") if x]
    except ValueError:
        indices = []
    if not paths or len(indices) != len(paths) or sorted(indices) != list(range(1, len(paths) + 1)):
        await update.effective_message.reply_text(
            f"Send images first, then list every index exactly once. Current image count: {len(paths)}.\n"
            "Example: /order 3,1,2"
        )
        return
    data["images"] = [paths[i - 1] for i in indices]
    await update.effective_message.reply_text("Page order updated: " + ", ".join(map(str, indices)))


async def compress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = sessions.get(update.effective_user.id)
    if not data or data["mode"] != "images":
        await update.effective_message.reply_text("This command applies to Images to PDF mode.")
        return
    value = context.args[0].lower() if context.args else ""
    if value not in {"on", "off"}:
        await update.effective_message.reply_text("Usage: /compress on or /compress off")
        return
    data["compress"] = value == "on"
    await update.effective_message.reply_text(
        "Image optimization " + ("ON — smaller PDF, some quality loss possible." if data["compress"]
                                 else "OFF — preserve more image detail.")
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data == "cancel":
        cleanup_session(user_id)
        await query.edit_message_text("❌ Task cancelled. Choose a tool:", reply_markup=home_keyboard())
    elif query.data == "mode_images":
        new_session(user_id, "images")
        await query.edit_message_text(
            "🖼️ IMAGES ➜ PDF\n\nSend images one by one, then /done.\n"
            "Optional commands:\n/filename my_document\n/order 3,1,2\n/compress on\n/cancel"
        )
    elif query.data == "mode_pdf":
        new_session(user_id, "pdf")
        await query.edit_message_text("📄 PDF ➜ IMAGES\n\nSend one PDF as a document/file.")
    elif query.data == "mode_pdf_compress":
        new_session(user_id, "pdf_compress")
        await query.edit_message_text(
            "🗜️ REDUCE PDF SIZE\n\nSend a PDF as a document/file.\n"
            "The bot will rasterize pages and rebuild a compressed PDF. "
            "This can reduce size, but selectable text, links, forms, and vector detail may be lost."
        )
    elif query.data == "mode_image_compress":
        new_session(user_id, "image_compress")
        await query.edit_message_text(
            "🪄 REDUCE IMAGE SIZE\n\nSend one photo or image document (JPG, PNG, WEBP, BMP, TIFF).\n"
            "The bot will resize large images and save an optimized JPG. "
            "Transparency will be replaced with a white background."
        )
    elif query.data == "show_help":
        await query.edit_message_text(
            "📘 HELP CENTER\n\n"
            "🖼️ Images to PDF: send images, then /done\n"
            "📄 PDF to Images: send a PDF document\n"
            "🗜️ Reduce PDF Size: send a PDF document\n"
            "🪄 Reduce Image Size: send an image\n\n"
            "Filename: /filename my_document\n"
            "Page order: /order 3,1,2\n"
            "PDF image optimization: /compress on|off\n"
            "Cancel: /cancel",
            reply_markup=home_keyboard(),
        )


async def get_file_bytes(file_id: str, context: ContextTypes.DEFAULT_TYPE) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    content = await tg_file.download_as_bytearray()
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError("file_too_large")
    return bytes(content)


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = sessions.get(user_id)
    message = update.effective_message
    if not data:
        await message.reply_text("Send /start and choose a tool first.", reply_markup=home_keyboard())
        return

    if data["mode"] == "images":
        if len(data["images"]) >= MAX_IMAGES:
            await message.reply_text(f"Maximum {MAX_IMAGES} images reached. Send /done.")
            return
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        try:
            content = await get_file_bytes(file_id, context)
            index = len(data["images"]) + 1
            suffix = ".jpg"
            if message.document and message.document.file_name:
                ext = Path(message.document.file_name).suffix.lower()
                if ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
                    suffix = ext
            path = Path(data["dir"]) / f"image_{index:03d}{suffix}"
            path.write_bytes(content)
            with Image.open(path) as im:
                im.verify()
            data["images"].append(str(path))
            await message.reply_text(f"✅ Added image {index}/{MAX_IMAGES}. Send more or /done.")
        except ValueError:
            await message.reply_text(f"File exceeds the {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB limit.")
        except Exception:
            logger.exception("Image input failed")
            await message.reply_text("Couldn't read that image. Try JPG or PNG.")
        return

    if data["mode"] == "image_compress":
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        status = await message.reply_text("⏳ Optimizing image...")
        try:
            content = await get_file_bytes(file_id, context)
            source_path = Path(data["dir"]) / "input_image"
            source_path.write_bytes(content)
            out_path = Path(data["dir"]) / "compressed_image.jpg"
            with Image.open(source_path) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
                if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                image.save(out_path, "JPEG", quality=IMAGE_OUTPUT_QUALITY, optimize=True, progressive=True)
            old_size = len(content)
            new_size = out_path.stat().st_size
            with out_path.open("rb") as f:
                await message.reply_document(
                    document=f,
                    filename="compressed_image.jpg",
                    caption=f"🪄 Optimized image\nBefore: {old_size // 1024} KB\nAfter: {new_size // 1024} KB",
                )
            await status.delete()
            cleanup_session(user_id)
            await message.reply_text("Finished. Choose another tool:", reply_markup=home_keyboard())
        except ValueError:
            await status.edit_text(f"File exceeds the {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB limit.")
        except Exception:
            logger.exception("Image compression failed")
            await status.edit_text("Image compression failed. Check the file format and try again.")
        return

    await message.reply_text("This mode expects a PDF document. Use /cancel to choose another tool.")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = sessions.get(user_id)
    if not data or data["mode"] != "images":
        await update.effective_message.reply_text("Choose Images to PDF and send images first.")
        return
    paths = data["images"]
    if not paths:
        await update.effective_message.reply_text("No images received yet.")
        return
    status = await update.effective_message.reply_text("⏳ Creating your PDF...")
    output = Path(data["dir"]) / f"{data['filename']}.pdf"
    try:
        pil_images = []
        for path in paths:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                if data["compress"]:
                    image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
                pil_images.append(image.copy())
        first, rest = pil_images[0], pil_images[1:]
        options = {"format": "PDF", "save_all": True, "append_images": rest, "resolution": 150}
        if data["compress"]:
            options.update({"quality": JPEG_QUALITY, "optimize": True})
        first.save(output, **options)
        for image in pil_images:
            image.close()
        if output.stat().st_size > MAX_DOWNLOAD_BYTES:
            await status.edit_text("Generated PDF is too large. Try fewer images or smaller source images.")
            return
        with output.open("rb") as f:
            await update.effective_message.reply_document(
                document=f, filename=output.name,
                caption=f"✅ PDF ready · {len(paths)} page(s) · {output.stat().st_size // 1024} KB",
            )
        await status.delete()
        cleanup_session(user_id)
        await update.effective_message.reply_text("Done! Choose another tool:", reply_markup=home_keyboard())
    except Exception:
        logger.exception("Image-to-PDF conversion failed")
        await status.edit_text("Conversion failed. Please check the image files and try again.")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = sessions.get(user_id)
    message = update.effective_message
    if not data or data["mode"] not in {"pdf", "pdf_compress"}:
        await message.reply_text("Choose PDF to Images or Reduce PDF Size first.", reply_markup=home_keyboard())
        return
    doc = message.document
    if not doc or (doc.mime_type != "application/pdf" and not (doc.file_name or "").lower().endswith(".pdf")):
        await message.reply_text("Please send a PDF using Telegram's Document/File option.")
        return
    status = await message.reply_text("⏳ Processing PDF...")
    try:
        content = await get_file_bytes(doc.file_id, context)
        input_path = Path(data["dir"]) / "input.pdf"
        input_path.write_bytes(content)
        with fitz.open(input_path) as pdf:
            if pdf.is_encrypted:
                await status.edit_text("This PDF is password-protected. Please send an unlocked PDF.")
                return
            if len(pdf) > MAX_PDF_PAGES:
                await status.edit_text(f"PDF has {len(pdf)} pages. Maximum supported is {MAX_PDF_PAGES}.")
                return
            if len(pdf) == 0:
                await status.edit_text("This PDF has no pages.")
                return

            if data["mode"] == "pdf":
                output_paths = []
                for i, page in enumerate(pdf):
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                    out = Path(data["dir"]) / f"page_{i + 1:03d}.jpg"
                    pix.save(str(out), jpg_quality=85)
                    output_paths.append(out)
                for index, path in enumerate(output_paths, start=1):
                    with path.open("rb") as f:
                        await message.reply_document(document=f, filename=path.name,
                                                     caption=f"Page {index}/{len(output_paths)}")
                await status.delete()
                cleanup_session(user_id)
                await message.reply_text(f"✅ Converted {len(output_paths)} page(s) to JPG.", reply_markup=home_keyboard())
            else:
                output = Path(data["dir"]) / "compressed.pdf"
                new_pdf = fitz.open()
                for page in pdf:
                    pix = page.get_pixmap(dpi=PDF_RENDER_DPI, alpha=False)
                    image_bytes = pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
                    rect = page.rect
                    new_page = new_pdf.new_page(width=rect.width, height=rect.height)
                    new_page.insert_image(new_page.rect, stream=image_bytes)
                new_pdf.save(output, garbage=4, deflate=True, clean=True)
                new_pdf.close()

        new_size = output.stat().st_size
        if new_size > MAX_DOWNLOAD_BYTES:
            await status.edit_text("Compressed PDF is still too large to send. Try a smaller source PDF.")
            return
        with output.open("rb") as f:
            await message.reply_document(
                document=f, filename="compressed.pdf",
                caption=(f"🗜️ Compressed PDF\nBefore: {len(content) // 1024} KB\n"
                         f"After: {new_size // 1024} KB\n"
                         "Note: pages were rasterized; selectable text and links may be lost."),
            )
        await status.delete()
        cleanup_session(user_id)
        await message.reply_text("Finished. Choose another tool:", reply_markup=home_keyboard())
    except ValueError:
        await status.edit_text(f"File exceeds the {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB limit.")
    except Exception:
        logger.exception("PDF processing failed")
        await status.edit_text("PDF processing failed. It may be damaged or unsupported.")


async def handle_other_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Unsupported document for the current mode. Choose a tool from /start."
    )


application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("cancel", cancel))
application.add_handler(CommandHandler("done", done))
application.add_handler(CommandHandler("filename", filename_command))
application.add_handler(CommandHandler("order", order_command))
application.add_handler(CommandHandler("compress", compress_command))
application.add_handler(CallbackQueryHandler(handle_button))
application.add_handler(MessageHandler(filters.PHOTO, handle_image))
application.add_handler(MessageHandler(filters.Document.IMAGE, handle_image))
application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
application.add_handler(MessageHandler(filters.Document.ALL, handle_other_document))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await application.initialize()
    await application.start()
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Open main menu"),
            BotCommand("help", "Show instructions"),
            BotCommand("done", "Create PDF from images"),
            BotCommand("filename", "Set PDF filename"),
            BotCommand("order", "Reorder image pages"),
            BotCommand("compress", "Optimize images for PDF"),
            BotCommand("cancel", "Cancel current task"),
        ])
        if RENDER_EXTERNAL_URL.startswith("https://"):
            await application.bot.set_webhook(
                url=f"{RENDER_EXTERNAL_URL}/telegram/{WEBHOOK_SECRET}",
                secret_token=WEBHOOK_SECRET,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            logger.info("Telegram webhook configured.")
        else:
            logger.warning("Webhook not configured: set RENDER_EXTERNAL_URL.")
        yield
    finally:
        try:
            await application.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            logger.exception("Webhook cleanup failed")
        await application.stop()
        await application.shutdown()


app = FastAPI(title="Image PDF Studio Telegram Bot", lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "ok", "service": "Image PDF Studio Telegram Bot"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/telegram/{secret_path}")
async def telegram_webhook(
    secret_path: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if secret_path != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        await application.process_update(update)
        return JSONResponse({"ok": True})
    except Exception:
        logger.exception("Webhook update processing failed")
        return JSONResponse({"ok": False}, status_code=200)
