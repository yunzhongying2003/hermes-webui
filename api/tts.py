"""
Hermes Web UI -- Text-to-Speech (TTS) via edge-tts.
Provides /api/tts (generate audio) and /api/audio/<filename> (serve audio files).
"""
import asyncio
import hashlib
import logging
import os
import secrets
import shutil
from pathlib import Path

from api.config import STATE_DIR
from api.helpers import j, bad, t, _security_headers

logger = logging.getLogger(__name__)

# Audio cache directory
AUDIO_CACHE_DIR = STATE_DIR / "tts_audio"

# Ensure cache directory exists
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Default TTS voice (Chinese female)
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# Supported voices (can be extended)
SUPPORTED_VOICES = {
    "zh-CN-XiaoxiaoNeural": "中文女声（晓晓）",
    "zh-CN-YunxiNeural": "中文男声（云希）",
    "zh-CN-XiaoyiNeural": "中文女声（小艺）",
    "zh-CN-YunjianNeural": "中文男声（云健）",
    "en-US-JennyNeural": "英文女声（Jenny）",
    "en-US-GuyNeural": "英文男声（Guy）",
}


def _generate_audio_filename(text: str, voice: str) -> str:
    """Generate a unique but deterministic audio filename based on text hash."""
    hash_input = f"{text}:{voice}"
    file_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:16]
    return f"tts_{file_hash}.mp3"


async def _generate_tts_async(text: str, voice: str, output_path: Path) -> bool:
    """Generate TTS audio using edge-tts asynchronously."""
    try:
        import edge_tts
    except ImportError:
        logger.error("edge-tts 未安装，请运行: pip install edge-tts")
        return False

    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(output_path))
        return True
    except Exception as e:
        logger.error(f"TTS 生成失败: {e}")
        return False


def handle_tts_post(handler) -> bool:
    """Handle POST /api/tts - Generate TTS audio and return URL."""
    import json
    from urllib.parse import quote

    # Read request body
    content_length = int(handler.headers.get("Content-Length", 0))
    if content_length == 0:
        return bad(handler, "Request body is required", 400)

    try:
        body = handler.rfile.read(content_length)
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        return bad(handler, f"Invalid JSON: {e}", 400)

    text = data.get("text", "").strip()
    if not text:
        return bad(handler, "Text is required", 400)

    # Limit text length (edge-tts has limits)
    if len(text) > 5000:
        text = text[:5000]
        logger.warning(f"Text truncated to 5000 chars")

    voice = data.get("voice", DEFAULT_VOICE)

    # Validate voice
    if voice not in SUPPORTED_VOICES:
        # Allow any valid edge-tts voice, not just our defaults
        logger.info(f"Using custom voice: {voice}")

    # Generate audio filename
    audio_filename = _generate_audio_filename(text, voice)
    audio_path = AUDIO_CACHE_DIR / audio_filename

    # Check if audio already exists (cache hit)
    if audio_path.exists():
        audio_url = f"/api/audio/{quote(audio_filename, safe='')}"
        return j(handler, {
            "url": audio_url,
            "voice": voice,
            "cached": True,
            "text_length": len(text),
        })

    # Generate audio
    loop = asyncio.new_event_loop()
    try:
        success = loop.run_until_complete(
            _generate_tts_async(text, voice, audio_path)
        )
    finally:
        loop.close()

    if not success:
        return bad(handler, "TTS generation failed", 500)

    # Verify file was created
    if not audio_path.exists():
        return bad(handler, "Audio file was not created", 500)

    audio_url = f"/api/audio/{quote(audio_filename, safe='')}"
    return j(handler, {
        "url": audio_url,
        "voice": voice,
        "cached": False,
        "text_length": len(text),
    })


def handle_audio_get(handler, filename: str) -> bool:
    """Handle GET /api/audio/<filename> - Serve audio file."""
    import re

    # Validate filename (prevent path traversal)
    if not re.match(r'^tts_[a-f0-9]{16}\.mp3$', filename):
        return bad(handler, "Invalid filename", 400)

    audio_path = AUDIO_CACHE_DIR / filename

    if not audio_path.exists():
        return bad(handler, "Audio file not found", 404)

    # Serve the audio file
    try:
        file_size = audio_path.stat().st_size
        with open(audio_path, "rb") as f:
            data = f.read()
        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Content-Length", str(file_size))
        handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        handler.send_header("Accept-Ranges", "bytes")
        _security_headers(handler)
        handler.end_headers()
        handler.wfile.write(data)
        return True
    except Exception as e:
        logger.error(f"Failed to serve audio file: {e}")
        return bad(handler, "Failed to serve audio", 500)


def cleanup_old_audio_files(max_age_hours: int = 24, max_files: int = 100):
    """Clean up old audio files to prevent disk space issues."""
    if not AUDIO_CACHE_DIR.exists():
        return

    import time

    now = time.time()
    max_age_seconds = max_age_hours * 3600

    files = list(AUDIO_CACHE_DIR.glob("tts_*.mp3"))

    # Sort by modification time (oldest first)
    files.sort(key=lambda f: f.stat().st_mtime)

    # Remove old files
    removed_count = 0
    for f in files:
        age = now - f.stat().st_mtime
        if age > max_age_seconds or len(files) - removed_count > max_files:
            try:
                f.unlink()
                removed_count += 1
            except Exception as e:
                logger.warning(f"Failed to delete {f}: {e}")

    if removed_count > 0:
        logger.info(f"Cleaned up {removed_count} old audio files")
