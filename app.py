"""
FORGE — Video Builder Service  v2.0
Runs on Render.com (Free tier, Docker)
POST /build  →  downloads clips + music, generates TTS, assembles video, uploads to Cloudinary

Env vars expected on Render:
  FORGE_SECRET              — API key n8n sends in X-Api-Key header
  CLOUDINARY_CLOUD_NAME     — e.g. dkr7mwz6j
  CLOUDINARY_UPLOAD_PRESET  — e.g. flrxrhip  (must be UNSIGNED in Cloudinary dashboard)
"""

import os, uuid, time, base64, requests, logging, subprocess, tempfile
from functools import wraps
from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("forge")

app = Flask(__name__)

# ── Auth ──────────────────────────────────────────────────────────────────────
# Reads FORGE_SECRET from Render environment variables
# The VALUE of FORGE_SECRET must match forge_api_key in your n8n Variables node
FORGE_API_KEY = os.environ.get("FORGE_SECRET", "")

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Flask headers are case-insensitive — X-Api-Key and X-API-Key both work
        key = request.headers.get("X-Api-Key", "")
        if not FORGE_API_KEY:
            log.error("FORGE_SECRET env var is not set on Render!")
            return jsonify({"error": "Server misconfigured — FORGE_SECRET not set", "success": False}), 500
        if key != FORGE_API_KEY:
            log.warning(f"Unauthorized — received key: '{key[:8]}...' expected: '{FORGE_API_KEY[:8]}...'")
            return jsonify({"error": "Unauthorized", "success": False}), 403
        return f(*args, **kwargs)
    return decorated


# ── Helpers ───────────────────────────────────────────────────────────────────

def download_file(url: str, dest: str, timeout: int = 60) -> bool:
    """Download a URL to dest path. Returns True on success."""
    try:
        r = requests.get(url, stream=True, timeout=timeout,
                         headers={"User-Agent": "ForgeBot/1.0"})
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        size = os.path.getsize(dest)
        log.info(f"Downloaded {url[:60]}... → {size/1024:.0f}KB")
        return size > 0
    except Exception as e:
        log.warning(f"Download failed {url[:60]}: {e}")
        return False


def run_ffmpeg(*args, check=True):
    cmd = ["ffmpeg", "-y"] + list(args)
    log.info("FFmpeg: " + " ".join(cmd[:12]) + ("..." if len(cmd) > 12 else ""))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-2000:]}")
    return result


def upload_to_cloudinary(path: str, cloud: str, preset: str, public_id: str) -> str:
    """
    Upload using UNSIGNED preset — no API key/secret needed.
    Make sure your Cloudinary preset is set to 'Unsigned' in the Cloudinary dashboard.
    Dashboard → Settings → Upload → Upload presets → edit preset → Signing Mode = Unsigned
    """
    cloudinary.config(cloud_name=cloud)
    log.info(f"Uploading to Cloudinary: cloud={cloud}, preset={preset}, id={public_id}")
    result = cloudinary.uploader.unsigned_upload(
        path,
        preset,                     # upload_preset (must be unsigned)
        public_id=public_id,
        resource_type="video",
        timeout=300,
    )
    url = result.get("secure_url", "")
    if not url:
        raise RuntimeError(f"Cloudinary returned no URL. Full response: {result}")
    return url


# ── Main build logic ───────────────────────────────────────────────────────────

