# Image PDF Studio Telegram Bot (Render)

## Features
- Multiple images to one PDF
- Custom PDF filename and page order
- Image optimization while creating a PDF
- PDF to JPG pages
- Reduce PDF size by rasterizing pages and rebuilding a compressed PDF
- Reduce image size by resizing and exporting optimized JPG
- FastAPI webhook and health endpoint

## Setup
1. In Telegram, open @BotFather, send `/newbot`, follow instructions, and save the bot token privately.
2. Generate a random webhook secret (20-256 characters using letters, digits, `_`, `-`):
   `python -c "import secrets; print(secrets.token_urlsafe(32).replace('-', 'x'))"`
3. Push `main.py`, `requirements.txt`, `render.yaml`, and `.gitignore` to a GitHub repository.
4. In Render, choose **New + → Blueprint** and connect that repository.
5. Set environment variables in Render:
   - `BOT_TOKEN`: BotFather token
   - `WEBHOOK_SECRET`: generated random secret
   - Render supplies `RENDER_EXTERNAL_URL` automatically for the deployed web service.
6. Deploy, wait for healthy status, then open the bot and send `/start`.

## Menu usage
- **Images to PDF**: send images, optionally use `/filename notes`, `/order 3,1,2`, `/compress on`, then `/done`.
- **PDF to Images**: choose the option and send a PDF document.
- **Reduce PDF Size**: choose the option and send a PDF document.
- **Reduce Image Size**: choose the option and send an image/photo.

## Important
- PDF size reduction rasterizes pages. It can remove selectable text, hyperlinks, forms, accessibility features, and vector sharpness. Keep your original PDF.
- Image size reduction outputs JPG; transparency is replaced with white. PNG files may not always get smaller.
- Default limits: 20 images, 20 PDF pages, 15 MB per incoming/outgoing file (configured in the app).
- Render free instances may sleep. In-memory sessions and temporary files are lost on restarts.
- Do not commit tokens/secrets to GitHub.
