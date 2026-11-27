from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Sequence

from .models import Clip


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise FFmpegError("FFmpeg is required. Install it with: brew install ffmpeg")
    return binary


def _run(args: Sequence[str]) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise FFmpegError(exc.stderr[-2000:]) from exc


def loudness_per_second(source: Path) -> list[float]:
    """RMS level in dB of each second of the first audio track (-inf when silent)."""
    ffmpeg = require_ffmpeg()
    try:
        result = subprocess.run(
            [
                ffmpeg, "-v", "error", "-i", str(source), "-map", "0:a:0", "-vn",
                "-af", (
                    "aresample=8000,asetnsamples=8000,astats=metadata=1:reset=1,"
                    "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
                ),
                "-f", "null", "-",
            ],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise FFmpegError(exc.stderr[-2000:]) from exc
    return [
        float(line.split("=", 1)[1])
        for line in result.stdout.splitlines()
        if line.startswith("lavfi.astats.Overall.RMS_level=")
    ]


def has_audio(source: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(source),
            ],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise FFmpegError(exc.stderr[-2000:]) from exc
    return bool(result.stdout.strip())


def media_duration(source: Path) -> float:
    """Length of a media file in seconds."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(source),
            ],
            check=True, capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except subprocess.CalledProcessError as exc:
        raise FFmpegError(exc.stderr[-2000:]) from exc
    except ValueError as exc:
        raise FFmpegError(f"Cannot read the duration of {source}") from exc


def frame_rate(source: Path) -> float:
    """Average frames per second, capped at 60; 30 when the file does not say."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate", "-of", "csv=p=0", str(source),
            ],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise FFmpegError(exc.stderr[-2000:]) from exc
    try:
        fps = float(Fraction(result.stdout.strip().rstrip(",")))
    except (ValueError, ZeroDivisionError):
        return 30.0
    return round(min(fps, 60.0), 3) if fps > 0 else 30.0


def resize_filter(aspect_ratio: str = "16:9", portrait_mode: str = "crop") -> str:
    """FFmpeg video filter that brings a frame to the output size."""
    if aspect_ratio == "16:9":
        width, height = 1920, 1080
    elif aspect_ratio == "9:16":
        width, height = 1080, 1920
    else:
        raise ValueError(f"Unsupported aspect ratio: {aspect_ratio}")

    fill = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    if aspect_ratio == "9:16" and portrait_mode == "blur":
        return (
            f"split[bg][fg];[bg]{fill},boxblur=20:2[back];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2[front];"
            "[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    return f"{fill},setsar=1"


def cut_clip(
    source: Path,
    clip: Clip,
    destination: Path,
    aspect_ratio: str = "16:9",
    portrait_mode: str = "crop",
) -> None:
    """Cut one clip and resize it, so the video is not encoded again before rendering."""
    ffmpeg = require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run([
        ffmpeg, "-y", "-ss", str(clip.start), "-i", str(source),
        "-t", str(clip.duration), "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", resize_filter(aspect_ratio, portrait_mode),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-movflags", "+faststart", str(destination),
    ])


def concat_clips(clips: Sequence[Path], destination: Path, music: Path | None = None) -> None:
    if not clips:
        raise FFmpegError("No clips were selected; cannot assemble an output video.")

    ffmpeg = require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = clips[0].parent / "concat.txt"

    lines = []
    for clip in clips:
        escaped_path = clip.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped_path}'\n")
    manifest.write_text("".join(lines), encoding="utf-8")
    if music:
        # With no source audio there is nothing to mix, so the music is the soundtrack.
        mix = (
            "[1:a]volume=0.12[a1];"
            # normalize=0 keeps the source at full volume; amix would otherwise halve it.
            "[0:a][a1]amix=inputs=2:duration=first:normalize=0[aout]"
            if has_audio(clips[0])
            else "[1:a]anull[aout]"
        )
        _run([
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex", mix,
            "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac",
            "-shortest", "-movflags", "+faststart", str(destination),
        ])
    else:
        _run([
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(destination),
        ])
