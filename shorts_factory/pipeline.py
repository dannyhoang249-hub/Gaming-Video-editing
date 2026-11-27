from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .ffmpeg import (
    FFmpegError,
    concat_clips,
    cut_clip,
    frame_rate,
    loudness_per_second,
    make_vertical,
)
from .models import Clip, PipelineConfig, Transcript, TranscriptSegment, Word
from .selector import select_highlights
from .transcriber import load_transcript, save_transcript, transcribe

REMOTION_DIR = Path(__file__).resolve().parent.parent / "remotion"


def _timeline_transcript(transcript: Transcript, clips: list[Clip]) -> Transcript:
    """Map source timestamps onto the concatenated clip timeline."""
    mapped_segments: list[TranscriptSegment] = []
    timeline_offset = 0.0

    for clip in clips:
        for segment in transcript.segments:
            start = max(segment.start, clip.start)
            end = min(segment.end, clip.end)
            if end <= start:
                continue

            mapped_words = [
                Word(
                    word=word.word,
                    start=round(timeline_offset + max(word.start, clip.start) - clip.start, 3),
                    end=round(timeline_offset + min(word.end, clip.end) - clip.start, 3),
                )
                for word in segment.words
                if word.end > clip.start and word.start < clip.end
            ]
            mapped_segments.append(
                TranscriptSegment(
                    id=len(mapped_segments),
                    start=round(timeline_offset + start - clip.start, 3),
                    end=round(timeline_offset + end - clip.start, 3),
                    text=segment.text,
                    words=mapped_words,
                )
            )
        timeline_offset += clip.duration

    return Transcript(
        source=transcript.source,
        language=transcript.language,
        duration=round(timeline_offset, 3),
        segments=mapped_segments,
    )


def _get_transcript(config: PipelineConfig, path: Path, user_supplied: bool) -> Transcript:
    if user_supplied:
        # Read only: never rewrite a file the user passed in.
        return load_transcript(path)
    if path.exists():
        cached = load_transcript(path)
        if Path(cached.source).resolve() != config.input.resolve():
            print(f"warning: {path} belongs to {cached.source}, transcribing again", file=sys.stderr)
        elif config.input.stat().st_mtime > path.stat().st_mtime:
            print(f"warning: {config.input} changed after {path}, transcribing again", file=sys.stderr)
        else:
            return cached
    transcript = transcribe(config.input, config.model, config.language)
    save_transcript(transcript, path)
    return transcript


def write_render_manifest(
    config: PipelineConfig,
    clips: list[Clip],
    transcript: Transcript,
    transcript_path: Path,
    source_video: Path,
    fps: float = 30.0,
) -> Path:
    timeline_transcript = _timeline_transcript(transcript, clips)
    manifest = config.output_dir / "render-manifest.json"
    # Remotion's browser can only load files from its public dir, so every asset
    # lives next to the source video and is referenced by file name.
    brand = config.brand.model_dump(mode="json")
    logo = brand["logo_path"]
    if logo and not _is_url(logo):
        copied = source_video.parent / f"logo{Path(logo).suffix}"
        shutil.copyfile(logo, copied)
        brand["logo_path"] = copied.name
    manifest.write_text(
        json.dumps(
            {
                "source": source_video.name,
                "transcript": str(transcript_path.resolve()),
                "transcript_data": timeline_transcript.model_dump(mode="json"),
                "clips": [clip.model_dump(mode="json") for clip in clips],
                "brand": brand,
                "aspect_ratio": config.aspect_ratio,
                "fps": fps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def _is_url(path: str) -> bool:
    return path.startswith(("http://", "https://"))


def _check_inputs(config: PipelineConfig, render: bool) -> None:
    """Fail before the slow steps, not after them."""
    if not config.input.is_file():
        raise FileNotFoundError(f"Input video not found: {config.input}")
    if config.music_path and not config.music_path.is_file():
        raise FileNotFoundError(f"Music file not found: {config.music_path}")
    logo = config.brand.logo_path
    if logo and not _is_url(logo) and not Path(logo).is_file():
        raise FileNotFoundError(f"Logo file not found: {logo}")
    if render and not shutil.which("npx"):
        raise RuntimeError("npx is required for --render. Install Node.js 20 or newer.")


def run_pipeline(
    config: PipelineConfig,
    transcript_path: Path | None = None,
    render: bool = False,
) -> dict[str, object]:
    _check_inputs(config, render)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    transcript_file = transcript_path or config.output_dir / "transcript.json"
    transcript = _get_transcript(config, transcript_file, user_supplied=transcript_path is not None)

    try:
        loudness = loudness_per_second(config.input)
    except FFmpegError as exc:
        print(f"warning: audio analysis failed, selecting from speech only: {exc}", file=sys.stderr)
        loudness = []

    clips = select_highlights(
        transcript,
        count=config.clip_count,
        minimum=config.min_clip_seconds,
        maximum=config.max_clip_seconds,
        padding=config.padding_seconds,
        ai_model=config.ai_model,
        loudness=loudness,
    )

    clip_files: list[Path] = []
    for clip in clips:
        path = config.output_dir / "clips" / f"{clip.id}.mp4"
        cut_clip(config.input, clip, path)
        clip_files.append(path)

    assembled = config.output_dir / "assembled.mp4"
    concat_clips(clip_files, assembled, config.music_path)
    vertical = config.output_dir / "public" / "vertical-source.mp4"
    make_vertical(assembled, vertical, config.aspect_ratio, config.portrait_mode)
    manifest = write_render_manifest(
        config, clips, transcript, transcript_file, vertical, fps=frame_rate(vertical)
    )

    rendered: Path | None = None
    if render:
        rendered = config.output_dir / "short.mp4"
        subprocess.run(
            [
                # Full path: on Windows npx is npx.cmd, which the bare name does not find.
                shutil.which("npx"), "remotion", "render", "src/index.tsx", "ShortVideo",
                str(rendered.resolve()),
                f"--props={manifest.resolve()}",
                f"--public-dir={vertical.parent.resolve()}",
            ],
            cwd=REMOTION_DIR,
            check=True,
        )

    return {
        "transcript": transcript_file,
        "clips": clips,
        "assembled": assembled,
        "vertical_source": vertical,
        "manifest": manifest,
        "rendered": rendered,
    }