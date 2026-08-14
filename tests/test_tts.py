from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import book_video_workbench.tts as tts
from book_video_workbench.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        capture_backend_dir=tmp_path / "capture",
        llm_base_url="https://example.com/v1",
        llm_api_key="",
        llm_model="",
        image_base_url="https://example.com/v1",
        image_api_key="",
        image_model="gpt-image-2",
        image_size="1536x1024",
        volc_tts_endpoint="https://openspeech.example/api/v3/tts/unidirectional",
        volc_tts_api_key="secret-key",
        volc_tts_resource_id="seed-tts-2.0",
        volc_tts_voice_type="zh_female_vv_uranus_bigtts",
        reference_demo_video=tmp_path / "reference.mp4",
    )


def test_seed_tts_payload_uses_v2_fields_and_maps_speed() -> None:
    payload = tts._seed_tts_payload(
        "测试文本", "zh_female_vv_uranus_bigtts", 1.25
    )
    assert payload == {
        "req_params": {
            "text": "测试文本",
            "speaker": "zh_female_vv_uranus_bigtts",
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": 25,
            },
        }
    }


def test_decode_seed_tts_response_combines_chunked_and_sse_frames() -> None:
    raw = (
        b'data: {"code":0,"message":"OK","data":"'
        + base64.b64encode(b"first")
        + b'"}\n'
        + b'{"code":0,"data":"'
        + base64.b64encode(b"second")
        + b'","usage":{"text_words":4}}\n'
        + b'{"code":20000000,"message":"OK"}\n'
    )
    audio, metadata = tts._decode_seed_tts_response(raw)
    assert audio == b"firstsecond"
    assert metadata == {"provider_message": "OK", "usage": {"text_words": 4}}


def test_decode_seed_tts_response_surfaces_provider_error() -> None:
    with pytest.raises(RuntimeError, match="quota exceeded"):
        tts._decode_seed_tts_response(
            b'{"code":45000000,"message":"quota exceeded"}\n'
        )


def test_synthesize_volcengine_sends_api_key_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}
    response_body = (
        b'{"code":0,"message":"OK","data":"'
        + base64.b64encode(b"fake-mp3")
        + b'"}\n{"code":20000000,"message":"OK"}\n'
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return response_body

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    def fake_normalize(source: Path, target: Path) -> Path:
        target.write_bytes(source.read_bytes())
        return target

    monkeypatch.setattr(tts.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(tts, "normalize_audio", fake_normalize)
    monkeypatch.setattr(tts, "media_duration", lambda _path: 1.5)

    output = tmp_path / "voice.wav"
    metadata = tmp_path / "voice.json"
    tts.synthesize_volcengine(
        "测试文本", output, metadata, _settings(tmp_path), speed_ratio=1.0
    )

    request = captured["request"]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["x-api-key"] == "secret-key"
    assert headers["x-api-resource-id"] == "seed-tts-2.0"
    assert json.loads(request.data)["req_params"]["speaker"] == (
        "zh_female_vv_uranus_bigtts"
    )
    assert output.read_bytes() == b"fake-mp3"
    assert json.loads(metadata.read_text(encoding="utf-8"))["provider"] == (
        "volcengine-seed-tts-2.0"
    )
