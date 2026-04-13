"""
FORGE — Video Builder Service  v3.0  (Memory-Optimized)
Runs on Render.com Free tier (512MB RAM limit)

KEY CHANGES from v2:
- Single ffmpeg pass instead of two (was hitting 512MB OOM)
- 720x1280 output instead of 1080x1920 (saves ~55% memory, still HD vertical)
- threads=1 on all ffmpeg calls (caps memory usage)
- Max 3 clips instead of 6
- ultrafast preset instead of fast

Env vars on Render:
  FORGE_SECRET              — must match forge_api_key in n8n Variables node
  CLOUDINARY_CLOUD_NAME     — e.g. dkr7mwz6j
  CLOUDINARY_UPLOAD_PRESET  — e.g. flrxrhip  (must be UNSIGNED)
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
FORGE_API_KEY = os.environ.get("FORGE_SECRET", "")

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-Api-Key", "")
        if not FORGE_API_KEY:
            log.error("FORGE_SECRET env var is not set on Render!")
            return jsonify({"error": "Server misconfigured: FORGE_SECRET not set", "success": False}), 500
        if key != FORGE_API_KEY:
            log.warning(f"Unauthorized — got: '{key[:8]}...'")
            return jsonify({"error": "Unauthorized", "success": False}), 403
        return f(*args, **kwargs)
    return decorated


# ── Helpers ───────────────────────────────────────────────────────────────────

def download_file(url: str, dest: str, timeout: int = 60) -> bool:
    try:
        r = requests.get(url, stream=True, timeout=timeout,
                         headers={"User-Agent": "ForgeBot/1.0"})
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        size = os.path.getsize(dest)
        log.info(f"Downloaded → {size/1024:.0f}KB  {url[:70]}")
        return size > 1000
    except Exception as e:
        log.warning(f"Download failed: {e}  url={url[:70]}")
        return False


def run_ffmpeg(*args, check=True):
    """Run ffmpeg with -threads 1 to cap memory usage."""
    cmd = ["ffmpeg", "-y", "-threads", "1"] + list(args)
    log.info("FFmpeg: " + " ".join(cmd[:14]) + ("..." if len(cmd) > 14 else ""))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"FFmpeg error:\n{result.stderr[-2000:]}")
    return result


def upload_to_cloudinary(path: str, cloud: str, preset: str, public_id: str) -> str:
    """Unsigned upload — no API key/secret needed. Preset must be Unsigned in Cloudinary dashboard."""
    cloudinary.config(cloud_name=cloud)
    log.info(f"Uploading to Cloudinary: cloud={cloud} preset={preset}")
    result = cloudinary.uploader.unsigned_upload(
        path,
        preset,
        public_id=public_id,
        resource_type="video",
        timeout=300,
    )
    url = result.get("secure_url", "")
    if not url:
        raise RuntimeError(f"Cloudinary returned no URL. Response: {result}")
    return url


# ── Main build ────────────────────────────────────────────────────────────────

def assemble_video(payload: dict, workdir: str) -> str:
    """
    SINGLE-PASS approach to stay under 512MB RAM:
      1. Download clips (max 3) + voiceover audio + music
      2. Build concat list
      3. ONE ffmpeg call: concat → scale to 720x1280 → mix audio → burn text → output
    """
    clip_urls       = payload.get("clip_urls", [])
    music_url       = payload.get("music_url", "")
    voiceover_url   = payload.get("voiceover_url", "")
    voiceover_text  = payload.get("voiceover_text", "")
    voiceover_b64   = payload.get("voiceover_audio_b64", "")
    duration        = int(payload.get("duration", 60))
    cta_text        = payload.get("cta_text", "Follow for more!")
    title           = payload.get("title", "Video")

    if not clip_urls:
        raise ValueError("No clip_urls provided")

    # ── Step 1: Download clips (max 3 to save memory + disk) ──────────────
    clip_paths = []
    for i, url in enumerate(clip_urls[:3]):          # max 3 clips
        dest = os.path.join(workdir, f"clip_{i:02d}.mp4")
        if download_file(url, dest, timeout=90):
            clip_paths.append(dest)
        if len(clip_paths) == 3:
            break
    if not clip_paths:
        raise ValueError("All clip downloads failed")
    log.info(f"Got {len(clip_paths)} clips")

    # ── Step 2: Voiceover (3 priority levels) ─────────────────────────────
    vo_path = ""

    # Priority 1: base64 MP3 from Google Cloud TTS
    if voiceover_b64 and not vo_path:
        try:
            vo_path = os.path.join(workdir, "vo.mp3")
            with open(vo_path, "wb") as f:
                f.write(base64.b64decode(voiceover_b64))
            if os.path.getsize(vo_path) < 500:
                vo_path = ""
            else:
                log.info(f"Voiceover from base64: {os.path.getsize(vo_path)//1024}KB")
        except Exception as e:
            log.warning(f"Base64 decode failed: {e}")
            vo_path = ""

    # Priority 2: URL download
    if voiceover_url and not vo_path:
        vo_path = os.path.join(workdir, "vo_url.mp3")
        if not download_file(voiceover_url, vo_path, timeout=60):
            vo_path = ""

    # Priority 3: espeak-ng fallback (robotic but works)
    if voiceover_text and not vo_path:
        try:
            wav = os.path.join(workdir, "espeak.wav")
            subprocess.run(
                ["espeak-ng", "-w", wav, "-s", "150", "-v", "en",
                 voiceover_text[:600]],
                capture_output=True, timeout=30, check=True
            )
            vo_path = os.path.join(workdir, "vo_espeak.mp3")
            run_ffmpeg("-i", wav, "-c:a", "libmp3lame", "-b:a", "96k", vo_path)
            log.info("Voiceover from espeak-ng")
        except Exception as e:
            log.warning(f"espeak-ng failed: {e}")
            vo_path = ""

    # ── Step 3: Background music ───────────────────────────────────────────
    music_path = ""
    if music_url:
        music_path = os.path.join(workdir, "music.mp4")
        if not download_file(music_url, music_path, timeout=60):
            music_path = ""

    # ── Step 4: Build concat list (repeat clips to fill duration) ─────────
    concat_txt = os.path.join(workdir, "concat.txt")
    with open(concat_txt, "w") as f:
        repeats = max(1, -(-duration // (len(clip_paths) * 7)))  # ceil div
        for _ in range(repeats):
            for cp in clip_paths:
                f.write(f"file '{cp}'\n")

    # ── Step 5: SINGLE ffmpeg pass ─────────────────────────────────────────
    # 720x1280 vertical (saves ~55% memory vs 1080x1920)
    # All processing in one pass: concat + scale + audio mix + text burn
    final_out = os.path.join(workdir, "final.mp4")

    def esc(s):
        return str(s)[:42].replace("'", "").replace(":", " ").replace("\\", "").replace("%", "pct")

    vf = (
        "scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,"
        "setsar=1,"
        "fps=30,"
        f"drawtext=text='{esc(title)}':"
        "fontcolor=white:fontsize=30:"
        "box=1:boxcolor=black@0.55:boxborderw=8:"
        "x=(w-text_w)/2:y=70:"
        "enable='between(t\\,0\\,4)',"
        f"drawtext=text='{esc(cta_text)}':"
        "fontcolor=yellow:fontsize=34:"
        "box=1:boxcolor=black@0.65:boxborderw=10:"
        "x=(w-text_w)/2:y=(h-text_h-70):"
        f"enable='gte(t\\,{max(0, duration - 5)})'"
    )

    # Build ffmpeg command based on what audio we have
    cmd_inputs = ["-f", "concat", "-safe", "0", "-i", concat_txt]

    if music_path and vo_path:
        cmd_inputs += ["-stream_loop", "-1", "-i", music_path, "-i", vo_path]
        audio_filter = (
            f"[1:a]volume=0.12,atrim=0:{duration},asetpts=PTS-STARTPTS[music];"
            f"[2:a]volume=1.0,atrim=0:{duration},asetpts=PTS-STARTPTS[vo];"
            "[music][vo]amix=inputs=2:duration=first[aout]"
        )
        run_ffmpeg(
            *cmd_inputs,
            "-t", str(duration),
            "-filter_complex", audio_filter,
            "-vf", vf,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            final_out
        )
    elif vo_path:
        cmd_inputs += ["-i", vo_path]
        run_ffmpeg(
            *cmd_inputs,
            "-t", str(duration),
            "-vf", vf,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k", "-shortest",
            "-movflags", "+faststart",
            final_out
        )
    elif music_path:
        cmd_inputs += ["-stream_loop", "-1", "-i", music_path]
        run_ffmpeg(
            *cmd_inputs,
            "-t", str(duration),
            "-vf", vf,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            final_out
        )
    else:
        # No audio — video only
        run_ffmpeg(
            *cmd_inputs,
            "-t", str(duration),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-movflags", "+faststart",
            final_out
        )

    size_mb = os.path.getsize(final_out) / 1024 / 1024
    log.info(f"Final video: {size_mb:.1f}MB  duration:{duration}s  res:720x1280")
    return final_out


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
@app.route("/ping", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "FORGE v3.0", "memory_optimized": True})


@app.route("/build", methods=["POST"])
@require_api_key
def build():
    payload = request.get_json(force=True, silent=True) or {}
    run_id  = payload.get("run_id") or str(uuid.uuid4())[:8]
    cloud   = payload.get("cloudinary_cloud")  or os.environ.get("CLOUDINARY_CLOUD_NAME", "")
    preset  = payload.get("cloudinary_preset") or os.environ.get("CLOUDINARY_UPLOAD_PRESET", "")

    log.info(
        f"[{run_id}] BUILD START — "
        f"clips:{len(payload.get('clip_urls', []))} "
        f"has_b64_audio:{bool(payload.get('voiceover_audio_b64'))} "
        f"has_music:{bool(payload.get('music_url'))} "
        f"cloud:{cloud}"
    )

    if not cloud:
        return jsonify({"error": "cloudinary_cloud missing", "success": False}), 400
    if not preset:
        return jsonify({"error": "cloudinary_preset missing", "success": False}), 400

    start = time.time()
    with tempfile.TemporaryDirectory(prefix=f"forge_{run_id}_") as workdir:
        try:
            video_path = assemble_video(payload, workdir)
        except Exception as e:
            log.error(f"[{run_id}] assemble_video failed: {e}", exc_info=True)
            return jsonify({"error": str(e), "success": False}), 500

        try:
            video_url = upload_to_cloudinary(
                video_path, cloud, preset, f"forge/{run_id}"
            )
        except Exception as e:
            log.error(f"[{run_id}] Upload failed: {e}", exc_info=True)
            return jsonify({"error": f"Upload failed: {e}", "success": False}), 500

    elapsed = round(time.time() - start, 1)
    log.info(f"[{run_id}] DONE in {elapsed}s → {video_url}")

    return jsonify({
        "success":   True,
        "run_id":    run_id,
        "video_url": video_url,
        "elapsed_s": elapsed,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
