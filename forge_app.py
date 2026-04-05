"""
FORGE — Video Builder Service
Deploy FREE on Render.com (Web Service, Free tier)
n8n Cloud calls this via HTTP POST /build

Stack: Flask + FFmpeg + Cloudinary (all free)
Renders: 16:9 main video + 9:16 Reel version
"""

import os, json, tempfile, subprocess, glob, traceback
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── Cloudinary Config ────────────────────────────────────────
CLOUDINARY_CLOUD  = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_PRESET = os.environ.get("CLOUDINARY_UPLOAD_PRESET", "")
FORGE_SECRET      = os.environ.get("FORGE_SECRET", "change_this_secret")

# ── Health Check ─────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    ffmpeg_ok = subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
    return jsonify({"status": "ok", "ffmpeg": ffmpeg_ok})

# ── Main Build Endpoint ──────────────────────────────────────
@app.route("/build", methods=["POST"])
def build_video():
    data = request.json or {}

    run_id         = data.get("run_id", "test")
    title          = data.get("title", "")[:55]
    voiceover_url  = data.get("voiceover_url", "")
    music_url      = data.get("music_url", "")
    clip_urls      = data.get("clip_urls", [])[:6]
    duration       = int(data.get("duration", 60))
    cta_text       = data.get("cta_text", "Follow for more!")[:40]
    cloudinary_cloud  = data.get("cloudinary_cloud") or CLOUDINARY_CLOUD
    cloudinary_preset = data.get("cloudinary_preset") or CLOUDINARY_PRESET

    if not clip_urls:
        return jsonify({"success": False, "error": "No clip URLs provided"}), 400

    tmpdir = tempfile.mkdtemp(prefix=f"forge_{run_id}_")

    try:
        # ── Step 1: Download stock clips ──────────────────────
        print(f"[FORGE] Downloading {len(clip_urls)} clips...")
        downloaded_clips = []
        for i, url in enumerate(clip_urls):
            out = os.path.join(tmpdir, f"raw_{i}.mp4")
            try:
                r = requests.get(url, timeout=30, stream=True)
                if r.status_code == 200:
                    with open(out, "wb") as f:
                        for chunk in r.iter_content(65536):
                            f.write(chunk)
                    if os.path.getsize(out) > 5000:
                        downloaded_clips.append(out)
            except Exception as e:
                print(f"[FORGE] Clip {i} failed: {e}")

        if not downloaded_clips:
            return jsonify({"success": False, "error": "All clip downloads failed"}), 500

        # ── Step 2: Trim + normalize clips to 10s each ────────
        trimmed = []
        for i, clip in enumerate(downloaded_clips):
            out = os.path.join(tmpdir, f"trim_{i}.mp4")
            cmd = (
                f'ffmpeg -y -i "{clip}" -t 10 '
                f'-vf "scale=1280:720:force_original_aspect_ratio=decrease,'
                f'pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,fps=25,setsar=1" '
                f'-an -c:v libx264 -preset ultrafast -crf 30 "{out}" 2>/dev/null'
            )
            if subprocess.run(cmd, shell=True).returncode == 0 and os.path.getsize(out) > 1000:
                trimmed.append(out)

        if not trimmed:
            return jsonify({"success": False, "error": "Clip trimming failed"}), 500

        # ── Step 3: Write concat list (loop if needed) ────────
        concat_file = os.path.join(tmpdir, "concat.txt")
        loops = max(1, (duration // (10 * len(trimmed))) + 1)
        with open(concat_file, "w") as f:
            for _ in range(loops):
                for c in trimmed:
                    f.write(f"file '{c}'\n")

        # ── Step 4: Concatenate clips ─────────────────────────
        base_video = os.path.join(tmpdir, "base.mp4")
        subprocess.run(
            f'ffmpeg -y -f concat -safe 0 -i "{concat_file}" '
            f'-t {duration} -c:v libx264 -preset ultrafast -crf 28 "{base_video}" 2>/dev/null',
            shell=True
        )

        # ── Step 5: Download voiceover ────────────────────────
        voice_path = os.path.join(tmpdir, "voice.mp3")
        voice_ok = False
        if voiceover_url:
            try:
                r = requests.get(voiceover_url, timeout=20)
                if r.status_code == 200:
                    with open(voice_path, "wb") as f:
                        f.write(r.content)
                    voice_ok = os.path.getsize(voice_path) > 500
            except:
                pass
        if not voice_ok:
            subprocess.run(
                f'ffmpeg -y -f lavfi -i anullsrc=r=44100:cl=mono -t {duration} "{voice_path}" 2>/dev/null',
                shell=True
            )

        # ── Step 6: Download background music ─────────────────
        music_path = os.path.join(tmpdir, "music.mp3")
        music_ok = False
        if music_url:
            try:
                r = requests.get(music_url, timeout=20)
                if r.status_code == 200:
                    with open(music_path, "wb") as f:
                        f.write(r.content)
                    music_ok = os.path.getsize(music_path) > 500
            except:
                pass
        if not music_ok:
            subprocess.run(
                f'ffmpeg -y -f lavfi -i anullsrc=r=44100:cl=stereo -t {duration} "{music_path}" 2>/dev/null',
                shell=True
            )

        # ── Step 7: Build text overlay ────────────────────────
        safe_title = title.replace("'", "").replace('"', "").replace(":", " -")
        safe_cta = cta_text.replace("'", "").replace('"', "")
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

        if os.path.exists(font_path):
            text_vf = (
                f"drawtext=fontfile='{font_path}':text='{safe_title}':"
                f"fontsize=34:fontcolor=white:borderw=3:bordercolor=black:"
                f"x=(w-text_w)/2:y=60:enable='between(t,0,6)',"
                f"drawtext=fontfile='{font_path}':text='{safe_cta}':"
                f"fontsize=28:fontcolor=yellow:borderw=2:bordercolor=black:"
                f"x=(w-text_w)/2:y=h-50:enable='between(t,{duration-7},{duration})'"
            )
        else:
            text_vf = "null"

        # ── Step 8: Final assembly 16:9 ───────────────────────
        final_path = os.path.join(tmpdir, "final.mp4")
        audio_filter = (
            "[1:a]volume=1.0,apad[v];[2:a]volume=0.12,apad[m];[v][m]amix=inputs=2:duration=first[audio]"
        )
        cmd_final = (
            f'ffmpeg -y '
            f'-i "{base_video}" -i "{voice_path}" -i "{music_path}" '
            f'-filter_complex "{audio_filter};[0:v]{text_vf}[video]" '
            f'-map "[video]" -map "[audio]" '
            f'-t {duration} -c:v libx264 -preset medium -crf 23 '
            f'-c:a aac -b:a 128k "{final_path}" 2>/dev/null'
        )
        subprocess.run(cmd_final, shell=True)

        # Fallback without text overlay
        if not os.path.exists(final_path) or os.path.getsize(final_path) < 10000:
            print("[FORGE] Trying simple assembly without text overlay...")
            subprocess.run(
                f'ffmpeg -y -i "{base_video}" -i "{voice_path}" -i "{music_path}" '
                f'-filter_complex "[1:a]volume=1.0[va];[2:a]volume=0.12[ma];[va][ma]amix=inputs=2:duration=first[a]" '
                f'-map 0:v -map "[a]" -t {duration} '
                f'-c:v libx264 -preset fast -crf 26 -c:a aac -b:a 128k "{final_path}" 2>/dev/null',
                shell=True
            )

        if not os.path.exists(final_path) or os.path.getsize(final_path) < 10000:
            return jsonify({"success": False, "error": "Final video assembly failed"}), 500

        # ── Step 9: Create 9:16 Reel version ──────────────────
        reel_path = os.path.join(tmpdir, "reel.mp4")
        subprocess.run(
            f'ffmpeg -y -i "{final_path}" '
            f'-vf "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920" '
            f'-c:v libx264 -preset fast -crf 26 -c:a copy -t 60 "{reel_path}" 2>/dev/null',
            shell=True
        )

        # ── Step 10: Upload to Cloudinary ─────────────────────
        print("[FORGE] Uploading to Cloudinary...")
        video_url = upload_cloudinary(final_path, cloudinary_cloud, cloudinary_preset, f"{run_id}_main")
        reel_url  = upload_cloudinary(reel_path,  cloudinary_cloud, cloudinary_preset, f"{run_id}_reel") \
                    if os.path.exists(reel_path) else video_url

        if not video_url:
            return jsonify({"success": False, "error": "Cloudinary upload failed"}), 500

        print(f"[FORGE] Done. Video: {video_url}")
        return jsonify({
            "success": True,
            "video_url": video_url,
            "reel_url": reel_url,
            "clips_used": len(trimmed),
            "duration": duration,
            "run_id": run_id
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        # Cleanup temp files
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except:
            pass


def upload_cloudinary(file_path, cloud_name, upload_preset, public_id):
    """Upload a video file to Cloudinary free tier."""
    if not os.path.exists(file_path) or not cloud_name or not upload_preset:
        return None
    try:
        url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload"
        with open(file_path, "rb") as f:
            resp = requests.post(url, data={
                "upload_preset": upload_preset,
                "public_id": public_id,
                "resource_type": "video"
            }, files={"file": f}, timeout=120)
        if resp.status_code == 200:
            return resp.json().get("secure_url")
    except Exception as e:
        print(f"[FORGE] Cloudinary upload error: {e}")
    return None


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
