from pathlib import Path

import pytest

from shorts_factory.cli import build_config, build_parser, load_config_file


def parse(*argv: str):
    return build_parser().parse_args(list(argv))


def test_config_values_apply_when_flags_not_passed(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text("model: small\nclip_count: 5\noutput_dir: outx\n", encoding="utf-8")
    config = build_config(parse("a.mp4", "--config", str(cfg)), load_config_file(cfg))
    assert config.model == "small"
    assert config.clip_count == 5
    assert config.output_dir == Path("outx")


def test_flags_override_config(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text("model: small\nclip_count: 5\n", encoding="utf-8")
    args = parse("a.mp4", "--config", str(cfg), "--model", "tiny", "--clips", "2")
    config = build_config(args, load_config_file(cfg))
    assert (config.model, config.clip_count) == ("tiny", 2)


def test_input_can_come_from_config(tmp_path: Path):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"input": "from-config.mp4"}', encoding="utf-8")
    config = build_config(parse("--config", str(cfg)), load_config_file(cfg))
    assert config.input == Path("from-config.mp4")


def test_missing_input_exits():
    with pytest.raises(SystemExit):
        build_config(parse(), {})


def test_example_config_loads():
    data = load_config_file(Path(__file__).parent.parent / "config.example.yml")
    config = build_config(parse(), data)
    assert config.aspect_ratio == "16:9"
