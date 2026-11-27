"""Real FFmpeg + Remotion run. Slow, so it only runs with E2E=1."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from shorts_factory.ffmpeg import concat_clips, has_audio, loudness_per_second
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


@pytest.mark.parametrize("with_audio", [True, False])
def test_music_is_added_with_or_without_source_audio(tmp_path: Path, with_audio: bool):
    video, music, out = tmp_path / "v.mp4", tmp_path / "m.wav", tmp_path / "out.mp4"
    sound = ["-f", "lavfi", "-i", "sine"] if with_audio else []
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", *sound, "-t", "4", str(video))
    # Shorter than the video, so it has to loop.
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=220", "-t", "1", str(music))

    concat_clips([video], out, music)

    assert has_audio(out)
    assert float(probe(out)["format"]["duration"]) == pytest.approx(4, abs=0.2)


def mean_volume(path: Path) -> float:
    log = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr
    return float(re.search(r"mean_volume: (\S+) dB", log).group(1))


def test_music_does_not_lower_source_volume(tmp_path: Path):
    video, silence, out = tmp_path / "v.mp4", tmp_path / "s.wav", tmp_path / "out.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-f", "lavfi", "-i", "sine",
           "-t", "4", str(video))
    # Silent music, so any change in volume comes from the mix itself.
    ffmpeg("-f", "lavfi", "-i", "anullsrc", "-t", "1", str(silence))

    concat_clips([video], out, silence)

    assert mean_volume(out) == pytest.approx(mean_volume(video), abs=0.5)


def test_loudness_finds_the_loud_second(tmp_path: Path):
    audio = tmp_path / "a.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=10",
           "-af", "volume='if(between(t,6,6.999),1,0.05)':eval=frame", str(audio))

    levels = loudness_per_second(audio)

    assert len(levels) == 10
    assert levels.index(max(levels)) == 6
    assert levels[6] - levels[0] > 20
