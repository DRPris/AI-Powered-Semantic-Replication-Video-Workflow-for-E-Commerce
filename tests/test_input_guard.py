import pytest

from harness.input_guard import InputValidationError, validate_public_http_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/video.mp4",
        "http://127.0.0.1/video.mp4",
        "http://10.0.0.5/video.mp4",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "ftp://example.com/video.mp4",
        "https://user:pass@example.com/video.mp4",
    ],
)
def test_rejects_unsafe_remote_urls(url):
    with pytest.raises(InputValidationError):
        validate_public_http_url(url, "video_url")


def test_accepts_public_https_url():
    url = "https://cdn.example.com/assets/video.mp4?signature=abc"
    assert validate_public_http_url(url) == url
