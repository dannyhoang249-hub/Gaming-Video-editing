from __future__ import annotations

import json
import math
import os
import re
import sys
import unicodedata
from statistics import median
from typing import Any

from .models import Clip, Transcript, TranscriptSegment

HOOK_WORDS = {
    "how", "why", "secret", "mistake", "truth", "never", "best", "worst",
    "first", "because", "imagine", "important", "problem", "solution",
    "bí mật", "sai lầm", "sự thật", "tại sao", "cách", "quan trọng",
}
# Peak detection, adapted from AutoClip (see NOTICE): two seconds compared with
# the median of the 16 seconds before them, as linear amplitude.
PEAK_WINDOW = 16
PEAK_MIN_RATIO = 1.8
# This ratio or higher scores 1.0.
PEAK_FULL_RATIO = 4.0
# Stops a jump out of digital silence from counting as a huge peak.
PEAK_FLOOR = 0.003
# Transcript characters per AI request, so long streams fit in the model's context.
LLM_CHUNK_CHARS = 12_000
# AI timestamps this close to a sentence boundary are moved onto it.
SNAP_SECONDS = 3.0


def _heuristic_candidates(
    transcript: Transcript,
    minimum: float,
    maximum: float,
    padding: float,
) -> list[Clip]:
    segments = transcript.segments
    candidates: list[Clip] = []
    for index, segment in enumerate(segments):
        text = segment.text.strip()
        if not text:
            continue
        start = max(0.0, segment.start - padding)
        end = min(transcript.duration or segment.end + padding, segment.end + padding)
        while end - start < minimum and (index + 1 < len(segments)):
            index += 1
            end = min(transcript.duration or segments[index].end, segments[index].end + padding)
        end = min(end, start + maximum)
        words = re.findall(r"\w+", unicodedata.normalize("NFC", text.lower()))
        # Match whole words and multi-word phrases such as "bí mật".
        spaced = f" {'  '.join(words)} "
        hook_hits = sum(spaced.count(f" {'  '.join(hook.split())} ") for hook in HOOK_WORDS)
        punctuation = int("?" in text or "!" in text)
        density = min(len(words) / max(segment.end - segment.start, 1), 4) / 4
        score = min(1.0, 0.18 + hook_hits * 0.12 + punctuation * 0.12 + density * 0.35)
        candidates.append(
            Clip(
                id=f"clip-{len(candidates) + 1:02d}",
                start=round(start, 3),
                end=round(end, 3),
                score=round(score, 3),
                reason="heuristic hook/rhythm score",
                text=text,
                source=transcript.source,
            )
        )

    return candidates


def _loudness_candidates(
    loudness: list[float],
    minimum: float,
    maximum: float,
    source: str,
) -> list[Clip]:
    """Clips around sudden loud moments (shouts, explosions), with or without speech."""
    amplitude = [10 ** (min(0.0, level) / 20) for level in loudness]
    duration = float(len(amplitude))
    length = min(minimum, maximum, duration)
    candidates: list[Clip] = []
    for second in range(PEAK_WINDOW, len(amplitude) - 1):
        baseline = PEAK_FLOOR + median(amplitude[second - PEAK_WINDOW:second])
        ratio = (amplitude[second] + amplitude[second + 1]) / (2 * baseline)
        if ratio < PEAK_MIN_RATIO:
            continue
        # Two thirds of the clip is lead-up, one third is the reaction.
        start = min(max(0.0, second - length * 2 / 3), duration - length)
        candidates.append(
            Clip(
                id="peak",
                start=round(start, 3),
                end=round(start + length, 3),
                score=round(min(1.0, ratio / PEAK_FULL_RATIO), 3),
                reason=f"audio peak x{ratio:.1f}",
                source=source,
            )
        )
    return candidates


def _pick(candidates: list[Clip], count: int) -> list[Clip]:
    """Best-scoring non-overlapping clips, numbered in timeline order."""
    selected: list[Clip] = []
    for clip in sorted(candidates, key=lambda clip: clip.score, reverse=True):
        if all(clip.end <= item.start or clip.start >= item.end for item in selected):
            selected.append(clip)
        if len(selected) == count:
            break
    selected.sort(key=lambda clip: clip.start)
    return [clip.model_copy(update={"id": f"clip-{index + 1:02d}"}) for index, clip in enumerate(selected)]


