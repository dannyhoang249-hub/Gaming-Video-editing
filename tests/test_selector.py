import json
from types import SimpleNamespace

import openai
import pytest

from shorts_factory import selector
from shorts_factory.models import Transcript, TranscriptSegment
from shorts_factory.selector import select_highlights


@pytest.fixture(autouse=True)
def no_real_ai(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def make_transcript() -> Transcript:
    return Transcript(
        source="source.mp4",
        duration=90,
        language="en",
        segments=[
            TranscriptSegment(id=0, start=0, end=8, text="Here is the secret that changes everything!"),
            TranscriptSegment(id=1, start=10, end=25, text="This is a practical explanation with useful details."),
            TranscriptSegment(id=2, start=30, end=45, text="Why do most people make this mistake?"),
            TranscriptSegment(id=3, start=50, end=70, text="The solution is simple and actionable."),
        ],
    )


def test_selects_limited_non_overlapping_clips():
    clips = select_highlights(make_transcript(), count=2, minimum=5, maximum=30, padding=0)
    assert len(clips) == 2
    assert clips[0].start < clips[0].end
    assert clips[0].end <= clips[1].start


def test_empty_transcript_returns_empty():
    assert select_highlights(Transcript(source="empty.mp4"), count=3) == []

def test_heuristic_clips_skip_long_pauses_and_keep_the_minimum_length():
    transcript = Transcript(source="s", duration=104, segments=[
        TranscriptSegment(id=0, start=10, end=12, text="why?"),
        TranscriptSegment(id=1, start=100, end=104, text="bye"),
    ])
    clips = select_highlights(transcript, count=2, minimum=12, maximum=55, padding=1.5)
    # The first is not stretched across the pause; the last grows backwards.
    assert [(clip.start, clip.end) for clip in clips] == [(8.5, 20.5), (92, 104)]


def test_loud_moment_without_speech_is_selected():
    loudness = [-30.0] * 120
    loudness[100] = -10.0
    clips = select_highlights(make_transcript(), count=2, minimum=6, maximum=30, padding=0, loudness=loudness)
    peak = next(clip for clip in clips if clip.reason.startswith("audio peak"))
    assert peak.start <= 100 < peak.end
    assert peak.end - peak.start == 6
    assert [clip.id for clip in clips] == ["clip-01", "clip-02"]


def test_flat_or_silent_audio_adds_no_peaks():
    for level in (-30.0, float("-inf")):
        clips = select_highlights(make_transcript(), count=4, minimum=5, maximum=30, loudness=[level] * 90)
        assert not any(clip.reason.startswith("audio peak") for clip in clips)


def test_loudness_is_judged_against_recent_audio():
    # The stream gets much louder halfway; a later shout must still stand out.
    loudness = [-40.0] * 100 + [-20.0] * 100
    loudness[160:162] = [-6.0, -6.0]
    peaks = selector._loudness_candidates(loudness, 6, 30, "s.mp4")
    assert any(clip.start <= 160 < clip.end for clip in peaks)
    assert not any(clip.start > 170 for clip in peaks)


def test_vietnamese_phrases_count_as_hooks():
    def score(text: str) -> float:
        transcript = Transcript(source="s", duration=20, segments=[
            TranscriptSegment(id=0, start=0, end=10, text=text)])
        return select_highlights(transcript, count=1, minimum=5)[0].score

    assert score("đây là bí mật của tôi") > score("đây là bánh mì của tôi")


@pytest.mark.parametrize("item", [
    {"start": 30, "end": 10}, {"start": "soon", "end": 40}, {"start": 500, "end": 520},
    {"end": 5}, "not a clip", {"start": 1, "end": 9, "score": "nan"},
])
def test_unusable_ai_answers_are_dropped(item):
    assert selector._refine(item, make_transcript(), 5, 30, 0) is None


def test_ai_clip_snaps_to_sentences_and_respects_length():
    short = selector._refine({"start": 31, "end": 33}, make_transcript(), 5, 30, 0)
    assert (short.start, short.end) == (30, 35)
    long = selector._refine({"start": 1, "end": 69, "score": 7}, make_transcript(), 5, 40, 0)
    assert (long.start, long.end, long.score) == (0, 25, 1.0)


def test_long_transcript_is_split_and_one_failure_is_survived(monkeypatch, capsys):
    calls = []

    def create(**kwargs):
        segments = json.loads(kwargs["messages"][1]["content"])["segments"]
        calls.append(segments)
        if len(calls) == 1:
            raise RuntimeError("rate limited")
        content = json.dumps({"clips": [{**segments[0], "score": 0.9, "reason": "ai"}]})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai, "OpenAI", lambda: client)
    monkeypatch.setattr(selector, "LLM_CHUNK_CHARS", 150)
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    clips = select_highlights(make_transcript(), count=5, minimum=5, maximum=30, padding=0)

    assert len(calls) > 1
    assert sum(len(call) for call in calls) == 4
    assert clips and all(clip.reason == "ai" for clip in clips)
    assert "rate limited" in capsys.readouterr().err
