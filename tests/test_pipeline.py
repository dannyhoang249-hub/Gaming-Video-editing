import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from shorts_factory import pipeline
from shorts_factory.ffmpeg import FFmpegError, concat_clips
from shorts_factory.models import Clip, PipelineConfig, Transcript, TranscriptSegment, Word
from shorts_factory.pipeline import _get_transcript, _timeline_transcript, write_render_manifest
from shorts_factory.transcriber import save_transcript


def make_transcript() -> Transcript:
    return Transcript(
        source="s.mp4",
        duration=100,
        segments=[
            TranscriptSegment(
                id=0, start=10, end=20, text="first",
                words=[Word(word="a", start=10, end=15), Word(word="b", start=15, end=20)],
            ),
            TranscriptSegment(id=1, start=50, end=60, text="second", words=[Word(word="c", start=50, end=60)]),
        ],
    )


def test_timeline_remaps_clips_back_to_back():
    clips = [Clip(id="1", start=10, end=20), Clip(id="2", start=50, end=60)]
    result = _timeline_transcript(make_transcript(), clips)
    assert result.duration == 20
    assert [(s.start, s.end) for s in result.segments] == [(0, 10), (10, 20)]
    assert result.segments[1].words[0].start == 10


def test_timeline_clamps_segment_partly_inside_clip():
    clips = [Clip(id="1", start=15, end=18)]
    result = _timeline_transcript(make_transcript(), clips)
    assert (result.segments[0].start, result.segments[0].end) == (0, 3)
    assert [w.word for w in result.segments[0].words] == ["b"]


def test_timeline_skips_clip_with_no_speech():
    result = _timeline_transcript(make_transcript(), [Clip(id="1", start=30, end=40)])
    assert result.segments == []
    assert result.duration == 10


def test_manifest_written(tmp_path: Path):
    config = PipelineConfig(input=tmp_path / "in.mp4", output_dir=tmp_path)
    clips = [Clip(id="1", start=10, end=20)]
    path = write_render_manifest(config, clips, make_transcript(), tmp_path / "t.json", tmp_path / "v.mp4")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["aspect_ratio"] == "16:9"
    assert data["source"] == "v.mp4"
    assert data["transcript_data"]["duration"] == 10


def test_config_validation():
    with pytest.raises(ValidationError):
        PipelineConfig(input="a.mp4", clip_count=0)
    with pytest.raises(ValidationError):
        PipelineConfig(input="a.mp4", aspect_ratio="4:3")


def test_concat_without_clips_raises(tmp_path: Path):
    with pytest.raises(FFmpegError):
        concat_clips([], tmp_path / "out.mp4")


def test_cached_transcript_is_only_reused_for_the_same_video(tmp_path: Path, monkeypatch):
    cache = tmp_path / "transcript.json"
    save_transcript(Transcript(source=str(tmp_path / "old.mp4")), cache)
    monkeypatch.setattr(pipeline, "transcribe", lambda source, *_: Transcript(source=str(source)))

    same = PipelineConfig(input=tmp_path / "old.mp4")
    other = PipelineConfig(input=tmp_path / "new.mp4")
    # A transcript passed with --transcript is trusted and left untouched.
    before = cache.read_text()
    assert _get_transcript(other, cache, user_supplied=True).source.endswith("old.mp4")
    assert cache.read_text() == before
    assert _get_transcript(same, cache, user_supplied=False).source.endswith("old.mp4")
    assert _get_transcript(other, cache, user_supplied=False).source.endswith("new.mp4")
    assert "new.mp4" in cache.read_text()


def test_missing_user_transcript_is_an_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        _get_transcript(PipelineConfig(input="a.mp4"), tmp_path / "nope.json", user_supplied=True)
