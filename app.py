"""
FORGE — Video Builder Service
Runs on Render.com (Free tier)
POST /build  →  downloads clips + music + TTS, assembles video, uploads to Cloudinary
"""

import os, uuid, time, requests, logging, subprocess, tempfile
from functools import wraps
from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("forge")

app = Flask(__name__)

# ── Auth ──────────────────────────────────────────────────────────────────────
FORGE_API_KEY = os.environ.get("FORGE_API_KEY", "forge-secret-key-change-me")

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if key != FORGE_API_KEY:
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
    cloudinary.config(
        cloud_name=cloud,
        api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
        api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""),
    )
    # If using unsigned preset, don't need api_key/secret
    result = cloudinary.uploader.upload(
        path,
        upload_preset=preset,
        public_id=public_id,
        resource_type="video",
        timeout=300,
    )
    return result.get("secure_url", "")


# ── Main build logic ───────────────────────────────────────────────────────────

def assemble_video(payload: dict, workdir: str) -> str:
    """
    1. Download up to 6 clips, music, voiceover TTS
    2. Concatenate clips to target duration
    3. Mix music (low) + voiceover (loud) as audio
    4. Burn CTA subtitle onto last 3 seconds
    5. Return path to final MP4
    """
    clip_urls    = payload.get("clip_urls", [])
    music_url    = payload.get("music_url", "")
    voiceover_url = payload.get("voiceover_url", "")
    voiceover_text = payload.get("voiceover_text", "")
    duration     = int(payload.get("duration", 60))
    cta_text     = payload.get("cta_text", "Follow for more!")
    title        = payload.get("title", "Video")

    if not clip_urls:
        raise ValueError("No clip_urls provided")

    # ── Step 1: Download clips ──
    clip_paths = []
    for i, url in enumerate(clip_urls[:6]):
        dest = os.path.join(workdir, f"clip_{i:02d}.mp4")
        if download_file(url, dest, timeout=90):
            clip_paths.append(dest)
    if not clip_paths:
        raise ValueError("All clip downloads failed")
    log.info(f"Downloaded {len(clip_paths)} clips")

    # ── Step 2: Trim + concat clips to target duration ──
    # Write ffmpeg concat list
    concat_list = os.path.join(workdir, "concat.txt")
    with open(concat_list, "w") as f:
        # Repeat clips if needed to fill duration
        per_clip = max(5, duration // len(clip_paths))
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")

    raw_concat = os.path.join(workdir, "raw_concat.mp4")
    run_ffmpeg(
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-t", str(duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
        "-r", "30", "-c:v", "libx264", "-preset", "fast",
        "-crf", "23", "-an",
        raw_concat
    )

    # ── Step 3: Build audio track ──
    audio_inputs = []
    audio_filter = ""

    # Download music
    music_path = ""
    if music_url:
        music_path = os.path.join(workdir, "music.mp4")
        if not download_file(music_url, music_path, timeout=60):
            music_path = ""

    # Download / generate voiceover
    vo_path = ""
    if voiceover_url:
        vo_path = os.path.join(workdir, "voiceover.mp3")
        if not download_file(voiceover_url, vo_path, timeout=60):
            vo_path = ""
    # Fallback: TTS via espeak if no voiceover URL worked
    if not vo_path and voiceover_text:
        vo_path = os.path.join(workdir, "voiceover.mp3")
        try:
            subprocess.run(
                ["espeak-ng", "-w", vo_path, "-s", "150", voiceover_text[:500]],
                capture_output=True, timeout=30
            )
            if not os.path.exists(vo_path) or os.path.getsize(vo_path) == 0:
                vo_path = ""
        except Exception:
            vo_path = ""

    final_audio = os.path.join(workdir, "final_audio.aac")
    if music_path and vo_path:
        run_ffmpeg(
            "-stream_loop", "-1", "-i", music_path,
            "-i", vo_path,
            "-filter_complex",
            f"[0:a]volume=0.15,atrim=0:{duration},asetpts=PTS-STARTPTS[music];"
            f"[1:a]volume=1.0,atrim=0:{duration},asetpts=PTS-STARTPTS[vo];"
            f"[music][vo]amix=inputs=2:duration=longest[aout]",
            "-map", "[aout]", "-c:a", "aac", "-b:a", "128k",
            "-t", str(duration), final_audio
        )
    elif music_path:
        run_ffmpeg(
            "-stream_loop", "-1", "-i", music_path,
            "-t", str(duration),
            "-vn", "-c:a", "aac", "-b:a", "128k", final_audio
        )
    elif vo_path:
        run_ffmpeg(
            "-i", vo_path,
            "-t", str(duration),
            "-vn", "-c:a", "aac", "-b:a", "128k", final_audio
        )

    # ── Step 4: Merge video + audio + CTA subtitle ──
    cta_escaped = cta_text.replace("'", "\\'").replace(":", "\\:")
    title_escaped = title[:40].replace("'", "\\'").replace(":", "\\:")

    final_out = os.path.join(workdir, "final.mp4")
    vf_filter = (
        f"drawtext=text='{title_escaped}':fontcolor=white:fontsize=42:"
        f"box=1:boxcolor=black@0.5:boxborderw=8:"
        f"x=(w-text_w)/2:y=80:enable='between(t,0,4)',"
        f"drawtext=text='{cta_escaped}':fontcolor=yellow:fontsize=48:"
        f"box=1:boxcolor=black@0.6:boxborderw=10:"
        f"x=(w-text_w)/2:y=(h-text_h-80):"
        f"enable='gte(t,{max(0, duration-4)})'"
    )

    if os.path.exists(final_audio):
        run_ffmpeg(
            "-i", raw_concat,
            "-i", final_audio,
            "-vf", vf_filter,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-shortest",
            "-movflags", "+faststart",
            final_out
        )
    else:
        # No audio — still produce video
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
def health():
    return jsonify({"status": "ok", "service": "FORGE Video Builder"})


@app.route("/build", methods=["POST"])
@require_api_key
def build():
    payload = request.get_json(force=True, silent=True) or {}
    run_id  = payload.get("run_id") or str(uuid.uuid4())[:8]
    cloud   = payload.get("cloudinary_cloud", os.environ.get("CLOUDINARY_CLOUD", ""))
    preset  = payload.get("cloudinary_preset", os.environ.get("CLOUDINARY_PRESET", ""))

    log.info(f"[{run_id}] Build request — clips:{len(payload.get('clip_urls',[]))} cloud:{cloud}")

    if not cloud:
        return jsonify({"error": "cloudinary_cloud missing", "success": False}), 400

    start = time.time()
    with tempfile.TemporaryDirectory(prefix=f"forge_{run_id}_") as workdir:
        try:
            video_path = assemble_video(payload, workdir)
        except Exception as e:
            log.error(f"[{run_id}] assemble_video failed: {e}")
            return jsonify({"error": str(e), "success": False}), 500

        try:
            public_id = f"forge/{run_id}"
            video_url = upload_to_cloudinary(video_path, cloud, preset, public_id)
        except Exception as e:
            log.error(f"[{run_id}] Cloudinary upload failed: {e}")
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
