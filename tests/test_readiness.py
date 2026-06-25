from pathlib import Path
from types import SimpleNamespace

from harness.readiness import build_readiness_report


def _settings(**overrides):
    values = {
        "GEMINI_API_KEY": "configured",
        "QWEN_API_KEY": "configured",
        "AIRTABLE_API_KEY": "configured",
        "AIRTABLE_BASE_ID": "configured",
        "OSS_ACCESS_KEY_ID": "configured",
        "OSS_ACCESS_KEY_SECRET": "configured",
        "OSS_BUCKET_NAME": "configured",
        "OSS_ENDPOINT": "configured",
        "SEEDANCE_API_KEY": "configured",
        "KIE_API_KEY": "configured",
        "KLING_ACCESS_KEY": "",
        "KLING_SECRET_KEY": "",
        "ENABLE_KEYFRAME_STAGE": True,
        "FFMPEG_BIN_PATH": "sh",
        "FFMPEG_TEMP_DIR": "/tmp/video-replication-readiness-test",
        "ELEVENLABS_API_KEY": "",
        "ENABLE_AMBIENT_AUDIO": False,
        "SUNO_API_KEY": "",
        "ENABLE_BGM": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_ready_when_required_configuration_exists(tmp_path: Path):
    report = build_readiness_report(_settings(), tmp_path)
    assert report.ready is True
    assert report.blocking_issues == []


def test_not_ready_when_core_configuration_is_missing(tmp_path: Path):
    report = build_readiness_report(
        _settings(GEMINI_API_KEY="", AIRTABLE_BASE_ID=""),
        tmp_path,
    )
    assert report.ready is False
    assert report.checks["core_configuration"]["passed"] is False
    assert "GEMINI_API_KEY" in report.checks["core_configuration"]["missing"]


def test_keyframe_provider_is_required_when_stage_enabled(tmp_path: Path):
    report = build_readiness_report(_settings(KIE_API_KEY=""), tmp_path)
    assert report.ready is False
    assert report.checks["keyframe_provider"]["passed"] is False
