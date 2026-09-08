from __future__ import annotations

import json
import subprocess
from pathlib import Path


SAFE_VIDEO_CODECS = {"h264"}
SAFE_PIXEL_FORMATS = {"yuv420p", "yuvj420p"}
SAFE_AUDIO_CODECS = {"aac", "mp3"}


def _probe_streams(path: str | Path) -> dict:
    path = Path(path)
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,pix_fmt,width,height,profile:format=duration,format_name",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    )
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return {
        "video_codec": str(video.get("codec_name") or "").lower(),
        "pixel_format": str(video.get("pix_fmt") or "").lower(),
        "audio_codec": str(audio.get("codec_name") or "").lower(),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "duration": float((payload.get("format") or {}).get("duration") or 0.0),
        "format_name": str((payload.get("format") or {}).get("format_name") or "").lower(),
    }


def _is_ios_telegram_safe(info: dict) -> bool:
    video_ok = info.get("video_codec") in SAFE_VIDEO_CODECS
    pixel_ok = info.get("pixel_format") in SAFE_PIXEL_FORMATS
    audio_codec = info.get("audio_codec") or ""
    audio_ok = not audio_codec or audio_codec in SAFE_AUDIO_CODECS
    return video_ok and pixel_ok and audio_ok


def _run_ffmpeg(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def _validate_output(path: Path) -> dict:
    if not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError("Telegram-compatible MP4 was not created")
    info = _probe_streams(path)
    if info["video_codec"] != "h264" or info["pixel_format"] not in SAFE_PIXEL_FORMATS:
        raise RuntimeError(
            "Telegram-compatible MP4 validation failed: "
            f"video={info['video_codec'] or 'unknown'} pix_fmt={info['pixel_format'] or 'unknown'}"
        )
    if info["audio_codec"] and info["audio_codec"] not in SAFE_AUDIO_CODECS:
        raise RuntimeError(
            "Telegram-compatible MP4 validation failed: "
            f"audio={info['audio_codec']}"
        )
    if info["duration"] <= 0:
        raise RuntimeError("Telegram-compatible MP4 has invalid duration")
    return info


def prepare_telegram_mp4(path: str | Path, tmpdir: str | Path) -> Path:
    """Return an MP4 that Telegram on iOS can decode reliably.

    MP4 is only a container. yt-dlp may put AV1/VP9 or unusual pixel formats into
    an .mp4 file; Telegram can then advance the timer and play audio while the
    picture remains frozen on iPhone. We inspect the actual streams instead of
    trusting the file extension.

    Safe H.264/AAC input is remuxed with clean timestamps and +faststart. Anything
    else is transcoded once to H.264 8-bit yuv420p + AAC, tagged avc1, then probed
    again before it can be sent to Telegram.

    Railway containers can be memory-constrained. A default 1080p libx264 encode
    can consume several hundred MB before the first frame is produced. Unsafe
    sources therefore use a bounded-memory compatibility transcode: max long edge
    1280 px, single encoder thread, superfast + zerolatency. Safe H.264 sources are
    still remuxed without quality loss.
    """
    source = Path(path)
    tmpdir = Path(tmpdir)
    input_info = _probe_streams(source)
    output = tmpdir / "telegram-compatible.mp4"

    print(
        "TELEGRAM_VIDEO_INPUT "
        f"codec={input_info['video_codec'] or 'unknown'} "
        f"pix_fmt={input_info['pixel_format'] or 'unknown'} "
        f"audio={input_info['audio_codec'] or 'none'} "
        f"size={input_info['width']}x{input_info['height']} "
        f"duration={input_info['duration']:.2f}",
        flush=True,
    )

    if _is_ios_telegram_safe(input_info):
        remux = _run_ffmpeg(
            [
                "ffmpeg", "-y", "-fflags", "+genpts", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                str(output),
            ],
            timeout=900,
        )
        if remux.returncode == 0:
            try:
                info = _validate_output(output)
                print(
                    "TELEGRAM_VIDEO_READY mode=remux "
                    f"codec={info['video_codec']} pix_fmt={info['pixel_format']} audio={info['audio_codec'] or 'none'}",
                    flush=True,
                )
                return output
            except Exception as exc:
                print(f"TELEGRAM_VIDEO_REMUX_VALIDATE_FAIL: {exc}", flush=True)
        else:
            error = (remux.stderr or "")[-1200:].replace("\n", " ")
            print(f"TELEGRAM_VIDEO_REMUX_FAIL rc={remux.returncode}: {error}", flush=True)

    transcode = _run_ffmpeg(
        [
            "ffmpeg", "-y", "-fflags", "+genpts",
            "-threads", "1", "-filter_threads", "1",
            "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf",
            "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
            "-c:v", "libx264", "-preset", "superfast", "-tune", "zerolatency", "-crf", "22",
            "-profile:v", "high", "-level:v", "4.1", "-tag:v", "avc1",
            "-threads:v", "1",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            "-movflags", "+faststart",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",
            str(output),
        ],
        timeout=3600,
    )
    if transcode.returncode != 0:
        error = (transcode.stderr or "")[-2200:].replace("\n", " ")
        raise RuntimeError(
            "Не удалось перекодировать видео для Telegram/iPhone "
            f"(ffmpeg rc={transcode.returncode}): {error}"
        )

    info = _validate_output(output)
    print(
        "TELEGRAM_VIDEO_READY mode=transcode "
        f"codec={info['video_codec']} pix_fmt={info['pixel_format']} audio={info['audio_codec'] or 'none'} "
        f"size={info['width']}x{info['height']}",
        flush=True,
    )
    return output