def assemble_video(payload: dict, workdir: str) -> str:
    """
    1. Download up to 6 video clips
    2. Get voiceover: base64 audio (from Google Cloud TTS) → file, OR URL → download, OR text → espeak-ng
    3. Download background music
    4. Concatenate clips to target duration
    5. Mix music (low) + voiceover (loud)
    6. Burn title + CTA subtitles
    7. Return path to final MP4
    """
    clip_urls          = payload.get("clip_urls", [])
    music_url          = payload.get("music_url", "")
    voiceover_url      = payload.get("voiceover_url", "")         # direct audio URL
    voiceover_text     = payload.get("voiceover_text", "")        # plain text → espeak-ng
    voiceover_b64      = payload.get("voiceover_audio_b64", "")   # base64 MP3 from Google Cloud TTS ← NEW
    duration           = int(payload.get("duration", 60))
    cta_text           = payload.get("cta_text", "Follow for more!")
    title              = payload.get("title", "Video")

    if not clip_urls:
        raise ValueError("No clip_urls provided")

    # ── Step 1: Download clips ──────────────────────────────────────────
    clip_paths = []
    for i, url in enumerate(clip_urls[:6]):
        dest = os.path.join(workdir, f"clip_{i:02d}.mp4")
        if download_file(url, dest, timeout=90):
            clip_paths.append(dest)
    if not clip_paths:
        raise ValueError("All clip downloads failed")
    log.info(f"Downloaded {len(clip_paths)} clips")

    # ── Step 2: Trim + concat clips to target duration ──────────────────
    concat_list = os.path.join(workdir, "concat.txt")
    with open(concat_list, "w") as f:
        # Repeat clips if we don't have enough to fill the duration
        needed = max(1, -(-duration // (len(clip_paths) * 8)))  # ceil division
        for _ in range(needed):
            for cp in clip_paths:
                f.write(f"file '{cp}'\n")

    raw_concat = os.path.join(workdir, "raw_concat.mp4")
    run_ffmpeg(
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-t", str(duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
        "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an",
        raw_concat
    )

    # ── Step 3: Voiceover ────────────────────────────────────────────────
    vo_path = ""

    # Priority 1: base64 MP3 from Google Cloud TTS (sent by n8n ECHO node)
    if voiceover_b64 and not vo_path:
        try:
            vo_path = os.path.join(workdir, "voiceover.mp3")
            audio_bytes = base64.b64decode(voiceover_b64)
            with open(vo_path, "wb") as f:
                f.write(audio_bytes)
            size = os.path.getsize(vo_path)
            if size < 1000:
                log.warning(f"Base64 audio too small ({size}B), discarding")
                vo_path = ""
            else:
                log.info(f"Voiceover from base64: {size/1024:.0f}KB")
        except Exception as e:
            log.warning(f"Base64 audio decode failed: {e}")
            vo_path = ""

    # Priority 2: Direct URL download
    if voiceover_url and not vo_path:
        vo_path = os.path.join(workdir, "voiceover_url.mp3")
        if not download_file(voiceover_url, vo_path, timeout=60):
            vo_path = ""

    # Priority 3: espeak-ng TTS from text (fallback, robotic voice)
    if voiceover_text and not vo_path:
        vo_path = os.path.join(workdir, "voiceover_espeak.mp3")
        try:
            wav_path = os.path.join(workdir, "espeak.wav")
            subprocess.run(
                ["espeak-ng", "-w", wav_path, "-s", "150", "-v", "en",
                 voiceover_text[:800]],
                capture_output=True, timeout=30, check=True
            )
            # Convert WAV to MP3
            run_ffmpeg("-i", wav_path, "-c:a", "libmp3lame", "-b:a", "128k", vo_path)
            if not os.path.exists(vo_path) or os.path.getsize(vo_path) < 100:
                vo_path = ""
            else:
                log.info(f"Voiceover from espeak-ng: {os.path.getsize(vo_path)/1024:.0f}KB")
        except Exception as e:
            log.warning(f"espeak-ng TTS failed: {e}")
            vo_path = ""

    if not vo_path:
        log.warning("No voiceover generated — video will be music-only or silent")

    # ── Step 4: Background music ─────────────────────────────────────────
    music_path = ""
    if music_url:
        music_path = os.path.join(workdir, "music.mp4")
        if not download_file(music_url, music_path, timeout=60):
            music_path = ""

    # ── Step 5: Mix audio track ──────────────────────────────────────────
    final_audio = os.path.join(workdir, "final_audio.aac")

    if music_path and vo_path:
        run_ffmpeg(
            "-stream_loop", "-1", "-i", music_path,
            "-i", vo_path,
            "-filter_complex",
            f"[0:a]volume=0.12,atrim=0:{duration},asetpts=PTS-STARTPTS[music];"
            f"[1:a]volume=1.0,atrim=0:{duration},asetpts=PTS-STARTPTS[vo];"
            f"[music][vo]amix=inputs=2:duration=first[aout]",
            "-map", "[aout]", "-c:a", "aac", "-b:a", "128k",
            "-t", str(duration), final_audio
        )
    elif vo_path:
        run_ffmpeg(
            "-i", vo_path,
            "-t", str(duration), "-c:a", "aac", "-b:a", "128k", final_audio
        )
    elif music_path:
        run_ffmpeg(
            "-stream_loop", "-1", "-i", music_path,
            "-t", str(duration), "-vn", "-c:a", "aac", "-b:a", "128k", final_audio
        )

    # ── Step 6: Burn title + CTA overlay ────────────────────────────────
    def esc(s):
        return s[:45].replace("'", "").replace(":", " ").replace("\\", "")

    vf_filter = (
        f"drawtext=text='{esc(title)}':fontcolor=white:fontsize=40:"
        f"box=1:boxcolor=black@0.55:boxborderw=10:"
        f"x=(w-text_w)/2:y=90:enable='between(t,0,4)',"
        f"drawtext=text='{esc(cta_text)}':fontcolor=yellow:fontsize=46:"
        f"box=1:boxcolor=black@0.65:boxborderw=12:"
        f"x=(w-text_w)/2:y=(h-text_h-90):"
        f"enable='gte(t,{max(0, duration - 5)})'"
    )

    final_out = os.path.join(workdir, "final.mp4")

    if os.path.exists(final_audio):
        run_ffmpeg(
            "-i", raw_concat, "-i", final_audio,
            "-vf", vf_filter,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-shortest", "-movflags", "+faststart",
            final_out
        )
    else:
        run_ffmpeg(
            "-i", raw_concat,
            "-vf", vf_filter,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-movflags", "+faststart",
            final_out
        )

    log.info(f"Final video: {os.path.getsize(final_out)/1024/1024:.1f}MB")
    return final_out


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
@app.route("/ping", methods=["GET"])   # UptimeRobot pings this
def health():
    return jsonify({"status": "ok", "service": "FORGE Video Builder v2.0"})


@app.route("/build", methods=["POST"])
@require_api_key
def build():
    payload = request.get_json(force=True, silent=True) or {}
    run_id  = payload.get("run_id") or str(uuid.uuid4())[:8]

    # Cloudinary config: payload fields take priority, fallback to env vars
    cloud  = payload.get("cloudinary_cloud")  or os.environ.get("CLOUDINARY_CLOUD_NAME", "")
    preset = payload.get("cloudinary_preset") or os.environ.get("CLOUDINARY_UPLOAD_PRESET", "")

    log.info(f"[{run_id}] Build — clips:{len(payload.get('clip_urls', []))} "
             f"has_b64_audio:{bool(payload.get('voiceover_audio_b64'))} "
             f"cloud:{cloud}")

    if not cloud:
        return jsonify({"error": "cloudinary_cloud missing in payload and CLOUDINARY_CLOUD_NAME env not set", "success": False}), 400
    if not preset:
        return jsonify({"error": "cloudinary_preset missing in payload and CLOUDINARY_UPLOAD_PRESET env not set", "success": False}), 400

    start = time.time()
    with tempfile.TemporaryDirectory(prefix=f"forge_{run_id}_") as workdir:
        try:
            video_path = assemble_video(payload, workdir)
        except Exception as e:
            log.error(f"[{run_id}] assemble_video failed: {e}", exc_info=True)
            return jsonify({"error": str(e), "success": False}), 500

        try:
            public_id = f"forge/{run_id}"
            video_url = upload_to_cloudinary(video_path, cloud, preset, public_id)
        except Exception as e:
            log.error(f"[{run_id}] Cloudinary upload failed: {e}", exc_info=True)
            return jsonify({"error": f"Upload failed: {e}", "success": False}), 500

    elapsed = round(time.time() - start, 1)
    log.info(f"[{run_id}] Done in {elapsed}s → {video_url}")

    return jsonify({
        "success":   True,
        "run_id":    run_id,
        "video_url": video_url,
        "elapsed_s": elapsed,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
