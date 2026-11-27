"""Real FFmpeg + Remotion run. Slow, so it only runs with E2E=1."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from shorts_factory.ffmpeg import loudness_per_second
from shorts_factory.models import PipelineConfig
from shorts_factory.pipeline import run_pipeline

pytestmark = pytest.mark.skipif(os.getenv("E2E") != "1", reason="set E2E=1 to run")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def probe(path: Path) -> dict:
    output = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
        "-of", "json", str(path),
    ])
    return json.loads(output)


def test_render_matches_timeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Relative paths, as a user would type them.
    monkeypatch.chdir(tmp_path)
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-f", "lavfi", "-i", "sine",
           "-t", "20", "-c:v", "libx264", "-c:a", "aac", "in.mp4")
    ffmpeg("-f", "lavfi", "-i", "color=red:size=64x64", "-frames:v", "1", "logo.png")
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=220", "-t", "2", "music.wav")
    Path("t.json").write_text(json.dumps({
        "source": "in.mp4",
        "duration": 18,
        "segments": [{"id": 0, "start": 14, "end": 17, "text": "Why is this the secret?"}],
    }))
    config = PipelineConfig(
        input="in.mp4", output_dir="out", clip_count=1, min_clip_seconds=3, max_clip_seconds=5,
        padding_seconds=0, aspect_ratio="9:16", portrait_mode="blur", music_path="music.wav",
        brand={"logo_path": "logo.png"},
    )

    result = run_pipeline(config, transcript_path=Path("t.json"), render=True)

    info = probe(result["rendered"])
    assert float(info["format"]["duration"]) == pytest.approx(3, abs=0.2)
    video = next(stream for stream in info["streams"] if "width" in stream)
    assert (video["width"], video["height"]) == (1080, 1920)


def test_loudness_finds_the_loud_second(tmp_path: Path):
    audio = tmp_path / "a.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=10",
           "-af", "volume='if(between(t,6,6.999),1,0.05)':eval=frame", str(audio))

    levels = loudness_per_second(audio)

    assert len(levels) == 10
    assert levels.index(max(levels)) == 6
    assert levels[6] - levels[0] > 20