def _chunks(segments: list[TranscriptSegment]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    size = LLM_CHUNK_CHARS
    for segment in segments:
        item = {"start": segment.start, "end": segment.end, "text": segment.text}
        length = len(json.dumps(item, ensure_ascii=False))
        if size + length > LLM_CHUNK_CHARS:
            chunks.append([])
            size = 0
        chunks[-1].append(item)
        size += length
    return chunks


def _llm_select(
    transcript: Transcript, model: str, count: int, minimum: float, maximum: float
) -> list[dict[str, Any]]:
    """Ask the model for highlights, one request per transcript chunk."""
    from openai import OpenAI

    client = OpenAI()
    chunks = _chunks(transcript.segments)
    found: list[dict[str, Any]] = []
    failed = 0
    for number, chunk in enumerate(chunks, start=1):
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Select the most engaging highlight moments. Return JSON with "
                            "clips: [{start, end, score, reason, text}]. score is from 0 to 1 "
                            "and must be comparable across different parts of one video; return "
                            "an empty list if nothing stands out. Each clip must be self-contained, "
                            f"start and end on sentence boundaries, and last {minimum:g} to {maximum:g} "
                            "seconds. Return no more than the requested count."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps({"count": count, "segments": chunk}, ensure_ascii=False),
                    },
                ],
            )
            clips = json.loads(response.choices[0].message.content or "{}").get("clips", [])
            found.extend(clips[:count])
        except Exception as exc:  # one bad chunk must not discard the others
            failed += 1
            print(f"warning: AI selection failed for part {number}/{len(chunks)}: {exc}", file=sys.stderr)
    if failed:
        print(f"warning: {failed} of {len(chunks)} transcript parts were not analysed by AI", file=sys.stderr)
    return found


def _refine(
    item: Any, transcript: Transcript, minimum: float, maximum: float, padding: float
) -> Clip | None:
    """Turn one AI answer into a valid clip, or None if it is unusable."""
    try:
        start, end = float(item["start"]), float(item["end"])
        score = float(item.get("score", 0.8))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    duration = transcript.duration
    if not (math.isfinite(score) and 0 <= start < end and start < duration):
        return None

    segments = transcript.segments
    nearest_start = min((s.start for s in segments), key=lambda value: abs(value - start), default=start)
    nearest_end = min((s.end for s in segments), key=lambda value: abs(value - end), default=end)
    if abs(nearest_start - start) <= SNAP_SECONDS:
        start = nearest_start
    if abs(nearest_end - end) <= SNAP_SECONDS:
        end = nearest_end

    start = max(0.0, start - padding)
    end = min(duration, end + padding)
    if end - start < minimum:
        end = min(duration, start + minimum)
        start = max(0.0, end - minimum)
    if end - start > maximum:
        # Prefer the last sentence that still fits over cutting mid-sentence.
        fitting = [s.end for s in segments if start + minimum <= s.end <= start + maximum]
        end = max(fitting, default=start + maximum)
    if end <= start:
        return None

    return Clip(
        id="llm",
        start=round(start, 3),
        end=round(end, 3),
        score=min(1.0, max(0.0, score)),
        reason=str(item.get("reason", "LLM-selected highlight")),
        text=str(item.get("text", "")),
        source=transcript.source,
    )


def select_highlights(
    transcript: Transcript,
    count: int = 3,
    minimum: float = 12.0,
    maximum: float = 55.0,
    padding: float = 1.5,
    ai_model: str = "gpt-4o-mini",
    loudness: list[float] | None = None,
) -> list[Clip]:
    peaks = _loudness_candidates(loudness or [], minimum, maximum, transcript.source)

    if os.getenv("OPENAI_API_KEY"):
        try:
            raw = _llm_select(transcript, ai_model, count, minimum, maximum)
            refined = [_refine(item, transcript, minimum, maximum, padding) for item in raw]
            clips = [clip for clip in refined if clip]
            if clips:
                return _pick(clips + peaks, count)
            print("warning: AI returned no usable clips, using the local heuristic", file=sys.stderr)
        except Exception as exc:
            print(f"warning: AI selection failed, using the local heuristic: {exc}", file=sys.stderr)

    return _pick(_heuristic_candidates(transcript, minimum, maximum, padding) + peaks, count)
