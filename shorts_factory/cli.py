from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .models import PipelineConfig
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    # Optional flags default to None so we can tell "not passed" from "passed",
    # letting values from --config apply unless a flag explicitly overrides them.
    parser = argparse.ArgumentParser(description="Turn a long video into branded highlight videos.")
    parser.add_argument("input", nargs="?", type=Path, help="Source video path (or set `input` in --config)")
    parser.add_argument("--config", type=Path, help="YAML or JSON pipeline config")
    parser.add_argument("--output", type=Path, help="Output directory (default: out)")
    parser.add_argument("--transcript", type=Path, help="Use an existing Whisper JSON transcript")
    parser.add_argument("--model", help="Whisper model name (default: base)")
    parser.add_argument("--clips", type=int, help="Number of highlight clips (default: 3)")
    parser.add_argument("--render", action="store_true", help="Render the Remotion composition")
    return parser


def load_config_file(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        return yaml.safe_load(text) or {}
    return json.loads(text)


def build_config(args: argparse.Namespace, data: dict[str, Any]) -> PipelineConfig:
    merged = dict(data)
    if args.input is not None:
        merged["input"] = args.input
    if args.output is not None:
        merged["output_dir"] = args.output
    if args.model is not None:
        merged["model"] = args.model
    if args.clips is not None:
        merged["clip_count"] = args.clips
    if "input" not in merged:
        raise SystemExit("error: provide an input video as an argument or as `input` in --config")
    return PipelineConfig(**merged)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = build_config(args, load_config_file(args.config))
    result = run_pipeline(config, transcript_path=args.transcript, render=args.render)
    print(json.dumps({key: str(value) for key, value in result.items() if key != "clips"}, indent=2))
