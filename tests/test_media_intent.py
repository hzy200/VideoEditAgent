from __future__ import annotations

from smartcut.server.services.media_intent import parse_media_goal


def test_repair_intents_map_to_strategies() -> None:
    sync = parse_media_goal("视频音画不同步，帮我修复一下")
    assert sync is not None
    assert sync.operation == "repair"
    assert sync.strategy == "audio_sync"

    mismatch = parse_media_goal("音频和画面不对应")
    assert mismatch is not None
    assert mismatch.strategy == "audio_sync"

    stutter = parse_media_goal("画面卡顿，重新编码为恒定帧率")
    assert stutter is not None
    assert stutter.strategy == "reencode"

    blurry = parse_media_goal("画面有点模糊，锐化一下")
    assert blurry is not None
    assert blurry.strategy == "sharpen"

    broken = parse_media_goal("这个 mp4 打不开，修复")
    assert broken is not None
    assert broken.strategy == "remux"


def test_convert_and_compress_intents() -> None:
    convert = parse_media_goal("转成 mkv")
    assert convert is not None
    assert convert.operation == "convert"
    assert convert.target_container == "mkv"

    compress = parse_media_goal("压缩到 720p，体积小一点")
    assert compress is not None
    assert compress.operation == "compress"
    assert compress.max_height == 720

    crf = parse_media_goal("压到 CRF 30 就行")
    assert crf is not None
    assert crf.crf == 30

    audio = parse_media_goal("提取音频为 mp3")
    assert audio is not None
    assert audio.operation == "convert"
    assert audio.target_container == "mp3"


def test_diagnose_intent() -> None:
    draft = parse_media_goal("先检查一下这个视频")
    assert draft is not None
    assert draft.operation == "diagnose"


def test_non_media_goal_returns_none() -> None:
    assert parse_media_goal("剪成 3 条 60 秒竖版切片") is None
    assert parse_media_goal("") is None
